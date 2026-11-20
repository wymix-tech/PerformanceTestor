# PTDE B7 续批次 7 交接文档 — 真库执行体接线 + R5 装载基准 + 门禁 2 双跑对照（单表版）

> 状态：**已完成并通过门禁**（2026-11-19）
> 依据：续批次 6 交接 §7「剩余未闭环项收敛到真库/切换批次」；
> 实施方案 §10.2（A2 真库批次）/ §13.1（门禁 2）/ §10.3（装载链）；
> 代码位置：`server/offline_pipeline.py`、`server/realdb_pipeline.py`（新）、
> `tests/test_server_realdb_pipeline.py`（新）、`tests/test_ptde_realdb_benchmark.py`（新）、
> `tests/test_ptde_legacy_ptde_crosscheck.py`（新）、`perf-py/benchmarks/`（归档）；
> 前序交接：B7 前段 + 续批次 1~6 全部有效。

---

## 1. 交接基线与结果

- 入口基线：续批次 6 交付后口径（perf-py 1711 passed, 2 skipped；
  perf-web vitest 44 passed, build 通过）；
- **真库环境就位**（本批前置条件建立）：docker `mysql:8.0`（容器
  `ptde-mysql`，库 `performance_testor`，用户 `perf/perf_pw` 全权）+
  `sql/init.sql` 全量建表（29 表，`ptde_*` 六表就绪）+ 服务端
  `local_infile=ON`；元数据库（server 自己的 sqlite/MySQL）与目标数据库
  （灌数真库）按 DSN 分离；
- 交付后门禁：
  - perf-py 全库 `uv run --extra server --extra locust pytest tests/` →
    **1723 passed, 5 skipped, 0 failed**（基线 1711 + 本批 12 通过；
    5 skipped = 既有 2 + 3 个真库门控测试在未设 `PTDE_REALDB_URL` 时
    跳过——设置后全绿，见 §5）；
  - `uv run ruff check ptde/ perf_cli/ server/ tests/` → All checks passed；
  - perf-web `npm run build`（vue-tsc + vite）→ 通过（零改动基线确认）；
  - perf-web `npx vitest run` → 5 文件 44 passed（零改动基线确认）；
- 本批为 **A2 真库批次第一段**：真库执行体接线 + 原生装载基准 +
  门禁 2 双跑对照（单表版）；`ptde/**` 零修改（仅消费）；
  `/api/datagen` 契约与 golden 零触碰；T7.6/T7.7 未切换（见 §7 剩余前置）。

## 2. 交付内容

### 2.1 执行体真库通道（`server/offline_pipeline.py` + 新 `server/realdb_pipeline.py`）

- **`OfflinePipeline` 三处最小扩展（向后兼容，缺省行为不变）**：
  - `loader_chain` 注入缝：缺省 None = 既有单降级档
    DbApiBatchInsertLoader；真库通道注入 §10.3 原生链
    `[MysqlBulkLoadAdapter(load_data_local), MysqlServerInfileAdapter(
    load_data_server), DbApiBatchInsertLoader(batch_size)]`——原生档
    `ensure_capable` 逐档探测，不可用留痕降下一档（`@@local_infile` OFF /
    `secure_file_priv` 不满足 → 显式降级，不静默吞差异）；
  - **装载段取消安全点**（续批次 3 §4 决策 3 协议在装载段落地）：
    `load` 入口 + 逐实体步骤边界 `_check_cancel`（orchestrator 按实体步进
    调用 + LoadReport 逐段合并，返回形状不变）；已装载实体经 DB 检查点
    （DbCheckpointStore）保留，协作退出不静默丢弃；
  - **缺陷修正（能力快照崩溃）**：`_build_manifest` 逐档
    `ensure_capable(conn)` 原对不可用档直接抛 `BulkLoadUnsupported`——
    真库通道下 load 段合法降级后 verify 换新 conn 复查原生档必然复抛
    → 整次构建 FAILED。修正为 try/except 如实记录
    `"load_data_local: unsupported（原因）"`（快照如实、不伪装、不崩溃）。
- **新 `server/realdb_pipeline.py`（MySQL 通道装配，A2 交付物）**：
  - `parse_mysql_dsn` / `mysql_conn_factory`：`mysql://` 与
    `mysql+pymysql://` DSN 解析（方言/形态显式校验）→ pymysql 连接
    （`local_infile=True` 原生档客户端前提、`charset=utf8mb4`、
    `autocommit=False`）；DSN 缺省读环境变量 **`PTDE_REALDB_URL`**
    （不复用 `PERF_PY_DATABASE_URL`——server 元数据库与灌数目标库分离）；
  - **A2 最小版映射产出器** `entity_mappings_from_fields`：FieldSpec
    （JSON 原生 dict 亦可，distribution dict 归一化 DistributionFit）→
    `EntityMapping`（MySQL DDL 类型映射表 `_MYSQL_TYPES`，未知类型显式
    失败防口径漂移；`id` 字段落 NOT NULL PRIMARY KEY——planned_range
    主键区间语义；`ENGINE=InnoDB CHARSET=utf8mb4`）；
  - `build_realdb_pipeline`：装配入口（宿主 `register_builtin_adapters()`
    显式注册、检查点仓 `DbCheckpointStore`、链序组装、`conn_factory`
    可显式注入供测试）；`degradable_layers` 由装配方对
    `DatasetLifecycleService` 显式配置（§9.4 口径，真库 db_ready 统计
    新鲜度 WARN 是否放行由环境口径决定）；
- **依赖**：`pyproject.toml` server 组补 `pymysql>=1.1,<2`（A2 起
  `--extra server` 即自带 MySQL 驱动，生产 `PERF_PY_DATABASE_URL` 口径
  mysql+pymysql 不再依赖 `--extra datagen-drivers`）。

### 2.2 R5 真库装载基准（B5 §5 ⏳ 项闭合）

- `tests/test_ptde_realdb_benchmark.py`（`PTDE_REALDB_URL` 门控）：
  - 冒烟：全链 build → READY；count 层全量核对（真库 COUNT(*)=目标行数）；
    **原生档生效断言**（`ptde_shard_checkpoint.db_feedback_json` 的
    strategy == load_data_local，非降级批量 INSERT）；
  - 基准：`PTDE_REALDB_BENCH_ROWS`（默认百万级）/`_BENCH_WORKERS`（默认 4）
    → generate/load 分段计时，报告归档
    `perf-py/benchmarks/realdb_load_benchmark.json`：
    **百万级 4 分片 4 worker：generate 1151ms + load 2561ms = 3712ms，
    装载吞吐 ~39 万行/s**（A2 降级通道参照 12790ms，原生档 ~3.4×）。

### 2.3 门禁 2 双跑对照（实施方案 §13.1，单表版）

- `tests/test_ptde_legacy_ptde_crosscheck.py`（门控）：
  同规格同目标库双跑（`ptde_xcheck_orders`，默认 1 万行）——legacy
  `DefaultDataGenEngine`（MysqlDialect + DbApiBatchExecutor，seq 主键 +
  range 均匀 amount + enum 加权 status，seed=7）vs PTDE 真库通道
  （planned_range 主键 + uniform + weighted_categorical，seed=7）：
  - 行数精确一致 ✅（两侧均 10000）；
  - 分布统计容差内 ✅（status 频率 vs 权重绝对容差 0.03；amount 均值
    ±5%）；主键区间一致 ✅（两侧均 [1, N]）；
  - **关联关系对照 N/A（如实标注，不伪装通过）**——PTDE verify 关系层
    当前显式空集（关系映射随真库 Schema 批次接入），T7.6/T7.7 的门禁 2
    完整闭合以此为准（§7）；
  - 报告归档 `perf-py/benchmarks/gate2_crosscheck_report.json`。

### 2.4 真库通道协议测试（不连真库，FakeConn）

- `tests/test_server_realdb_pipeline.py`（12 例）：DSN 解析校验 /
  映射产出器（DDL 形态/未知类型显式失败）/ 装配断言（product/链序/
  校准批次/DbCheckpointStore）/ **协议级端到端**（FakeConn `%s`
  paramstyle + 原生档不可用 → 降级链逐档探测 → 批量 INSERT 档 → READY，
  行数记账核对 + 能力快照全链在册 + unsupported 如实记录）/
  装载段取消安全点（入口 + 实体步骤间，已装载实体保留）。

## 3. 文件清单

```text
新建（生产代码）
perf-py/server/realdb_pipeline.py          # MySQL 真库通道装配（DSN/conn 工厂 +
                                           #  A2 最小映射产出器 + 原生链组装）

修改（生产代码）
perf-py/server/offline_pipeline.py         # loader_chain 注入缝 + load 段取消
                                           #  安全点 + 能力快照容错修正
perf-py/pyproject.toml                     # server 组补 pymysql

新建（测试，perf-py +15）
perf-py/tests/test_server_realdb_pipeline.py        # 12（协议级，FakeConn）
perf-py/tests/test_ptde_realdb_benchmark.py         # 2（真库门控：冒烟+基准）
perf-py/tests/test_ptde_legacy_ptde_crosscheck.py   # 1（真库门控：双跑对照）

归档
perf-py/benchmarks/realdb_load_benchmark.json       # R5 原生装载基准
perf-py/benchmarks/gate2_crosscheck_report.json     # 门禁 2 双跑对照报告

未改动：ptde/** 全部、datagen/**、/api/datagen 契约与 golden、
perf-web/**、sql/init.sql、CLI（datagen_runner/ptde_runner）
```

## 4. 关键设计决策与踩坑记录

1. **元数据库与目标库 DSN 分离**：`PTDE_REALDB_URL`（灌数目标库）≠
   `PERF_PY_DATABASE_URL`（server 元数据库）——两库职责/生命周期/权限
   不同，混用会让灌数 DDL 写进元数据库。
2. **装载安全点落在实体步骤边界而非批次回调**：`on_batch → record_chunk`
   仅 DbApiBatchInsertLoader 档触发（原生 LOAD DATA 整文件原子），且
   orchestrator.run 无取消缝——按实体步进调用 run + 报告合并是零侵入
   ptde/** 的最小落点（续批次 3 协议口径：段落/步骤边界协作退出）。
3. **能力快照容错修正（本轮踩坑）**：降级链语义（探测-降级）与能力快照
   采集（逐档 ensure_capable）在真库通道组合下冲突——load 段合法降级后，
   verify 必然在原生档 ensure_capable 复抛。修正原则：快照如实记录
   unsupported 原因（与降级留痕同口径），既不伪装可用也不崩溃。
4. **A2 映射产出器最小版边界**：本版覆盖 `fields_by_entity` 显式注入
   （类型映射表显式收口）；完整 Schema 快照 → 映射（索引/分区/环组/
   关系）仍随 A2 后续扩展——映射缺失/不一致沿用 A3 显式失败口径。
5. **门禁 2 单表版的诚实口径**：行数/分布/主键区间三项实测通过并归档；
   关联关系对照 N/A（PTDE 关系层显式空集，非「未测当作通过」）——
   T7.6/T7.7 硬前置的完整闭合以此项为准。
6. **踩坑：distribution dict 归一化**——FieldSpec 为 dataclass，
   JSON 原生 dict 直接 `FieldSpec(**f)` 会让 distribution 留存 dict，
   采样器运行时才炸（`'dict' has no attribute 'type'`）；映射产出器
   统一在入口归一化 `DistributionFit.model_validate`。
7. **踩坑：repo 必须同源共享**——pipeline 与 DatasetLifecycleService
   各建 repo（各自 sqlite 内存库）时，Definition 对 pipeline 不可见
   （`find_definition` None → 显式失败）；装配口径：同一 repo 实例
   注入两侧（测试首轮即踩）。

## 5. 验收对照

| 验收项 | 结果 | 落点 |
|---|---|---|
| 真库通道协议端到端（含降级） | ✅ | `test_realdb_channel_protocol_reaches_ready_with_degradation`（FakeConn 原生档不可用 → 批量 INSERT 档 → READY） |
| 装载段取消安全点 | ✅ | `test_realdb_channel_load_cancel_at_entry` / `..._between_entities_keeps_loaded_rows`（已装载实体保留） |
| 原生 LOAD DATA 真库端到端 | ✅ | `test_realdb_channel_native_loaddata_reaches_ready`（真库 READY + strategy=load_data_local + COUNT 核对） |
| R5 真库装载基准归档 | ✅ | `test_realdb_load_benchmark_native_channel` → `benchmarks/realdb_load_benchmark.json`（百万级 3.71s，原生档） |
| 门禁 2 双跑对照（单表版） | ✅（关系项 N/A 如实标注） | `test_gate2_crosscheck_legacy_vs_ptde` → `benchmarks/gate2_crosscheck_report.json` |
| 装载链可注入且缺省行为不变 | ✅ | `test_build_realdb_pipeline_assembles_mysql_channel` + 既有 offline 全部测试零改动通过 |
| 门禁 | ✅ | perf-py 1723 passed, 5 skipped（3 个真库门控 skip）；ruff 全绿；perf-web build + vitest 44 passed |

## 6. 回归保障与回滚（§10.4）

- **契约零改动**：`/api/datagen` 13 端点 + golden、stress 绑定、CLI 面
  全部未触碰；`ptde/**` 零修改；`OfflinePipeline` 三处扩展缺省路径
  行为不变（既有 offline/dataset_api 测试零改动通过）；
- 回滚：删除 `realdb_pipeline.py` + 3 个新测试文件 + benchmarks 归档，
  还原 offline_pipeline 三段 + pyproject pymysql 行即可；真库侧仅目标表
  数据（DROP 可重建），无破坏性变更。

## 7. 后续批次衔接（供下一 session 使用）

- **本批已闭环**：真库执行体接线（MySQL 通道 + 原生装载链 + DbCheckpointStore
  + 装载段取消安全点）；R5 真库装载基准（原生档归档）；门禁 2 单表版
  （行数/分布/主键区间三项）；
- **T7.6/T7.7 接管切换的剩余前置**（按依赖序）：
  1. **PTDE 关系映射接线**——verify 关系层从显式空集到实映射（依赖
     DatasetSpec/Definition 关系语义细化与 A2 映射产出器扩展）；
  2. **门禁 2 关系对照补齐**（ref 外键有效率对照 → 报告更新，单表版
     报告已留 relationship_check=N/A 位）；
  3. **PTDE 规则语义对齐 legacy FieldRule**（seq 前缀串/orphan_rate/
     refSkew 热行/over_threshold 等接管语义；A1 legacy 钳制随接管归一）；
  - 完成后按 §13.2 步骤 3/4 切换（`perf_cli/datagen_runner.py` 路由 +
    `server/datagen_api.py` 实现替换，端点/形状不变，契约全绿）；
- **selfCheck probe 注入缝已就位**：`build_realdb_pipeline(endpoint_probe=)`
  ← 装配方传 `datagen_service.self_check_probe(repo_id)`（部署入口接线时
  闭合，OfflinePipeline verify 已透传）；
- **API 超时控制**：随切换批次编排层一并落（口径不变）；
- **T7.9 legacy 整包退役**：切换 + 观察期后（迁移脚本续批次 4 已就位）；
- 真库环境复现口径：docker `mysql:8.0`（`--local-infile=1`）+
  `sql/init.sql` + `GRANT ALL ON performance_testor.* TO 'perf'@'%'` +
  `PTDE_REALDB_URL=mysql://perf:perf_pw@127.0.0.1:3306/performance_testor`。

---

*B7 续批次 7 交接完成：A2 真库批次第一段——执行体 MySQL 通道（原生
LOAD DATA 链 + DbCheckpointStore + 装载段取消安全点）、R5 百万级装载
基准归档（3.71s，原生档 ~3.4× 降级通道）、门禁 2 双跑对照单表版；
T7.6/T7.7 剩余前置收敛为关系映射接线 + 规则语义对齐，随后切换。*
