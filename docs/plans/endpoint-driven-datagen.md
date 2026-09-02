# 接口驱动的数据准备 — 实施与验收报告

> 需求原文：现在的数据准备是针对被测项目整体，我想到一个优化点，可以根据压测接口，分析源代码，判断需要对哪些表进行数据准备，判断数据如何构成，如何造数据
>
> ISSUE：https://cnb.cool/wymix.top/PerformanceTestor/-/issues/10

## 一、做了什么

把数据准备从「按被测项目整体」升级为「**按压测接口驱动**」：在压测中心选好接口后，静态分析源码调用链，自动推导「该准备哪些表、每张表数据如何构成、各造多少行」，生成可落库、可回看、可重复执行的数据方案，跳到数据准备页先试运行再正式执行。

改造前 `repoId` 只用于把整个项目的 `.java/.xml`（上限 200 个文件）喂给引擎做魔法值扫描；改造后按接口精确收口，每条结论都带 `文件:行号` 证据。

## 二、架构：解析下沉、编排留在宿主

```
perf-web    StressForm「按接口准备数据」 → StressCenterView 跳转 → DataGenForm 方案回显卡
perf-server EndpointDataPlanService（编排）→ RowCountEstimator / RtBaselineResolver /
            PlanRuleComposer / SeqStartResolver / PlanIntegrityChecker / LlmPlanEnhancer
            + data_gen_plan 方案表（落库回看）
perf-datagen EndpointIndexParser → CallGraphAnalyzer（BFS）→ MyBatisSqlResolver
            → SqlTableExtractor / SqlPredicateExtractor / RuleEngine(SEQ)
```

JavaParser 调用链分析是可复用纯解析，放在 perf-datagen（无状态、不落库、不引 Spring/JDBC）；方案合成、行数推导、落库、LLM 增强、HTTP 暴露属宿主职责，放在 perf-server。**零新增第三方依赖**（`javaparser-core` 本就是该模块唯一 compile 依赖）。

## 三、关键实现

### 3.1 接口定位（双解析器一致性）

宿主已有正则实现 `EndpointScanner`（压测中心选接口依赖它，改动有回归风险），因此**不替换**，另加 AST 实现 `EndpointIndexParser`。两者共用 `EndpointPath` 单一归一化工具，并由 `EndpointPathConsistencyTest` 逐条断言 `httpMethod + path + className` 三者一致。

- `className` 统一为 **simpleName（文件名去 .java）**，不是 FQCN —— 否则第③层 `classFile#methodName` 匹配对不齐
- `{id}` 路径变量原样保留（替换会与 Spring 的 `/{id}` 语义错位）
- 匹配顺序：① `(httpMethod, path)` 精确 ② 忽略方法 ③ `classFile#methodName`；降级层级写入 warnings 便于定位

### 3.2 调用链 BFS（按需展开 + 四重熔断）

不做全仓库建图（大仓库是分钟级），从 Controller 方法出发逐层展开，受 **深度 4 / 文件 300 / 节点 20 万 / 超时 15s** 保护。终止于 Mapper 调用（`@Mapper`、`@Select` 等、或类名以 `Mapper`/`Dao`/`Repository` 结尾）。接口多实现时**全部纳入并集**（confidence 记 0.6），避免漏表。

**告警通道的洁净度**：初版把 `Math.min(...)`、`UUID.randomUUID()`、`list.size()` 都报成「未覆盖」，几十条噪声会淹没真正需要用户处理的问题。已改为：

- 无法静态确定的调用（JDK 静态工具 / 容器方法 / `var` 推导）→ **静默跳过**，与数据库访问无关
- 只对「疑似业务类型」（`Service`/`Manager`/`Mapper`/`Client`/`Handler` 等后缀）无源码时报未覆盖
- 空表清单时明确提示「未从源码分析出该接口涉及的表…请手动补表」，不让用户面对空白页

### 3.3 表名与读写类型：宁缺勿滥

`SqlTableExtractor` 只接受确定形态（单表 `FROM/JOIN/UPDATE/INSERT INTO`，可带别名与库前缀）。子查询、CTE、多表 UPDATE **不猜**，把「文件:行号 + 片段」写进 unresolved 与 warnings。理由：对错误的表造数据（可能污染业务表）危害远大于漏表，而漏表有明确出口。

### 3.4 行数推导（并发 × 时长 × QPS）

```
peakQps      = qpsLimit > 0 ? qpsLimit : concurrency * (1000 / assumedRtMs)
totalRequests= Σ stages(peakQps × durationSec)，clamp[1, 1e9]
场景模式      = stepRequests_i = totalRequests × w_i / Σw
READ  主表    = clamp(ceil(req × 0.1), 1000, 200000)，且 ≥ pageSize × 20
WRITE 主表    = clamp(ceil(req × 1.0), 1000, 200000)
父表          = clamp(ceil(子表 / 10), 100, 200000)
合并          = 同表取 max（避免放大），谓词值集取 union（避免漏造）
全局          = Σrows > 100 万时按比例缩减并告警
```

`assumedRtMs` **三级取值**（固定 50ms 在慢接口上会把请求量高估几十倍）：① 同场景历史压测 `avgMs`（退化 `p95Ms`）② 该任务采样点 `p50` 均值 ③ 缺省 50，clamp[5, 5000]。取值来源写入方案 `rtSource` 并在前端标注，**避免用户误以为行数都是精算值**。

### 3.5 数据构成 → FieldRule

| 信号 | 规则 |
| --- | --- |
| WHERE 等值 / IN | `enum` + values；命中值（来自谓词）共占 80%，枚举建议值作为补充值均分 20% |
| 范围谓词 | `range(min,max)` |
| JOIN / 按 id 详情 | `ref`；详情类改 `refSkew`（hotPercent 20 / hotShare 80） |
| 唯一索引 / 业务编码列 | `seq`（**只落 `seqPrefix`**） |
| 值集超 64 | 纯数值转 `range`，混合类型均匀采样保留 64 个，并告警 |

#### 外键引用（ref/refSkew）的两级证据

ref 规则是「父表补齐」与「ref 池热点倾斜」的前提，方向判定分两级：

1. **JOIN ON**（置信度 0.9 / 0.7）：解析 `join t [as] b on x.col = y.col`，方向按
   ① 外键命名（`o.user_id = u.id` → `perf_order.user_id` 指向 `perf_user`）② 主键侧（`= u.id`）
   ③ 兜底「FROM 侧为子表」依次判定。
2. **外键命名兜底**（置信度 0.6）：仅在无 JOIN ON 证据时启用，覆盖 N+1 这类
   「主表与子表出现在不同 SQL」的写法——先跨 SQL 累积各表被引用到的列名（单表 SQL
   额外收非限定列），再用 `xxx_id ↔ 表名` 匹配（支持去 `perf_`/`t_` 前缀与复数）。
   匹配不到对应表时**不凭空造父表**。

perf-demo 实测：`GET /api/user/list`（N+1）产出
`{"user_id":{"type":"ref","table":"perf_user","column":"id"}}`。

### 3.6 三个评审要点的落地

**① 灌数顺序 + 父表必须入清单（高）**

引擎已有保障：`TableSpec.fields` 的 `ref` → `ExplicitRuleHints` → `SRC_EXPLICIT`（priority 40）→ `autoEdges` → `buildDependencies` + `TopoSorter.sort`，父表自动先灌，故 `runFromPlan` 不自排序。

但顺着链路发现一个**更危险的静默失效**：父表不在 `tables` 清单时，`ExplicitRuleHints.extract` 被 `metaMap.containsKey(parentTable)` 直接过滤，边丢失后 ref 池为空，生成逻辑退化为随机数，数据毫无关联而**全流程无任何报错**。

对策：`PlanIntegrityChecker` 在**构建 `DataGenSpec` 之前**把 ref 到的父表并入清单（时序不可调换——`metaMap` 由 `spec.getTables()` 构建，补晚了不生效），并按父表公式给行数、写告警。

**② seqStart 执行时刷新（高）**

方案只存 `seqPrefix`，`execute` 时由 `SeqStartResolver` 探测 `MAX(col)+1` 写入本次请求。这是「方案可重复执行」的前提——固化起始值第二次执行必然撞唯一键。引擎侧 `SeqRuleTest` 用 H2 唯一约束验证了「连续两批、第二批 seqStart 已刷新 → 400 行全部插入成功」。

**③ 环依赖降级**

捕获引擎 `IllegalArgumentException`（`TopoSorter` 消息已定位环中表名）→ 剔除环中**置信度最低**的一条 ref 边 → 重试一次（硬编码上限 1，复用同一 `dryRun` 与 `EngineListener`，避免重复告警）→ 仍失败写 warnings 交用户手工调整，不让任务直接失败。

**④ LLM 脱敏边界**

`LlmPlanEnhancer.sanitize()` 只发结构元信息（表名/列名/类型/谓词算子形状/索引类型），值一律打码为 `<int>`/`<str:len=8>`/`<date>`；禁止 SQL 全文、源码、连接串、真实样本。`unresolvedReason` 可能来自 SQL 片段，额外经 `redact()` 剥离引号串、长数字与 `jdbc:` 连接串后再外发。

`LlmPlanEnhancerSanitizeTest` 用**哨兵值**断言：在事实中埋入 `ORD_20260902_99887766`、`13800001111`、SQL 全文、连接串，验证提示词中绝不出现。

## 四、验收结果

### 4.1 perf-demo 真实源码端到端（15 项全通过）

读取仓库里真实的 `perf-demo/src/main/java`（构造器注入 + MyBatis 注解 SQL + N+1 循环），压测参数 concurrency=50 / duration=120：

| 接口 | 推导结果 | 证据 |
| --- | --- | --- |
| `GET /api/order/list` | `perf_order` READ 12000 行 | `OrderService.java:24` |
| `GET /api/order/stats` | `perf_order` READ 12000 行 | `OrderService.java:29` |
| `GET /api/order/recent` | `perf_order` READ 12000 行 | `OrderService.java:34` |
| `GET /api/user/list` | `perf_user` READ 6000 行 + `perf_order` READ 6000 行（`user_id` → `perf_user.id` 的 ref 规则） | `UserService.java:31`、`UserService.java:35` |
| `POST /api/scenario/order` | 空清单 + 「未从源码分析出…请手动补表」 | 该 Controller 刻意无 DB 访问 |
| `GET /api/order/notExist` | 空清单 + 「接口未定位」 | — |

要点：

- **N+1 双表都覆盖**：`UserService.listUsersWithOrderCount` 里 `selectAll`(perf_user) 与循环内 `countOrders`(perf_order) 都被抓到，没有因为循环嵌套而漏表
- **行数由压测参数推导**：并发 50 × 时长 120、RT 50ms → peakQps=1000 → 请求量 120000 → 读表 12000 行（非固定默认值）
- **证据可定位**：落在 Service 层的调用点，不是 Mapper 声明处
- **无 DB 访问的接口不报错**，而是给出可操作的提示

### 4.2 单元测试覆盖

| 模块 | 用例数 | 覆盖重点 |
| --- | --- | --- |
| perf-datagen | 160 | 调用链 BFS（字段/构造器注入、多实现、熔断）、接口索引、SQL 表/谓词/JOIN 边/列抽取、双解析器一致性、SEQ 唯一性（H2） |
| perf-server | 432 | 行数推导（读/写/父表/权重对齐/分页/上限缩减）、RT 三级取值、规则合成（权重/64 上限/seq 只落 prefix/ref-refSkew）、seqStart 刷新（含方言）、父表补齐与环降级、LLM 脱敏哨兵与合入、perf-demo E2E、外键引用闭环 E2E、三级匹配与权重占位回归、方案执行数据一致性 |
| perf-agent | 27 | 既有用例，无回归 |

合计 **619 项全通过**，`mvn test` 零回归。

### 4.3 验收标准的达成情况

| # | 标准 | 结果 |
| --- | --- | --- |
| 1 | 四类接口推导表与读写类型 + 证据可定位 | ✅ 见 4.1 |
| 2 | 行数按压测参数推导、超 100 万缩减并告警 | ✅ `RowCountEstimatorTest` |
| 3 | 查询接口造的数据能被查到 | ⚠️ 需真实 MySQL 环境联调，单测层已保证谓词→枚举值集与权重正确 |
| 4 | 写接口无主键/唯一键冲突 | ✅ `SeqRuleTest`（H2 唯一约束，连续两批 400 行） |
| 5 | 方案可回看、可重复执行；无 Key 降级 | ✅ 落库 + `SeqStartResolver`；`LlmPlanEnhancerSanitizeTest` |
| 6 | ref 池覆盖（子表外键在父表 ID 内） | ✅ 父表补齐 + 引擎拓扑排序；写入链路需真实库联调复核 |
| 7 | 重复执行无唯一键冲突 | ✅ `SeqRuleTest` |
| 8 | 双解析器一致 | ✅ `EndpointPathConsistencyTest` |
| 9 | 子查询不误造表 | ✅ `SqlTableExtractorTest` |
| 10 | LLM 脱敏 | ✅ `LlmPlanEnhancerSanitizeTest` |

**说明**：perf-demo 的接口全部为 GET（无数据库写入的 POST），故写链路（INSERT/UPDATE）由合成源码夹具验证（`EndpointDataPlanServiceTest#createEndpointResolvesWriteAccess` 覆盖了 `perf_order` WRITE 与 `perf_user` UPDATE）；标准 3、6 的端到端确认需在真实 MySQL + perf-demo 运行实例上联调完成。

## 五、变更清单

**perf-datagen（纯解析，无新增依赖）**

- `endpoint/EndpointIndexParser.java` [NEW] AST 接口索引
- `endpoint/CallGraphAnalyzer.java` [NEW] BFS 调用链
- `endpoint/MyBatisSqlResolver.java` [NEW] Mapper → SQL（XML + 注解，含字符串拼接）
- `util/EndpointPath.java` [NEW] 路径归一化单点
- `util/SqlTableExtractor.java` / `util/SqlPredicateExtractor.java` [NEW]
- `model/EndpointRef.java` / `model/EndpointTableFact.java` [NEW]
- `model/FieldRule.java` / `engine/RuleEngine.java` [MODIFY] SEQ 规则

**perf-server（编排与持久化）**

- `datagen/plan/` [NEW] `EndpointDataPlanService`、`RowCountEstimator`、`RtBaselineResolver`、`PlanRuleComposer`、`SeqStartResolver`、`PlanIntegrityChecker`、`LlmPlanEnhancer`
- `entity/DataGenPlan.java` + `mapper/DataGenPlanMapper.{java,xml}` [NEW]
- `controller/DataGenController.java` [MODIFY] 方案 6 个端点
- `datagen/DataGenService.java` [MODIFY] `runFromPlan`（严格时序）
- `datagen/DataGenSpecAdapter.java` [MODIFY] 支持 seq 规则 JSON
- `source/SourceFileProvider.java` [MODIFY] 新增 `loadFilesByPaths`
- `source/EndpointScanner.java` [MODIFY] 归一化委托单点工具（**对外产出契约不变**）

**SQL / 前端 / 文档**

- `sql/init.sql`、`sql/init_empty.sql` [MODIFY] 幂等新增 `data_gen_plan`
- `perf-web/src/api/index.ts` [MODIFY] `dataGenPlanApi`
- `components/stress/StressForm.vue` [MODIFY] 「按接口准备数据」按钮 + `prepare-data` 事件
- `views/StressCenterView.vue` [MODIFY] 分析并跳转 datagen Tab
- `components/datagen/DataGenForm.vue` [MODIFY] `initialPlanId` + 方案回显卡（行数可编辑、证据折叠、告警条、试运行/正式灌数）
- `style.css` [MODIFY] `btn-ghost` 描边幽灵按钮

## 五之二、PR 评审修复（PR #13 行级评审）

评审指出两处「实现与 PR 描述不符」，修复过程中又连带发现一处更隐蔽的数据丢失问题。

**① `contexts` 恒空、`refRule()` 无调用点 → ref/refSkew 能力空转**

`doAnalyze` 里 `contexts` 始终是 `new LinkedHashMap<>()`，`PlanRuleComposer.refRule()` 在 main 源码中无调用点。后果是 JOIN/详情类查询**不会**生成 ref/refSkew 规则，连带 `PlanIntegrityChecker` 的补父表逻辑也永远不触发——描述里「评审关键问题①的对策」实际是死代码。

修复：新增外键引用两级证据链（`SqlTableExtractor.extractJoinEdges` 解析 JOIN ON 判定方向 + `resolveFkByNaming` 跨 SQL 用 `xxx_id ↔ 表名` 兜底），`EndpointTableFact` 承载 `joinRefs`/`columns`/`detailLookup`，`doAnalyze` 按事实填充 `TableContext`，`PlanRuleComposer` 据此生成 ref/refSkew。新增 `JoinRefE2ETest`（9 项）守住闭环，并用「任何 ref 目标都必须在最终表清单中」这一不变量做断言。

**② 权重与 demands 维度不匹配 → 多接口权重分摊恒退化为均分**

`splitByWeight(totalRequests, weights, demands.size())` 的第三个参数是**表访问次数**（一张表多次访问产生多条 demand），而 `weights` 长度是**接口数**。`weights.size() == count` 几乎恒为 false（除非恰好 1:1），权重恒走均分分支。

修复：`TableDemand` 增加 `endpointIndex`，`estimate` 先把接口级权重按 `endpointIndex` 展开为表访问级权重，再分摊。新增 `endpointWeightsApplyWhenDemandsOutnumberEndpoints` 等 4 项回归用例。

**③ 方案规则 JSON 回读时被转义 → 执行时规则被静默丢弃**

`parseTables` 用 `n.get("fields").toString()`，对 TextNode 返回的是**带引号与转义的 JSON 字面量**（`"{\"type\":\"ref\"}"`）；下游 `DataGenSpecAdapter.parseFields` 的 `readTree` 会解析成 TextNode 而非 ObjectNode，`root.fields()` 为空 → **所有规则静默丢失**，灌数时退化成按列类型随机生成。改用 `asText()`（非文本节点仍用 `toString()` 兜底）。

三处的共同点都是「**不报错的静默失败**」——功能看起来在跑，实际没有生效。每处都补了针对性单测。

## 五之三、复审修复（PR #13 第二轮，含代码规范维度）

复审结论：4 warning + 6 info，全部修复。

**warning**

1. **权重与接口下标错位**（我上一轮修复引入的新 bug）：接口未定位时 `endpointIndex++` 但未 `weights.add(...)`，导致紧凑的 `weights` 与含空洞的 `endpointIndex` 错位，后续接口会取到**前一个接口**的权重——恰与修复②的意图相悖。改为未定位时也占位。
   回归用例：`unmatchedEndpointStillConsumesItsWeightSlot`（对比「未定位+高权重」与「仅高权重」两种输入的行数应一致）。
2. **`loadFilesByPaths` 是死代码，`doAnalyze` 仍走 200 上限**：分析场景改为 `loadFiles(repoId, ANALYSIS_LIMIT=5000)`，并新增 `countIndexed()` 在截断时显式告警——避免「分析不出表」被误判为「接口不查库」。原死代码方法删除。
3. **`match` javadoc 声称三级、实际只有两级**：补齐第③级「类#方法」兜底（`EndpointRef.classMemberKey()`），前端在 `buildPayload`/`buildScenario` 中上送端点扫描产出的 `className`/`methodName`。
4. **`DataGenForm.executePlan` 无 catch**：执行失败（连接校验不过、方案状态异常）时异常被吞、用户无感知，补 `catch` 并 `alert` 错误信息。

**info**

- 删除 `PlanIntegrityChecker` 的无效三元 `arrow >= 0 ? message : message`
- 删除 `SqlPredicateExtractor` 末尾「无论真假都返回」的死条件块，意图上移到 javadoc
- `PlanRuleComposer` 循环内 `new ObjectMapper()` → 静态复用（该类线程安全、创建开销大）
- `"seq"` 魔法字符串 → `FieldRule.TYPE_SEQ`（含 `DataGenService` 里遗漏的一处）
- `CallGraphAnalyzer` 的 `implements` 文本扫描 → **文本粗筛 + AST 精判**两级：纯文本会把注释/字符串里的 `implements` 误判且漏判 `implements A, B` 中非首位的类型；纯 AST 要对全仓库建 AST 会触发文件数熔断，故先用文本筛候选再用 `getImplementedTypes()` 确认
- `StressCenterView` 分档时长 `Math.floor` 丢余数（100s÷3 档 = 33×3 只跑 99s）→ 余数补到最后一档

## 五之四、终审修复（PR #13 第三轮）

终审结论：2 处数据一致性问题 + 2 处功能失配，全部修复；3 处改进建议一并处理。

**① `runFromPlan` 全字段 update 覆盖刚回写的 task_id/status**

`updateTaskId` 已把 `status` 置为 `executed` 并写入 `task_id`，随后用方法开头 `findById` 取出的旧对象走全字段 `update(plan)`——其 `taskId` 为 null、`status` 为旧值，会把刚回写的值冲掉，「方案 → 任务」追溯链路丢失。

改为专用 `updateWarnings(id, warningsJson)` 只更新告警字段。新增 `DataGenPlanExecutionTest`（10 项），核心断言是 `verify(planMapper, never()).update(any())`。

**② 前端编辑的行数不生效**

方案卡行数可编辑并打「已手动调整」角标，但 `execute` 只回传连接，后端按落库的 `tablesJson` 执行——用户改的行数只是前端状态，角标成为误导。

改为：`execute` 请求体带 `rowOverrides`（`[{table,rowCount}]`），后端 `runFromPlan` 应用覆盖值（同样受 `MAX_ROWS` 保护）。

**③ `LlmPlanEnhancer` 的 `applied=true` 实为 no-op**

LLM 返回解析后未合入任何结论却报「增强已应用」。

改为实现**真实合入**，但严格限定范围：**只补充已有列的枚举取值**，不接受新表、新列、新关联。理由——本项目对表识别坚持「宁缺勿滥」，让模型凭语义猜测表名等于把这条底线交出去（猜错会污染业务表）；补充取值则不然，列的存在性已由静态分析保证，取值只影响数据分布。合入数受 `MAX_MERGED_VALUES=32` 保护；**没有任何建议被采纳时返回 `applied=false`**，不再制造假象。

**④ `SeqStartResolver` 的 SQL 仅 MySQL 可用**

反引号定界 + `CAST(SUBSTRING(...) AS DECIMAL(38,0))` 在 PostgreSQL 上会报错，被 `queryMax` 吞掉后静默回退 `seqStart=1`——恰好撞上本类要防的唯一键冲突。

改为按 `DatabaseMetaData.getURL()` 判定方言：MySQL 用反引号 + `CAST(... AS DECIMAL)`，PG 用双引号 + `substring(... from N)::numeric`；元数据不可用时回退 MySQL 写法。

**改进建议（3）**

- `PlanRuleComposer` 注释编号重复（两个「4.」）→ 修正为 1~5
- `PlanIntegrityChecker` 环依赖降级未接入主流程（有实现无调用）→ 在 `DataGenService.runEngine` 中接入 `executeWithCycleFallback`：捕获含「循环依赖」的 `IllegalArgumentException` → 剔除最低置信度 ref 边 → **复用同一 listener 与 dryRun** 重建 spec 重试一次 → 仍失败写告警后抛出
- 前端 `loadPlan` 轮询存在叠加请求风险（慢响应时后到的旧响应覆盖新数据）→ 加 `planLoading` 防重入标志，`finally` 释放

## 六、新增 API

```
POST   /api/datagen/plans/analyze          # 按接口分析并落库（异步分析，立即返回方案）
GET    /api/datagen/plans                  # 方案列表（repoId + 时间倒序）
GET    /api/datagen/plans/{id}             # 方案详情（表清单/规则/证据/告警）
POST   /api/datagen/plans/{id}/adopt       # 采纳/放弃
POST   /api/datagen/plans/{id}/execute     # 按方案灌数（dryRun；请求体可带 connId/dbUrl）
DELETE /api/datagen/plans/{id}             # 删除方案
```

## 七、工程约束遵守情况

- 引擎模块无状态、不落库、不引 Spring/JDBC 驱动 ✅
- SQL 双份幂等（`init.sql` 与 `init_empty.sql`）✅
- 连接校验 `connId` 与 `dbUrl` 二选一：**分析阶段**禁止两者都给（允许都为空，分析只读源码），**执行阶段**强制二选一 ✅
- 向后兼容：`DataGenRequest`/`DataGenSpec`/引擎门面签名不变；`DataGenForm` 未传 `initialPlanId` 时行为完全同现状 ✅
- 前端沿用深色主题与既有交互范式，仅增量 UI ✅
