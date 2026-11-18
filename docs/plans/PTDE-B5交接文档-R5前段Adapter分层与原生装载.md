# PTDE B5 批次交接文档 — R5 前段：Adapter 分层 + Capability Probe + 原生装载（T5.1~T5.4）

> 状态：**已完成并通过门禁**（2026-11-18）
> 依据：`PTDE-实施方案-R0-R7分批执行计划.md` §8.2（R5 任务表 T5.1~T5.4）、§8.6（验收门禁）、§8.7（回归保障）；
> `性能测试平台-智能测试数据引擎-完整技术方案.md` §9.2/§9.3/§9.4/§10.1/§10.2/§13.3；
> 代码位置：`perf-py/ptde/adapters/`（base / probe / mysql/bulkload / postgres/bulkload）
> 前序交接：`PTDE-B4.3交接文档-R4.3分片并行与PilotRun.md` §4 踩坑记录与 §7 衔接清单延续有效；
> `PTDE-B4.2交接文档-R4.2关系生成器泛型化.md`（原始交接已归档，遗留问题口径见其 §7）

---

## 1. 交接基线与结果

- 入口基线：`uv sync --all-extras` 后门禁 **1022 passed, 1 skipped**（与交接要求一致；
  首次运行曾出现 1 例失败，经排查为 contract 域时序型 flaky 用例，详见 §4 第 9 条）。
- 交付后门禁：
  - `uv run pytest tests/contract tests/test_datagen_* tests/test_ptde_* -q` →
    **1062 passed, 1 skipped**（新增 40，零回归）；
  - `uv run ruff check ptde/ tests/test_ptde_* tests/test_datagen_*` → **All checks passed**。
- 本批次为 `ptde/adapters/` 内纯新增（base/probe/两个 bulkload + 包导出更新）；
  **未触碰** legacy `datagen/`（A1 分轨延续）、R1/R2 既有 metadata/statistics 适配器、
  `ptde/generator/**`（staging 协议仅消费未改动）。

## 2. 交付内容（按任务）

### B4.2 遗留问题 1/3 — 按 B4.2 交接文档 §7 原始口径核对

> 注：本批开工时 B4.2 原始交接未落盘，曾按代码证据推定 1/3 口径并先行处理；
> 原始交接归档后核对——**推定与原始口径不符，原推定内容（能力探测来源 /
> 环内执行原语）不是 B4.2 遗留问题**，已在 §7 按原始口径改记。原推定中
> T5.1 Constraint/Transaction 接口与 T5.2 deferred_constraints 探测项仍是
> R5 后续装载编排的合理落点，但不再记为遗留问题闭环。

- 遗留问题 1（M:N sharded / external 两策略只有选型与接口，执行载体待
  R4.3 分片映射 + R5 外部 KV/装载侧）：R4.3 侧由 partitioner/staging 承载；
  **本批 T5.1~T5.4 未落 external KV/装载侧执行通道**——T5.1 SPI 注册与
  BulkLoad 接口为其预留落点，实现归 T5.5+ 装载编排 / B6（§7 衔接）；
  `select_mn_strategy` 返回值语义已锁定，勿改枚举值（B4.2 §7 原始口径）；
- 遗留问题 3（assign_children 无 zero_child_ratio 时精确收敛靠调整：
  超大规模 + 重倾斜下调整量与分布保形的权衡未压测）：延续为 Pilot Run
  观察点——B4.3 百万级 12.79s 实测（均匀口径）未暴露，随真库装载基准
  归档（§7）继续观察，不视为已闭环。

### T5.1 Adapter 分层接口 — `ptde/adapters/base.py`

- 七大子接口（总方案 §9.2 分层）：Metadata / Statistics / BulkLoad /
  CapabilityProbe / Constraint / Transaction / StorageMetrics（抽象基类，接口形状固化；
  R1/R2 已落的 metadata/statistics 语义对应 Metadata/Statistics 层）；
- **SPI 注册机制沿用 legacy `datagen/spi.py` 的 `DatabaseDialect` 语义映射**
  （legacy 本体不动）：`supports(jdbc_url)` → `url_prefixes` 前缀匹配（最长前缀优先）、
  驱动懒加载 → `factory` 惰性实例化、重复注册须显式 `overwrite=True`；
  `adapter_for_url` / `get_adapter` 未命中显式失败不静默换用（A3）；
- 宿主装配约定：`register_builtin_adapters()`（对齐 legacy「宿主注册」惯例，
  包导入不隐式注册）；本批注册 mysql/postgres 两个 BulkLoad 适配器（T5.5 降级链消费）；
- 结果侧模型 `BulkLoadResult`（行数核对锚点 + 警告样例 + 预检说明，
  指纹排除 `collected_at`，协议 §7.5 JSON 原生 `to_snapshot`）；
  `BulkLoadError`（期望/实际行数、错误样例、清理提示全部可见）、
  `BulkLoadUnsupported`（降级决策输入，带原因不降级）。

### T5.2 Capability Probe — `ptde/adapters/probe.py`

- 探测项覆盖总方案 §10.1 全集 13 项（`CAPABILITY_KEYS` 锁死：
  schema/index/FK/partition/statistics 读取、bulk_load、parallel_load、
  sequence_support、deferred_constraints、trigger_fires_on_bulk_load、
  lob_json_binary_support、storage_statistics、post_load_analyze）；
  模型层校验：快照缺项显式失败（不得缺省伪装已探测）；
- **快照探测填充，非复制示例**（§10.2 末行口径）：MySQL 实测
  `@@local_infile / @@secure_file_priv / @@sql_mode / @@version_comment`
  （MariaDB 变体、ANSI 兼容线索）；PG 实测 `pg_catalog.pg_attribute` 列
  存在性（deferrable / relrowsecurity / 分区 / 约束）；
- A3：单项探测失败 → 该项 None（未知）+ notes 说明，不伪装 True/False、
  不中断其余探测项；版本不可得 → `CapabilityProbeError`（无法识别环境禁止
  写入，§13.1）；`post_load_analyze` 为推定项并在 notes 标注（不伪装事实）；
- 回填通道：`AdapterCapability.to_snapshot()` → `ExecutionManifest.
  adapter_capability_snapshot`（快照带指纹、`probed_at` 不入指纹，门禁 6 可审计）；
- `probe_adapter(conn, product)` 分发 + 别名归一（postgresql/pg、mariadb），
  未注册探测器显式失败。

### T5.3 MySQL Bulk Load — `ptde/adapters/mysql/bulkload.py`

- `LOAD DATA LOCAL INFILE`（QW4 `tests/poc_load_data_infile.py` 实测口径，
  勿另起一套）：
  - **secure_file_priv**：LOCAL 通道不经服务端文件白名单（仅 SERVER 端
    INFILE 受限）——探测值记录进预检说明与 Capability 快照供审计；
  - **LOCAL 权限**：服务端 `@@local_infile=ON` **且** 客户端连接
    `local_infile=True`（pymysql 属性鸭子探测）——任一不满足
    `BulkLoadUnsupported`（带原因；降级决策归 T5.5，本方法只判定不降级）；
  - **错误/警告计数**：`cur.rowcount` 与期望行数显式比对 + `SHOW COUNT(*)
    WARNINGS` / `SHOW WARNINGS` 样例回传——**短装即 BulkLoadError，
    不静默丢弃错误记录**（QW4 结论；§8.6 门禁 3 用例锁死）；
  - **触发器/分区影响**：装载前查 information_schema 预检，说明随
    `precheck_notes` 回传（触发器逐行触发 + 外部副作用提示 §13.3；
    分区表锁等待/日志增长提示 §9.5）；
  - csv_v1 契约：`IGNORE 2 LINES` 跳格式声明+列头；装载前核对首行
    `format_version=csv_v1`（不符显式失败）；`\N`（NULL_TOKEN）与 MySQL
    默认 ESCAPED BY 语义天然对齐，NULL/空串区分保留；
  - 标识符校验复用 `statistics.validate_identifier`（收编口径）。

### T5.4 PG Bulk Load — `ptde/adapters/postgres/bulkload.py`

- `COPY ... FROM STDIN WITH (FORMAT csv, NULL '\N')` 流式装载：
  `ShardTextStream` 流包装器逐块透传（剥离声明+列头，**不整文件驻内存**）；
- **RLS 检查**：`pg_class.relrowsecurity / relforcerowsecurity` 开启的表
  显式拒绝装载（插入受策略约束，结果无法保证可见/完整——不由 Loader
  静默放行，A3），错误带处置建议（业务通道或显式批准策略豁免）；
- **约束检查**：INSERT 权限 `has_table_privilege` 实测（不足显式失败）；
  FK/CHECK/唯一约束在 COPY 中照常校验（默认保留全部约束，§9.4）；
- **失败空间清理提示**：COPY 失败回滚留死元组——`BulkLoadError` 携带
  `cleanup_hint`（VACUUM 建议，§8.2 验收项）；
- 触发器预检（`pg_trigger` 非内部触发器清单）说明回传。

## 3. 文件清单

```text
新建（生产代码）
perf-py/ptde/adapters/base.py               # T5.1
perf-py/ptde/adapters/probe.py              # T5.2
perf-py/ptde/adapters/mysql/bulkload.py     # T5.3
perf-py/ptde/adapters/postgres/bulkload.py  # T5.4

修改（生产代码）
perf-py/ptde/adapters/__init__.py           # register_builtin_adapters 宿主装配入口
perf-py/ptde/adapters/mysql/__init__.py     # 导出 MysqlBulkLoadAdapter
perf-py/ptde/adapters/postgres/__init__.py  # 导出 PostgresBulkLoadAdapter

新建（测试）
perf-py/tests/test_ptde_adapters_base.py      # 13（接口形状 + SPI + 错误模型）
perf-py/tests/test_ptde_adapters_probe.py     # 9（§10.1 全集 + 未知降级 + manifest 回填）
perf-py/tests/test_ptde_adapters_bulkload.py  # 18（两通道可用性/装载/失败路径 + 流包装器）

新建（文档）
docs/plans/PTDE-B4.2交接文档-R4.2关系生成器泛型化.md  # 文档缺口归档
  （本批曾推定补写；B4.2 原始交接随后归档并以其为准——遗留问题 1/3 的
  R5 侧承接见本文件 §2 勘误与 §7 衔接）

未改动：datagen/**、ptde/generator/**、ptde/models/**、ptde/planner/**、sql/init.sql
```

## 4. 关键设计决策与踩坑记录（承接 B4.1/B4.3 §4）

1. **能力探测不硬编码**：adapter 能力一律实测（§9.3），探测失败记 None+notes
   而非伪装布尔——模型层用「缺项显式失败」把 §10.1 全集契约锁死在
   `model_post_init`（少声明即拦下，B4.3 pydantic `extra="forbid"` 教训同源）。
2. **pydantic `extra="forbid"` 拦截误写属性**：开发中曾在 `_probe_identity`
   误写 `cap.bulk_load_probe = ...` 占位行，被 extra=forbid 立即拦下——
   版本化模型当鸭子对象用会直接失败（延续 B4.3 §4 第 8 条教训）。
3. **鸭子类型假游标的布尔值必须用真 bool**：FakeCursor 返回 `("f",)`/
   `("t",)` 时 `bool("f")` 为 True——RLS/权限判定测试因此静默翻车；
   假连接路由返回值一律用 `(True,)/(False,)`（样本量/时序教训同源：
   断言要确定性，不依赖类型巧合）。
4. **假游标路由 first-match 语义**：按 marker 包含匹配、字典序先到先得——
   新增路由时注意 marker 与其他 SQL 的包含关系（如 `information_schema.TABLES
   WHERE TABLE_SCHEMA` 与 COLUMNS 查询互不包含才可安全并存）。
5. **MySQL ESCAPED BY 与 csv_v1 的交互（遗留约束，勿在 Loader 侧另起转义）**：
   csv_v1 未转义反斜杠，而 LOAD DATA 默认 ESCAPED BY '\\'——值内裸
   反斜杠（如 `\n` 序列）会被解释为转义序列。当前口径：分片值域为
   业务文本（无裸反斜杠）时安全；若未来值域引入反斜杠，须在
   file_format 写入侧统一转义（全链路一处改，不在 Loader 侧补）。
6. **`SHOW COUNT(*) WARNINGS` 形态**：返回单行单列（`@@session.warning_count`），
   取 `row[0][0]`；`SHOW WARNINGS` 样例上限为服务端默认（样例截断展示
   前 3 条在 BulkLoadError 文案，完整清单保留在异常属性）。
7. **COPY 与 LOAD DATA 的反斜杠语义相反**：PG COPY CSV 格式下反斜杠
   **非**转义字符（`\N` 按字面量匹配 NULL token）；MySQL LOAD DATA 下
   反斜杠是转义字符。两通道 NULL 语义对齐 csv_v1 但转义语义不同——
   勿把 MySQL 侧结论直接平移到 PG（模块 docstring 已注明）。
8. **PG `bool(row[0])` 对 `"f"` 陷阱**：与第 3 条同源——psycopg2 返回真
   bool 无此问题，但预检代码统一 `bool(row[0])` 时对驱动返回值形态保持
   防御（假连接测试已锁死正确布尔路径）。
9. **既有时序型 flaky 用例（非本批引入）**：入口基线首跑
   `tests/contract/test_contract_source_write.py::
   test_async_import_thread_pool_and_failure_status` 偶发失败（线程池异步
   导入 + sleep 轮询，负载下轮询超时），单独重跑与全量重跑均绿；
   本批未触碰 contract 域，建议后续批次给该用例延长轮询上限或改为事件等待。
10. B4.3 已知坑延续有效：行计数按 csv 记录数、多进程 staging 父进程合并、
    校验和接入写路径、stream+多进程显式失败、行工厂按名称还原、
    conditional None 哨兵、pydantic extra=forbid、指纹排除时间戳、
    ruff line-length=100（E501 ignore）。

## 5. 验收对照（实施方案 §8.6 属本批范围的条目）

| 条目 | 结果 | 落点 |
|---|---|---|
| 1. 导入结果可核对（逐分片 loaded==expected，全量非抽样） | ✅ 行数核对锚点：rowcount 与期望行数显式比对，短装即 BulkLoadError（假连接层锁死；真库复核随 T5.7 检查点归档） | `test_mysql_row_mismatch_raises_with_samples` / `test_pg_rowcount_mismatch_carries_vacuum_hint` |
| 3. 不静默丢弃错误 | ✅ 错误计数/样例可见（QW4 口径） | `test_mysql_row_mismatch_raises_with_samples`（样例断言） |
| 4. 降级有说明 | ✅ 原生通道不可用显式 `BulkLoadUnsupported` 带原因（@@local_infile=OFF / 客户端未启用 / INSERT 权限）；自动降级链路 T5.5 承接 | `test_mysql_local_infile_server_off_unsupported` 等 |
| 6. Probe 快照可审计 | ✅ 快照带指纹 + ExecutionManifest 回填 + 缺项显式失败（版本级测试锁死键集） | `test_capability_snapshot_seals_and_json_native` / `test_capability_snapshot_rejects_missing_keys` |
| 2. 失败可安全恢复（注入中断故障） | ⏳ T5.7 分片检查点（staging manifest 协议已就绪可直接消费） | — |
| 5. 门禁生效（生产标识库拒绝+审计） | ⏳ T5.9 执行前安全门禁 | — |
| 门禁 1/2 真库端到端复核 | ⏳ 真库装载基准（A2 归档口径）随 T5.5+ 落地后补 | — |

## 6. 回归保障与回滚（§8.7）

- legacy `datagen/` 路径零修改；`DbApiBatchExecutor` 降级档语义原样保留
  （T5.5 收编时承接）；`ptde/generator/**` 协议零件零修改（staging 仅消费）。
- 回滚：删除 `base/probe/mysql/bulkload/postgres/bulkload` 四个新模块 +
  3 个新测试文件，并还原 3 个 `__init__.py` 导出小改动即可；其余批次不受影响。
- 双跑对照（§8.7）：原生通道（本批）vs legacy `executemany` 通道的同
  DatasetSpec 结果对照，随 T5.5 降级链接管装载编排后推进。

## 7. 后续批次衔接（R5 后段 T5.5~T5.9，供下一 session 使用）

- **T5.5 降级链** `ptde/adapters/bulkload/fallback.py`：原生（本批
  `BulkLoadAdapter.load_shard`）→ 官方替代批量接口 → `DbApiBatchExecutor`
  参数化批量 INSERT（批次/并发降档，收编 executor.py，DEFAULT_BATCH_SIZE=500
  与 refSkew>50% 热行 batch 调优建议语义保留）→ 降级告警；预算内无法完成
  显式失败（§10.3）。`BulkLoadUnsupported`（带原因）即降级判定输入。
- **T5.6 装载前目标表检查** `ptde/adapters/precheck.py`（总方案 §9.4）：
  本批 T5.3/T5.4 的 `precheck_notes`（触发器/分区/RLS/权限）为起点口径；
  生成列/自增/复制限制等逐项补齐；默认保留全部约束。
- **T5.7 分片检查点与断点恢复** `ptde/loader/checkpoint.py`（§12.4
  `ptde_shard_checkpoint`）：直接消费 `ptde/generator/staging.py`
  检查点协议（ShardRecord status/rows/checksum/loaded_rows 锚点、
  manifest 原子落盘、`restore()`、`mark_loaded` 行数复核、`verify_complete`）
  与 `BulkLoadResult.to_snapshot()`；恢复流程按 §9.6（读清单 → 查实际
  写入量 → 区分已完成/部分提交 → 安全清理或偏移确认）。
- **T5.8 并发与限流** `ptde/loader/throttle.py`：Pilot Run 校准（无通用
  固定值）；监控 CPU/IO/日志增长/锁等待软限流。
- **T5.9 执行前安全门禁** `ptde/loader/gate.py`（总方案 §13.3，§8.4 清单）：
  Capability Probe（本批 T5.2）提供环境识别输入（版本不可得即
  CapabilityProbeError——「无法识别 → 禁止写入」已就位）；破坏性操作
  显式授权+审计、凭据分离（profile_dsn/target_dsn，A5）待落。
- **真库基准归档**：A2 结论（12.79s）目前走降级通道；T5.3/T5.4 原生通道
  落地后回 `tests/test_ptde_planner_pilot_generation.py::
  test_generation_pilot_full_million_rows` 补真库基准并归档。
- **B4.2 遗留问题 1（R5 侧承接，未闭环）**：M:N external KV / 装载侧
  执行通道未实现（本批仅落 SPI 注册 + BulkLoad 接口落点）；随 T5.5+
  装载编排或 B6 推进；`select_mn_strategy` 返回值语义已锁定勿改枚举
  （B4.2 §7 原始口径；sharded 分片映射侧归 R4.3 partitioner/staging，
  现状接线同样待 T5.5+ 装载编排统一消费）。
- **B4.2 遗留问题 3（观察点，未闭环）**：assign_children 收敛权衡随真库
  装载基准归档继续观察（§7 上一条）。
- 探测→策略与执行编排接线（T5.2 deferred_constraints 快照接入
  `resolve_cycle` 调用方、Constraint/Transaction 执行 NULLABLE_BACKFILL /
  STAGED_LOAD 的回填与关闭/恢复约束）随 T5.5+ 装载编排一并推进
  （本批接口已就位；注意此为 R5 内部衔接项，非 B4.2 遗留问题）。
- **staging_root 生命周期**：生产 staging 目录归 T5.5+ 存储管理
  （R6 lifecycle 清理），延续 B4.3 §7 口径。

---

# R5 后段（T5.5~T5.9）交付续记（2026-11-18，同文档续写）

> 状态：**已完成并通过门禁**
> 依据：实施方案 §8.2（T5.5~T5.9）/§8.4/§8.6/§8.7/§12.4；总方案 §9.2/§9.4/
> §9.5/§9.6/§10.3/§13.1/§13.3；B4.3 交接 §4/§7（staging 协议与检查点锚点）。
> 代码位置：`ptde/adapters/bulkload/fallback.py`、`ptde/adapters/precheck.py`、
> `ptde/loader/{checkpoint,throttle,gate}.py`。

## 8. 交接基线与交付内容（按任务）

- 入口基线：`uv sync --all-extras` 后门禁 **1062 passed, 1 skipped**（与 §1 一致；
  contract 时序型 flaky 用例本轮未触发，全量两跑均绿——§4 第 9 条延续）。
- 交付后门禁：全量 **1158 passed, 1 skipped**（新增 96，零回归）；
  `ruff check ptde/ tests/test_ptde_* tests/test_datagen_*` → **All checks passed**。
- legacy `datagen/` 本体零修改（A1：executor.py 为**收编副本**进 fallback.py，
  原体保留不动）；前段 adapter/staging 模块仅一处最小改动（见 §10 第 1 条）。

### T5.5 降级链 — `ptde/adapters/bulkload/fallback.py`

- 链序（总方案 §10.3，逐档显式判定、留痕）：external KV/装载侧
  （B4.2 遗留问题 1 **接入点预留**，未注册如实记 trace，不伪装）→ 原生
  （前段 BulkLoadAdapter，`ensure_capable`/`BulkLoadUnsupported` 即降级判定
  输入）→ 官方替代（MySQL 服务端 `LOAD DATA INFILE`，**受 secure_file_priv
  白名单约束**——与 LOCAL 通道语义相反，勿混淆 §4 第 5 条）→ 参数化批量
  INSERT（收编 `DbApiBatchInsertLoader`：DEFAULT_BATCH_SIZE=500、逐批 commit、
  行数以 rows 数量为权威、refSkew>50% 热行 batch 调优警告语义保留）；
- **降级只发生在「通道不可用」**：运行时失败（BulkLoadError）原样抛出不静默
  换通道（A3）；链穷尽 → BulkLoadUnsupported 带逐档原因；
- 降级原因可见（门禁 4）：`FallbackOutcome.degradation_trace` 逐档留痕 +
  降级告警随 `BulkLoadResult.precheck_notes` 回传；
- 预算（§10.3）：`LoadBudget.ensure_feasible` 执行前判定——超预算
  `BudgetExceededError` 显式失败；**吞吐校准值必须来自 Pilot Run 实测**
  （`LoadBudget.from_pilot`；无校准返回说明不伪装判定，§9.5 口径）；
- chunk 进度经 `on_batch` 回调外传（供 T5.7 chunk 级检查点）；
- 探测→策略接线（§7 衔接项承接）：`plan_cycle_strategy` 把 Capability 快照
  （`deferred_constraints` 能力位，未知不得视为 True）接入 `resolve_cycle`
  调用方；`staged_load_guard` 为 STAGED_LOAD 提供 Constraint/Transaction
  执行原语（关闭 FK 校验须显式 approved，退出恢复+提交）。
- **pilot_run.py 未改**：`run_generation_pilot` 的 DbApiBatchExecutor 借用点
  （L405）保留 R4 基线口径；切换到 FallbackLoader 随真库装载基准归档一并做
  （见 §13，避免本批混入 A2 基线扰动）。

### T5.6 装载前目标表检查 — `ptde/adapters/precheck.py`

- §9.4 逐项全集（结构化 `PrecheckFinding`，缺项显式失败）：触发器/生成列/
  约束（FK/CHECK/唯一）/分区/LOB·JSON/自增·IDENTITY·Sequence/RLS/
  复制·只读限制——MySQL（information_schema + @@read_only/@@super_read_only）
  与 PG（pg_catalog：attgenerated/attidentity/relrowsecurity/pg_is_in_recovery）
  双实现；前段 precheck_notes 口径为起点，未另起一套；
- 默认保留全部约束（notes 固定声明 §9.4/§19 口径）；RLS/只读/备库 →
  `blocked`，`require_loadable()` 显式拒绝（对齐 T5.4 口径，A3）。

### T5.7 分片检查点与断点恢复 — `ptde/loader/checkpoint.py`

- `DDL_PTDE_SHARD_CHECKPOINT`（§12.4 原文）+ `DbCheckpointStore`
  （ensure_table/upsert `ON DUPLICATE KEY UPDATE` 幂等可重放/get/list）；
- `ShardCheckpoint` 字段对齐 DDL 列；`SubmittedState` 枚举锁死
  （PENDING/PARTIAL/SUBMITTED/CONFIRMED/CLEANED，勿改）；
- **直接消费既有协议（不重复不遗漏，门禁 1/2）**：
  `record_bulkload_result` = staging.mark_loaded（行数复核锚点复用）
  + DB SUBMITTED + `BulkLoadResult.to_snapshot()` 原样入 `db_feedback_json`；
- chunk 编号从 1 起（**chunk_id=0 保留为 shard 级汇总行**——DDL 唯一键
  语义）；多子任务非全文件原子：shard 级 SUBMITTED 但存在未满 chunk →
  恢复计划显式失败（§9.6 末段口径）；
- `plan_recovery`（§9.6 全流程）：读 staging manifest 清单 → actual_rows
  实测回调 → 分类（GENERATE/LOAD/CONFIRM/CLEANUP/OFFSET_CONFIRM）→
  部分提交按 `partial_action` 处置（cleanup 属破坏性操作须过 T5.9 授权）；
  staging/DB 检查点/实际写入量三方不一致 → `require_recoverable()`
  显式失败（不重复不遗漏锚点）。

### T5.8 并发与限流 — `ptde/loader/throttle.py`

- **并发度/批次必须经 Pilot Run 校准**（§9.5 无通用固定值）：
  `ThrottleConfig` 未标 calibrated 配固定值 → 模型层显式失败；
  `calibrate_from_pilot(GenerationPilotReport, batch_rows)`（干跑报告
  显式拒绝）；软限流指标键集锁死 `SOFT_LIMIT_METRICS`
  （database_cpu/io_util/log_growth_mb/lock_waits）；
- `ThrottleGuard`（线程安全、确定性）：acquire/release 并发闸 + observe
  软限流（超限并发减半下限 1、降档不中断、信号可见不吞）。

### T5.9 执行前安全门禁 — `ptde/loader/gate.py`

- §8.4 清单逐项（`GateCheck` 全集）：环境识别（**凭据=Capability Probe
  实测快照**——版本不可得 CapabilityProbeError → 拒绝；报告明示不得仅凭
  连接名/主机名/环境变量名，§13.1）/环境策略（PRODUCTION/UNKNOWN 禁止
  写入；SHARED_TEST 须确认隔离）/Schema 隔离/写入权限（未实测不得视为
  通过）/资源预算（未提供 probe 记「[未执行]」不伪装通过）/外部副作用
  （触发器>0 须确认隔离，默认隔离）/破坏性操作（`DestructiveApproval`
  显式授权 + `AuditLog` 审计，操作域锁死 DDL/TRUNCATE/DROP/CLEANUP）/
  凭据分离（A5：target_dsn ≠ profile_dsn，缺一即拒）；
- `evaluate_write` / `evaluate_cleanup`（生产库禁止清理）；决策
  （allowed/denied）与破坏性授权均入审计；`GateReport.require_passed()`
  强制拒绝语义（门禁 5）。

## 9. 文件清单（R5 后段）

```text
新建（生产代码）
perf-py/ptde/adapters/bulkload/__init__.py    # T5.5 包导出
perf-py/ptde/adapters/bulkload/fallback.py    # T5.5 降级链 + 预算 + 接线
perf-py/ptde/adapters/precheck.py             # T5.6 目标表预检
perf-py/ptde/loader/__init__.py               # T5.7~T5.9 包导出
perf-py/ptde/loader/checkpoint.py             # T5.7
perf-py/ptde/loader/throttle.py               # T5.8
perf-py/ptde/loader/gate.py                   # T5.9

修改（生产代码，最小改动）
perf-py/ptde/adapters/mysql/bulkload.py       # 抽出 _load_statement 钩子
                                              # （供官方替代档子类复用，
                                              #  语义/测试零变化）

新建（测试）
perf-py/tests/test_ptde_adapters_fallback.py    # 24（链序/预算/收编/接线/接入点）
perf-py/tests/test_ptde_adapters_precheck.py    # 13（§9.4 全集 + blocked + 分发）
perf-py/tests/test_ptde_loader_checkpoint.py    # 20（DDL/双向核对/§9.6 分类/原子性）
perf-py/tests/test_ptde_loader_throttle.py      # 13（校准语义/软限流/并发闸）
perf-py/tests/test_ptde_loader_gate.py          # 22（§8.4 逐项 + 审计 + 拒绝）

未改动：datagen/**、ptde/generator/**、ptde/planner/**、ptde/models/**、
ptde/adapters/postgres/**、probe.py、base.py
```

## 10. 踩坑记录（R5 后段，续 §4）

1. **收编=复制语义而非移动**：`DbApiBatchInsertLoader` 是 executor.py 的
   收编副本（A1 分轨本体不动）；但 ptde 侧补了标识符校验
   （validate_identifier）——legacy 无此防线，收编不等于照抄漏洞。
2. **chunk_id=0 的 DDL 唯一键语义**：§12.4 `uk_shard_chunk` 中 chunk_id
   DEFAULT 0 且为唯一键成员——shard 级汇总行天然占用 chunk_id=0，
   **批量子任务 chunk 编号必须从 1 起**，否则 on_batch 回调登记会覆盖
   shard 级记录（测试 `test_record_chunk_progress` 首跑即暴露）。
3. **§9.4 逐项检查要「逐项存在」而非「有则报」**：MySQL 无 RLS、PG 无
   @@read_only——每个产品对 8 个检查项都要产出 finding（不适用 → ok+说明），
   不能按能力静默省略（§10.1 全集口径同源）。
4. **marker 路由包含冲突再现**：`@@read_only` 与 `@@super_read_only`
   天然前缀重叠风险（`@@super_read_only` 不含 `@@read_only` 子串，实测
   安全）；新增路由仍须逐一核对包含关系（§4 第 4 条延续）。
5. **LOCAL 与服务端 INFILE 的 secure_file_priv 语义相反**（§4 第 5 条
   的降级链侧推论）：官方替代档（服务端 INFILE）受白名单约束——客户端
   staging 路径不在白名单目录时 `BulkLoadUnsupported`，降级链会继续落到
   批量 INSERT 档，不会卡死。
6. **pydantic extra=forbid 再次拦截越界字段**：开发中曾往 `BulkLoadResult`
   写 `load_ms`/`last_chunk` 占位（前段模型无此字段）——chunk 进度改为
   on_batch 回调外传（结果模型保持协议 §7.5 形态不变）。
7. **软限流降档的确定性实现**：Python `Semaphore` 不支持运行中收缩——
   降档经「limit 计数 + in_flight 对比」把关新增并发（运行中连接自然
   回落），acquire 达限返回 False 由调用方退避（不无限排队）。
8. **门禁的「未执行」不是「通过」**：资源预算 probe 未提供、写入权限未
   实测、触发器计数缺失——一律 failed + 明示「不得视为通过」
   （§13.3 检查项缺信息时显式暴露，A3）。
9. 既有时序型 flaky 用例延续（§4 第 9 条）：本批全量两跑均绿，未修复
   （不在本批范围）。

## 11. 验收对照（实施方案 §8.6 六条门禁，R5 全部完成后的闭环记录）

| 门禁 | 结果 | 落点 |
|---|---|---|
| 1. 导入结果可核对（逐分片 loaded==expected，全量非抽样） | ✅（fake 层锁死） | 前段 `test_mysql_row_mismatch_raises_with_samples` / `test_pg_rowcount_mismatch_carries_vacuum_hint` + T5.7 `test_record_bulkload_result_row_mismatch_fails`（staging.mark_loaded 复核锚点复用） |
| 2. 失败可安全恢复（注入中断故障，恢复后行数精确一致、无重复） | ✅（恢复协议 fake 层锁死；真库故障注入随真库基准归档） | T5.7：`test_plan_recovery_*`（部分提交/提交丢失/chunk 非原子/三方不一致显式失败） |
| 3. 不静默丢弃错误 | ✅ | 前段（QW4 口径）+ T5.5 运行时失败不换通道 `test_runtime_error_propagates_not_degraded` |
| 4. 降级有说明 | ✅ | T5.5：`test_native_unsupported_degrades_with_visible_reasons`（逐档 trace + 降级告警随结果回传） |
| 5. 门禁生效（生产标识库拒绝+审计；无授权破坏性操作被拒） | ✅ | T5.9：`test_production_write_denied_and_audited` / `test_destructive_without_approval_rejected` / `test_cleanup_production_denied` |
| 6. Probe 快照可审计 | ✅ | 前段 + T5.9 `test_recognition_uses_probe_snapshot_not_names`（快照随门禁报告可审计） |

- **真库装载基准（A2 归档口径）仍未归档**：A2 12.79s 走降级通道；原生通道
  （LOAD DATA/COPY）真库基准需真库环境，记入 §13 B6 未闭环项（§8.6 门禁 1/2
  的真库端到端复核随归档补）。

## 12. 回归保障与回滚（§8.7）

- legacy `datagen/` 零修改；前段仅 `_load_statement` 钩子一处提取（语义等价，
  前段 18 例测试零改动通过）；pilot_run.py 未改（A2 基线口径保留）。
- 回滚：删除 §9 所列 7 个新模块 + 5 个新测试文件，还原 mysql/bulkload.py
  钩子提取即可；其余批次不受影响。
- 双跑对照（§8.7）：原生通道 vs `DbApiBatchInsertLoader` 收编档的同
  DatasetSpec 对照，随真库装载基准归档推进（收编档与 legacy executemany
  语义逐行对齐已由测试锁定）。

## 13. 后续批次衔接（B6，供下一 session 使用）

- **真库装载基准归档（R5 唯一未闭环项）**：`tests/test_ptde_planner_pilot_generation.py::
  test_generation_pilot_full_million_rows` 补真库原生通道基准；归档时同步
  ① pilot_run.py L405 借用点切换到 `FallbackLoader`（收编口径归一）；
  ② B4.2 遗留问题 3（assign_children 收敛权衡）观察点随基准归档继续观察；
  ③ §8.6 门禁 1/2 的真库端到端复核与故障注入。
- **B4.2 遗留问题 1（M:N sharded/external 执行载体）——接入点已预留，
  实现归 B6**：`EXTERNAL_LOADER_REGISTRY`/`register_external_loader` 装配位
  就绪（fallback 链档 0 消费，未注册留痕不伪装）；`select_mn_strategy`
  枚举语义已锁定勿改；sharded 侧与 staging/partitioner 的接线同样归 B6
  装载编排统一消费。未实现部分如实记入 B6 交接未闭环项。
- **探测→策略与执行编排接线（R5 内部衔接项，部分承接）**：
  `plan_cycle_strategy`（Capability 快照 → resolve_cycle 调用方）与
  `staged_load_guard`（STAGED_LOAD 的 Constraint/Transaction 执行原语）
  已就位；**尚缺**：依赖序装载编排器（按 CycleResolution.steps 消费
  NULLABLE_BACKFILL/STAGED_LOAD 全流程的多表编排 + 回填）归 B6
  dataset_manager/装载编排。
- **T5.7 检查点接入装载主流程**：checkpoint 协议已就绪（staging manifest ↔
  DB 双向核对 + §9.6 恢复分类），装载编排落位后接入
  `record_generation/record_bulkload_result/plan_recovery`；cleanup 动作
  执行须过 T5.9 `evaluate_cleanup` 授权（破坏性操作）。
- **T5.8 校准闭环**：`calibrate_from_pilot` 已就位；真库基准归档后把实测
  workers/batch/吞吐回填 ThrottleConfig 与 LoadBudget（source=pilot:*），
  软限流阈值随目标库实测确定（无通用固定值）。
- **R6 承接**：ExecutionManifest.shard_summaries 回填（staging.totals() 形态
  已对齐）；staging_root 生命周期归 R6 lifecycle 清理（B4.3 §7 口径延续）。
- **既有时序型 flaky 用例（§4 第 9 条）**：建议 B6 给 contract
  `test_async_import_thread_pool_and_failure_status` 延长轮询上限或改事件等待。

