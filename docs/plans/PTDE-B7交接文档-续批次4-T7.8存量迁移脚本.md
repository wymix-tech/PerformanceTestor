# PTDE B7 续批次 4 交接文档 — T7.8 存量迁移脚本（legacy 三表 → ptde_*）

> 状态：**已完成并通过门禁**（2026-11-19）
> 依据：`PTDE-B7交接文档-续批次3-运行中取消与selfCheck接线.md` §7「可立即
> 推进项」第 1 项；`PTDE-B7交接文档-R7平台集成前段.md` §7（脚本本体可提前
> 开发）；`PTDE-实施方案-R0-R7分批执行计划.md` T7.8 任务行 + §12.5 迁移映射
> + §13.2 切换步骤 2；总方案 §12.5；
> 代码位置：`server/ptde_migration.py`（迁移本体）、`perf_cli/ptde_runner.py`
> （`run_legacy_migrate`）、`perf_cli/main.py`（`perf ptde migrate-legacy`
> 命令壳）；
> 前序交接：B7 前段 + B7 续批次 1/2/3 全部有效。

---

## 1. 交接基线与结果

- 入口基线：B7 续批次 3 交接口径全库 **1696 passed, 2 skipped**；
- 交付后门禁：
  - `uv run pytest tests/contract tests/test_datagen_* tests/test_ptde_* -q` →
    exit=0、零失败（与 B7 前段交接口径持平，零回归）；
  - 全库 `uv run --extra server --extra locust pytest tests/` →
    **1707 passed, 2 skipped, 0 failed**（基线 1696 + 新增 11；skip 既有
    locust 导入闸门口径）；
  - `uv run ruff check ptde/ perf_cli/ server/ tests/` → **All checks passed**；
- 本批为 **server/ 侧新增模块 + CLI 接线**；**未触碰** legacy `datagen/**`
  运行行为（仅 import 其 ORM 模型做读取）、`ptde/**` 全部模块（状态机转换表
  零变更）、`/api/datagen` 与 `perf datagen` 契约形状、`sql/init.sql`
  （迁移目标表列在 R0/B0 即建全，`legacy_*` 映射列原生支持，无 DDL）。

## 2. 交付内容

### 2.1 迁移本体：`server/ptde_migration.py`（T7.8，§12.5 映射逐条落地）

| 源表 | 目标 | 落地口径 |
|---|---|---|
| `data_gen_task` | `ptde_dataset_instance`（`legacy_task_ids=[task.id]`）+ 按需 definition（`legacy-task-{id}`@version='legacy'） | `tables_json`（`tableName/rowCount` 表组形态）反解为 DatasetSpec 草案 rows；单表形态回退 `table_name + row_count/total_rows`；无法反解且无可用行数 → spec 空 rows + `error_msg` 说明（原始 JSON 由归档表承载，§12.5 原文口径）；finished→READY（actual_rows=inserted）/ failed→FAILED（error_msg 透传）/ pending\|running→CANCELLED（未执行完成说明文案）；未知状态失败即停 |
| `data_gen_plan` | `ptde_dataset_definition`（version='legacy'，def_key=`legacy-plan-{id}`）+ `ptde_dataset_instance`（`legacy_plan_id`） | `tables_json`（`table/rowCount` 方案形态，经 `datagen_plan_service.parse_tables` 形状核对）反解为 spec 草案；`endpoint_json/stress_json/warnings_json` **原始引用保留**于 instance.validation_summary（可解析则结构化存，否则原样保留——不丢信息）；executed→READY / failed→FAILED / pending\|adopted\|discarded→CANCELLED |
| `semantic_hint` | `ptde_finding`（`legacy_hint_id`） | hint_type→finding_type 映射表：value→`enum`（魔法值候选）、relation→`relation`（对齐 ptde 分析器既有词汇）；adopt_status 直迁（pending/adopted/dismissed，未知失败即停）；status_three 缺省 `INFERRED`（semantic_hint 无三态证据模型，不伪装 CONFIRMED）；evidence_json 透传 source_refs，confidence 透传，analyzer_version='legacy-migration' |

**脚本要求四项（§12.5）落地方式**：

1. **幂等（先查后写，以 legacy_* 字段为去重键）**：task→扫
   `ptde_dataset_instance.legacy_task_ids` JSON；plan→`legacy_plan_id`
   （idx_legacy_plan 就绪）；definition→`uk_def_version`（def_key 确定性
   派生自 legacy 主键，重放稳定）；hint→`legacy_hint_id`，另以
   `uk_finding` 四元组兜底去重（重复 hint 跳过不中断整批）；
2. **可重放**：重放时已迁移行全部 skipped，目标表行数不变（单测 +
   CLI 冒烟双验证）；
3. **行数核对报告**：报告含 sources（total/created/skipped/sourceRows）与
   targets（before/after/created/verified）；核对 `after == before +
   created`，不通过抛 `MigrationError` 整体回滚；
4. **失败即停不留半态**：单 Session 单事务承载三类目标表全部写入
   （definitions/instances/findings 同事务），异常/核对失败即回滚，不提交
   任何变更（单测锁定回滚后目标表为空）。

### 2.2 CLI 入口：`perf ptde migrate-legacy`（T7.8「+ 迁移脚本」落点）

- `perf_cli/ptde_runner.py` 新增 `run_legacy_migrate(db_url, output,
  session_factory)`：DB 解析复用 `resolve_cli_db`（--db → PTDE_CLI_DB →
  本地 sqlite）；sqlite 便捷建表口径对齐 `_default_service`（生产 MySQL
  由 init.sql 建表，不越权 DDL）；退出码 0=成功 / 1=MigrationError 失败
  即停（文案明示「未提交任何变更」）；
- `perf_cli/main.py` 新增命令壳 `@ptde_app.command("migrate-legacy")`
  （薄壳 → runner，对齐既有命令样式）；
- legacy 与 ptde **同库**（server 栈 DB），符合「存量数据在同一 perf 库」
  的切换前提（§13.2 步骤 2 直连执行）。

### 2.3 状态映射的显式决策（承接批次 3 §4 决策风格）

- **历史登记不经 `state_machine.transition()`**：存量迁移登记的是历史
  artifact 的实际状态，非活体流转；伪造 PLANNED→GENERATING→…→READY 多跳
  链是假历史，单条 `TransitionRecord(from=PLANNED, to=映射状态,
  operator='legacy-migration', reason='A4a 存量迁移（实施方案 §12.5）：
  legacy <表>(id=N) status=<原状态>')` 才是诚实审计——审计落在
  transition_log（复用 TransitionRecord 形状，下游 `instance_to_model`
  读回无感）；
- **spec 草案只承载可反解要素**：DatasetSpec（extra=forbid）不含
  endpoint/stress 侧写——原始引用落 instance.validation_summary，定义层
  spec 保持「行数草案」最小形态，语义漂移风险为零；
- **READY 映射的含义边界**：finished→READY 仅表示「历史灌数已完成、
  inserted 行数可核对」，五层验证从未执行——validation_summary 的
  legacy 溯源块（source/legacyTaskId/legacyStatus/inserted/costMs）显式
  承载这一点（未执行 ≠ 通过口径延续）。

## 3. 文件清单

```text
修改（生产代码）
perf-py/server/ptde_migration.py        # 新建：迁移本体（映射/幂等/单事务/核对报告）
perf-py/perf_cli/ptde_runner.py         # + run_legacy_migrate（DB 解析/sqlite 便捷建表/退出码）
perf-py/perf_cli/main.py                # + @ptde_app.command("migrate-legacy") 命令壳

修改（测试，共 11 例）
perf-py/tests/test_server_ptde_migration.py   # 新建：四态任务映射/表组反解/方案映射+
                                              #   原始引用保留/hint 映射+直迁/幂等重放/
                                              #   失败即停回滚（两例）/核对报告形状/
                                              #   transition_log 审计

未改动：datagen/**（legacy 零修改，仅 import ORM 模型读取）、ptde/** 全部
（转换表零变更）、perf_web 侧、sql/init.sql（迁移目标表 R0 已建全）
```

## 4. 关键设计决策与踩坑记录

1. **def_key 确定性派生 = 幂等的根**：`legacy-task-{id}` / `legacy-plan-{id}`
   使重放时「先查后写」的查侧命中稳定；不按 plan_name/表名派生（重名/改名
   会破坏重放语义）。
2. **uk_finding 之外的迁移去重键**：uk_finding 不含 legacy 来源，且 value
   fingerprint 依赖反解结果——迁移去重以 `legacy_hint_id` 为主键、uk_finding
   兜底（重复 hint 降级 skip 而非唯一键冲突中断整批），两层都不命中才插入。
3. **踩坑：DatasetSpec 无 camelCase 别名**（extra=forbid）——spec 草案 dict
   须 snake_case（`target_database`）；pydantic 即时拦截，测试首跑暴露后
   修正（与 B6「旧 dataclass 手写 to_camel」事实勘误 F4 同源）。
4. **tables_json 两种键形**：task 表组形态键为 `tableName`（Java DTO 序列化），
   plan 方案形态键为 `table`（`parse_tables` 口径）——反解函数参数化键名，
   不做隐式兼容。
5. **空态 ≠ 反解失败**：tables_json 为 None/空是正常空态（方案未分析出表
   清单、单表任务），不算「无法反解」；仅「内容存在但非法」才落 error_msg
   说明（§12.5 口径精确落地，避免对正常空态制造噪音）。
6. **报告层不落库**：核对报告为 CLI 输出（stdout/--output JSON），不新增
   迁移审计表——transition_log + validation_summary 已承载行级审计，批次级
   报告归档由切换手册 §13.2 步骤 2 的「报告归档」口径承载。

## 5. 验收对照（T7.8 任务行 + §12.5）

| 验收项 | 结果 | 落点 |
|---|---|---|
| 映射逐条落地（task/plan/hint 三行） | ✅ | `test_task_status_mapping_and_results`、`test_plan_migration_definition_instance_and_raw_refs`、`test_hint_mapping_value_relation_and_adopt_status` |
| 幂等可重放（重放全 skipped、行数不变） | ✅ | `test_replay_idempotent_all_skipped_and_counts_unchanged` + CLI 有数据二次重放冒烟（run1 created 2/2/1 → run2 skipped 1/1/1、targets 不变、verified 均 True） |
| 行数核对报告 | ✅ | `test_report_shape_and_count_verification`（before/after/created/verified + sourceRows） |
| 失败即停不留半态 | ✅ | `test_unknown_task_status_fails_and_rolls_back`、`test_hint_unknown_type_fails_and_rolls_back`（单事务回滚，目标表为空） |
| 原始 JSON 保留口径 | ✅ | `test_unparseable_tables_json_error_msg_note_and_empty_rows_spec`（error_msg 指路归档表）；plan endpoint/stress 原始引用断言 |
| 迁移审计（transition_log） | ✅ | `test_transition_log_records_migration_provenance`（operator/reason/from/to/occurred_at 全落） |
| CLI 入口 | ✅ | `perf ptde migrate-legacy` 空库 + 有数据 + 重放三轮冒烟，exit=0 |
| 门禁 | ✅ | contract/datagen/ptde 域 exit=0 零回归；全库 1707 passed, 2 skipped；ruff All checks passed |

## 6. 回归保障与回滚（§10.4）

- **契约零改动**：`/api/datagen` + `perf datagen` 契约与 golden 全绿；
  迁移为 server/ 新增模块 + `/api/datasets` 端点族之外的 CLI 命令，无 HTTP
  面新增；`datagen/**` 运行行为零修改（hint_upsert/insert_task 等原样）；
- 回滚：删 `server/ptde_migration.py` + `tests/test_server_ptde_migration.py`
  + 还原 ptde_runner.py / main.py 两处追加段即可；无 DDL、无模型层变更；
  已迁移的 ptde_* 行为追加数据，回滚脚本后不影响既有功能（instances 走
  既有生命周期逻辑无感）。

## 7. 后续批次衔接（供下一 session 使用）

- **本批已闭环**：~~T7.8 迁移脚本本体~~（✅——切换前置 T7.6/T7.7 未发生，
  脚本先行就位；§13.2 步骤 2 切换时直连执行 + 报告归档即可）；
- **真库切换批次衔接**（执行 §13.2 时）：
  - 迁移前预检：`ptde_*` 表就绪（init.sql）+ 回滚点 git tag；
  - 迁移后动作（本脚本不越权 DDL，环境侧落地）：旧三表收权只读归档
    （保留回溯期 ≥ 一个发布周期）；
  - 行数核对报告归档（--output JSON 工件）；
- **仍未闭环**（承接批次 3 §7 清单，原口径转记）：
  - T7.6/T7.7 接管切换（被 §13 门禁 1/2 前置；门禁 2 双跑对照需真库）；
  - T7.9 legacy 整包退役（被 T7.8 验证通过 + 观察期前置；
    `ptde/models/compat.py` 随整包清理）；
  - API 超时控制 / 外部观测接线 / R1R2 QW2 收编（随真库与部署形态批次）；
  - perf-web：数据集管理页（可暴露取消按钮）/ 绑定还原 / 场景步骤级引用 /
    `GET /api/datasets` 游标分页（纯前端，可立即推进）。

---

*B7 续批次 4 交接完成：T7.8 存量迁移脚本本体落地（§12.5 三表映射逐条对应，
legacy_* 去重键幂等可重放，单事务失败即停 + 行数核对报告），CLI 入口
`perf ptde migrate-legacy` 就位——R7 切换手册 §13.2 步骤 2 的执行缝闭合。*
