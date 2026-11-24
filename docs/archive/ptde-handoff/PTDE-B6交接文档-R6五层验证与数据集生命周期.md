# PTDE B6 批次交接文档 — R6：五层验证 + 生命周期管理 + READY 门禁（T6.1~T6.9）

> 状态：**已完成并通过门禁**（2026-11-18）
> 依据：`PTDE-实施方案-R0-R7分批执行计划.md` §9（R6 任务表 T6.1~T6.9）、§9.4（READY 门禁设计）、
> §9.5（验收门禁）、§9.6（回归保障）；`性能测试平台-智能测试数据引擎-完整技术方案.md`
> §12.4/§12.5/§13.1/§18.4；R5 交接 §7/§13（衔接清单）；
> 代码位置：`perf-py/ptde/validator/`（五层）、`perf-py/ptde/lifecycle/`（manifest/cleanup/dataset_manager）；
> 前序交接：`PTDE-B5交接文档-R5前段Adapter分层与原生装载.md`（R5 全量，含后段续记）全部有效。

---

## 1. 交接基线与结果

- 入口基线：`uv sync --all-extras` 后交接口径门禁 **1158 passed, 1 skipped**
  （与 B5 交接 §8 一致；contract 时序型 flaky 用例本轮未触发）。
- 交付后门禁：
  - `uv run pytest tests/contract tests/test_datagen_* tests/test_ptde_* -q` →
    **1293 passed, 1 skipped**（新增 135，零回归）；
  - `uv run ruff check ptde/ tests/test_ptde_* tests/test_datagen_*` → **All checks passed**；
  - 全库 `uv run pytest tests/ -q` → 1555 tests, 0 failures, 2 skipped
    （skip 为 locust 导入闸门，既有口径）。
- 本批为 `ptde/validator/` 与 `ptde/lifecycle/` **纯新增**（lifecycle 仅
  `__init__.py` 导出扩容）；**未触碰** legacy `datagen/`（A1 分轨延续）、
  R5 全部模块、`ptde/models/**`、`ptde/generator/**`、`sql/init.sql`
  （`ptde_validation_report` / `ptde_dataset_definition` / `ptde_dataset_instance`
  三张表 R0 已建，本批模型字段与 DDL 对齐，无需变更）。

## 2. 交付内容（按任务）

### T6.1 数量验证 — `ptde/validator/count_validator.py`

- 逐表 `SELECT COUNT(*)` 全表精确核对（非抽样；QW3 verify_min
  行数核对收编升级）；**行数不可得 → FAIL，不伪装通过**；
- 分片之和一致：`shard_loaded`（staging totals / manifest 汇总）与实际
  行数显式核对，不一致即 FAIL（数量复核锚点，§8.6 门禁 1 延续）；
- 精确/统计目标分别处理：source ∈ {explicit, derived} 必须精确相等；
  {production, estimated} 走 `CountTolerance`——**容差默认 0
  （无通用固定值，须按 Pilot Run 校准显式配置，R5 LoadBudget 口径同源）；
  容差内偏差 → WARN（可降级项，随报告留痕），超出 → FAIL**。

### T6.2 结构关系验证 — `ptde/validator/relationship_validator.py`

- FK 孤儿：**零孤儿要求的关系全量核对（非抽样）**——verify_min ≤200 行
  抽检收编升级为全量 LEFT JOIN，通过即提供强完整性证据（记入 evidence）；
  DB_FK/ORM 违规 → FAIL；LOGICAL（推断）→ WARN（不作为硬规则，A3）；
- 主键/唯一完整性：`COUNT(*) vs COUNT(DISTINCT cols)`——DISTINCT 漏 NULL，
  二者相等 ⇔ 无重复且无 NULL（单条 SQL 覆盖两类破损）；
- M:N 重复：联结表 (left, right) 重复对全表检测；
- 关联基数：每父实体子实体数上限（GROUP BY + HAVING 全量核对）。

### T6.3 分布验证 — `ptde/validator/distribution_validator.py`

- 分布类型对齐 R4 采样器参数形态（registry 全集）：
  - weighted_categorical（values/weights 列表与 {bucket: ratio} 字典两形态）：
    **精确配额（source=explicit）直查目标数量**（每桶 COUNT(*) 等于配额 +
    配额外取值 NOT IN 核对，漏项/多桶显式失败）；概率分布用统计容差；
  - zipf/skewed：热点 Top-1 占比 vs 目标；无目标值 → WARN 观测留痕
    （不伪装 PASS）；
  - normal（mean/sigma）、lognormal（理论均值）、uniform（min/max 范围）；
  - sequential → PASS（不适用统计核对，ok+说明）；temporal/null_ratio/
    未知类型 → WARN 并指引对应核对通道（不静默省略也不伪装，§10.1 全集口径）；
- 画像核对（NULL 比例/Distinct vs ColumnProfile，容差）；
- 联合分布（GROUP BY 全量，漏桶/多桶均 FAIL）与时间分桶
  （[start, end) 半开区间，精确/容差两档）核对。

### T6.4 业务不变量验证 — `ptde/validator/invariant_validator.py`

- **仅执行 status=CONFIRMED 的 BusinessInvariant**（AI 推断不作为硬规则）；
  INFERRED/UNKNOWN → WARN 跳过，不参与 READY 硬门禁；
- 结构化谓词编译为验证 SQL（白名单校验延续 R2 机制）：
  `relation_exists`（全量孤儿核对）/ `not_null`（全量 NULL 计数）；
- 已确认但谓词缺失/类型不可编译/标识符白名单拒绝 → **显式 FAIL**
  （不伪装通过；不可结构化表达的规则应登记为 UNKNOWN 待人工审核，
  总方案 §5.3 口径）。

### T6.5 数据库就绪验证 — `ptde/validator/db_ready_validator.py`

- §9.2 T6.5 逐项全集：表/索引大小（StorageMetricsAdapter 实测 vs
  StorageEstimate 预算；**预算已配置而不可实测 → FAIL**）、索引完整性
  （计划索引逐个存在性，MySQL information_schema / PG pg_indexes 分发，
  未支持产品显式失败）、分区分布（分区数 vs 计划）、统计信息更新
  （MySQL UPDATE_TIME/CHECK_TIME / PG last_analyze[last_autoanalyze]；
  analyze_required 可配，缺省 WARN 可降级）、核心查询执行计划
  （EXPLAIN 可产出）、选择性接近画像、热点达成；
- **selfCheck 收编**（datagen_service._self_check 口径）：端点抽检回调——
  0 命中 WARN、部分命中 PASS（legacy selfCheck 从不阻断），
  未提供回调 → WARN「[未执行]」（未执行 ≠ 通过，R5 gate 第 8 条同源）。

### T6.7 Manifest 汇总 — `ptde/lifecycle/manifest.py`

- `missing_elements`：总方案 §12.4 可复现要素全集逐项核对（spec/plan/
  schema/画像四指纹、Seed、生成器版本（哨兵 0.0.0 视为未回填）、分片算法
  版本、Adapter 版本与能力快照、分片摘要、实际 ID 范围）；缺项清单显式
  返回，`require_complete` 缺项即 `ManifestIncomplete`（READY 条件 7 判定输入）；
- `time_inputs` 缺省不判缺（无时间窗数据集合法），`require_time_inputs`
  显式开启；
- **R6 承接项落地**：`summaries_from_staging` / `backfill_from_staging` ——
  ExecutionManifest.shard_summaries 回填（staging.totals() 形态已对齐，
  R5 §13 口径）。

### T6.8 清理策略 — `ptde/lifecycle/cleanup.py`

- 档位与优先级（总方案 §12.5 原文）：SCHEMA_REBUILD > DATABASE_RESET >
  PARTITION_CLEAN > OWNERSHIP_BATCH_DELETE；
- 硬约束：生产库/无法识别环境禁止清理（§13.1）；数据被其他批次引用 →
  冲突报告并阻断；**共享表无 owner_token 不得猜测归属 → 阻止并报告**；
  待清理行数超 `LARGE_DELETE_ROWS`（100 万）且无分批计划 → 阻止
  （大规模逐行 DELETE 不作默认方式）；
- 决策与执行分离：`plan_cleanup` 产出 `CleanupDecision`（blocked 不得绕过，
  `CleanupBlocked`）；`execute_cleanup` 复用 R5 T5.9 `SecurityGate.
  evaluate_cleanup`（CLEANUP 显式授权）+ 三级审计（决策/授权/执行，
  §9.5 门禁 3）。

### T6.6/T6.9 数据集管理 + READY 门禁 — `ptde/lifecycle/dataset_manager.py`

- T6.6：Definition/Instance 分离——`register_definition`（def_key+version
  唯一，重复显式失败不覆盖；spec_fingerprint 注册时封存）、同一 Definition
  可建多 Instance（UUID 标识，初始 PLANNED）；状态流转唯一入口 = R0
  `transition()`（非法转换/缺操作者原因显式失败）；
- T6.9：`evaluate_ready` 按 §18.4 八项逐项程序化判定（非人工宣告）：
  1 数量达标 / 2 分片全完成且 loaded_rows==generated_rows（无未处理装载
  错误）/ 3 关系 / 4 分布 / 5 不变量（仅 CONFIRMED）/ 6 DB 就绪 /
  7 Manifest+五层报告完整且 instance_id 链路一致（可追溯性）/ 8 清理档位
  已定 + owner_token 非空 + 目标环境明确；
- **可降级层默认仅 DISTRIBUTION**（`degradable_layers` 可配；§9.4「WARN
  是否放行由配置决定」）；业务不变量层 WARN 默认不放行（更严层不放行）；
  任一 FAIL → `finalize_verification` 经状态机转 FAILED（原因入
  transition_log），全部满足 → VERIFYING→READY。

## 3. 文件清单

```text
新建（生产代码）
perf-py/ptde/validator/__init__.py                  # 共享基座（白名单查询/检查聚合）
perf-py/ptde/validator/count_validator.py           # T6.1
perf-py/ptde/validator/relationship_validator.py    # T6.2
perf-py/ptde/validator/distribution_validator.py    # T6.3
perf-py/ptde/validator/invariant_validator.py       # T6.4
perf-py/ptde/validator/db_ready_validator.py        # T6.5
perf-py/ptde/lifecycle/manifest.py                  # T6.7
perf-py/ptde/lifecycle/cleanup.py                   # T6.8
perf-py/ptde/lifecycle/dataset_manager.py           # T6.6 + T6.9

修改（生产代码，最小改动）
perf-py/ptde/lifecycle/__init__.py                  # 包导出扩容（T6.6~T6.9）

新建（测试，共 135 例）
perf-py/tests/test_ptde_validator_count.py            # 13（精确/容差/分片锚点/白名单）
perf-py/tests/test_ptde_validator_relationship.py     # 14（全量孤儿/键完整性/M:N/基数）
perf-py/tests/test_ptde_validator_distribution.py     # 26（配额/统计容差/联合/时间桶）
perf-py/tests/test_ptde_validator_invariant.py        # 9（CONFIRMED 执行/推断跳过/编译失败）
perf-py/tests/test_ptde_validator_db_ready.py         # 25（逐项 + 产品分发 + selfCheck 收编）
perf-py/tests/test_ptde_lifecycle_manifest.py         # 7（要素核对/回填）
perf-py/tests/test_ptde_lifecycle_cleanup.py          # 14（档位/阻止/授权审计）
perf-py/tests/test_ptde_lifecycle_dataset_manager.py  # 20（T6.6 管理 + §18.4 八项）

未改动：datagen/**、ptde/adapters/**、ptde/loader/**、ptde/generator/**、
ptde/models/**、ptde/planner/**、sql/init.sql
```

## 4. 关键设计决策与踩坑记录（承接 B5 §4/§10）

1. **只读白名单延续 R2 机制**：验证器全部查询统一走 `readonly_scalar` /
   `readonly_rows`（`assert_readonly_sql` 前置，白名单外直接拒绝）；
   标识符拼接前必过 `validate_identifier`（R5 收编口径复用）。唯一例外是
   EXPLAIN：**内层语句先过白名单再前置 EXPLAIN 发送**（EXPLAIN 本身不是
   SELECT 开头，会被白名单误拒——内层已校验，语义等价安全）。
2. **容差默认 0，无通用固定值**：`CountTolerance`/`DistributionTolerance`
   的 `relative` 默认 0 = 逐行/逐桶精确；统计目标必须在调用方按 Pilot Run
   校准显式配置（R5 LoadBudget/ThrottleConfig 口径同源）——不引入拍脑袋
   的默认容差。容差内偏差统一 WARN（可降级项），不伪装 PASS。
3. **「未执行」≠「通过」延续**：self_check 未提供回调、analyze 清单未提供
   → WARN +「[未执行]」明示（B5 gate 第 8 条同源）；预算已配置而不可实测
   则直接 FAIL。
4. **联合分布核对「不重复不遗漏」**：GROUP BY 全量比对，漏桶（期望组合
   缺失）与多桶（未声明组合）都 FAIL——与 R5 checkpoint 三方核对同源锚点
   语义。
5. **marker 路由包含冲突再现 + 参数级路由**：`COUNT(DISTINCT ...) FROM t`
   与 `COUNT(*) FROM t` 天然包含重叠，first-match 下必须具体 marker 在前；
   同 SQL 不同 params（类别配额逐值 COUNT）引入 **params 元组比对路由**
   （fake cursor 三元组 (marker, rows, params)）——开发中两类问题首跑即
   暴露（B5 §4 第 4 条教训延续）。
6. **EXPLAIN 检查项的失败不伪装**：内层白名单拒绝、计划为空、执行异常
   均落入该检查项 FAIL（不中断其余检查项，verify_min「单项失败不阻断」
   口径延续）。
7. **selfCheck 收编语义对齐**：legacy `_self_check` 从不阻断（0 命中仅
   warning）——收编后保持 0 命中 WARN、部分命中 PASS；改 FAIL 会破坏
   legacy 语义（收编=复制语义，收编不等于加严）。
8. **clean 决策与执行分离**：`plan_cleanup` 是纯函数（同输入同决策），
   blocked 决策传给 `execute_cleanup` 直接抛 `CleanupBlocked`——「决策
   可审计、执行须过门禁」，避免调用方绕过决策直接执行（K5 风险）。
9. **READY 可降级层默认集最小化**：默认仅 DISTRIBUTION 可 WARN 放行
   （分布统计容差天然非精确）；business/db_ready 层 WARN 默认拒绝——
   收紧默认、显式放宽（`degradable_layers` 参数），比「默认全放行」安全。
10. **`Instance.definition_id` 形态**：本批以 `def_key@version` 字符串
    承载（R0 模型字段为 str）；DB 落库（B7 API 批次）时如需外键化改为
    Definition 主键 ID，`definition_version` 字段已有冗余，不阻塞。
11. B4.3/B5 已知坑延续有效：pydantic extra=forbid、假游标布尔返回值、
    指纹排除时间戳、ruff line-length=100（E501 ignore）。
    本批新增模型（CountTolerance/DistributionTolerance/JointBucket 等）
    均继承 VersionedModel（extra=forbid 误写字段即拦下）。

## 5. 验收对照（实施方案 §9.5 属本批范围的条目）

| 门禁 | 结果 | 落点 |
|---|---|---|
| 1. 五层验证各有独立单测与故障注入（错行数/断外键/偏分布/破不变量/索引缺失） | ✅ | count：`test_exact_source_mismatch_fails_with_delta`；relationship：`test_declared_fk_orphans_fail`/`test_pk_integrity_null_or_duplicate_fails`；distribution：`test_joint_distribution_missing_bucket_fails`；invariant：`test_confirmed_relation_exists_violation_fails`；db_ready：`test_mysql_index_missing_fails` |
| 2. READY 需全部约定验证通过：任一层 FAIL 不得进入 READY（状态机强制） | ✅ | `test_layer_fail_blocks_ready` + `test_finalize_to_failed_records_reason`（FAILED 转换经状态机、原因入 transition_log） |
| 3. 清理可控：无所有权标记共享表清理被阻止并报告；独立 Schema 级清理成功；全部清理动作有审计 | ✅ | `test_shared_without_owner_token_blocked` / `test_dedicated_schema_takes_highest_priority` + `test_execute_with_approval_runs_and_audits`（三级审计） |
| 4. 结果可追溯：Instance → Manifest → Shard Checkpoint → ValidationReport 链路 ID 完整可查 | ✅（模型层） | `ready.artifacts_complete`（manifest.instance_id == instance.id + 五层齐全 + §12.4 要素齐备）；Shard Checkpoint 侧 R5 T5.7 已落（staging manifest ↔ DB 双向核对） |
| 5. 压测语义隔离：READY 仅表示数据构建与约定验证完成，文档与 API 输出不得声称 TPS 目标 | ⏳ B7 | 本批未触碰文档/API 输出层；READY 判定文本不含 TPS 语义；随 T7.1/T7.2 落地 |

## 6. 回归保障与回滚（§9.6）

- 验证器为只读组件：全部查询经语句白名单（SELECT-only）+ 标识符白名单；
  32 例白名单/注入拒绝用例锁死（`test_readonly_scalar_enforces_whitelist` /
  `test_identifier_whitelist_enforced` / `test_core_query_whitelist_enforced`）。
- legacy `datagen/**` 零修改（verify_min/selfCheck 原体保留，收编语义经
  测试对齐）；R5 全部模块零修改。
- 回滚：删除 §3 所列 9 个新模块 + 8 个新测试文件，还原 lifecycle/__init__.py
  导出即可；其余批次不受影响。

## 7. 后续批次衔接（B6 续 / B7，供下一 session 使用）

- **B6 未闭环项（如实记录，本批范围外）**：
  - **真库装载基准归档（R5 §13 延续）**：本环境无真库——`pilot_run.py`
    L405 借用点切换、原生通道基准、门禁 1/2 真库端到端复核与故障注入、
    T5.8 校准回填（ThrottleConfig/LoadBudget）继续挂起，需真库环境批次；
  - **B4.2 遗留问题 1（M:N external KV 执行载体）**：接入点就绪
    （`EXTERNAL_LOADER_REGISTRY`，fallback 链档 0），实现未落；
  - **依赖序装载编排器**（按 CycleResolution.steps 消费 NULLABLE_BACKFILL/
    STAGED_LOAD 多表编排）与 **T5.7 检查点接入装载主流程**：R5 §13 口径
    延续，checkpoint/staging/READY 协议两侧均已就绪，缺编排器本体；
  - **contract flaky 用例**（B5 §4 第 9 条）：延续未修复。
- **B7 承接（R7 平台集成）**：
  - validator/lifecycle **落库层**：`ptde_validation_report` /
    `ptde_dataset_definition` / `ptde_dataset_instance` 三表 repo（本批为
    内存模型层，`DatasetManager` 不持久化）；R1/R2 QW2 最小清理收编为
    T6.8 完整版子项（本批仅协议就位）；
  - T7.1 CLI（`perf ptde dataset verify/cleanup` 直连本批
    `evaluate_ready` / `execute_cleanup`）；T7.2 API；T7.3 压测绑定；
  - **压测语义隔离**（§9.5 门禁 5）：READY 文案与 API 输出层归 T7.1/T7.2；
  - selfCheck 端点抽检回调的真实接线（datagen_service → endpoint_probe
    适配器）随 T7.2 API 批次落。
- **R6 内部待接线**：`ValidationReport` 落 `ptde_validation_report`
  （layer/status/metrics_json/tolerance_json/evidence_json 列已对齐
  LayerResult 字段形态）；READY 八项判定结果可入
  `instance.validation_summary`（落库随 B7）。
- **staging_root 物理清理**：cleanup.py 档位协议就位（SCHEMA_REBUILD 等
  档 executor 回调由调用方注入）；staging 目录/分片文件物理删除动作与
  T5.7 `plan_recovery` 的 CLEANUP 动作衔接（须过 `evaluate_cleanup`
  授权）随编排器一并落。

---

*B6 主体交接完成。以下为同批次续记。*

# B6 续记：R5 衔接收口 — 依赖序装载编排器 + 检查点接入 + staging 清理衔接（2026-11-18）

> 状态：**已完成并通过门禁**
> 依据：本交接 §7「R6 内部待接线 / B6 未闭环项」；B5 交接 §13（编排器本体、
> T5.7 检查点接入装载主流程、staging 物理清理衔接——协议两侧就绪、缺编排器本体）；
> 代码位置：`ptde/loader/orchestrator.py`（本体）+ `staging.py` / `checkpoint.py`
> 两处协议最小扩展。

## 7A. 交付内容

### 依赖序装载编排器 — `ptde/loader/orchestrator.py`

- **消费 CycleResolution（不另起判定）**：无环实体按依赖序（父先子后）装载；
  环组按 `cycle_policy.resolve_cycle` 的策略执行：
  - `NULLABLE_BACKFILL`：两阶段——阶段一派生「阶段一分片」（staging.root/
    `_phase1/` 下，环内 FK 列置 NULL，流式改写不整驻内存）装载；阶段二经
    `backfill_provider` 回填（`BackfillStep` 参数化 UPDATE，标识符白名单 +
    UPDATE 前缀 + 表/列引用三重校验），回填后 `conn.commit()`；
  - `DEFERRED_CONSTRAINT`：同两阶段，回填后经 `TransactionAdapter` 单点提交；
    **逐批 commit 档（DbApiBatchInsertLoader）前置显式失败**（与事务内延迟
    校验冲突，不事后炸）；需单事务装载通道（Stub/native）；
  - `STAGED_LOAD`：`staged_load_guard`（B5 T5.5 原语复用）内完成两阶段装载
    与回填，退出恢复 FK 校验并提交；`allow_constraint_disabled=False` 显式
    拒绝（默认保留全部约束，总方案 §9.4/§19）；Constraint/Transaction 适配器
    缺失显式失败；
  - `SELF_REFERENCE`：普通装载（深度上限语义在生成侧，无需环列投影）。
- **回填提供方为生成侧映射接入缝**：`backfill_provider(entity) → [BackfillStep]`
  ——编排器不重推「哪行回填哪个父 ID」的映射；环列投影由调用方
  `project_fk_edges` 产出（`cycle_columns` 构造参数），环成员缺投影显式失败
  （A3：可空性信息不足不得伪装可装载）。
- **清单/状态显式失败**：实体在 staging 无分片、缺列清单、重复出现于装载序、
  环组未提供 Resolution、运行时装载失败（BulkLoadError 原样抛出不静默换通道）。

### T5.7 检查点接入装载主流程

- `prepare_checkpoints`：staging generated → DB PENDING（生成后登记）；
- 每分片装载：`loader.load_shard`（on_batch → `record_chunk`，chunk 从 1 起）
  → `record_bulkload_result`（staging.mark_loaded 行数复核锚点 + DB SUBMITTED
  + `BulkLoadResult.to_snapshot()` 落 db_feedback，不重复不遗漏）；
- 跑完 `staging.checkpoint()` 原子落盘 manifest；
- `recover(actual_rows_of, ...)`：消费 `plan_recovery` §9.6 分类——
  SKIP 跳过 / CONFIRM → `confirm()`（SUBMITTED→CONFIRMED）/ LOAD → 重装
  （staging 已 loaded 而 DB 实测 0 行的提交丢失场景经
  `staging.reset_to_generated` 退回可装载态，行数/校验和锚点保留）/
  CLEANUP → 过 `SecurityGate.evaluate_cleanup` CLEANUP 授权 + 注入
  `cleanup_executor` 后清理重装 / GENERATE、OFFSET_CONFIRM → 编排器显式拒绝
  （不做生成、不自动偏移确认）/ 计划不一致 → `require_recoverable()` 显式失败。

### staging 物理清理衔接

- `purge_staging(env_class, approvals, gate, physical_executor)`：
  `staging.verify_complete()`（不完整显式失败）→ `evaluate_cleanup`
  （生产/未知环境拒绝；无 CLEANUP 授权拒绝——文件不动）→ 物理删除
  （executor 注入，缺省 rmtree）→ 分片级检查点转 CLEANED（仅
  SUBMITTED/CONFIRMED 可转，其余显式失败）→ `staging_purged` 入审计。

## 7B. 协议侧最小扩展（两处）

| 文件 | 扩展 | 语义 |
|---|---|---|
| `ptde/generator/staging.py` | `reset_to_generated` | 仅 loaded→generated 单向（提交丢失重装场景 §9.6）；行数/checksum 保留 |
| `ptde/loader/checkpoint.py` | `CheckpointCoordinator.confirm` / `mark_all_cleaned` | §9.6 CONFIRM 落位；staging 清理后 CLEANED 状态语义（§12.4） |

## 7C. 文件清单（续记）

```text
新建（生产代码）
perf-py/ptde/loader/orchestrator.py       # 编排器本体（消费 CycleResolution/staging/checkpoint/gate）

修改（生产代码，最小扩展）
perf-py/ptde/generator/staging.py         # reset_to_generated（§9.6 提交丢失重装）
perf-py/ptde/loader/checkpoint.py         # confirm / mark_all_cleaned（恢复与清理状态落位）
perf-py/ptde/loader/__init__.py           # 导出扩容

新建（测试，共 35 例）
perf-py/tests/test_ptde_loader_orchestrator.py
  # 依赖序/检查点接入（prepare/chunk 累计/提交）/BulkLoadError 不换通道/
  # 清单显式失败 ×3 / NULLABLE_BACKFILL 两阶段+回填三重校验+派生文件清理 /
  # STAGED_LOAD 批准/适配器/disable-enable+guard 提交 /
  # DEFERRED 逐批档拒绝+TransactionAdapter 单点提交 / SELF_REFERENCE /
  # recover 分类执行（load/confirm/generate 拒绝/offset 拒绝/不一致/
  # cleanup 无 gate/无授权/授权执行重装）/ purge（不完整/无授权/生产拒绝/
  # 成功+CLEANED+审计）/ staging 重置协议
```

## 7D. 关键设计决策（承接 §4）

1. **编排器只做装载编排，不重推生成侧映射**：回填语句由
   `backfill_provider` 提供（生成侧拥有「子行 ↔ 父 ID」映射知识）；
   编排器负责时序（装载→回填→提交）、边界（guard/事务）与校验
   （标识符/UPDATE 形态/引用一致）。
2. **阶段一统一「环内 FK 列置 NULL」**：NULLABLE_BACKFILL 与 STAGED_LOAD
   共用同一数据形态（派生分片流式改写）——guard 只解决「装载期间约束
   校验」，不改变数据形态；派生文件为编排器自产中间产物，回填后即删
   （非 DB 破坏性操作，不入 CLEANUP 授权面）。
3. **DEFERRED_CONSTRAINT 前置拒绝逐批 commit 档**：T5.5 收编档逐批 commit
   与「事务内延迟到提交才校验」语义直接冲突——在开装前按 loader 链检测
   显式失败（不装到一半才炸），须换单事务装载通道（真库接线时由
   原生 LOAD DATA/COPY 单语句通道承载）。
4. **「已装载分片不静默重复装载」**：`run` 只接受 generated；loaded 分片
   重复装载必须走 `recover`（DB 实测裁决），提交丢失经
   `reset_to_generated` 显式重置——不重复不遗漏锚点延续（§8.6 门禁 1/2）。
5. **恢复 CLEANUP 的授权面与主门禁共用**：`recovery_gate` 构造注入
   （审计归一），未注入/无 CLEANUP 授权/生产环境三分支全部显式拒绝；
   cleanup_executor（部分行 DB 清理回调）由调用方注入。
6. **checkpoints CLEANED 只吃 SUBMITTED/CONFIRMED**：与
   `verify_complete`（staging 全 loaded）串联——不完整 staging 先在
   `purge_staging` 入口被挡（显式失败）。

## 7E. 门禁（续记）

- 交接口径：`uv run pytest tests/contract tests/test_datagen_* tests/test_ptde_* -q`
  → **全绿（0 失败），1 skipped**（A2 全量基准环境闸门，既有口径）；
  收集 1329（= B6 交接 1294 + 本续记 35）；
- 全库 `uv run pytest tests/ -q` → **全绿**；
- `uv run ruff check ptde/ tests/test_ptde_* tests/test_datagen_*`
  → **All checks passed**；
- legacy `datagen/` 零修改；R5/R6 既有模块仅 §7B 两处协议最小扩展
  （既有 20 例 checkpoint 测试零改动通过）。

## 7F. 更新后的衔接清单（替代 §7 对应条目）

- **已闭环（本续记）**：依赖序装载编排器本体；T5.7 检查点接入装载主流程
  （record_generation/record_bulkload_result/plan_recovery 消费面）；
  staging 物理清理与 `plan_recovery` CLEANUP 动作衔接（过 `evaluate_cleanup`
  授权）——R5 §13 三个「随编排器一并落」项全部落地；
- **仍开放（口径不变）**：
  - 真库装载基准归档（A2 12.79s 降级通道口径；pilot_run.py L405 借用点
    切换 `FallbackLoader`、门禁 1/2 真库端到端复核与故障注入、
    T5.8 校准回填——需真库环境批次）；真库接线时编排器 loader 换
    `FallbackLoader` 全链（fake 层已验证协议兼容）；
  - B4.2 遗留问题 1（M:N external KV 执行载体）：`EXTERNAL_LOADER_REGISTRY`
    装配位仍空置（fallback 档 0 未注册留痕不伪装）；
  - contract 时序型 flaky 用例：延续未修复（本轮未触发）；
- **B7 承接**：validator/lifecycle 落库层 repo；T7.1 CLI（`perf ptde dataset
  verify/cleanup` 现可直连 `evaluate_ready` / `execute_cleanup` /
  `purge_staging`）；T7.2 API；T7.3 压测绑定；selfCheck 端点抽检回调接线。

---

*B6 续记交接完成：R5 全部协议侧衔接项收口，编排器本体落地。*
