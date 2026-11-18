# PTDE 智能测试数据引擎落地方案：现状差距分析与改造路线

- **文档类型**：落地实施方案 / 改造路线基线
- **版本**：1.1（1.0 评审后确认 §7 六项决策，并补充 A4a 终态接管决策）
- **状态**：已评审确认（决策已定，待启动 R0）
- **依据文档**：《性能测试平台-智能测试数据引擎-完整技术方案.md》（PTDE 总体技术方案 v1.0，下称"总方案"）
- **适用代码基线**：`perf-py`（Python 数据准备引擎主力实现）、`docs/plans/` 既有规划文档

---

## 1. 目的与范围

本文回答两个问题：

1. 当前项目中的测试数据准备引擎（灌数引擎）与 PTDE 总方案之间的**具体差距**是什么，差距落在哪些代码上。
2. 如何**在不推翻现有能力的前提下**，按总方案 §17 的 P0～P7 路线渐进改造 `perf-py`，每阶段改什么、复用什么、验收什么。

本文是实施前的评审文档，不包含具体代码实现。所有文件路径均为当前仓库真实路径。

## 2. 现状摘要

### 2.1 当前引擎架构（事实基线）

当前主力实现为 Python 版灌数引擎，位于 `/workspace/perf-py/datagen/`，宿主装配在 `/workspace/perf-py/perf_cli/datagen_runner.py`，服务层在 `/workspace/perf-py/server/`。核心特征：

- **入口三条**：CLI（`perf datagen run`）、HTTP API（`/api/datagen/*`，`server/datagen_api.py`）、Agent 工具（`tools/datagen_tools.py`）。
- **编排**：`default_engine.py` 六步流水线——校验 → 方言取元数据 → 源码语义扫描 → 四级关联解析 + 拓扑排序 → 枚举建议注入 → Dry-Run 或批量写入。
- **生成模式**：内存逐行构造 + `DbApiBatchExecutor.executemany` 批量写（batch=500），单线程、无分片、无并行、无 Seed。
- **规模**：表组逐表 `row_count`，总行数硬钳制 1～100 万（`default_engine.py` `_validate`）。
- **数据库**：MySQL / PostgreSQL 双方言（SPI 抽象 `spi.py`，方言实现 `dialects/mysql.py`、`dialects/postgres.py`）。
- **源码分析**：仅 Java 栈（tree-sitter-java），覆盖 MyBatis XML、JPA 注解、调用链、SQL 字面量/谓词抽取、端点索引。
- **持久化**：`data_gen_task` / `semantic_hint` / `data_gen_plan` 三张表（`sql/init.sql`，`server/datagen_repo.py`）。
- **质量保障**：15 个单测文件（`perf-py/tests/test_datagen_*.py`）+ 对 Java 版的契约测试（`perf-py/tests/contract/`）。契约测试意味着 **`/api/datagen` 的请求/响应形状是对外承诺，不能随意破坏**。

### 2.2 定性结论

当前引擎与 PTDE 是同一领域的两个代际：现状是"灌数工具"（把行数灌进去），PTDE 是"数据集工程"（把可复现、可验证、可追踪、可清理的 Performance Dataset 构建出来）。现有代码可作为 PTDE 多个子系统的**雏形**演进，不需要推倒重来；但生成内核（单线程内存生成）和数据集管理（无生命周期）两块需要结构性改造。

## 3. 差距分析（逐维度，含代码证据）

差距等级：**缺失**（无对应能力）／**大**（有雏形但需结构性改造）／**中**（有基础，需补全）。

| # | 维度 | PTDE 要求（总方案章节） | 当前现状（证据） | 差距 |
|---|---|---|---|---|
| G1 | 项目分析 | 多语言/多 ORM；CONFIRMED/INFERRED/UNKNOWN 三态证据模型（§3.6）；迁移文件与测试代码分析（§3.1）；ProjectSnapshot + 指纹（§5.1） | 仅 Java 栈解析器（`datagen/mybatis_source_parser.py`、`jpa_source_parser.py`、`java_ast.py`）；`RelationHint` 有 priority/confidence 但无证据三态；无迁移/测试代码分析；无 ProjectSnapshot 与 schema 指纹 | 大 |
| G2 | 统计画像 | 脱敏生产统计采集（行数/基数/NULL/直方图/关联基数/热点，§4.2）、采集安全（§4.3）、Profile Merger 融合（§4.4） | 完全没有统计采集。最接近的仅是 SQL 字面量 → MagicHint（`sql_literal_extractor.py`） | 缺失 |
| G3 | 统一数据模型 | 13 个核心模型 + 版本 + 指纹（§5.1） | 扁平规则模型 `DataGenSpec/FieldRule/TableSpec/SchemaMetadata/RelationHint`（`datagen/model.py`）；无版本、无指纹、无 DatasetDefinition/Instance | 大 |
| G4 | 规模规划 | 5 种目标类型（§6.1）、关系规模分布推导（§6.3）、存储估算 + Pilot Run 校准（§6.5）、约束冲突显式失败（§6.4） | 逐表 row_count + 总量硬钳 100 万；接口驱动模式有 QPS 峰压行数推导（`plan_pipeline.py` RowCountEstimator）；无存储估算、无约束求解、无 Pilot Run | 大 |
| G5 | 数据分布 | 12 种分布 + 四层分布（字段/字段关联/实体关联/全局时间热点，§7.2）+ 条件分布（§7.3） | fixed / range（含 overThreshold）/ enum+weights / seq / refSkew 热点（`rule_engine.py`、`id_pool.py`）；无 Normal/LogNormal/Zipf/Temporal/Correlated/Conditional/NullRatio/LengthDistribution | 大 |
| G6 | 生成内核 | 确定性 Seed 可复现（§12.4）、流式/分片/并行（§8）、ID 预分配（§7.6）、staging 文件与检查点 | 内存单线程逐行构造 + batch=500 写库；无 Seed、无并行、无分片；`IdPool` 仅整数、上限 50 万，字符串/UUID 外键不支持（`naming_relation_resolver.py` 仅整数族列） | 大 |
| G7 | 批量装载 | 原生批量导入（LOAD DATA / COPY / BULK INSERT，§9.3）+ Capability Probe（§10）+ 降级链 + 限流 + 断点恢复（§9.6） | 仅 DBAPI `executemany` + URL 参数优化（`executor.py`、`dialects/*`）；无能力探测、无原生导入、无检查点/恢复。SPI 方言抽象已留扩展口 | 中 |
| G8 | 循环依赖 | SCC 分析 + 显式安全策略，无安全方式则阻止执行（§8.5） | 自动剔除最低置信度边重试一次（`default_engine.py` 环降级逻辑）——静默降级，与总方案"不得默认绕过"原则冲突 | 中 |
| G9 | 数据验证 | 五层验证（数量/结构关系/分布/业务不变量/数据库就绪，§11）+ ValidationReport | 仅 Dry-Run 预检 + 端点非空率 selfCheck（`server/datagen_service.py`）；无生成后验证 | 大 |
| G10 | 数据集生命周期 | Definition/Instance 分离、PLANNED→…→READY 状态机（§12.2）、Manifest、可复现性（§12.4）、清理策略与所有权（§12.5） | 仅任务/方案/提示三张表的状态机；灌入的数据无标记、无清理、无复用（`运维与数据生命周期加固方案.md` 仅为未实施的规划文档） | 缺失 |
| G11 | 安全与隔离 | 环境识别、权限分离（统计只读/测试库写入）、执行前门禁、禁止生产写入（§13） | 无环境门禁、单凭据 | 缺失 |
| G12 | CLI/API/集成 | ptde 命令族 + dataset API + 压测任务绑定 DatasetInstance（§15） | `perf datagen run` + `/api/datagen` 13 端点；压测任务（`server/stress_*`）不记录数据集绑定 | 中 |
| G13 | 可观测性 | 吞吐/资源/恢复时长指标、分片级日志、Pilot Run 校准（§16） | `EngineResult/tableStats/costMs`/进度回调；无吞吐指标、无 Pilot Run | 中 |

### 3.1 现状已具备、且超出总方案文字描述的能力（应保留并纳入 PTDE）

- **枚举值四级发现**（`enum_value_discoverer.py`：内联 ENUM → CHECK 约束 → 列注释 → Java 枚举反查）→ 对应 EntityModel 的 `allowed_values` 来源。
- **语义提示采纳流**（`semantic_hint`：pending/adopted/dismissed + uk_hint 幂等）→ 正是总方案 §3.6 "INFERRED 项由用户确认"的现成机制。
- **接口驱动方案管线**（`plan_pipeline.py`：QPS 峰压行数推导、规则合成、完整性检查、LLM 增强）→ 对应总方案 §6.1 第 4 类"工作负载目标"。
- **overThreshold 越阈分布**（热点列超过阈值占比取高值段）→ 热点/倾斜分布的雏形。

## 4. 复用资产映射

| 现有资产（路径） | 映射为 PTDE 组件 | 演进方式 |
|---|---|---|
| `datagen/spi.py`（DatabaseDialect/BatchExecutor 抽象） | Database Adapter 分层（§9.2） | 拆分为 Metadata / Statistics / BulkLoad / CapabilityProbe 子适配器接口，现有方言实现逐步实现新接口 |
| `dialects/mysql.py`、`dialects/postgres.py`（`fetch_metadata`） | DatabaseSchemaSnapshot 采集器 | 扩展采集：索引（含复合列序）、分区、触发器、生成列、Sequence/Identity、存储统计 |
| `datagen/model.py`（SchemaMetadata/FkMeta/RelationHint） | EntityModel / RelationModel / DatabaseSchemaSnapshot | 新统一模型承接旧模型字段；旧 `DataGenSpec` 保留为兼容视图（模型形态见 §7 A6） |
| `relation_resolver.py` + `naming_relation_resolver.py` + 四级优先级 | RelationModel 解析器 + INFERRED 证据流 | 输出补 `status/evidence` 字段；naming 级一律 INFERRED，走 `semantic_hint` 采纳 |
| `semantic_hint` 采纳工作流（`server/datagen_service.py`、`datagen_repo.py`） | 用户确认机制（§3.6/§11.4） | 直接复用，扩展到实体/规则/分布建议 |
| `plan_pipeline.py`（RowCountEstimator 等） | Scale & Distribution Planner 的工作负载目标分支 | 收编为规划器的一种目标类型，补齐其余四种目标 |
| `sql_table_extractor.py` / `sql_predicate_extractor.py` / `sql_literal_extractor.py` | AccessPattern 提取器 | 输出结构化 AccessPattern 模型（表/JOIN/WHERE/ORDER BY/选择性线索） |
| `topo_sorter.py`（Kahn + DFS 环定位） | §8.5 循环依赖处理 | 升级为强连通分量（Tarjan）分析 + 显式安全策略决策，禁止静默剔边 |
| `executor.py`（DbApiBatchExecutor） | Bulk Load Adapter 的"通用降级"档 | 保留为降级链最后一档；原生 LOAD DATA / COPY 作为新增首选档 |
| `rule_engine.py`（fixed/range/enum/seq） | Primitive Generators | 收编为基础生成器层，补分布采样器层 |
| `id_pool.py`（均匀/倾斜/无效 ID 采样） | Relationship Generators + Key Generation Adapter | 泛型化（支持 bigint/字符串/UUID）、去 50 万上限、支持预分配区间 |
| `default_engine.py` | 旧执行编排 | 过渡期保留为"legacy 管线"；新管线（§6 R4+）并行建设，契约测试保障切换安全 |
| `perf_cli/datagen_runner.py`（宿主装配） | 新引擎装配点 | 扩展注册新组件，保留 `build_default_engine` 对旧 CLI 的兼容 |
| 15 个单测 + 契约测试 | 回归安全网 | 全程保持绿色；新模块按同风格补测 |

## 5. 目标架构映射

PTDE 七子系统（总方案 §2.3）在 `perf-py` 中的落位建议——**新建独立子包 `perf-py/ptde/`**，总方案 §22 要求 PTDE 作为独立子系统，不与负载引擎混淆；旧 `datagen/` 包过渡期继续运行：

```text
perf-py/ptde/                      # PTDE 独立子系统（新建，终态取代 datagen）
├── models/                        # R0：统一数据模型（§5.1 全部 13 个模型，含版本与指纹）
│   ├── snapshot.py                #   ProjectSnapshot / DatabaseSchemaSnapshot
│   ├── entity.py                  #   EntityModel / RelationModel / BusinessInvariant
│   ├── access.py                  #   AccessPattern
│   ├── profile.py                 #   StatisticsProfile / UnifiedDataProfile
│   ├── spec.py                    #   DatasetSpec / GenerationPlan / ExecutionManifest
│   └── dataset.py                 #   DatasetDefinition / DatasetInstance / ValidationReport
├── analyzer/                      # R1：项目分析（收编 datagen 现有解析器 + 证据三态）
├── profiler/                      # R2：统计画像采集与融合（Statistics Collector + Merger）
├── planner/                       # R3：规模与分布规划（含 plan_pipeline 收编）
├── generator/                     # R4：确定性生成内核（分布采样器/关系生成器/分片）
├── loader/                        # R5：Bulk Load（能力探测/原生装载/检查点恢复）
├── validator/                     # R6：五层验证
├── lifecycle/                     # R6：Dataset 状态机/Manifest/清理
└── adapters/                      # 横切：Database Adapter 分层（含 capability probe）

perf-py/datagen/                   # 过渡期保留：legacy 管线，/api/datagen 契约不变
                                   # 终态：PTDE 接管 perf datagen CLI 与 /api/datagen 实现后，
                                   # 本包删除，存量 data_gen_* 表只读归档（见 §7 A4a）
```

宿主集成：`perf_cli/`、`server/`、`tools/` 逐阶段新增 PTDE 入口（§6 R7）；R7 收官时现有入口的实现切换为 PTDE，命令与契约形状不变。

## 6. 改造路线（P0～P7 → 本项目 R0～R7）

原则（承接总方案 §14.2）：第一阶段不引入分布式设施；生成内核先用 Python 多进程 + 分片文件，吞吐确实不足时再评估 Go/Rust worker（总方案 §14.3 允许沿用平台既有技术栈，不强行引入第二语言）。每阶段独立可验收、可回退，旧管线在 R7 接管切换前全程可用（终态取代，见 §7 A4a）。

### R0（对应 P0）：统一模型与执行框架

**改造点**
- 新建 `perf-py/ptde/models/`：落 §5.1 的 13 个模型，全部带 `version` 与 `fingerprint`（sha256）。
- **共享组件前置抽取（A4 约束）**：把模型基类、`id_pool.py`、`enum_value_discoverer.py` 抽到共享包，`ptde/` 与 legacy `datagen/` 两边引用；legacy 仅改 import，行为不变。
- 模型间传递只走结构化模型，禁止自然语言作为执行依据（总方案 §2.4"模型先行"）。
- 数据集状态机实现为确定性程序状态机（PLANNED/GENERATING/LOADING/VERIFYING/READY/PARTIAL/FAILED/CLEANING/CLEANED/CANCELLED），记录操作者/时间/原因。
- 数据库迁移：新增 `ptde_dataset_definition` / `ptde_dataset_instance` / `ptde_execution_manifest` / `ptde_validation_report` 表（写入 `sql/init.sql`，保持幂等风格，参考现有 `data_gen_task` 写法）；**表设计预留 A4a 存量迁移映射字段**（`data_gen_task`/`semantic_hint`/`data_gen_plan` → ptde 新表的来源 ID 映射），避免 R7 返工。
- `datagen/model.py` 旧模型不删；提供旧 → 新模型的适配函数。

**复用**：`datagen/model.py` 字段定义、`server/datagen_repo.py` 的 SQLAlchemy 表风格。
**验收（对齐总方案 P0）**：模型不依赖特定数据库/语言；输入输出有 Schema；模型有版本和指纹；状态转换由程序控制；共享组件抽取后 legacy 行为不回归、契约测试仍绿色。

### R1（对应 P1）：项目分析引擎增强

**改造点**
- 新建 `ptde/analyzer/`，收编 `datagen/` 的 Java 解析器作为第一个 Language Adapter（接口按多语言预留，本期只实现 Java）。
- 发现结果统一 `CONFIRMED / INFERRED / UNKNOWN` 三态，每条带 `id/type/value/source/evidence/confidence/analyzer_version`（总方案 §3.6）。naming 级关联、注解 SQL JOIN 正则解析结果一律降为 INFERRED，禁止静默跳过——改为显式记录（修正现状 G8 同类的"静默降级"问题）。
- DatabaseSchemaSnapshot 扩展采集：复合索引列序、分区、触发器、生成列、Sequence/Identity、Check 约束（现有 `fetch_metadata` 基础上扩展方言方法）。
- 新增迁移文件分析（Flyway/Liquibase 目录）与测试代码 Fixture 复用（本期可降级为"识别并登记，不解析"）。
- 产出 `ProjectSnapshot`（源码版本 + 技术栈 + schema 指纹）。

**复用**：`mybatis_source_parser.py`、`jpa_source_parser.py`、`call_graph_analyzer.py`、`sql_*_extractor.py`、`java_ast.py`、`semantic_hint` 采纳流。
**验收**：真实项目验证实体/约束/访问模式/证据；未知项显式标记；AI 推断不伪装为数据库事实；现有 `datagen_service` 源码扫描行为不回归。

### R2（对应 P2）：统计画像与融合

**改造点**
- 新建 `ptde/profiler/`：Statistics Collector（只读）——表行数、字段基数、NULL 比例、Top-K 频次、数值直方图/分位数、时间分桶、关联基数分布、热点倾斜。
- 采集安全（总方案 §4.3）：独立只读凭据（与写库凭据分离，G11 的第一块）；优先读数据库已有统计信息；采样/超时/资源限制；高基数敏感字段分桶/Top-K/哈希类别；记录采集时间、采样率、精度。
- Profile Merger：按 §4.4 优先级表融合源码画像、Schema 硬约束与统计画像；冲突生成显式报告。
- StatisticsProfile 支持"用户提供统计文件"作为无生产库访问时的替代来源。

**复用**：`dialects/*` 元数据查询、`id_pool.py` 的倾斜分布参数化思路。
**验收**：不导出生产明文业务记录；画像带采集元数据；源码硬约束不被统计覆盖；无生产库时可用文件来源。

### R3（对应 P3）：规模与分布规划器

**改造点**
- 新建 `ptde/planner/`：DatasetSpec v2 支持五种目标类型（行数/生产倍率/存储容量/工作负载/自动），可组合；目标冲突显式失败，禁止静默降低行数或改变分布（推翻现状 100 万硬钳制，改为约束求解 + 显式报错）。
- 关系规模推导：按离散分布采样 + 配额修正（总方案 §6.3），用户订单量分布先于用户数推导。
- 存储估算：行成本 `B_i` 由 Pilot Run 校准（总方案 §6.5），禁止字段长度简单相加。
- 收编 `plan_pipeline.py` 的 RowCountEstimator / PlanRuleComposer / PlanIntegrityChecker 为工作负载目标分支；LLM 增强保持"建议"定位，不进入确定性路径（总方案 §2.4 AI 与执行隔离）。

**复用**：`plan_pipeline.py` 全部组件、`endpoint_index_parser.py`/`call_graph_analyzer.py` 提供的访问事实。
**验收**：每表规模有 `explicit/production/derived/estimated` 来源标注与推导说明；冲突明确失败；容量估算经试装载校准。

### R4（对应 P4）：确定性数据生成内核（改造面最大的一阶段）

**改造点**
- 新建 `ptde/generator/`，三层结构（总方案 §7.4）：Primitive Generators（收编 `rule_engine.py`）→ Distribution Samplers（补 Normal/LogNormal/Zipf/Temporal/Conditional/Correlated/NullRatio/Length，覆盖 G5）→ Relationship Generators（泛型化 `id_pool.py`，支持 bigint/UUID/复合键/预分配 ID 区间，去掉 50 万与整数限制）。
- 条件分布支持：`P(paid_at | status)` 型字段依赖图（总方案 §7.3），字段生成顺序由依赖图决定。
- 确定性 Seed：全链路可复现——Seed + 生成器版本 + 分片算法版本 + 画像快照统一记录进 ExecutionManifest（总方案 §12.4）。
- 执行模式：文件中转（staging 分片文件）与流式直传（有界缓冲）双模式（总方案 §8.2），共用分片元数据与检查点协议；生成 Worker 多进程化，与装载解耦（不持有数据库连接）。
- 文件格式：按方言能力定义 CSV/TSV/原生格式，明确 NULL/转义/时区/Decimal/LOB/校验和（总方案 §8.3）。
- 循环依赖：`topo_sorter.py` 升级为 Tarjan SCC + 显式安全策略（可空字段回填 / 延迟约束 / 分阶段装载），无安全方式则阻止执行并输出原因，废除静默剔边。

**复用**：`rule_engine.py`、`id_pool.py` 采样逻辑、`topo_sorter.py`、`executor.py`（转为 Loader 侧）。
**验收**：相同输入复现相同逻辑数据；满足字段/关系约束；百万级数据不要求全量常驻内存；legacy 管线并行可用作对照。

### R5（对应 P5）：多数据库批量装载

**改造点**
- 新建 `ptde/adapters/`：Capability Probe 按产品/版本/兼容模式/权限/部署形态探测（总方案 §10.1），输出 Adapter Capability 快照（可审计、非硬编码）。
- MySQL 实现 `LOAD DATA LOCAL INFILE` Bulk Load Adapter，PostgreSQL 实现 `COPY FROM STDIN`；`DbApiBatchExecutor` 降为通用降级档，降级链与告警按总方案 §10.3。
- 装载前目标表检查（触发器/生成列/约束/分区/LOB/自增，总方案 §9.4）；并发与批次经 Pilot Run 校准，不设通用固定值（总方案 §9.5）。
- 分片检查点与断点恢复：每 Shard/Chunk 记录期望行数/实际行数/校验摘要/提交状态，恢复前核对实际提交（总方案 §9.6）。
- **执行前安全门禁（G11 剩余部分，总方案 §13.1/13.3）**：环境识别（产品/实例/Schema/环境用途，不得凭名称猜测）、授权与写入权限校验、目标 Schema 隔离策略确认、磁盘/日志/资源预算检查、破坏性操作（DDL/TRUNCATE/DROP/清理）显式授权与审计；门禁作为装载与清理的统一前置检查，生产库与无法识别的环境禁止写入。

**复用**：`executor.py`、`dialects/*` 的 URL 与连接处理、SPI 注册机制。
**验收**：导入结果可核对；失败可安全恢复；不静默丢弃错误记录；原生能力受限时说明降级原因。

### R6（对应 P6）：完整验证与数据集管理

**改造点**
- 新建 `ptde/validator/`：五层验证（数量/结构关系/分布/业务不变量/数据库就绪，总方案 §11）；业务不变量只接受 CONFIRMED 来源；分布验证区分统计容差与精确配额。
- 新建 `ptde/lifecycle/`：DatasetDefinition/Instance 分离；状态机驱动构建/恢复/清理；Manifest 汇总 §12.4 全部可复现要素；ValidationReport 落库。
- 清理策略按优先级实现（独立 Schema > 独立库 > 分区 > 所有权标记分批，总方案 §12.5）；共享表无归属标记时阻止清理；大规模逐行 DELETE 不作默认方式。
- 清理与验证在 §6.1 快赢项（R1/R2 并行交付）中已有最小版；本阶段交付完整版并收编最小版实现。

**复用**：`datagen_service.py` 的 selfCheck（收编为数据库就绪验证的一个子项）、`datagen_repo.py` 表结构风格。
**验收**：仅约定验证项全部满足才进 READY；清理只作用于本系统拥有且可确认归属的数据；全部结果可追溯。

### R7（对应 P7）：平台集成、规模扩展与 legacy 接管

**改造点**
- CLI：`perf ptde analyze / profile / plan / dataset build|status|verify|cleanup` 命令族（总方案 §15.1），支持 `--dry-run/--seed/--resume`；生产环境不提供构建与清理入口。
- API：新增 `/api/datasets/*` 端点（总方案 §15.2），异步任务执行，权限/幂等/审计；**不改 `/api/datagen` 既有契约**（契约测试保障）。
- 压测任务绑定：`stress_*` 任务记录 DatasetInstance ID + Definition 版本 + Schema 指纹 + 验证结果，压测报告可反查数据集（总方案 §15.3）。
- 可观测性：吞吐/资源/恢复时长指标 + 分片级结构化日志（总方案 §16.1/16.2）。
- 分布式 Worker 仅在单机吞吐确实不足时启用（总方案 §14.2）。

**legacy 接管与退役（A4a 终态，本阶段收官项）**
1. 双跑对照期结束且满足两道门禁——契约测试全绿 + 同规格双跑结果一致（行数/关联/分布）——后执行切换。
2. `perf datagen` CLI 命令形状不变，实现路由到 PTDE 管线（legacy 钳制口径随 A1 归一移除）。
3. `/api/datagen` 请求/响应契约形状不变，服务端实现替换为 PTDE；`perf-web` 零改动。
4. 存量数据迁移：`data_gen_task` / `semantic_hint` / `data_gen_plan` 迁入 ptde 新表；旧表转只读归档，保留一段回溯期后可下线。
5. 删除 `perf-py/datagen/` 包；原 `datagen/` 专属测试并入 ptde 测试体系。

**验收**：压测任务能绑定和复用数据集；压测结果能反查数据集版本；任务取消/重试/清理完整可控；**切换后全部现有入口行为与契约不变，legacy 代码清零**。

### 6.1 短期快赢项（可从 R4/R5/R6 提前拆出的独立小项）

以下项不依赖新架构，可在 R1/R2 期间并行实施，提前兑现价值：

1. **Seed 可复现**：给现有 `rule_engine.py`/`id_pool.py` 接入 `random.Random(seed)` 实例注入（改动小，价值高）。
2. **数据清理**：基于现有 `data_gen_task` 记录灌数范围，提供按任务清理（先只支持"整库/整 Schema 重建"与"按任务 ID 标记删除"两档）。
3. **生成后验证最小集**：行数核对 + 外键孤儿抽检 + 枚举值合法率，输出简化 ValidationReport。
4. **MySQL 原生装载 PoC**：单方言验证 `LOAD DATA LOCAL INFILE` 收益，为 R5 提供实测依据。

## 7. 关键架构决策记录（已评审确认，2026-11-16）

| # | 决策 | 现状与方案的冲突 | **确认结论** | 关键约束 |
|---|---|---|---|---|
| A1 | 规模上限 | 现状总行数硬钳 100 万（`default_engine.py` `_validate`）vs 方案"无固定上限、约束求解失败显式报错" | **分轨处理**：legacy 管线保留 100 万钳制；新 PTDE 管线无钳制、按约束求解显式失败；R7 legacy 退役后口径归一 | 过渡期两套口径需在文档/CLI 帮助中明确说明 |
| A2 | 生成内核语言 | 现状纯 Python 单线程 vs 方案建议高吞吐核心可选 Go/Rust/Java | **Python 多进程 + 语言无关协议预留**：分片文件格式与 worker 协议按语言无关设计并版本化；R4 末 Pilot Run 实测不达标再补编译 worker | Pilot Run 验收基准示例：百万级 < 30 分钟；协议不做 Python 专属假设 |
| A3 | 静默降级废除程度 | 环剔除、源码解析失败、PG 元数据残缺均静默跳过 vs 方案"显式状态、不得伪装成功" | **分级显式**：硬约束类（主键/外键/唯一/元数据残缺）缺失 → 显式失败；推断类（naming 关联、注解 SQL、枚举猜测）→ 显式 warning + 进 `semantic_hint` 采纳流，不阻断；提供"严格模式"开关收紧为全阻断 | 需维护硬约束/推断项分级清单，随 R1 落地；默认档位经真实项目试跑校准 |
| A4 | 新旧管线关系 | 方案要求独立子系统 vs 现有 `/api/datagen` 契约与存量测试 | **新建 `perf-py/ptde/` 独立子包 + 共享组件前置抽取，最终取代 datagen**：R0 第一步把模型基类、IdPool、枚举发现抽到共享包，两边引用；legacy 仅改 import，行为不变；**终态（R7 收官）PTDE 接管全部现有入口并退役 legacy**，详见 A4a 终态决策 | 前置重构纳入 R0 验收；契约测试全程绿色是合入条件 |
| A4a | 终态接管方式（A4 补充） | PTDE 与 datagen 的最终归属 | **PTDE 最终取代现有 datagen，成为唯一数据准备引擎**：① `perf datagen` CLI 命令形状不变，实现切换到 PTDE 管线；② `/api/datagen` 请求/响应契约形状不变，实现替换为 PTDE（前端 `perf-web` 零改动）；③ `data_gen_task/semantic_hint/data_gen_plan` 存量数据迁移到 ptde 新表，旧表只读归档；④ `datagen/` 包在迁移验证通过后删除 | 切换必须通过"契约测试全绿 + 双跑对照一致"两道门禁；旧入口保留只读查询一段时间供回溯 |
| A5 | 凭据分离 | 现状单凭据读写 vs 方案统计只读/测试库写入分离 | **双 DSN + 缺省回退**：配置层分 `profile_dsn`（只读采集）/ `target_dsn`（测试库写入）；未配置 `profile_dsn` 时回退同值并输出显式告警；生产环境策略文档强制要求分离 | 告警等级为 warning 且记入执行清单；统计采集记录所用凭据身份（脱敏） |
| A6 | 模型形态 | 方案示例为 YAML；项目现状为 JSON（spec.json / 规则 JSON）+ SQLAlchemy 落库 | **内部 pydantic 模型（版本+指纹）+ 对外 YAML/JSON 双读 + SQLAlchemy 落库**：内部模型为单一事实源；pydantic 已随 FastAPI 存在，零新增依赖 | 所有模型带 `version` 与 `fingerprint`；YAML/JSON 解析统一入口，落库沿用现有 SQLAlchemy 风格 |

## 8. 兼容与回归保障

1. **契约不变**：`/api/datagen` 13 个端点与 `perf datagen run` 的 spec.json 形状全程不变（`tests/contract/` 为准）；PTDE 新能力走新端点/新命令。**R7 收官时实现切换为 PTDE，契约形状仍不变**（终态见 §7 A4a）。
2. **旧管线过渡期保留**：`datagen/` legacy 管线在 R7 接管切换前始终可用；`build_default_engine` 装配不变；切换后按 A4a 退役删除。
3. **测试策略**：每阶段新增模块单测按现有 `tests/test_datagen_*.py` 风格；R4 起 legacy 与新管线对同一 DatasetSpec 双跑，行数/关联/分布结果对照；契约测试全程绿色是每个阶段的准入条件，也是 R7 切换的门禁。
4. **数据库迁移**：新表一律追加进 `sql/init.sql`，保持现有幂等迁移风格，不修改既有表结构（`data_gen_task` 等仅加列时用幂等 `ADD COLUMN IF NOT EXISTS` 风格）；R7 存量迁移脚本保持幂等可重放。

## 9. 风险与依赖

| 风险 | 影响 | 应对 |
|---|---|---|
| R4 改造面最大，生成内核重写可能长期双轨 | 维护成本 | R4 拆分为：分布采样器 → 关系生成器泛型化 → 分片并行三步，每步独立交付进 `ptde/` 新管线；共享组件（A4）保证不与 legacy 重复 |
| 统计画像依赖生产库访问权限 | R2 无法实测 | 先实现文件来源 + 测试库自画像，生产采集在拿到只读凭据后补验 |
| Python 生成吞吐不达标 | A2 决策反转 | R4 末尾用 Pilot Run 实测；预留分片文件格式与 worker 协议，便于替换 Go/Rust worker |
| 契约测试覆盖有限，破坏兼容未被发现 | 集成回归 | R0 起为旧引擎关键行为补齐契约用例后再动工 |
| 清理作用于共享库 | 误删 | 严格按总方案 §12.5：无所有权标记即阻止，默认只支持独立 Schema/库级清理 |

## 10. 里程碑与验收总览

| 阶段 | 交付物 | 关键验收（对齐总方案 §17） |
|---|---|---|
| R0 | ptde/models + 状态机 + 新表 | 模型有版本指纹、Schema 化、程序化状态机；契约绿色 |
| R1 | analyzer + 三态证据 + Schema 扩展 | 真实项目实体/约束/访问模式有证据；未知项显式标记 |
| R2 | profiler + merger | 无明文导出；画像带采集元数据；硬约束不被覆盖 |
| R3 | planner（5 目标） | 每表规模有来源；冲突显式失败；容量经校准 |
| R4 | generator 三层 + Seed + 分片 | 同输入复现；百万级不全量驻内存 |
| R5 | loader（探测/原生装载/恢复） | 结果可核对；失败可恢复；降级有说明 |
| R6 | validator 五层 + lifecycle | READY 需全部约定验证通过；清理可控 |
| R7 | CLI/API + 压测绑定 + 监控 + legacy 接管退役（A4a：入口切换/存量迁移/删除 datagen 包） | 数据集可绑定复用、可反查、可审计；切换后契约与行为不变，legacy 清零 |

---

**结论**：现状引擎是 PTDE 的合法起点而非障碍——关联解析、语义提示采纳、接口驱动规划、方言 SPI 均可直接收编。结构性缺口集中在：统计画像（G2）、统一模型（G3）、生成内核（G5/G6）、验证与生命周期（G9/G10）。§7 六个决策点已评审确认，可按 R0→R7 推进；R0（统一模型 + 共享组件前置抽取，含 A4 约束）与短期快赢项（§6.1）可并行启动。
