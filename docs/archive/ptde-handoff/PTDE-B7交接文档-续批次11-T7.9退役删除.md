# PTDE B7 续批次 11 交接文档 — T7.9 退役删除（§13.2 步骤 6）

> 状态：**已完成并通过门禁**（2026-11-19）
> 依据：续批次 10 交接 §7「后续批次衔接」——datagen 整包退役、依赖收编
> 归位、专属测试处置、门禁核对；代码位置：`ptde/models/wire.py`（新，
> 原样收编）、`ptde/adapters/direct/`（新，原样收编）、
> `ptde/planner/llm_enhance.py`（新，收编补全）、`ptde/validator/verify_min.py`
> （新，原样收编）、`ptde/planner/workload.py`（收编补全 + 模型居所切换）、
> `server/datagen_plan_service.py`、`server/datagen_service.py`、
> `perf_cli/datagen_runner.py`、`perf_cli/ptde_datagen_engine.py`、
> `tests/test_ptde_realdb_gate.py`（新）；
> 前序交接：B7 前段 + 续批次 1~10 全部有效。
> **说明：步骤 5「观察期」为运维动作（发布观察/监控对比/双签确认），
> 代码侧不可执行；本次按项目指令直接执行步骤 6，观察期口径与双签记录
> 由项目侧补记于发布归档。**

---

## 1. 交接基线与结果

- 入口基线：续批次 10 交付后口径（perf-py 1787 passed, 5 skipped；
  ruff 全绿）；
- 交付后门禁：
  - perf-py 全库 `uv run --extra server --extra locust pytest tests/` →
    **1643 passed, 5 skipped, 0 failed**（基线 1787 − 退役专属/对照测试
    144 + 收编平移净变化；5 skipped 口径不变：4 个真库门控测试需
    `PTDE_REALDB_URL`、1 个 locust 门控）；
  - `uv run ruff check ptde/ perf_cli/ server/ tests/ tools/ ptde_common/` →
    All checks passed；
  - `grep -rn "from datagen\.\|import datagen"` **运行时路径零残留**
    （仅存两处 docstring 历史说明，属文档豁免口径）；
  - 导入冒烟：`perf_cli.main` / `server.datagen_api` / `tools.source_tools` /
    `ptde.planner.llm_enhance` / `ptde.adapters.direct.dialects` 全部可导入。

## 2. 交付内容

### 2.1 收编归位（四件原样搬迁 + 两件收编补全）

| 收编物 | 来源 | 去向 | 说明 |
|---|---|---|---|
| wire 模型 | `datagen/model.py` | `ptde/models/wire.py` | 宿主链唯一居所：spec 解析/PTDE 引擎/写路径服务/contract 测试（19 dataclass 原样，stdlib-only） |
| 直连套件 | `datagen/spi.py` + `executor.py` + `dialects/{mysql,postgres}` | `ptde/adapters/direct/` | 服务层表列探测（/tables、/columns）、水位捕获、verifyMin、pilot_run 降级通道（与 R5 装载适配器职责不同，语义各自冻结） |
| QW3 验证 | `datagen/verify_min.py` | `ptde/validator/verify_min.py` | 服务层 verifyMin 继续消费 |
| LLM 增强段 | `datagen/plan_pipeline.py` L743-931 | `ptde/planner/llm_enhance.py` | R3 遗留缺口补全（脱敏边界/降级链逐字保留） |
| 环降级三函数 | `plan_pipeline.parse_cycle/remove_ref_edge/break_weakest_cycle_edge` | `ptde/planner/workload.py` | R3 收编缺口补全（环依赖自动降级路径） |
| —— | `datagen/` 整包 26 模块 + `ptde/models/compat.py` | **删除** | legacy 引擎/拓扑/关联解析/规则引擎/id_pool 垫片/ORM 解析器本体全部随包退役 |

### 2.2 模型双居所决策（本批最关键架构决策）

程序化比对证实：`_legacy_model` 9 类中 6 类（EndpointRef/JoinRef/MagicHint/
Op/Predicate/RelationHint）与 datagen.model **源码有差异**（R1 三态字段
等价演进），且 SqlAccess 枚举跨模块成员**不相等**。因此：

- **`ptde/models/wire.py`** = 灌数宿主链模型（DataGenSpec/FieldRule/
  EngineResult/ColumnMeta/SchemaMetadata/…）；
- **`ptde/analyzer/lang_java/_legacy_model.py`**（保持不动，docstring 更新）
  = 分析链模型唯一居所——lang_java 解析器 / `workload.compose` /
  `llm_enhance` / `datagen_plan_service` 全部消费其对象（`ta.access ==
  SqlAccess.WRITE` 等枚举比较必须同类）；
- 两居所**并存不合并**（重名类 3 个：SourceFile/SqlAccess/OverThreshold，
  语义一致，归属各自链路）。

### 2.3 运行时 import 改路（全量）

- `server/datagen_plan_service.py`：分析链 → lang_java 副本 + workload +
  llm_enhance；EndpointRef/SourceFile/SqlAccess → `_legacy_model`；
- `server/datagen_service.py`：方言 → `ptde.adapters.direct.dialects`；
  base_type → `ptde.generator.primitive`；verifyMin → `ptde.validator`；
  环降级 → workload；EndpointIndexParser → lang_java；enum 发现 →
  `ptde_common`；SourceFile → wire；
- `perf_cli/datagen_runner.py`：**仅保留 wire 契约解析/序列化**
  （rule_from_json/parse_fields/spec_from_request/parse_fields_json/
  collect_source_files/load_spec/ConsoleListener/result_to_camel_json）；
  legacy 装配（build_legacy_engine）随包退役；`build_default_engine`
  维持 PTDE 路由（monkeypatch seam 不变）；
- `perf_cli/ptde_datagen_engine.py`、`ptde/generator/primitive.py`、
  `ptde/generator/worker.py`、`ptde/planner/workload.py`（Op/Predicate/
  SqlAccess → `_legacy_model`）、`ptde/planner/pilot_run.py`（executor →
  direct）、`server/endpoint_scanner.py`、`tools/source_tools.py`。

### 2.4 测试处置（−144 净）

- **删除（legacy 本体专属/对照使命完成，10 文件 + 段落）**：
  test_datagen_engine / test_datagen_topo_sorter /
  test_datagen_relation_resolver / test_datagen_naming_sampling /
  test_datagen_rule_engine / test_datagen_id_pool / test_ptde_compat /
  test_ptde_lang_java_migrate / test_ptde_workload（绝对断言覆盖由
  test_datagen_plan_pipeline 平移承载）/ test_ptde_common_extraction；
  primitive 对照段 9 例（保留 absolute 矩阵并补 base_type 全值断言）；
  seed 的 legacy 引擎逐行哈希复现段（QW1 双跑证据归档；PTDE 侧 seed
  语义由引擎协议测试承载）；explicit_rule_hints 段；crosscheck legacy 侧。
- **改路归位**：plan_pipeline 测试 → workload（parse_cycle 等 4 个
  函数级导入 + 内联 `__import__`）；llm 测试 → `ptde.planner.llm_enhance`；
  方言/端点/call_graph/ORM/sql 提取器测试 → lang_java 副本（Op/Predicate/
  SqlAccess 一律 `_legacy_model`，枚举同类可比）；verify_min/cleanup/
  model/contract/runner/samplers/rule_translator/引擎协议测试 → wire。
- **新增**：`tests/test_ptde_realdb_gate.py`——真库端到端门控承接
  原 §13.1 门禁 2 双跑对照的 PTDE 终态（切换引擎生产路径：内省 →
  translate_fields → generate/load；行数精确/分布容差/主键区间/
  零孤儿外键；报告归档 `benchmarks/ptde_realdb_gate_report.json`；
  原 `gate2_crosscheck_report.json` 归档保留）。
- `test_ptde_models.py` 依赖扫描去掉 compat.py 例外（ptde/models 无
  datagen 依赖，无例外）。

## 3. 文件清单

```text
新增（生产代码，收编归位）
perf-py/ptde/models/wire.py                       # 宿主链 wire 模型（datagen/model 原样）
perf-py/ptde/adapters/direct/__init__.py          # 直连套件包说明
perf-py/ptde/adapters/direct/spi.py               # DatabaseDialect/BatchExecutor 协议
perf-py/ptde/adapters/direct/executor.py          # DbApi 批量执行器
perf-py/ptde/adapters/direct/dialects/{__init__,mysql,postgres}.py
perf-py/ptde/validator/verify_min.py              # QW3 验证子集
perf-py/ptde/planner/llm_enhance.py               # LLM 可选增强段

修改（生产代码）
perf-py/ptde/planner/workload.py                  # 环降级三函数收编补全
                                                  #  + 模型居所切换
perf-py/server/datagen_plan_service.py            # 分析链/模型改路
perf-py/server/datagen_service.py                 # 方言/验证/建议改路
perf-py/perf_cli/datagen_runner.py                # 仅保留 wire 契约解析/序列化
perf-py/perf_cli/ptde_datagen_engine.py           # wire 改路
perf-py/ptde/generator/primitive.py               # wire 改路
perf-py/ptde/generator/worker.py                  # wire 改路
perf-py/ptde/planner/pilot_run.py                 # direct.executor 改路
perf-py/ptde/analyzer/lang_java/_legacy_model.py  # docstring：分析链模型居所
perf-py/server/endpoint_scanner.py、tools/source_tools.py

删除
perf-py/datagen/                                  # 整包 26 模块
perf-py/ptde/models/compat.py

修改（测试，净 −144）
perf-py/tests/test_ptde_realdb_gate.py            # 新（真库门控 PTDE 终态）
perf-py/tests/test_datagen_{engine,topo_sorter,relation_resolver,
  naming_sampling,rule_engine,id_pool}.py 等删除 + 其余改路/收编平移

未改动：ptde 管线本体（R4-R6 交付零触碰）、server 契约面与 golden、
perf-web/**、CLI 命令形状与退出码、pyproject extras（datagen-parsers/
datagen-drivers 依赖组名称保留，供 direct 套件与 DRIVER_HINT 文案）
```

## 4. 关键决策与踩坑记录

1. **模型双居所而非合并**：程序化 source 比对发现 `_legacy_model` 与
   datagen.model 的 6 个重名类有实质差异（三态字段演进）且枚举跨模块
   不可比——合并会静默改变 lang_java 解析器行为；两居所按链路归属。
2. **workload 模型居所切换**：`workload.compose` 直接消费 plan_service
   传入的 lang_java 解析器产物（`p.op == Op.RANGE`），故 Op/Predicate/
   SqlAccess 必须取 `_legacy_model` 而非 wire——这是「对照测试全绿」
   背后唯一的语义敏感点（sql 提取器测试改路时同样踩中：wire.Op 与
   _legacy_model.Op 成员不相等，9 例失败后归位修正）。
3. **收编补全两处缺口**：R3 收编时 `break_weakest_cycle_edge` 系与
   `llm_enhance` 段留在了 legacy 本体（分别因「环降级走 legacy 路径」
   与「LLM 建议定位」）；退役后二者均成为必活代码，按原语义补进
   workload / 独立模块，环降级测试平移全绿。
4. **对照测试的终态处置口径**：所有「同输入同输出」对照测试
   （workload/primitive/lang_java 迁移/common extraction）随对照对象
   退役而退役；其保护价值由**绝对断言**承接（plan_pipeline 测试语义
   逐条平移 workload、primitive 补 base_type 全值矩阵）。primitive
   parity 段删除时以实际运行值固化 `test_base_type_matrix`
   （ENUM/SET→TEXT、TIMESTAMP→DATETIME、BOOLEAN→BIT 等）。
5. **crosscheck 的 PTDE 终态承接**：门禁 2 双跑对照的 legacy 侧不可复现
   （引擎已退役），改为切换引擎真库端到端门控（test_ptde_realdb_gate），
   断言口径继承（行数/分布容差 0.03/主键区间/零孤儿外键），报告归档
   换名；历史双跑报告 `benchmarks/gate2_crosscheck_report.json` 保留。
6. **CLI spec 解析（rule_from_json 等）保留在 runner**：它们产出 wire
   FieldRule 供 `server.rule_translator` 消费，是灌数契约解析的唯一出处
   （续批次 9 定位），不是 legacy 引擎遗留。
7. **pyproject extras 保留**：`datagen-parsers`/`datagen-drivers` 依赖组
   名称不动（direct 套件与 lang_java 仍消费 tree-sitter/pymysql 等；
   DRIVER_HINT 安装指引文案依赖该名称）。
8. **踩坑**：sed 全局改路前必须先手工处理 workload（否则被 model→wire
   映射误伤枚举身份）；replace_in_file 对 CRLF 文件的模糊匹配曾在两处
   「报错未应用」但实际已应用——以 grep/read 复核为准；ruff I001 20 处
   （搬迁文件的 import 排序）自动修复后全绿。

## 5. 验收对照（T7.9）

| 验收项 | 结果 | 落点 |
|---|---|---|
| 按 §2.4 收编总表逐模块核对 | ✅ | §2.1 收编表 + 模型双居所决策 |
| 删除 `perf-py/datagen/` 整包 | ✅ | 目录已不存在，导入冒烟通过 |
| 全量测试绿 | ✅ | 1643 passed, 5 skipped |
| `grep` 运行时路径零残留（文档除外） | ✅ | 仅 2 处 docstring 历史说明 |
| 宿主解供建立后切换引擎真库端到端 | ✅ | test_ptde_realdb_gate（需 `PTDE_REALDB_URL`） |
| 契约面零改动 | ✅ | 13 端点 + golden + CLI 形状 + golden 快照零触碰 |
| ruff | ✅ | All checks passed |

## 6. 回归保障

- **无回退路径**（§13.2 步骤 6 性质）：legacy 代码与专属测试已删，
  回退只能靠版本控制（git 历史/发布归档）——这正是实施方案将步骤 6
  置于观察期之后的原因；本次按项目指令提前执行，**项目侧需补记观察期
  口径与双签确认于发布归档**；
- PTDE 侧门禁不变：契约三件套 + golden、引擎协议 16 例、真库门控
  （e2e/pilot_run/crosscheck 沿革）、workload/llm 收编测试平移全绿。

## 7. 后续批次衔接（供下一 session 使用）

- **§13.2 六步骤全部闭环**（1/2 切换准备、3/4 接管切换、5 观察期
  （运维口径待补记）、6 退役删除）——**B7 主线代码工作至此收口**；
- 遗留技术债（不阻塞，按需排期）：
  - `ptde/models/wire.py` 与 `_legacy_model.py` 的 3 个重名类
    （SourceFile/SqlAccess/OverThreshold）仍两份并存，后续可考虑统一为
    pydantic 三态模型（A6 终态口径）；
  - `perf_cli/datagen_runner.py` / `server/datagen_service.py` /
    `server/datagen_plan_service.py` / `server/endpoint_scanner.py` 等
    模块名与 wire 模型 dataclass 名仍沿用 legacy 命名（datagen_* 表名、
    DataGenSpec 等）——**API wire 契约与表结构不动**的前提下仅是命名
    历史，重命名属纯美观改动，不值得单独开批；
  - worker 收编副本对 wire 的 ColumnMeta 依赖（A1 分轨终态未完全达成，
    属 R4 遗留，行为冻结无风险）；
  - `>2M` 行多分片 sequential per-shard offset、非 `id` 名非自增主键
    planned_range 泛化（续批次 10 已列，随分片编排批次）；
- 真库复跑口径不变：docker `mysql:8.0`（`--local-infile=1`）+
  `sql/init.sql` + `GRANT ALL ON performance_testor.* TO 'perf'@'%'` +
  `PTDE_REALDB_URL=mysql://perf:perf_pw@127.0.0.1:3306/performance_testor`
  → `uv run --extra server --extra locust pytest tests/test_ptde_realdb_gate.py
  tests/test_server_realdb_pipeline.py -q`。

---

## 8. 技术债处理记录（2026-11-20，§7 遗留清单处置）

| # | 债项 | 处置 | 落点 |
|---|---|---|---|
| 1 | wire.py 与 _legacy_model.py 重名类双居所 | **已统一**：datagen 退役后 A4 反向依赖禁令成因消失，9 个重名类（OverThreshold/RelationHint/SourceFile/MagicHint/SqlAccess/Op/Predicate/JoinRef/EndpointRef）以 `ptde.models.wire` 为唯一居所，`_legacy_model.py` 退化为兼容 re-export 垫片（语义逐类核对等价，SqlAccess 枚举自此跨模块可比） | `ptde/analyzer/lang_java/_legacy_model.py`、`ptde/models/wire.py` |
| 2 | 模块命名沿用 legacy（datagen_runner 等） | **维持不动**（与原判一致）：API wire 契约与表结构不动的前提下属纯美观改动，不值得单独开批 | — |
| 3 | worker 收编副本对 wire ColumnMeta 依赖 | **已消除**：primitive 抽出类型口径窄接口 `generate_default_for(type_name, size)`，worker 协议层不再 import 宿主 wire 模型；FieldSpec 增加可选 `size`（CHARACTER_MAXIMUM_LENGTH 口径，缺省 0 = 历史行为），引擎内省同步采集列长度（文本列缺省生成长度保真） | `ptde/generator/primitive.py`、`ptde/generator/worker.py`、`perf_cli/ptde_datagen_engine.py` |
| 4a | >2M 行多分片 sequential per-shard offset | **已修复**：ShardTask 增加实体全局行偏移 `row_offset`（shard_tasks_from_plan 按 shard_id 序累计），sequential 等行号敏感分布跨分片连续；缺省 0 = 单分片历史行为不变 | `ptde/generator/worker.py` |
| 4b | 非 id 名非自增主键 planned_range 泛化 | **已落地**：worker `spec.name == "id"` 特判放宽为 `task.pk_column`（协议字段，缺省 "id" = 历史行为）；OfflinePipeline/realdb 装配增加 `pk_columns` 注入缝；引擎按内省对任意单整型主键下传（非自增主键接管留痕告警，关系子列校验同步泛化） | `ptde/generator/worker.py`、`server/offline_pipeline.py`、`server/realdb_pipeline.py`、`perf_cli/ptde_datagen_engine.py` |

回归口径：全量 pytest **1649 passed + 5 skipped**（新增 6 例：行偏移连续性、
pk_column 泛化、缺省行为冻结、primitive 双口径类型矩阵、非 id 主键端到端），
ruff All checks passed。协议新增字段均为可选缺省兼容（旧载荷解析行为不变）。
遗留：`test_bind_ready_dataset_snapshot_recorded` 为异步构建 5s 轮询的时序
敏感用例，全量跑偶发超时（与本次改动无关，复跑即绿）。

---

*B7 续批次 11 交接完成：T7.9 退役删除（datagen 整包 26 模块 + compat.py；
收编归位 wire/direct/verify_min/llm_enhance + workload 补全；模型双居所；
对照测试终态处置 −144；真库门控 PTDE 终态承接）。§13.2 六步骤代码工作
全部闭环，B7 主线收口。技术债清理见 §8。*
