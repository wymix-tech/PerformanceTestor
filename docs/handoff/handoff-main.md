# PerformanceTestor 平台改进 — 工作交接文档

> 交接日期：2026-08-31（第四轮更新 / 最终版）
> 前任执行：AI 编码助手（CodeBuddy / Hy4）
> 交接范围：改进计划 M0–M6 **全部完成**（16/16 任务），已通过真实 MySQL + PostgreSQL 端到端验证
> 计划文档：`docs/plans/performance-improvement-plan-detail.md`（含勾选进度，本交接是其配套执行指南）
> 基线提交：`d49721f`（M3 完成）；第二轮提交见文末「本轮变更」

---

## 0. 最新轮次变更摘要（批次 2：P1 可观测性与防呆）

**结论：批次 2 三项（Actuator/自检、游标分页、repoId 防呆）落地，perf-server 563 测试全绿，前端构建 + 突变门禁 83.33 通过。**

| 条目 | 新增文件 / 改动 |
|---|---|
| P1-1 健康检查与自检 | `health/HealthProperties`（[NEW] `perf.health.*`）、`health/HealthCheckService`（[NEW] 库/表/分区/目录/磁盘五类探针）、`health/PlatformHealthIndicator`（[NEW] `/actuator/health` 的 `platform` 项）、`health/StartupSelfCheck`（[NEW] `ApplicationRunner`，默认只告警）、`health/HealthMapper(.xml)`（[NEW] 元数据只读回查）、`perf-server/pom.xml`（[MODIFY] +`spring-boot-starter-actuator`）、`application.yml`（[MODIFY] +`perf.health.*` / `management.*`）、`auth/WebAuthConfig`（[MODIFY] 注释说明 Actuator 不在 `/api/**` 之下） |
| P1-2 游标分页 | `common/page/CursorPage`（[NEW] 返回体）、`CursorPageRequest`（[NEW] size 默认 50/上限 500/旧 limit 兼容）、`CursorCodec`（[NEW] `Base64Url("epochMillis|id")`）、`service/MetricQueryService`（[MODIFY] `page()` 泛型 + 四个 `*Page` 方法）、`TraceController`/`SlowSqlController`/`SlowCommandController`/`JvmMetricController`（[MODIFY] 改游标入参）、`TraceChainMapper`/`JvmMetricMapper`/`SlowSqlMapper`/`SlowCommandMapper` 及对应 XML（[MODIFY] +`queryByCursor`）、`perf-web/src/api/index.ts`（[MODIFY] +`CursorPage`/`normalizePage`）、`TraceAnalyzerView`（[MODIFY] 「加载更多」+ `initProjects` 拆出）、`JvmMonitorView`（[MODIFY] `latestRow` 取最新值，不依赖数组末位）、`DashboardView`/`QuickStartView`（[MODIFY] 解包 `.items`） |
| P1-3 repoId 防呆 | `ai/tools/AnalyzeCodeIssuesTool`（[MODIFY] `repoId` 必填 + Schema `required`，缺失/≤0/不存在/回查异常一律不落库并回仓库清单；`Map.of` 改 `LinkedHashMap` 避免根因为 null 时 NPE） |

**接手必读（本轮口径）**：

- 分页排序键必须是**翻页期间稳定**的列，故慢 SQL/慢命令用 `execute_time` 而非页面热点的 `cost_ms`；`jvm_metric` 的图表查询保持 `ASC`，分页查询 `DESC`，两条语句分开。
- 游标是 `(time, id)` 复合键，条件为 `(time < ? OR (time = ? AND id < ?))`；**简化成 `time < ?` 会整批跳过同秒记录**（`CursorPagingSqlScriptTest` 已静态锁死）。
- `hasMore` 靠**多取一条**判定（底层取 `size+1`），游标取"实际返回的最后一条"而非多取的那条。
- 启动自检默认 `fail-fast=false`：容器编排下平台常先于 MySQL 启动，fail-fast 会把"库未就绪"变成"平台起不来"。AI 连通性**不进**健康检查（无 key 是正常形态）。
- 分页只改了四张时序表；`hotspot_method`/`/api/source/issues`/`/api/apps` 未纳入，理由见 `docs/plans/运维与数据生命周期加固方案.md` 批次 2 落地记录。

---

## 0. 第四轮变更摘要（2026-08-31 最终版，M6 端到端验证）

**结论：改进计划 M0–M6 全部 16 项任务完成**，通过真实 `mysql:8.0` + `postgres:16-alpine` 容器端到端验证，全量构建 228 测试全过。

**本轮（m6-e2e-verify）新增/修复**：

| 类型 | 新增文件 / 改动 |
|---|---|
| 缺陷① PG 驱动缺失 | 父 `pom.xml`（[MODIFY] 托管 `org.postgresql:postgresql:42.7.3`）；`perf-server/pom.xml`（[MODIFY] runtime 引入）；`perf-datagen/pom.xml`（[MODIFY] test 引入，供真实 PG 冒烟） |
| 缺陷② 枚举值未参与生成 | `DefaultDataGenEngine`（[MODIFY] 新增 `dbEnumValues` 缓存（键 `表名.列名`），`buildRow` 中未配置规则的列优先取 DB 发现值；`execute` 开头清空缓存防单例 Bean 跨任务串用） |
| 缺陷③ PG 严格类型 | `PostgreSQLDialect.enrichJdbcUrl`（[MODIFY] 追加 `stringtype=unspecified`，用户显式指定时不覆盖） |
| 真实 PG 冒烟 | `PostgreSQLDialectLiveTest`（[NEW] 5 例：元数据/SERIAL/`pg_enum` 原生枚举/CHECK 枚举/双引号计数）、`PostgresEnumGenerationLiveTest`（[NEW] 2 例：枚举列取值合法性 + 缓存不串用）；无 PG 实例时 `assumeTrue` 自动跳过 |

**后续接手必读（本轮踩坑）**：
- 容器内**无 mysql 客户端**，建库建表需走 `docker exec -i <容器> mysql -uroot -pxxx < sql/init.sql`。
- 后台起服务必须用 `setsid nohup ... < /dev/null & disown`，否则命令返回时进程被回收。
- 真实 PG 才能覆盖 `pg_enum` / `pg_catalog` / `SERIAL` 分支——H2 的 `MODE=PostgreSQL` 是模拟，这三处会静默降级，务必保留真机冒烟用例。
- `PostgreSQLDialectLiveTest` / `PostgresEnumGenerationLiveTest` 依赖 `PG_JDBC_URL`（默认 `jdbc:postgresql://127.0.0.1:5432/demo`），CI 无 PG 时自动跳过，不影响构建。

---

## 0.1 第三轮变更摘要（2026-08-31，接手后已完成）

**环境**：容器再次重建，`/opt/java`、`/opt/maven`、`perf-web/node_modules` 全部丢失，已按 `docs/handoff/environment-setup.md` 重装（JDK 17.0.20.1+1 / Maven 3.9.6 / 阿里云镜像 / npmmirror），基线验证通过（后端 144 测试 + 前端 vue-tsc + build 全绿）。

**已完成 m4-capacity-science**（14 新单测，后端累计 158 测试全绿）：

| 类型 | 新增文件 / 改动 |
|---|---|
| 拟合工具 | `capacity/CurveFitter`（[NEW] 二项式最小二乘解 3×3 正规方程组（高斯消元+列主元，零依赖）、R²=1-SSE/SST、a<0 时顶点 `c*=-b/2a` 为理论最大并发、非均匀网格二阶差分 RT 拐点检测、QPS 泊松 σ=√qps 与 P95 分位数反推 σ 的 95% 置信区间） |
| 分析器 | `CapacityAnalyzer`（[MODIFY] 拆为 `interpolate`（原判定逻辑零改动）+ `fillScience`（过载档截断、<3 档静默跳过、拟合曲线 24 点等距重采样）） |
| 结果模型 | `CapacityResult`（[MODIFY] 向后兼容新增 `model`/`r2`/`fittedCurve`/`confidenceInterval`/`rtKneeConcurrency` 与 `FitModel`/`FittedPoint`/`ConfidenceInterval` 三个内部类；`CurvePoint` 补 `p50Ms`/`totalRequests`） |
| 前端 | `components/stress/StressReport.vue`（[MODIFY] 容量卡片展示拟合公式/R²/理论拐点/RT 拐点 + 分档置信区间表；新增拟合曲线图：实测散点 + 拟合虚线 + 95% 置信带 + 理论拐点 diamond + RT 拐点 markLine） |

**已完成 m5-pg-dialect**（22 新单测，后端累计 184 测试全绿）：

| 类型 | 新增文件 / 改动 |
|---|---|
| PG 方言 | `dialect/postgres/PostgreSQLDialect`（[NEW] `supports(jdbc:postgresql:)` URL 路由、`reWriteBatchedInserts=true` 增强、JDBC `DatabaseMetaData` 元数据（columns/primaryKeys/importedKeys）、双引号标识符、`pg_enum` 原生枚举 + `pg_get_constraintdef` CHECK 文本解析双通道枚举发现、SERIAL/BIGSERIAL（`nextval(` 默认值）识别） |
| JPA 解析 | `orm/jpa/JpaSourceParser`（[NEW] `@Entity`/`@Table`/`@ManyToOne`/`@OneToOne`/`@JoinColumn`/`@ManyToMany`/`@JoinTable` → `relationHints`（SRC_ORM_JOIN，置信 0.9）；`mappedBy` 反向声明跳过；`order()=20` 排在 MyBatis 之后） |
| 公共片段 | `orm/support/JavaSourceHints`（[NEW] 枚举常量反查 / 状态常量魔法值 / 类名→表名转换，MyBatis 与 JPA 共用，避免两份实现漂移；`MyBatisSourceParser` 已改为委托调用） |
| 引擎路由 | `DefaultDataGenEngine`（[MODIFY] 单方言→`List<DatabaseDialect>`（按 `supports(jdbcUrl)` 路由）+ 单解析器→`List<OrmSourceParser>`（特征检测 + `order()` 排序合并）；保留原三参构造向后兼容；新增 `getDialects()` 供调用方复用同一套判别） |
| 装配 | `DataGenEngineConfig`（[MODIFY] 注册 `MySQLDialect`+`PostgreSQLDialect`、`MyBatisSourceParser`+`JpaSourceParser`） |
| 服务层 | `DataGenService`（[MODIFY] `validateDbUrl` 改为按已注册方言 `supports` 判别；`connect` 的 URL 增强由硬编码 MySQL 参数改为走方言 `enrichJdbcUrl`） |
| 单测 | `PostgreSQLDialectH2Test`（[NEW] H2 `MODE=PostgreSQL`，URL 路由/SQL 构建/元数据/行数 + 引擎 Dry-Run 与正式写入全链路）/ `JpaSourceParserTest`（[NEW] 关联/mappedBy/@JoinTable/枚举/容错）/ `DataGenServiceDialectRoutingTest`（[NEW] 多库种 URL 校验与增强路由） |

**已完成 m5-middleware**（21 新单测，后端累计 205 测试 + agent 27 全绿）：

| 类型 | 新增文件 / 改动 |
|---|---|
| 拦截公共逻辑 | `instrumentation/MiddlewareSupport`（[NEW] 命令渲染 + 阈值上报；与 JdbcInterceptor 同级低开销策略） |
| 拦截 Advice | `instrumentation/RedisInterceptor` / `EsInterceptor` / `MqInterceptor`（[NEW] 三个 Advice，仅转调 MiddlewareSupport） |
| 匹配注册 | `instrumentation/TraceEnhancer`（[MODIFY] 新增 `redisTypeMatcher`/`esTypeMatcher`/`mqTypeMatcher` 与 `middlewareMethods()` 统一方法过滤，受 `middlewareEnabled` 开关控制） |
| 配置 | `config/AgentConfig`（[MODIFY] +`middlewareEnabled`（默认 false）、+`slowCommandThresholdMs`（默认 100）及 fromArgs 解析） |
| 上报 | `reporter/MetricsReporter`（[MODIFY] +`TYPE_SLOW_COMMAND`、+`slowCommandQueue`、`reportSlowCommand()`、`dropped.slowCommand`、停机/未知项目时清队列）；`trace/TraceContext`（[MODIFY] +`currentTraceId()`） |
| 服务端 | `entity/SlowCommand` / `mapper/SlowCommandMapper(.xml)` / `service/MetricQueryService#slowCommands` / `controller/SlowCommandController`（[NEW] `GET /api/slow-commands`）；`collector/AgentReportService`（[MODIFY] +`saveSlowCommands`）；`DataCleanupMapper/Service`（[MODIFY] 级联清理） |
| 建表 | `sql/init.sql` + `sql/init_empty.sql`（[MODIFY] +`slow_command` 表） |
| 前端 | `views/TraceAnalyzerView.vue`（[MODIFY] 慢 SQL 列表 → **慢命令合并视图**：SQL/Redis/ES/MQ 统一按耗时倒序 + 类型徽标配色 + 类型筛选）；`api/index.ts`（[MODIFY] +`slowCommandApi`）；`views/ProjectManageView.vue`（[MODIFY] 清理提示计入慢命令） |
| 脚本 | `scripts/start-agent.sh`（[MODIFY] +`MIDDLEWARE_ENABLED` / `SLOW_COMMAND_THRESHOLD_MS`，仅开启时追加 agent 参数） |
| 单测 | `MiddlewareSupportTest`（[NEW] 渲染 8 例 + 阈值/上报 3 例 + 配置 3 例）/ `AgentReportServiceSlowCommandTest`（[NEW] 落库 7 例） |

**已完成 m5-report-export**（15 新单测，后端累计 220 测试 + agent 27 全绿）：

| 类型 | 新增文件 / 改动 |
|---|---|
| HTML 渲染 | `report/ReportHtmlRenderer`（[NEW] 自包含 HTML：内联 CSS（A4/白底黑字/表格边框/`page-break-inside:avoid` 防跨页断裂）、任务元信息 + 核心指标卡 + 响应时间分布 + 分档数据 + 容量评估（含拟合公式/R²/双拐点/95% CI 表）+ 扩容建议；`esc()` HTML 转义、`num()` 千分位格式化、缺失字段渲染 `-`） |
| 导出服务 | `report/ReportExportService`（[NEW] html/pdf/doc 三格式；PDF 走 openhtmltopdf + PDFBox，探测注册系统中文字体；Word 用 MHTML/Office XML 头 + `.doc`，零 POI 依赖；文件名非法字符清洗） |
| 导出 API | `controller/ReportExportController`（[NEW] `GET /api/reports/export?stressTaskId=&format=&printMode=`，返回 `ResponseEntity<byte[]>` 绕过 `ApiResponse` 包壳，RFC 5987 中文文件名） |
| 依赖 | 父 pom + `perf-server/pom.xml`（[MODIFY] 新增 `openhtmltopdf-core` / `openhtmltopdf-pdfbox` 1.0.10（阿里云镜像可拉取）） |
| 前端 | `components/stress/StressReport.vue`（[MODIFY] 头部新增「打印预览 / 导出 PDF / 导出 Word」）；`api/index.ts`（[MODIFY] +`reportApi.exportUrl`） |
| 单测 | `ReportExportServiceTest`（[NEW] 15 例：HTML 各区块/转义/空结果兜底/缺字段占位、PDF 魔数与空结果、Word 命名空间与 style 保留、文件名清洗、未知格式降级、任务不存在报错、千分位格式化） |

**关键坑（后续接手必读，踩过的都在这）**：
- **报告 HTML 必须是良构 XHTML**：openhtmltopdf 用 TRaX XML 解析器，`<meta charset="UTF-8">` 未自闭合会直接抛 `SAXParseException: 元素类型 "meta" 必须由匹配的结束标记终止`，PDF 生成失败。`<script>` 内容需用 `<![CDATA[...]]>` 包裹。
- **`PdfRendererBuilder.useFont` 签名是 5 参**：`(File, String family, Integer weight, FontStyle, boolean subset)`，1.0.10 没有 4 参重载。且必须在 `withHtmlContent` **之前**注册。
- **中文 PDF 需外挂字体**：PDFBox 内置 Base-14 字体无中文字形，已实现候选路径探测（wqy/noto/msyh/pingfang）；容器无中文字体时 PDF 中文会显示为空白方块——这是环境问题不是代码问题。
- **前端导出必须用裸 axios**：`api/http.ts` 的响应拦截器 `return response.data` 会解包，导致拿不到 `Content-Disposition`（文件名）；且 `window.open(url)` 直开不带 JWT 会被 401 跳登录，故打印预览改为 axios 取回 HTML 后 `document.write` 进新窗口再 `print()`。
- **导出接口不走 `ApiResponse` 包壳**：JSON 包壳会破坏文件字节流，直接返回 `ResponseEntity<byte[]>`。

**关键设计决策（m5-middleware，接手必读）**：
- **agent 零中间件依赖**：类型匹配全部用 `hasSuperType(named("io.lettuce.core.api.sync.RedisCommands"))` 这类「按类型名匹配」，不在 agent 里引入 lettuce/jedis/es/rabbitmq 任何依赖——类不存在时匹配器自然返回 false，故本任务**不需要**新增 pom 依赖。
- **ES 只拦 RestClient**：`RestHighLevelClient` 的 search/index/bulk 最终都汇聚到 `RestClient#performRequest`，只拦底层可避免高层 API 与底层调用重复计数。注意 `hasSuperType` 不含类型自身，`RestClient` 是具体类，需写成 `named("...RestClient").or(hasSuperType(named("...RestClient")))`。
- **复杂对象不调 toString**：ES Request、AMQP.BasicProperties 等对象 toString 可能触发大序列化，一律渲染为 `<SimpleClassName>`；`byte[]` 记 `<长度B> 前256字节解码`，既保留 MQ 消息体线索又不拖慢上报路径。
- **`middlewareEnabled` 默认 false**：中间件命令调用极高频（单次请求可达数十次 Redis 命令），常态开启开销不可接受，需定位时显式开启。
- **command 列 VARCHAR(1024)**：agent 侧截断 1000，服务侧落库前再截断一次（双保险），否则超长直接导致入库报错。

**关键坑（后续接手必读）**：
- H2 PG 兼容模式**不支持** `AUTO_INCREMENT`，DDL 需用 `GENERATED BY DEFAULT AS IDENTITY`（与 PG 10+ 一致）。
- H2 PG 模式**没有** `pg_catalog` / `pg_enum`，故原生枚举与 CHECK 枚举发现会静默降级——这两条分支需真实 PG 才能覆盖，单测改为直接测静态解析方法 `parseCheckDefinition`。
- `DefaultDataGenEngine` 原来把 MySQL 专有参数（`rewriteBatchedStatements`/`useInformationSchema`）硬编码拼到任意 URL 上，会污染 PG 连接串，已改为按方言路由 `enrichJdbcUrl`。
- JPA 表名推导：无 `@Table` 时按 `JavaSourceHints.toTableName` 剥离 `DO`/`Entity`/`PO` 后缀再 snake_case，如 `UserEntity` → `user`（不是 `user_entity`）。

**落地差异（与 4.3 原改造要点对照）**：
- 拟合相关字段**仅作解释性输出**，不参与既有插值判定，`maxConcurrency`/`maxQps`/`extrapolated` 口径零变化（历史报告兼容）。
- 置信区间的「该档采样点数」：QPS 用采样秒数（= `totalRequests / qps`，StagePoint 未落库时退化为 0 宽度），P95 用该档请求数；σ_QPS 按泊松到达取 √qps，σ_RT 由 `(P99-P95)/0.6814` 反推（缺 P99 退化为 `(P95-P50)/1.6449`）。
- 拟合要求 ≥3 个有效档位（错误率 >5% 的过载档不参与拟合），不满足时 `model=null` 且不影响判定结果。

---

## 0.1 第二轮变更摘要（2026-08-30，接手后已完成）

**环境**：容器已重建，`/opt/java`、`/opt/maven`、`perf-web/node_modules` 全部丢失，已按 `docs/handoff/environment-setup.md` 重装（JDK 17.0.20.1+1 / Maven 3.9.6 / 阿里云镜像 / npmmirror），基线验证通过（后端 102 测试 + 前端 vue-tsc 全绿）。

**已完成 m4-ai-upgrade**（33 新单测）：

| 类型 | 新增文件 / 改动 |
|---|---|
| 领域模型 | `ai/AiRole`（三角色）、`ai/ReactPhase`、`ai/ReactStep`（record，Thought/Action/Observation 留痕）、`ai/AiStreamEventType`、`ai/AiStreamEvent` |
| 流式 | `ai/StreamAccumulator`（合并 content delta + 按 index 分片合并 tool_calls）、`LlmClient#chatWithTools(messages, tools, tokenSink)`（stream=true，`HttpResponse.BodyHandlers.ofLines()`） |
| 流式接口 | `ai/AiChatStreamController`（`POST /api/ai/chat/stream`，SseEmitter，`perf.ai.stream-timeout-sec:300`） |
| 编排 | `AiAgentService` 重写为 ReAct 显式循环（Observation 截断 2000 字符、轮次上限 10 收尾摘要、结论自动入库、流式事件推送含轮次心跳） |
| 提示词 | `PromptBuilder` 重写：公共基座 + 三角色段落 + `detectRole(intent)` 路由；角色切换时 system prompt 原地替换（保留历史） |
| 知识库 | `entity/AiKnowledge` + `mapper/AiKnowledgeMapper(.xml)`（FULLTEXT ngram）+ `AiKnowledgeService`（入库门槛 40 字、检索降级到最近结论）+ `tools/SearchKnowledgeTool` + `GET /api/ai/knowledge` |
| 建表 | `sql/init.sql` / `sql/init_empty.sql` 同步新增 `ai_knowledge`（`FULLTEXT KEY ft_content (title, content) WITH PARSER ngram`），init.sql 另含 2 条演示结论 |
| 前端 | `AiAssistantView.vue` 改 fetch + ReadableStream 逐 token 渲染（SSE 帧解析、停止按钮、ReAct 过程留痕、角色与轮次展示）；`api/index.ts` 增 `aiApi.knowledge` |

**已完成 m4-fix-verify**（9 新单测）：

| 类型 | 新增文件 / 改动 |
|---|---|
| 建表 | `performance_issue` 加列 `stress_task_id BIGINT DEFAULT 0` / `verify_json MEDIUMTEXT` / `verify_time DATETIME`；两份脚本同步，并新增幂等补列存储过程 `add_column_if_missing`（MySQL 无 `ADD COLUMN IF NOT EXISTS`） |
| 实体/Mapper | `PerformanceIssue` 增三字段；`PerformanceIssueMapper` 增 `updateStressTask` / `updateVerify`；XML 同步 resultMap 与 BaseColumns |
| 服务 | `service/IssueVerifyService`（重放同场景压测 → `BaselineCompareService.diff(重放, 快照)` → P95 降幅 ≥30% 且无回归则 `resolved`，写 `verify_json`；三分支：达标/未达标/无快照，另含重放失败分支） |
| 服务 | `SourceIssueService#createFromStress` 自动写场景快照；新增 `bindStressTask` 补绑（非压测来源问题建立验证基线） |
| 接口 | `POST /api/source/issues/{id}/verify`、`POST /api/source/issues/{id}/bind-stress` |
| 配置 | `perf.verify.timeout-sec: 300`（重放等待超时） |
| 前端 | `OptimizeView.vue` 加"验证修复"按钮 + 结果卡片（P95 前后/降幅/QPS、基线对比表格、无快照补绑入口）；`utils/format.ts` 增 `fixed`/`resolved` 状态样式；`api/index.ts` 增 `verifyIssue` / `bindIssueStress` |

**验证状态**：后端 144 个测试全绿（datagen 55 + agent 13 + server 76），前端 `vue-tsc` + `vite build` 通过。

## 1. 项目一页速览

**PerformanceTestor** 是一个 Java 性能检测智能体平台，四个 Maven 模块 + 一个前端：

| 模块 | 技术 | 职责 |
|---|---|---|
| `perf-server` | Spring Boot 3.2.5 + JDK 17 + MyBatis + MySQL 8 | 平台后端：采集存储/压测引擎/容量分析/AI 编排/源码关联 |
| `perf-agent` | JDK 17 + ByteBuddy（shade 打包） | 无侵入探针：方法链/JDBC/JVM 采集，批量上报 |
| `perf-datagen` | 纯 Java 库 + SPI | Mock 数据引擎：联表/语义关联/枚举/魔法值/Dry-Run |
| `perf-web` | Vue 3.4 + TS 5.4 + Vite 5 + Tailwind + ECharts | 前端（现为十个视图） |
| `perf-demo` | Spring Boot | 被测示例应用（内置 N+1/慢 SQL 可复现问题） |

**数据库**：MySQL 8，库名 `performance_testor`；建表脚本 `sql/init.sql`（含演示数据）与 `sql/init_empty.sql`（空库）。

## 2. 当前状态（交接基线）

### 2.1 已完成（M0–M6 全部，16/16 任务，均有单测覆盖）

| 里程碑 | 内容 | 关键产出 |
|---|---|---|
| M0 | JWT 鉴权 | `perf-server .../auth/` 包 5 类；前端 `LoginView`/路由守卫/`http.ts` Bearer+401；敏感配置环境变量化 |
| M0 | 探针根语义 | `FilterRootAdvice` + `TraceContext` 三态状态机（filterOwned/chainSubmitted/NOT_SAMPLED 短路） |
| M0 | 压测引擎加固 | `StressStatistician` LongAdder+16 分片无锁直方图；warmupSec/rampUpSec；全局 CAS 令牌桶；sampler 竞态修复 |
| M0 | agent JVM 修复 | GC 精确名称映射表；CPU MXBean 缓存+`getCpuLoad()`；`MetricsReporter#stop` final flush；`agentmain` 幂等 |
| M1 | 灌数引擎 | `RelationResolver` ormHints 参数化（无状态契约）；`TopoSorter` DFS 精确环路提取 |
| M2 | 前端工程 | 卸载 tdesign/vue-echarts；`repoStore`；`StressForm`/`StressReport` 公共组件；404 页+title 同步 |
| M3 | CPU 热点采样 | agent `HotspotSampler`（10ms 弱采样/8192 帧上限）→ `hotspot_method` 表 → `/api/hotspots` → TraceAnalyzerView 火焰排行 Tab |
| M3 | JMeter 适配 | `JmeterStressEngine`：JMX 生成+CLI 零依赖+JTL 解析（warmup 过滤）；`available()` 未安装降级 |
| M3 | 定时压测基线 | `stress_schedule` 表+`StressScheduler`（CAS 防重）+`BaselineCompareService`（回归阈值）+`NotifyService`（钉钉/飞书/webhook）+前端"定时压测"Tab；双引擎 `X-Perf-Test: 1` 流量标记 |
| M4 | AI ReAct 编排升级 | `AiRole`/`ReactStep`/`AiStreamEvent`/`StreamAccumulator` 领域模型；`AiAgentService` ReAct 显式循环（Observation 截断 2000 字符 + 10 轮上限收尾）；`PromptBuilder` 三角色路由；`LlmClient` stream 模式；`AiChatStreamController`（SSE 300s + 轮次心跳）；前端 fetch+ReadableStream 逐 token |
| M4 | 知识库 | `ai_knowledge` 表（FULLTEXT ngram）+ `AiKnowledgeService`（入库门槛/检索降级）+ `SearchKnowledgeTool` + `GET /api/ai/knowledge` + 结论自动入库 |
| M4 | 修复效果自动验证 | `performance_issue` 补 `stress_task_id`/`verify_json`/`verify_time`；`IssueVerifyService`（重放同场景 → 基线 diff → P95 降幅 ≥30% 且无回归自动 `resolved`）；`POST /api/source/issues/{id}/verify` + `/bind-stress`；前端验证按钮与结果卡片 |
| M4 | 容量评估科学化 | `CurveFitter`（二项式最小二乘 + R² + 顶点拐点 + RT 二阶导拐点 + 95% CI）；`CapacityAnalyzer` 插值逻辑零改动 + `fillScience` 补模型；`CapacityResult` 向后兼容新增 `model/r2/fittedCurve/confidenceInterval/rtKneeConcurrency`；前端拟合曲线图（实测点/拟合/置信带/拐点）与置信区间表 |
| M5 | 中间件观测 | `MiddlewareSupport` 命令渲染 + Redis/ES/MQ 三个 Advice + `TraceEnhancer` 类型匹配注册（agent 零中间件依赖）；`slow_command` 表 + `SlowCommandMapper` + `GET /api/slow-commands` + `AgentReportService.saveSlowCommands`；前端慢 SQL 列表升级为慢命令合并视图（类型徽标 + 筛选）；`middlewareEnabled` 默认关 |

**已完成 m6-e2e-verify**（真实 MySQL + PostgreSQL 容器端到端验证通过）：

**验证方式与结果**（容器：`mysql:8.0` + `postgres:16-alpine`；无 mysql 客户端，DDL 经 `docker exec` 执行）：

| 验证项 | 结果 |
|---|---|
| 全量构建 | ✅ `mvn clean package` 228 测试全过（datagen 85 + agent 27 + server 116），jar 内含 PG 驱动与 openhtmltopdf |
| 登录鉴权 | ✅ 未登录 `/api/apps` → 401；登录取 JWT 后 → 200 |
| Agent 免鉴权上报 | ✅ `/api/agent/report` 不带 token → 200（非 401），JVM 指标/调用链正常落库 |
| 基线压测复现 | ✅ 20 并发 × 30s：QPS 323.9、P95 112ms、错误率 0% |
| warmup 生效 | ✅ 采样首点 P99 低于均值（无冷启动尖刺） |
| 分档压测 + 容量拟合 | ✅ 5 档（5/10/15/20/25）：QPS 抛物线拟合 R²=0.9558，顶点 17.97 并发，RT 拐点 20，5 档置信区间 |
| 报告导出 | ✅ PDF（`%PDF` 魔数，8.7KB）/ Word（Office 命名空间 + style 保留）/ 打印页 HTML（19 项内容检查全中） |
| 数据清理级联 | ✅ `slowCommand: 2` 等 9 类数据一并清理 |
| 全平台接口冒烟 | ✅ 11/11 返回 200（含新增 `/api/slow-commands`） |
| PG 方言（真实 PG） | ✅ 建表/元数据/SERIAL/`pg_enum` 原生枚举/CHECK 枚举/双引号计数，5 项冒烟全过 |
| PG 灌数（真实写入） | ✅ 30 行，原生枚举与 CHECK 枚举列取值均落在合法集内且分布均匀 |
| MySQL 灌数回归 | ✅ 20 行，ENUM 列取值为 A/B/C，PG 专有参数未串用 |

**E2E 暴露并修复的 3 个真实缺陷**（均未能在纯单测中发现，是本次端到端验证的核心价值）：

1. **全项目缺失 PostgreSQL JDBC 驱动** —— `PostgreSQLDialect` 写好了却连不上任何 PG 库。已在父 pom 托管 `org.postgresql:postgresql:42.7.3`，`perf-server` 以 runtime 引入、`perf-datagen` 以 test 引入（真实 PG 冒烟用）。
2. **枚举发现值未参与值生成** —— 枚举发现原先只产出「建议」供人工采纳，生成期仍按 `VARCHAR` 填随机长串，导致 PG 原生枚举列直接报 `column "status" is of type e2e_status but expression is of type character varying`（MySQL ENUM 列则写入非法值）。已在 `DefaultDataGenEngine` 增加 `dbEnumValues` 缓存（键 `表名.列名`），未配置规则的列优先取发现值；因引擎是单例 Bean，每次 `execute` 开头清空缓存防跨任务串用。
3. **PG 严格类型导致枚举/JSON 列写入失败** —— 即便值正确，PG JDBC 默认把字符串参数按 `varchar` 发送，与目标枚举类型不匹配。已在 `PostgreSQLDialect.enrichJdbcUrl` 追加 `stringtype=unspecified`（参数以 unknown 发送，由 PG 按列类型推断），用户显式指定 `stringtype` 时不覆盖。

**新增真实 PG 冒烟用例**（无 PG 实例时自动跳过，不影响常规构建）：`PostgreSQLDialectLiveTest`（5 例：方言元数据/SERIAL/`pg_enum`/CHECK/计数）、`PostgresEnumGenerationLiveTest`（2 例：枚举列取值合法性与缓存不串用）。

**验证状态**：后端全模块 `mvn test` 228 个测试全过（datagen 85 + agent 27 + server 116）；前端 `vue-tsc` + `vite build` 通过。

### 2.2 待执行（无 —— 16/16 任务全部完成）

**已完成任务的执行细节见第 0 节「第二轮变更摘要」**，后续任务的开发指导见第 4 节。

后续任务的**详细开发指导**见第 4 节；执行时先读第 3 节环境准备与第 5 节约束。

## 3. 环境准备（必读）

### 3.1 工具链

本环境（Linux 容器）**最初无 Java/Maven**，已手动安装到非标准路径：

```bash
# JDK 17（/opt/java/jdk-17.0.20.1+1）
export JAVA_HOME=/opt/java/jdk-17.0.20.1+1
# Maven 3.9.6（/opt/maven/apache-maven-3.9.6）
export PATH=$JAVA_HOME/bin:/opt/maven/apache-maven-3.9.6/bin:$PATH
```

- Maven 已配阿里云镜像（`~/.m2/settings.xml`），依赖可正常拉取。
- Node 22 环境内置；前端依赖已安装（`perf-web/node_modules`），npm registry 用 `https://registry.npmmirror.com`。
- **注意**：容器重建后 `/opt/java` 与 `/opt/maven` 可能丢失，需按 `docs/handoff/environment-setup.md` 重装。

### 3.2 常用命令

```bash
# 后端全量测试（提交前必跑）
cd /workspace && mvn test

# 单模块快速测试
mvn -q -pl perf-server -am test -Dtest='XxxTest' -Dsurefire.failIfNoSpecifiedTests=false

# 前端类型检查 + 构建（改动 .vue/.ts 后必跑）
cd /workspace/perf-web && npx vue-tsc -b && npm run build

# 端到端（M6 用，需本机 MySQL 8）
cd /workspace && bash scripts/start-all.sh
```

### 3.3 敏感配置（环境变量）

```bash
export MYSQL_URL='jdbc:mysql://127.0.0.1:3306/performance_testor?...'   # 平台库
export MYSQL_USER=root
export MYSQL_PASSWORD=root123
export PERF_ADMIN_USER=admin            # 登录账号（默认 admin）
export PERF_ADMIN_PASSWORD=admin123     # 登录密码（默认 admin123）
export PERF_JWT_SECRET='<32+字节随机串>' # JWT HS256 密钥
export PERF_AI_API_KEY='<LLM key>'      # AI 编排用（OpenAI 兼容）
export JMETER_HOME=/opt/jmeter          # JMeter 引擎（可选）
```

## 4. 后续任务开发指导（M4 → M6）

### 4.1 m4-ai-upgrade ✅ 已完成：AI ReAct 编排 + SSE 流式 + 知识库

**落地差异（与下方原改造要点对照）**：
- `AiAgentService` 构造签名已变为 `(LlmClient, PromptBuilder, List<AiTool>, AiKnowledgeService, MetricQueryService, StressTaskService, SourceIssueService)`——新增 `AiKnowledgeService` 注入，后续扩展注意。
- `MAX_ROUNDS` 与 `MAX_OBSERVATION_CHARS` 改为包可见（`static final`），供单测断言。
- 新增 `chat(sessionId, message, appName, sink)` 四参重载（appName 用于结论入库打标）；两参/三参重载保留。
- SSE 的 `data` 负载统一走 JSON 编码（正文含换行，直接写文本会破坏 SSE 帧格式），前端 `handleFrame` 需 `JSON.parse`。
- `LlmClient` 新增 `perf.ai.stream-timeout-sec`（默认 300）与 `perf.ai.stream-threads`（默认 4）两项配置。
- 结论入库门槛：`reply` 正文 ≥ 40 字且 `mode=llm` 才写库（规则降级文本不入库）。

**原改造要点（供理解设计意图，均已落地）**：
1. **ReAct 显式循环**：`AiAgentService` 改为 Thought/Action/Observation 结构化循环（现有 Function Calling 已具备骨架，需把工具结果截断到 ~2000 字符防上下文膨胀）；三角色 system prompt（压测执行/根因分析/修复建议）由编排层按用户意图路由，`PromptBuilder` 加 `roleFor(intent)`。
2. **SSE 流式**：新增 `AiChatStreamController`，`POST /api/ai/chat/stream` 用 `SseEmitter`；`LlmClient` 增加 stream 模式（OpenAI 兼容 `stream=true`，逐行读 `data:` 前缀的 SSE 行解析 delta）。**注意**：SseEmitter 超时设 ≥ 300s；工具调用轮次间向前端推 `event: thought` 心跳事件防止代理断连。
3. **知识库**：新表 `ai_knowledge`（`id, app_name, title, content TEXT, tags, create_time`，`FULLTEXT KEY ft_content (title, content)` —— **MySQL 8 默认 ngram 解析器支持中文**：`WITH PARSER ngram`，建表语句双脚本同步）；分析结论入库（`AiAgentService` 会话结束时提取结论自动写入）；新工具 `SearchKnowledgeTool extends AbstractAiTool`（`MATCH(title, content) AGAINST(? IN NATURAL LANGUAGE MODE)` 检索 top5）。
4. **前端**：`AiAssistantView` 改用 `fetch` + `ReadableStream` 读 SSE（axios 不支持流式），逐 token 渲染。

**验收**：单测（知识库检索 SQL、ReAct 循环状态机）+ 手动流式对话冒烟。

### 4.2 m4-fix-verify ✅ 已完成：修复效果自动验证

**落地差异（与下方原改造要点对照）**：
- `performance_issue` 除 `stress_task_id` 外**额外增加** `verify_json` 与 `verify_time` 两列（原要点仅提到验证记录），结论存 `verify_json` 而非单独表。
- 新增幂等补列存储过程 `add_column_if_missing(tbl, col, col_def)`——MySQL 无 `ADD COLUMN IF NOT EXISTS`，两份脚本均需老库可重复执行。
- 重放压测走 `StressTaskService#createAndRun`（异步），`IssueVerifyService` 内部轮询等待（500ms 间隔，超时 `perf.verify.timeout-sec` 默认 300s）。
- 达标判定为**双条件**：P95 降幅 ≥ 30% **且** `BaselineCompareService.diff(...).regressed == false`（QPS 降幅 ≤10%、P95 涨幅 ≤20%、错误率上升 ≤5pct）。
- 额外增加 `POST /api/source/issues/{id}/bind-stress`：慢 SQL/热点方法类问题（创建时无压测任务）可补绑验证基线，否则"验证修复"按钮为不可用态。
- 无快照分支**不写库**（`noSnapshot` 直接返回，避免污染 `verify_time` 与状态）。

**原改造要点（供理解设计意图，均已落地）**：
1. `perf_issue` 加列 `stress_task_id BIGINT DEFAULT 0`（快照：问题创建时的压测场景，双脚本同步）。
2. `POST /api/source/issues/{id}/verify`：取快照任务参数 → `StressTaskService.createAndRun` 重放同场景 → `BaselineCompareService.diff(newTaskId, snapshotTaskId)` → P95 降幅 ≥ 30% 且无回归 → 自动 `status='resolved'` 并写验证记录（`verify_json` 列存 diff 结果）。
3. 前端问题列表加"验证修复"按钮 + 验证结果展示（复用基线对比表格组件模式）。

**验收**：单测（验证判定逻辑：达标/未达标/无快照三分支）。

### 4.3 m4-capacity-science ✅ 已完成：容量评估科学化

**落地差异见第 0 节「第三轮变更摘要」**（拟合字段不参与判定、置信区间样本量口径、≥3 档才拟合）。

**现状**：`capacity/CapacityAnalyzer` 分段线性插值 + 阈值规则。

**改造要点**：
1. 在插值之上加**二项式拟合**（最小二乘解 3×3 线性方程组即可，无需引依赖）：对 (并发, QPS) 拟合 `qps = a·c² + b·c + k`，a<0 时拐点 `c* = -b/2a` 为理论最大并发；R² 计算 `1 - SSE/SST`。
2. RT-并发曲线二阶导拐点检测（数值差分即可）。
3. **置信区间**：分档采样方差 → 95% CI = `mean ± 1.96·σ/√n`（n=该档采样点数）。
4. 结果 JSON 向后兼容地加 `model/r2/fittedCurve/confidenceInterval` 字段（旧字段不动，前端 capacity 卡片透传展示即可）。

**验收**：单测（构造抛物线数据验证拟合参数、拐点、R²≈1）。

### 4.4 m5-pg-dialect：PostgreSQL 方言

**现状**：`perf-datagen .../dialect/MySQLDialect` 实现 `spi.DatabaseDialect`；H2 测试 `MySQLDialectH2Test` 已验证 SPI 可插拔模式。

**改造要点**：
1. `PostgreSQLDialect`：元数据查询改 `information_schema`（PG 无 `SHOW TABLES`/`TYPE_NAME ENUM`）；标识符双引号包裹；CHECK 约束枚举发现（PG 原生 CHECK 常见）；SERIAL/BIGSERIAL 主键识别（跳过插值）。**先读 `MySQLDialect` 每个方法逐一对齐**。
2. `JpaSourceParser implements OrmSourceParser`：JavaParser 解析 `@Entity/@ManyToOne/@OneToMany/@JoinColumn` 构建 `RelationHint`（参考现有 `MyBatisSourceParser` 的扫描入口与返回结构）。
3. 路由：`DataGenService`（perf-server 宿主层）按 JDBC URL 前缀 `jdbc:postgresql:` 选方言——**注意 `supports()` 判别即可，勿硬编码 if-else 链**。
4. 单测：H2 `MODE=PostgreSQL`（`jdbc:h2:mem:test;MODE=PostgreSQL;DATABASE_TO_LOWER=TRUE`）复用现有 SPI 测试模式。

### 4.5 m5-middleware：Redis/ES/MQ 观测

**现状**：`TraceEnhancer` 已有 JDBC 拦截（Statement/PreparedStatement advice 模式）与 Filter 根拦截，是本任务的实现模板。

**改造要点**：
1. 拦截点（hasSuperType 名称匹配，无需依赖客户端 jar）：
   - Redis：`io.lettuce.core.api.sync.RedisCommands` / `redis.clients.jedis.Jedis`（命令方法拦截，取 `methodName` 作为命令名）
   - ES：`org.elasticsearch.client.Request`（`RestHighLevelClient` 底层）或 `co.elastic.clients.elasticsearch.ElasticsearchClient`
   - RabbitMQ：`com.rabbitmq.client.Channel`（`basicPublish/basicConsume`）
2. 超阈值（默认 100ms，`middlewareSlowMs` 配置）入新表 `slow_command`（`id, app_name, type, command, cost_ms, trace_id, execute_time`，**双脚本同步**）；与调用链经 `traceId` 关联（`TraceContext.currentMethodChain()` 复用）。
3. `middlewareEnabled` 默认 **false**（控制常态开销，同 hotspot 模式）。
4. 上报走 `MetricsReporter` 新增 `TYPE_SLOW_COMMAND` 队列（**完全照抄 slowSql 三件套**：report 方法 + flush 分支 + finalFlush 分支）；平台侧 `AgentReportService` 加 case。
5. 前端：TraceAnalyzerView 慢 SQL Tab 扩展为慢命令合并视图（type 徽章区分）。

**执行前置**：按计划用 code-explorer 子代理分析现有 JDBC 拦截的精确实现（`JdbcInterceptor`/`TraceEnhancer` 逐方法读），再动手。

### 4.6 m5-report-export：报告导出

1. 依赖：父 POM `dependencyManagement` 加 `openhtmltopdf-pdfbox 1.0.10`（仅 perf-server）。
2. `report/ReportExportService`：压测报告 HTML（复用前端卡片语义的简版模板）→ PDF；`GET /api/reports/export?stressTaskId=&format=pdf|doc`（`doc` 用 HTML 兼容头输出 .doc，零额外依赖）。
3. 前端 `ReportPrintView`（`/report/print?taskId=`）：Tailwind `print:` 变体（屏幕深色/打印白底 A4、`@page { size: A4 }`、表格去阴影）；StressReport 组件加导出按钮。

### 4.7 m6-e2e-verify：端到端验证

按计划：全量构建（`mvn test` + `npm run build` + `bash scripts/build.sh`）→ `start-all.sh` 拉起 MySQL+server+demo+web → 验证清单：
1. 登录链路（未登录 401 → 登录取 token → 携带访问）
2. agent 免鉴权上报正常（demo 启动带 `-javaagent`）
3. 压测 `GET /api/user/list?limit=200` 20 并发复现基线（QPS≈300 / P95≈135ms），确认 warmup 生效（首秒无冷启动尖刺）
4. CPU 热点/基线 diff/报告导出冒烟（无 JMeter 环境时该项降级跳过并留日志）

## 5. 开发约束（红线，违反会导致返工）

1. **`/api/agent/**` 必须免鉴权**——探针在无 JWT 能力的被测服务器上运行，白名单在 `WebAuthConfig`，改动后跑 `JwtAuthInterceptorTest` 相关用例。
2. **新增表必须同步两份脚本**：`sql/init.sql` 与 `sql/init_empty.sql`（幂等 `CREATE TABLE IF NOT EXISTS`）。
3. **Mapper XML 禁止 `${}`**（SQL 注入），一律 `#{}`。
4. **agent 热路径零新增锁**：埋点 enter/exit 路径只允许 ThreadLocal 读写与 boolean 判断；新拦截器参照 `FilterRootAdvice` 的 Advice 内联约束（仅调 `TraceContext` 可内联静态方法，enter 返回 Boolean 单例比较）。
5. **`StressStatistician` 对外 API 签名与 30000 桶×1ms 语义不变**（历史报告对比兼容）。
6. **perf-datagen 公共接口变更**需同步 perf-server 宿主层 `DataGenService` 调用点（`registerSource` 已 @Deprecated，勿再新增调用）。
7. **前端响应处理模式**：`http.ts` 拦截器已解包 `{code,message,data}`，视图里需 `as unknown as {code,message,data}` 断言（axios 类型无法感知），照抄 LoginView/StressCenterView 现有写法。
8. **依赖最小化**：新增依赖须父 POM `dependencyManagement` 统一版本；agent 保持零新依赖（shade 包体积敏感）；JMeter 走 CLI 零依赖；知识库用 MySQL FULLTEXT（ngram）不引入向量库/ES。
9. **提交纪律**：用户明确要求才 commit；`.codebuddy/` 已在 `.gitignore`，勿提交。
10. **测试先行验证**：每个任务完成即跑对应单测；提交前 `mvn test` 全量 + 前端 build 必须绿。

## 6. 相关文档索引

| 文档 | 内容 |
|---|---|
| `docs/plans/performance-improvement-plan-detail.md` | **总计划**（产品概述/技术栈/六里程碑方案/关键文件清单/TODOS 勾选进度）——执行新任务前先读对应小节 |
| `docs/archive/performance-improvement-plan.md` | 计划固化版（历史归档，里程碑任务表/技术约束/关键文件变更清单） |
| `docs/handoff/environment-setup.md` | 工具链重装指南（JDK/Maven/Maven 镜像，容器重建后用） |
| `docs/archive/ptde-handoff/datagen-optimization_交接.md` | 上一轮交接（已归档，灌数引擎三任务：联表/枚举/魔法值，已全部实施） |
| `docs/design/architecture.md` | 平台整体架构（部署拓扑/模块职责/数据流） |
| `docs/reports/` | 各子系统设计定稿（压测引擎/agent/datagen/AI 编排等 12 篇） |
| `docs/issues/` | 已知问题与设计决策记录 |
| `docs/artifacts/` | 过程产物（验收记录/回归记录/工具链安装记录等） |

## 7. 交接确认清单

接手后建议按顺序完成：
1. ☑ 读本文档 + `docs/plans/performance-improvement-plan-detail.md`
2. ☑ 验证环境：`mvn -v`、`node -v`、`cd perf-web && npx vue-tsc -b`（全过说明基线完好）
3. ☑ 跑一次 `mvn test` 确认 144 个测试全绿
4. ☑ 完成 m4-ai-upgrade 与 m4-fix-verify（按 4.1 / 4.2 开发指导）
5. ☑ 完成 m4-capacity-science（按 4.3 开发指导）
6. ☑ 完成 m5-pg-dialect（按 4.4 开发指导）
8. ☑ 完成 m5-report-export（按 4.4 开发指导）
9. ☑ 完成 m6-e2e-verify（按 4.5 开发指导）—— 改进计划 16/16 任务全部完成，详见第 2.2 节验证矩阵与「E2E 暴露并修复的 3 个真实缺陷」

## 8. 本轮变更（待提交）

第三轮改动文件清单（M4 容量科学化）：

```
perf-server .../capacity/CurveFitter.java                  # [NEW] 二项式拟合/R²/拐点/95% CI
perf-server .../capacity/CapacityAnalyzer.java             # [MODIFY] 插值逻辑不变 + fillScience
perf-server .../capacity/CapacityResult.java               # [MODIFY] +model/r2/fittedCurve/confidenceInterval/rtKneeConcurrency
                                                           #          +FitModel/FittedPoint/ConfidenceInterval，CurvePoint +p50Ms/totalRequests
perf-server/src/test/.../capacity/CapacityAnalyzerScienceTest.java  # [NEW] 14 单测
perf-web/src/components/stress/StressReport.vue            # [MODIFY] 模型卡片 + 置信区间表 + 拟合曲线图
docs/plans/performance-improvement-plan-detail.md          # 进度标记（M4 3/3）
docs/handoff/handoff-main.md                               # 本交接更新
```

第二轮改动文件清单（M4 前两项）：

```
sql/init.sql                                              # +ai_knowledge 表 + performance_issue 补列 + add_column_if_missing
sql/init_empty.sql                                        # 同步（无演示数据）
perf-server .../ai/AiRole.java                            # [NEW]
perf-server .../ai/ReactPhase.java                        # [NEW]
perf-server .../ai/ReactStep.java                         # [NEW]
perf-server .../ai/AiStreamEvent.java                     # [NEW]
perf-server .../ai/AiStreamEventType.java                 # [NEW]
perf-server .../ai/StreamAccumulator.java                 # [NEW]
perf-server .../ai/AiKnowledgeService.java                # [NEW]
perf-server .../ai/AiChatStreamController.java            # [NEW]
perf-server .../ai/AiAgentService.java                    # [REWRITE] ReAct 循环 + 流式 + 结论入库
perf-server .../ai/PromptBuilder.java                     # [REWRITE] 三角色路由
perf-server .../ai/LlmClient.java                         # [MODIFY] stream 模式
perf-server .../ai/tools/SearchKnowledgeTool.java         # [NEW]
perf-server .../controller/AiController.java              # [MODIFY] +GET /api/ai/knowledge
perf-server .../entity/AiKnowledge.java                   # [NEW]
perf-server .../mapper/AiKnowledgeMapper.java             # [NEW]
perf-server .../resources/mapper/AiKnowledgeMapper.xml    # [NEW]
perf-server .../entity/PerformanceIssue.java              # [MODIFY] +stressTaskId/verifyJson/verifyTime
perf-server .../mapper/PerformanceIssueMapper.java        # [MODIFY] +updateStressTask/updateVerify
perf-server .../resources/mapper/PerformanceIssueMapper.xml  # [MODIFY]
perf-server .../service/IssueVerifyService.java           # [NEW]
perf-server .../service/SourceIssueService.java           # [MODIFY] 场景快照 + bindStressTask
perf-server .../controller/SourceController.java          # [MODIFY] +verify/+bind-stress
perf-server .../resources/application.yml                 # [MODIFY] ai.stream-* / verify.timeout-sec
perf-server/src/test/.../ai/{StreamAccumulator,PromptBuilderRole,AiAgentReactLoop,
                             AiKnowledgeService,AiKnowledgeSqlScript}Test.java   # [NEW] 33 单测
perf-server/src/test/.../service/IssueVerifyServiceTest.java                     # [NEW] 9 单测
perf-web/src/views/AiAssistantView.vue                    # [MODIFY] SSE 流式渲染
perf-web/src/views/OptimizeView.vue                       # [MODIFY] 验证修复按钮与结果卡片
perf-web/src/api/index.ts                                 # [MODIFY] +aiApi.knowledge / verifyIssue / bindIssueStress
perf-web/src/utils/format.ts                              # [MODIFY] +fixed/resolved 状态样式
docs/plans/performance-improvement-plan-detail.md         # 进度标记
docs/archive/performance-improvement-plan.md              # 进度标记（已归档）
docs/handoff/handoff-main.md                              # 本交接更新
```
