# PTDE B7 续批次 10 交接文档 — 接管切换 T7.6/T7.7（§13.2 步骤 3/4）

> 状态：**已完成并通过门禁**（2026-11-19）
> 依据：续批次 9 交接 §7「下一批 = 切换」——`perf_cli/datagen_runner.py`
> 路由 + `/api/datagen` 服务端实现替换（端点/形状不变，契约全绿）；
> 代码位置：`perf_cli/ptde_datagen_engine.py`（新）、
> `perf_cli/datagen_runner.py`、`perf_cli/main.py`、
> `server/datagen_service.py`、`server/offline_pipeline.py`、
> `server/realdb_pipeline.py`、`ptde/generator/worker.py`、
> `tests/test_perf_cli_ptde_datagen_engine.py`（新）、
> `tests/test_datagen_runner.py`；
> 前序交接：B7 前段 + 续批次 1~9 全部有效。

---

## 1. 交接基线与结果

- 入口基线：续批次 9 交付后口径（perf-py 1771 passed, 5 skipped；
  ruff 全绿）；
- 交付后门禁：
  - perf-py 全库 `uv run --extra server --extra locust pytest tests/` →
    **1787 passed, 5 skipped, 0 failed**（基线 1771 + 本批 16：PTDE 路由
    引擎协议级 15 + 路由入口 1；5 skipped = 既有口径不变，真库门控测试
    未设 `PTDE_REALDB_URL` 时跳过，crosscheck 未改动随真库复跑核对）；
  - `uv run ruff check ptde/ perf_cli/ server/ tests/ datagen/` →
    All checks passed。

## 2. 交付内容

### 2.1 PTDE 路由灌数引擎（`perf_cli/ptde_datagen_engine.py`，新）

legacy 灌数入口的 PTDE 执行体，宿主契约 `execute(spec, listener) →
EngineResult` 不变。语义映射（逐项对齐 legacy `DefaultDataGenEngine`）：

- **方言路由**：`jdbc:mysql://` 前缀（`jdbc_url_to_dsn` 转 mysql:// DSN；
  非 MySQL 前缀——含 oracle/postgreSQL——沿用 legacy「未注册匹配方言」
  文案显式失败，契约测试断言该子串；PTDE 真库通道 MySQL-first）；
- **表元数据**：information_schema 内省（列名/类型/可空/主键/自增，
  ORDINAL_POSITION 序 = 装载列序）；缺失表 → legacy 同文案
  「表不存在或无可见列」；未知列类型显式失败；
- **自增列剔除**：不进字段清单（DB 自动分配；legacy insert_columns
  同口径）；
- **id 主键区间追加语义**：单整型主键（自增任意名 / 非自增限 `id` 名）
  `SELECT MAX(pk)` → `existing_max` 注入 `IdRangeAllocator`（T4.5 既有
  数据检查接线）——追加灌数自存量最大值 +1 续区间；
- **规则翻译**：`server.rule_translator.translate_fields`（orphan_rate
  缺省链：表级 → 全局级 → 0；`nullable_columns` 由内省注入）；
- **ref 语义双路**：父表在任务清单 → 声明关系四元组（db_fk；
  orphan_rate>0 降级 logical），子列外键零孤儿由 planned span 注入；
  父表不在清单（存量父表）→ **不声明关系**（存量父表不重建），子列 ref
  分布以父表存量 ID 域（MIN/MAX）显式给出（域稠密时与 legacy 池采样
  统计等价）；父表缺失/空表 → 显式失败（legacy 兜底随机数为伪装可用
  路径，按 A3 拒绝）；
- **seed**：spec.seed 缺省 → 随机分配并落 `definition.seed`（可追溯
  复现，非不可复现伪装路径）；
- **dry-run**：完整内省/翻译/装配，不 generate/load；
  `estimated_max_txn_ms` 沿用 legacy 经验公式（每批 5ms）；
- **正式执行**：definition/instance 落元数据库（`PERF_PY_DATABASE_URL`，
  ptde_* 四表幂等建齐）→ `build_realdb_pipeline` → generate → load；
  staging 分片文件装载成功后尽力清理（检查点行留目标库审计）；
- **A1 归一**：legacy `max_rows` 钳制随接管移除（上限由入口校验层承担）；
- 测试/装配方注入缝：`conn_factory`（FakeConn 协议级验证）、`repo`、
  `metadata_url`。

### 2.2 路由接线（T7.6 CLI + T7.7 API）

- `perf_cli/datagen_runner.py`：**`build_default_engine()` 生产路由返回
  `PtdeDatagenEngine`**——CLI `perf datagen run`（`main.py` 内延迟导入
  该名，monkeypatch seam 保留）与服务层 `_build_engine`（同名导入）自动
  走 PTDE，命令形状/退出码/`engine=` 注入缝零改动；legacy 装配整段保留
  于 **`build_legacy_engine()`**（A1 分轨，供回退/双跑对照，T7.9 随
  datagen 包退役清理）；
- `server/datagen_service.py`：仅 docstring 更新（`_run_engine` 的 spec
  装配/listener/detailJson/verifyMin/selfCheck/水位/清理全链不动——
  语义提示 on_hints 恒空 → 建议面板不再由灌数任务新增，存量提示与
  adopt 流不受影响）；
- `perf_cli/main.py`：`datagen run` 帮助文本补 PTDE 路由说明
  （`--source-dir` 在 PTDE 路由不进行源码语义扫描，引擎侧留 WARN）。

### 2.3 管线缝扩展（切换适配所需，纯增量）

- `server/offline_pipeline.py`：ctor 增参 `existing_max`（缺省 None =
  空表构建口径 [1, N]，既有行为零改动）→ `_sharding_of` 传入
  `build_sharding_plan`（T4.5 三重检查既有数据项落地）；
- `server/realdb_pipeline.py`：`build_realdb_pipeline` 增参
  `existing_max` 透传；`_MYSQL_TYPES` 扩展（TINYINT/TIME/BIT/YEAR/JSON/
  BLOB/VARBINARY/ENUM/SET——真实存量表的列类型覆盖）；
- `ptde/generator/worker.py`：`plan_row_factory` 向 `sample_distribution`
  注入 `{"row_index": row_index}` 上下文（一行）——sequential 等
  行号敏感分布此前恒取起始值（恒值缺陷），现按分片内行号推进；
  其余采样器不读 row_index，既有测试零改动通过。

### 2.4 协议级测试（+16 例）

- `tests/test_perf_cli_ptde_datagen_engine.py`（新，15）：方言路由失败
  （oracle「未注册匹配方言」/ postgres「仅支持 jdbc:mysql://」）/ 非法
  表名与空表组 / 表缺失 / dry-run 估算与不写库 / 追加区间
  （MAX=100 → id 101~110）/ 自增列剔除 / 任务内父表关系声明 + 零孤儿
  span / 存量父表 ID 域采样（不声明关系）/ 存量父表缺失显式失败 /
  orphan_rate 缺省链 → logical 降级 + 孤儿值落地 / 非 id 列 seq 逐值
  推进（U-1~U-5）/ seed 随机分配落库 / 0 行表跳过；
- `tests/test_datagen_runner.py`（+1 −0）：`test_build_default_engine_
  routes_to_ptde`（路由断言）+ legacy 装配/方言路由两例改指
  `build_legacy_engine`（原断言逐字保留）。

## 3. 文件清单

```text
新增（生产代码）
perf-py/perf_cli/ptde_datagen_engine.py             # PTDE 路由灌数引擎
                                                   #  （宿主契约 execute 不变）

修改（生产代码）
perf-py/perf_cli/datagen_runner.py                 # build_default_engine 路由
                                                   #  + build_legacy_engine 保留
perf-py/perf_cli/main.py                           # datagen run 帮助文本
perf-py/server/datagen_service.py                  # docstring（切换说明）
perf-py/server/offline_pipeline.py                 # existing_max 缝
perf-py/server/realdb_pipeline.py                  # existing_max 透传
                                                   #  + _MYSQL_TYPES 扩展
perf-py/ptde/generator/worker.py                   # row_index 上下文一行

修改（测试，perf-py +16）
perf-py/tests/test_perf_cli_ptde_datagen_engine.py # 新（路由引擎 15 例）
perf-py/tests/test_datagen_runner.py               # 路由断言 + legacy 指向

未改动：datagen/**（legacy 本体 A1 分轨保留）、契约测试三件套与 golden、
test_datagen_cli.py（monkeypatch seam 生效，零改动通过）、perf-web/**、
crosscheck/迁移脚本/CLI 命令形状与退出码
```

## 4. 关键设计决策与踩坑记录

1. **路由落点 = `build_default_engine` 变 PTDE 背书**：CLI（`main.py`
   函数级延迟导入）与服务层（`_build_engine`）均经此名取引擎——改名会
   打破 `test_datagen_cli.py`/`test_datagen_seed.py` 的 monkeypatch seam；
   legacy 装配保留为 `build_legacy_engine`，契约测试与 CLI 形状测试零
   改动通过（门禁 1 不受影响）。
2. **追加灌数语义（本轮最大语义对齐点）**：legacy 是「向已有库追加灌数」
   （父 ID 池读存量、seq 起始值探测回写）；PTDE 管线原为「空库全量构建」。
   对齐方案：`existing_max`（SELECT MAX(pk)）接入 IdRangeAllocator——
   主键区间自存量最大值 +1 续接，同时覆盖两类情形：非自增 `id` 主键
   （worker 显式写区间值，防撞存量）与自增主键（DB 按同口径分配，
   子表外键 planned span 与 DB 自增对齐）。`refresh_seq_starts` 的切换
   编排层决策落点 = id 列起始值由 existing_max 结构保证，非 id 列
   seq_start 按规则值。
3. **存量父表双路**：任务内父表 → 声明关系（零孤儿 span 结构保证）；
   存量父表 → 不声明关系（父表不重建、不重复灌数），子列 ref 分布以
   存量 ID 域（MIN/MAX）显式给出。域稀疏（有空洞）时的孤儿由服务层
   verifyMin 如实暴露（legacy 池采样自实际 ID 集合，域稠密时两侧统计
   等价——观察期验收口径）。空/缺父表 legacy 兜底随机数为伪装可用路径，
   PTDE 显式失败（A3）。
4. **不跑 pipeline.verify**：count 层为全表精确口径（数据集构建语义），
   与追加灌数（表内已有存量）结构性冲突；引擎走 generate/load 两段，
   验证由服务层 verifyMin（QW3）承担。staging 装载成功后尽力清理；
   ptde_shard_checkpoint 行留目标库审计。
5. **语义提示归零**：PTDE 通道为 Schema 驱动（A2），无源码魔法值/ORM
   关联扫描——`on_hints` 恒空，`/api/datagen` 建议面板不再由灌数任务
   新增建议（存量提示与 adopt 流不受影响）；`--source-dir` 在 PTDE 路由
   留 WARN。
6. **worker row_index 一行修复**：`plan_row_factory` 此前对
   `sample_distribution` 恒不传 context → sequential 恒取起始值（恒值
   缺陷）；补 `{"row_index": row_index}` 后非 id 列 seq 语义逐值对齐
   legacy（仅 sequential 读 row_index，其余采样器零影响，既有测试零
   改动通过）。分片大小固定 2,000,000（单分片覆盖 legacy 规模上限，
   1M 服务层上限内恒单分片）；>2M 多分片 sequential 分片内重启为
   已知差异（后续分片编排批次接 per-shard offset）。
7. **seed 缺省随机分配落库**：legacy seed=None 走模块级 random（不可
   复现）；PTDE A3 口径不容忍不可复现路径——编排层折中：随机分配并在
   definition.seed 落库（事后可复现），告警留痕。
8. **known behavior changes（切换引入，perf-web 零改动但行为收窄）**：
   postgres 目标库显式失败（MySQL-first）；max_rows 钳制移除（服务层
   MAX_ROWS 校验仍在）；灌数任务不再产出语义提示；id 命名列的
   seq_prefix/seq_start 由 planned_range 接管（连续性结构保证）；
   非 `id` 名的非自增主键无 planned_range 语义（按规则/缺省生成，
   legacy 同款）。
9. **踩坑（测试侧）**：staging 文件格式往返后数值以字符串进入装载批
   （MySQL/LOAD DATA 文本装载同口径强制转换）——断言按 `int()` 归一；
   引擎装配关系四元组初版遗漏 `relation_kind` 键（关系声明测试捕获后
   补齐——测试先行的价值实证）；ruff I001 两处 import 排序自动修复。

## 5. 验收对照

| 验收项 | 结果 | 落点 |
|---|---|---|
| T7.6 CLI 切换：`perf datagen` 命令形状不变，实现路由 PTDE | ✅ | `build_default_engine` → `PtdeDatagenEngine`；`test_datagen_cli.py` 零改动通过 |
| T7.7 API 切换：`/api/datagen` 实现替换，端点/形状不变 | ✅ | 契约三件套 + golden 零改动全绿；写路径执行体经路由替换 |
| 门禁 1：契约测试全绿 | ✅ | `tests/contract/test_contract_datagen*.py` + golden + CLI 形状（222 例受影响面全绿） |
| 门禁 2 双跑（真库门控） | ✅（未改动） | crosscheck 本批零改动，设 `PTDE_REALDB_URL` 复跑核对 |
| 追加灌数主键续接（existing_max） | ✅ | `test_run_appends_after_existing_max`（MAX=100 → 101~110） |
| 存量父表 ID 域采样 + 不声明关系 | ✅ | `test_ref_parent_outside_task_uses_existing_domain` |
| 任务内父表零孤儿关系声明 | ✅ | `test_ref_parent_in_task_declares_relation_with_zero_orphan_span` |
| orphan_rate 链 → logical 降级 | ✅ | `test_orphan_rate_chain_downgrades_relation_to_logical` |
| 非 id 列 seq 逐值推进（row_index 接线） | ✅ | `test_seq_rule_non_id_column_advances_per_row` |
| dry-run 不写库 + 估算公式 | ✅ | `test_dry_run_estimates_without_writes` |
| A1 max_rows 钳制移除 | ✅ | `_prepare` 显式文案（「A1 归一移除」） |
| 门禁 | ✅ | perf-py 1787 passed, 5 skipped；ruff 全绿 |

## 6. 回归保障与回滚（§10.4）

- **契约零改动**：`/api/datagen` 13 端点 + golden、`perf datagen run`
  命令形状/退出码、`engine=` 注入缝全部保持；`datagen/**` 零修改；
  管线缝扩展均为缺省 None 的纯增量（既有 offline/realdb/dataset 测试
  零改动通过）；
- 回滚：还原 `datagen_runner.build_default_engine`（指回 legacy 装配，
  现存于 `build_legacy_engine`）+ 删除 `ptde_datagen_engine.py` + 还原
  offline_pipeline/realdb_pipeline 两参 + worker 一行即可；无数据/
  契约破坏性变更（legacy 代码与测试全量在位）。

## 7. 后续批次衔接（供下一 session 使用）

- **本批已闭环**：§13.2 步骤 3/4（CLI/API 切换）——实施手册剩余：
  **步骤 5 观察期**（≥一个发布周期；旧表只读；监控对比任务成功率/
  耗时）与 **步骤 6 T7.9 退役删除**（观察期无回退需求后）；
- **T7.9 前置清理清单**（退役时逐项核对）：
  - `perf_cli/ptde_datagen_engine.py` 仍 import `datagen.model`
    （DataGenSpec/EngineResult/FieldRule/OverThreshold/TableSpec）——
    随 `ptde/models/compat.py`（V2 镜像）归一或内化；
  - `ptde/generator/worker.py` 收编副本 import `datagen.model.ColumnMeta`
    （R4 起共享，收编总表核对）；
  - `server/datagen_service.py`（spec_from_request/内省方言/verify_min/
    endpoint_index_parser/plan_pipeline 环依赖降级）与
    `server/datagen_plan_service.py`（分析链）整块依赖 datagen——
    退役时随「读路径/分析链收编或同步退役」决策一并处置；
  - `build_legacy_engine` 与 test_datagen_runner legacy 两例随包删除；
  - `grep -r "from datagen\|import datagen"` 运行时路径清零（文档除外）；
- **真库复跑**：设 `PTDE_REALDB_URL` 复跑 crosscheck（本批零改动）+
  建议补一轮「切换引擎 vs legacy 双跑」真库对照（适配器全链真库端到端
  目前仅有协议级覆盖）；
- **已知差异修复方向**：>2M 行多分片 sequential per-shard offset
  （分片编排批次）；非 id 名非自增主键的 planned_range 泛化
  （worker `spec.name == "id"` 特判放宽）；
- **API 超时控制**：随编排层一并落（口径不变）；
- 真库环境复现口径：docker `mysql:8.0`（`--local-infile=1`）+
  `sql/init.sql` + `GRANT ALL ON performance_testor.* TO 'perf'@'%'` +
  `PTDE_REALDB_URL=mysql://perf:perf_pw@127.0.0.1:3306/performance_testor`。

---

*B7 续批次 10 交接完成：接管切换 T7.6/T7.7（PTDE 路由灌数引擎 +
build_default_engine 生产路由〔legacy 装配保留 build_legacy_engine〕+
existing_max 追加区间缝 + worker row_index 接线）；§13.2 步骤 3/4 闭环，
剩余观察期（步骤 5）与 T7.9 退役删除（步骤 6）。*
