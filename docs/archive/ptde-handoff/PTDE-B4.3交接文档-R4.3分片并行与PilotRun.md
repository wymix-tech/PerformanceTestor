# PTDE B4.3 批次交接文档 — R4.3 分片并行 + Pilot Run（T4.8~T4.12）

> 状态：**已完成并通过门禁**（2026-11-18）
> 依据：`PTDE-实施方案-R0-R7分批执行计划.md` §7.2（R4.3 任务表）、§7.4/§7.5（格式与协议草案）、§7.6（门禁 3/4）、§7.7（回归保障）；
> `性能测试平台-智能测试数据引擎-完整技术方案.md` §7.6/§7.7/§8.1~§8.4；
> 代码位置：`perf-py/ptde/generator/`（R4.3 五个新模块）、`perf-py/ptde/planner/pilot_run.py`（扩展）
> 前序交接：`PTDE-B4.1交接文档-R4.1分布采样器.md` §4 踩坑记录与 §7 衔接清单延续有效

---

## 1. 交接基线与结果

- 入口基线：`uv sync --all-extras` 后门禁 **961 passed**（与交接要求一致）。
- 交付后门禁：
  - `uv run pytest tests/contract tests/test_datagen_* tests/test_ptde_* -q` → **1022 passed, 1 skipped**（新增 61 + 1 可选 skip，零回归）；
  - `uv run ruff check ptde/ tests/test_ptde_* tests/test_datagen_*` → **All checks passed**。
- 本批次新增均为 `ptde/` 内纯新增模块 + 新测试文件；**未触碰** legacy `datagen/` 生成路径（A1 分轨延续；`datagen.executor.DbApiBatchExecutor` 仅借用未改动）。

## 2. 交付内容（按任务）

### 遗留问题 2 — `cycle_policy.project_fk_edges`（B4.2 遗留，顺带处理）

- Schema 快照 → 环内 `CycleFkEdge` 投影：child_table ∈ 环成员的声明式 FK 按
  `child_columns` 逐列展开；可空性取 `ColumnSchema.nullable`。
- 环成员不在快照 → 不产边（交由 `resolve_cycle` 按「信息不足」显式失败）；
  表在快照但 FK 子列缺失 → `ValueError`（快照自相矛盾，显式失败）。
- 端到端测试：快照投影 → `resolve_cycle` 判定可空回填（投影已接入主链路）。

### T4.8 分片规划器 — `ptde/generator/partitioner.py`

- `compute_rows_per_shard`：§8.4 指标动态调节（Worker 均衡 / 暂存空间 /
  导入吞吐 / 倾斜度），约束取交集后钳制 `[MIN, MAX]`，配对参数只给其一时显式失败。
- `plan_shards`：planned_range 实体经 `keys.IdRangeAllocator` 三重检查打包区间
  （保留区间冲突显式失败）；hash_bucket 实体用 `hash_bucket`（sha256，**禁用内置 hash**——
  跨进程可复现延续 seed.py 口径）；`rows` 逐分片精确回填（含末片余数）。
- `build_sharding_plan(plan, ...)`：`GenerationPlan` → `ShardingPlan` 便捷入口；
  `models.spec.ShardPlanDef` 增加 `rows` 字段（R4.3 细化，旧字段兼容）。

### T4.9 文件中转 — `ptde/generator/file_format.py` + `staging.py`

- 格式要素全集（§7.4）：UTF-8 文件头声明、RFC4180 转义、`NULL=\N` 与空串区分、
  datetime UTC ISO、Decimal 字符串化保精度、bytes Base64（`b64:` 前缀）、
  sha256 + 行数校验和、`format_version=csv_v1`。
- **踩坑修正**：行计数按 csv 记录数（引号字段内嵌换行按物理行会算错，已用例锁死）；
  写入侧 sha256 流式计算（不在内存攒全文件）；`verify_shard_file` 复核行数+校验和。
- `StagingArea`：状态机 pending→generated→loaded（对齐 ShardSummary）；
  manifest.json 原子落盘（tmp + os.replace）、`restore()` 断点恢复（版本不符显式失败）、
  `verify_complete()` 终态校验；装载行数 ≠ 生成行数显式失败（数量复核锚点）。

### T4.10 流式直传 — `ptde/generator/stream.py`

- `BoundedRowStream`：有界队列 + 生产端线程；背压 = put 阻塞（不丢不覆盖）；
  行序一致；生产端异常穿透到消费端重新抛出（哨兵行处重抛）。
- cancel 语义：生产端下次 put 前停止，缓冲内行仍可取完（`pushed == drained`）；
  `pump_into` sink 异常 → cancel + 排空缓冲再上抛（不遗留阻塞线程）。
- 与文件模式共用分片元数据 / 格式定义 / 检查点协议（§8.2）。

### T4.11 多进程 Worker — `ptde/generator/worker.py`

- `ShardTask`/`ShardResult`：协议 §7.5 JSON 原生（`to_dict/from_dict` 往返 +
  `protocol_version` 校验显式失败）；
- `plan_row_factory`（模块级可 pickle）：主键自预分配区间顺序生成；hash 策略主键
  `derive_seed` 派生；每字段独立 rng（seed.py 派生）逐行复用——同任务跨进程逐行一致；
- `run_shards(workers>1)` 走 ProcessPoolExecutor：Worker 只收 JSON 原生任务、
  **无 DB 连接、无共享可变状态**；行工厂必须可按名称还原（lambda/闭包显式失败）；
- **多进程 + staging：状态迁移在父进程合并**（子进程只回传结果 dict，
  避免 manifest 并发写竞争）；stream 模式仅 workers=1 支持（跨进程显式失败）。
- 验收核心用例：`test_run_shards_multiprocess_reproducible_across_workers`
  （workers=1 与 workers=4 全部分片 checksum 一致）。

### T4.12 A2 生成 Pilot — `ptde/planner/pilot_run.py` 扩展

- `run_generation_pilot(conn_factory, config)`：分片规划 → 多进程生成 → staging →
  DbApiBatchExecutor 降级装载（§7.3：executor.py R4 借用，R5 归 Loader）；
- 门禁判定：`within_budget` = 时长 < 30 分钟 **且** tracemalloc 峰值 < max_memory
  （默认 8GB）；不达标 → notes 追加 A2 决策反转评估提示；
- conn_factory=None 干跑路径：rows_loaded=None + notes 显式说明（不伪装已装载）；
- **A2 实测**：`PTDE_PILOT_FULL=1 pytest tests/test_ptde_planner_pilot_generation.py::
  test_generation_pilot_full_million_rows` → 百万级（4 Worker、4 分片）端到端
  **12.79s**（生成→staging→降级装载，FakeConn 行数复核 rows_verified），
  远低于 30 分钟预算（门禁 4 达标；真库装载基准随 R5 归档）。

## 3. 文件清单

```text
新建（生产代码）
perf-py/ptde/generator/partitioner.py        # T4.8
perf-py/ptde/generator/file_format.py        # T4.9
perf-py/ptde/generator/staging.py            # T4.9
perf-py/ptde/generator/stream.py             # T4.10
perf-py/ptde/generator/worker.py             # T4.11

修改（生产代码）
perf-py/ptde/generator/cycle_policy.py       # 遗留问题 2：project_fk_edges
perf-py/ptde/generator/__init__.py           # 导出 R4.3 模块 + docstring
perf-py/ptde/models/spec.py                  # ShardPlanDef.rows 字段
perf-py/ptde/planner/pilot_run.py            # T4.12 扩展

新建（测试）
perf-py/tests/test_ptde_generator_partitioner.py        # 14
perf-py/tests/test_ptde_generator_staging.py            # 12
perf-py/tests/test_ptde_generator_stream.py             # 8
perf-py/tests/test_ptde_generator_worker.py             # 17
perf-py/tests/test_ptde_planner_pilot_generation.py     # 5 + 1 skip

修改（测试）
perf-py/tests/test_ptde_generator_cycle_policy.py       # +5（投影）

未改动：datagen/**、ptde_common/**、sql/init.sql、server/**、perf_cli/**
```

## 4. 关键设计决策与踩坑记录（承接 B4.1 §4）

1. **行计数必须按 csv 记录数**：RFC4180 引号字段可内嵌换行，物理行计数会多算
   （写入侧 `write_shard_file` 与复核侧 `verify_shard_file` 统一走 csv.reader）。
2. **多进程 staging 竞争**：子进程不写 manifest，结果回传父进程统一
   register → mark_generated → checkpoint（一次落盘）。
3. **校验和接入写路径**：写入时流式 update hasher，勿写完重读（大文件省一遍 IO）；
   复核侧用原始字节重算比对。
4. **stream + 多进程显式失败**：流对象不可 pickle 跨进程传递——改 file 模式，
   不静默降级（A3）。
5. **行工厂多进程传输**：要求模块级函数且可按 `module:name` 还原；
   lambda/闭包/不可还原对象显式失败。
6. **背压边界断言**：停止消费后 `pushed - drained ≤ buffer_size`（确定性断言，
   不依赖 sleep 时序——B4.1 §4 第 5 条样本量/容差教训延续）。
7. **pilot 干跑路径**：conn_factory=None 时 rows_loaded=None + notes 说明，
   不伪装已装载；dry-run 的 within_budget 仅含生成段时长。
8. B4.1 已知坑延续有效：conditional None 哨兵（`key in branches`）、uniform 整数域
   randint 口径、null_ratio 消耗一次 random、pydantic `extra="forbid"`（本次
   GenerationPilotReport 漏声明 collected_at 即被其拦下）、指纹排除时间戳、
   `zip(strict=)`、ruff line-length=100（E501 ignore）。

## 5. 验收对照（实施方案 §7.6 属 R4.3 的条目）

| 条目 | 结果 | 落点 |
|---|---|---|
| 1. 可复现（逐行） | ✅ 分片级：跨进程/跨 Worker checksum 一致 + Pilot 两次运行逐片一致 | `test_ptde_generator_worker.py` / `test_generation_pilot_reproducible_across_runs` |
| 3. 内存可控 | ✅ 流式逐行 + 分片级控制；Pilot 报告 peak_memory_bytes ≤ 8GB 预算 | `stream.py` / `test_generation_pilot_end_to_end_with_fake_conn` |
| 4. Pilot Run（A2） | ✅ 百万级端到端 12.79s < 30 分钟（降级装载通道） | `test_generation_pilot_full_million_rows`（PTDE_PILOT_FULL=1） |
| 门禁 1 端到端逐行 | ✅ 逻辑数据一致 = 同 seed 逐分片文件 sha256 逐字节一致 | 同上两测试 |
| 门禁 6 legacy 对照 | ⏳ legacy 管线全程未动；同 DatasetSpec 双跑行数/关联/分布对照随 R5 装载通道落地后推进 | — |

## 6. 回归保障与回滚（§7.7）

- R4.3 Worker/staging 全新代码，legacy 生成路径未触碰；回滚仅删除
  5 个新模块 + 5 个新测试文件，并还原 `cycle_policy.py` / `spec.py` /
  `pilot_run.py` / `__init__.py` 四处小改动即可。
- R4 遗留风险已清零：`executor.py` R5 移 Loader 侧（§7.3 计划不变）。

## 7. 后续批次衔接（R5，供下一 session 使用）

- **B5（R5 批量装载与安全门禁）**：T5.1 Adapter 分层接口（前置 R4.2 已满足）→
  T5.2 Capability Probe → T5.3 MySQL `LOAD DATA LOCAL INFILE`（QW4 PoC 结论参照）→
  T5.4 PG `COPY FROM STDIN`；staging manifest 检查点协议 + ShardSummary.status
  （pending/generated/loaded）为 Loader 侧恢复与核对锚点，直接消费。
- 本批次 staging 目录仅按需创建于 `tmp_path`（测试）或 `/tmp/ptde_pilot_*`
  （A2 全量基准）；生产 staging_root 归 R5 存储生命周期管理（R6 lifecycle 清理）。
- 遗留问题 1/3 按 B4.2 交接口径随 R5 推进。
- 文档缺口：`PTDE-B4.2交接文档-R4.2关系生成器泛型化.md` 至今未落盘
  （R4.2 交付状态见本文件 §1 基线与 `ptde/generator/__init__.py` docstring），
  建议补写归档。
