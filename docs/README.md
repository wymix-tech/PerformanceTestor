# PerformanceTestor — 性能检测智能体平台

面向 Java 应用的**性能检测智能体平台**：平台（perf-server、MySQL、源码存储）与被测应用**分离部署**在不同服务器，避免压测与监控互相干扰。通过 Java Agent 无侵入采集 JVM 指标、业务方法调用链耗时、慢 SQL、中间件慢命令与 CPU 热点方法；提供自研压测引擎（单接口 / 多接口混合场景）与自动灌数据能力；支持 SSH 远程编译部署、定时压测与 CI 性能门禁；AI 自然语言对话自动编排检测任务；采集项目源代码，将检测结果关联到源码位置，由大模型分析根因并给出多种修改方案，选定方案后生成修复提示词，并可重放压测自动验证修复效果。

> 本次更新相对上一版的主要增量：JWT 鉴权与登录页、SSH 部署管理、中间件慢命令与 CPU 热点采样、JVM 告警规则、定时压测与基线对比、CI 性能门禁、报告导出（HTML/PDF/Word）、修复效果自动验证、AI 流式对话（SSE）与知识库沉淀、多接口场景步骤上下文引用（`{{p1.result.x}}`）、perf-datagen 新增 PostgreSQL 方言与 JPA 解析器。

## 模块总览

```
/workspace/
├── perf-agent/     # 监控探针（Java 17 + ByteBuddy）：方法埋点 / JDBC 拦截 / 中间件拦截 /
│                   #   JMX 采集 / CPU 热点采样 / HTTP 批量上报
├── perf-datagen/   # Mock 数据生成引擎（独立纯 Java 库，SPI 可插拔）：联表外键感知 / 无外键语义
│                   #   关联识别 / 枚举语义化 / 魔法值感知 / Dry-Run 试运行；MySQL + PostgreSQL
│                   #   双方言，ORM 侧实现 MyBatis 与 JPA，三类 SPI 扩展点
├── perf-server/    # 后端平台（Spring Boot 3.2 + MyBatis + MySQL）：采集存储 / 压测引擎 / 灌数据 /
│                   #   容量评估 / 扩容建议 / 数据库连接管理 / 源码管理 / 远程部署 / 定时调度 /
│                   #   报告导出 / 告警 / 鉴权 / AI Function Calling 编排
├── perf-web/       # 前端（Vue3 + TS + Vite + Tailwind + ECharts）：10 个路由页面
├── perf-demo/      # 示例被测应用（Spring Boot 3.2 + MyBatis）：N+1 / 慢 SQL / 场景串联演示
├── sql/init.sql    # 平台库全量建表脚本（幂等，含演示数据）
├── sql/init_empty.sql # 平台库全量建表脚本（幂等，无演示数据，一键初始化空库，脚本默认使用）
├── scripts/        # build.sh / start-all.sh(单机) / start-server.sh / start-agent.sh(多机) /
│                   #   link-codebuddy.sh / perf-gate.sh(CI 性能门禁) /
│                   #   verify-datagen-mysql.sh / verify-datagen-postgres.sh(灌数引擎端到端验收) /
│                   #   verify-scenario-context.sh(场景步骤上下文自验)
└── docs/           # 本文档 + design / handoff / plans / reports / issues / artifacts 归档
```

## 快速开始（单机演示模式）

前置条件：JDK 17+、Maven、Node ≥22、MySQL 8（默认 `root/root123`，可用环境变量覆盖）。

```bash
# 1. 全量构建（后端 jar + 前端 dist）
cd /workspace && scripts/build.sh          # --skip-backend / --skip-frontend 可分段构建

# 2. 一键启动（MySQL + perf-server + perf-demo(挂 agent) + perf-web）
scripts/start-all.sh
#    Web 后台: http://localhost:5173       默认账号 admin / admin123
#    API 服务: http://localhost:8080/api
#    被测应用: http://localhost:8081/api/user/list?limit=200   （N+1 热点接口）

# 3. 手动分步启动（等价）：
#    mvn -q -pl perf-agent,perf-datagen,perf-server,perf-demo package -DskipTests
#    java -jar perf-server/target/perf-server.jar &
#    java -javaagent:perf-agent/target/perf-agent.jar=reportUrl=http://127.0.0.1:8080/api/agent/report,appName=perf-demo,appPort=8081,sampleRate=1.0 \
#         -jar perf-demo/target/perf-demo.jar --server.port=8081 &
#    (cd perf-web && npm run dev)
```

关键环境变量：`SERVER_PORT`(8080) / `WEB_PORT`(5173) / `DEMO_PORT`(8081) / `MYSQL_HOST|PORT|USER|PASSWORD` / `SQL_FILE`(默认 `sql/init_empty.sql`，需要首屏演示数据改用 `sql/init.sql`)。

单机模式下平台库 `performance_testor` 与被测库 `demo` 隔离在同一 MySQL 实例，仅用于演示。

## 多机分离部署

平台服务器与被测服务器分离，仅需网络白名单互通三处：**平台 → 被测**（发起压测、灌数据）、**被测 → 平台**（agent 上报）、**平台 → 被测服务器 SSH**（可选，远程编译部署）。

```
[平台服务器]  perf-server:8080 + perf-web:5173 + MySQL + 源码存储 + OpenAI 兼容 API
    ↑ agent 上报（被测→平台:8080）   ↓ 压测/灌数据（平台→被测:8081、被测库:3306）
[被测服务器]  perf-demo:8081 + MySQL(demo库) + -javaagent 探针
```

### 平台服务器

```bash
scripts/start-server.sh    # 端口 8080，前端 5173
```

### 被测服务器

```bash
# REPORT_URL 必须改为平台服务器真实地址
REPORT_URL=http://<平台IP>:8080/api/agent/report scripts/start-agent.sh
```

`start-agent.sh` 可配置项：`REPORT_URL` / `DEMO_PORT` / `SAMPLE_RATE`(演示 1.0，生产建议 0.1) / `SLOW_SQL_THRESHOLD_MS`(200) / `MIDDLEWARE_ENABLED`(false) / `SLOW_COMMAND_THRESHOLD_MS`(100) / `AGENT_TOKEN`（需与平台 `PERF_AGENT_TOKEN` 一致）。

网络要求：被测服务器可访问平台 `8080`（上报）；平台可访问被测 `8081`（压测）与被测库 `3306`（灌数据）。agent 上报采用本地队列 + 批量 flush，失败指数退避重试、队列满丢最旧并计数，网络抖动不影响被测业务。

> **项目准入**：agent 上报时以 `appName` 匹配平台「源码仓库」项目名，平台侧做准入校验——**项目不存在则数据全部丢弃**（返回 `PROJECT_NOT_FOUND` 让 agent 停报）。因此演示环境需先在「项目管理」导入源码仓库，或使用 `targetProjectName` 参数显式指定项目名。

> agent 上报时自动注册应用实例（来源 IP + `appPort` 配置的端口 + 应用版本号）。压测中心选定项目后**服务地址自动预填** `http://{appHost}:{appPort}`（用户手动修改过后不再覆盖），配合接口扫描实现「选项目 → 选接口 → 直接压测」。
>
> **版本号来源优先级**：agent 参数 `appVersion=x.y.z` > 被测 jar `META-INF/MANIFEST.MF` 的 `Implementation-Version` > 空（平台保留已有值）。版本随上报维护，压测任务自动快照，报告页支持版本间性能对比。

### 探针低开销设计（对被测应用影响最小化）

- 埋点范围为**业务包全量**：显式 `includePackages` > 被测 jar `Start-Class`/`Main-Class` 包名自动推导（推荐零配置）> Spring 注解兜底；
- **POJO / 数据类不埋点**：entity/dto/vo/model 包名启发 + 结构判断（全部方法均为 getter/setter/equals）+ 枚举/record 三层排除；
- 方法级防噪音：构造器 / Object 基类方法 / getter/setter / 桥接合成 / main 不埋；
- 业务线程零转换：调用链以原生树 O(1) 入队，树转 Map/JSON 序列化全部在 reporter 线程完成；
- 单 ThreadLocal 上下文：方法热路径仅 1 次 `ThreadLocal.get`；未采样链路零分配；
- 采样率（比例）+ 每秒令牌桶（绝对量，默认 50/s，`maxTracesPerSec`）双重控制；
- trace 队列条数 + 字节预算（默认 8MB，`traceBufferBytes`，下限 1MB）双重限制防大树内存洪水；
- JDBC 惰性 SQL：enter 仅取参数引用，超阈值后才做 toString 兜底与截断；
- **Servlet Filter 根模式**（默认开）：以最外层 `Filter#doFilter` 作为唯一采样决策点，消除采样稀释与半截链路；
- 中间件拦截与 CPU 热点采样**默认关闭**（`middlewareEnabled` / `hotspotEnabled`），按需开启以控制常态开销。

### Agent 参数表（`-javaagent:perf-agent.jar=k=v,...`）

| 参数 | 默认值 | 说明 |
| ---- | ------ | ---- |
| `reportUrl` | `http://127.0.0.1:8080/api/agent/report` | 平台上报地址（多机部署必改） |
| `appName` | `unknown-app` | 应用名，需与平台项目名一致（否则数据丢弃） |
| `targetProjectName` | 空 | 目标项目名，优先于 `appName` 做准入匹配 |
| `appPort` | `0` | 被测 HTTP 端口，供压测表单预填服务地址；0 时读 `server.port` 兜底 |
| `appVersion` | 空 | 版本号，留空自动读 jar MANIFEST |
| `sampleRate` | `0.1` | 调用链采样率 0~1 |
| `includePackages` | 空 | 显式埋点包前缀（逗号分隔），留空自动推导业务根包 |
| `slowSqlThresholdMs` | `500` | 慢 SQL 阈值 |
| `slowCommandThresholdMs` | `100` | 中间件慢命令阈值 |
| `middlewareEnabled` | `false` | Redis / ES / RabbitMQ 拦截开关 |
| `hotspotEnabled` | `false` | 方法级 CPU 热点采样 |
| `hotspotIntervalMs` / `hotspotReportSec` / `hotspotTopN` | `10` / `30` / `50` | 热点采样周期 / 上报周期 / 上报条数 |
| `methodTraceEnabled` / `jdbcEnabled` / `jmxEnabled` / `filterRootEnabled` | `true` | 各类采集开关 |
| `jmxCollectIntervalSec` | `5` | JMX 采集周期 |
| `batchSize` / `bufferSize` | `500` / `5000` | 上报批次 / 缓冲区上限（超出丢弃最旧） |
| `maxTracesPerSec` / `traceBufferBytes` | `50` / `8388608` | 每秒 trace 上限 / trace 队列字节预算 |
| `agentToken` | 空 | 上报令牌，需与平台 `perf.auth.agent-token` 一致 |

## perf-demo 示例应用（可分析问题点）

| 问题类型 | 位置 | 表现 |
| -------- | ---- | ---- |
| N+1 查询（最典型） | `UserService.listUsersWithOrderCount`（循环内逐条 `countOrders`） | `GET /api/user/list?limit=N`，N 个用户产生 N+1 次 SQL |
| 慢 SQL：无索引全表扫描 | `OrderMapper.selectByStatus`（`status` 列刻意无索引） | `GET /api/order/list?status=PAID` |
| 慢 SQL：大表 GROUP BY | `OrderMapper.selectStats`（5 万行聚合） | `GET /api/order/stats` |
| 慢 SQL：无覆盖索引排序 | `OrderMapper.selectRecent`（`created_at` filesort） | `GET /api/order/recent` |

场景串联演示接口（`DemoScenarioController`，用于验证多接口混合场景的步骤上下文引用）：`POST /api/scenario/login` → `POST /api/scenario/order` → `GET /api/scenario/order/{orderId}`，登录返回 token 与 `X-Request-Id`，下游步骤零 extract 声明直接引用（配合 `scripts/verify-scenario-context.sh` 自验）。

启动时自动建表并灌入 1 万用户 / 5 万订单（`DemoDataInitializer`，幂等，仅空表灌入；`demo.data.enabled=false` 可只建表），并自动补充 `perf_order.user_id` 外键约束与 `status` 列枚举注释（作为灌数引擎的验收载体）。压测 `GET /api/user/list?limit=200`（20 并发）可稳定复现 N+1 热点调用链与慢 SQL 上报。

## 数据准备引擎（perf-datagen 独立模块）

Mock 数据生成能力沉淀为**独立纯 Java 库**（`com.perftestor.datagen`，可脱离平台复用），宿主通过唯一门面 `DataGenEngine` 调用，进度与语义提示经回调交还：

- **联表生成**：表组拓扑排序（环检测定位表名）、父 ID 池插入后回查采样（>50 万分段）、`ref`/`refSkew` 热点倾斜（累计权重二分 O(log n)）、`orphanRate` 独立池后置替换、自引用深度限制；
- **无外键关联识别（四级合并）**：显式规则 > DB 声明外键 > ORM 源码语义（MyBatis resultMap association/collection + 注解 SQL JOIN；JPA `@ManyToOne`/`@JoinTable` 等）> 命名约定推断（`父表_id` + 单复数归一 + 类型兼容 + 数据抽样置信度）；空表兜底（父表 0 行 → 低置信建议交前端确认）；低置信关联不静默生效，经建议接口采纳；
- **枚举语义化**：MySQL ENUM → CHECK 约束 → 列注释「PENDING=待支付…」→ Java 枚举 四级发现 + weights 权重分布；PG 侧查 `pg_enum`，降级 `pg_get_constraintdef` 文本解析；
- **魔法值感知**：扫描 MyBatis XML / 注解 SQL / 静态常量 / Java 枚举，产出「表列→候选值（文件：行号）」，范围谓词生成 `overThreshold` 规则；源码不可读自动降级纯 DB 元数据并黄色警告；
- **Dry-Run 试运行**：完整执行元数据/规则/关联/拓扑/外键池预检但不 INSERT，产出预计行数、事务时长估算与警告清单，前端试运行通过后启用正式执行；
- **SPI 可插拔**：`DatabaseDialect` / `OrmSourceParser` / `BatchExecutor` 三扩展点，按 `supports()` + `order()` 路由注册；已实现 MySQL + PostgreSQL 双方言、MyBatis + JPA 双 ORM 解析器、`JdbcBatchExecutor`（batch 500）；扩展 Oracle / 其他 ORM 仅需新增实现类；
- **PostgreSQL 方言**：URL 前缀 `jdbc:postgresql:`，自动追加 `reWriteBatchedInserts=true` 与 `stringtype=unspecified`；`SERIAL/BIGSERIAL` 的 `nextval(...)` 默认值识别为自增并跳过插值；标识符用双引号。

规则 JSON 契约（前端编辑器 / 引擎 / AI 工具三方一致）：

```json
{"order.user_id": {"type": "refSkew", "table": "user", "column": "id", "hotPercent": 20, "hotShare": 80},
 "order.status":  {"type": "enum", "values": ["PENDING","PAID"], "weights": [20, 80]},
 "order.amount":  {"type": "range", "min": 100, "max": 5000, "overThreshold": {"op": ">", "value": 1000, "share": 30}}}
```

端到端验收：

- `scripts/verify-datagen-mysql.sh` —— MySQL：Dry-Run 拦截错误表名 / 联表 refSkew / weights / 语义建议接口；
- `scripts/verify-datagen-postgres.sh` —— PostgreSQL：同上用例，并额外校验原生枚举（`CREATE TYPE ... AS ENUM`）写入、BIGSERIAL 自增列跳过、枚举建议值发现。

质量门禁：13 个测试类约 85 个用例，H2（MySQL 模式）驱动引擎/方言/批量全流程单测，PG 真实库冒烟在无实例时自动跳过；已接 PIT 突变测试（`mvn -pl perf-datagen pitest:mutationCoverage`，阈值 75）。

## 压测引擎：多接口混合场景与步骤上下文引用

压测引擎支持 `http`（自研）与 `jmeter` 两种引擎，单接口模式与 scenario（多步骤按权重混合）模式。scenario 支持**步骤上下文引用**，下游步骤可直接引用任意上游步骤的响应头与响应体，无需声明 extract：

```json
{"scenario": [
  {"alias": "login", "url": "{{baseUrl}}/api/scenario/login", "method": "POST", "body": "{\"username\":\"u1\"}"},
  {"alias": "order", "weight": 3, "url": "{{baseUrl}}/api/scenario/order", "method": "POST",
   "headers": {"Authorization": "Bearer {{p1.result.data.token}}", "X-Trace-Id": "{{p1.header.X-Request-Id}}"},
   "body": "{\"userId\":\"{{login.result.data.userId}}\"}"},
  {"alias": "detail", "url": "{{baseUrl}}/api/scenario/order/{{order.result.data.orderId}}",
   "headers": {"Authorization": "Bearer {{p1.result.data.token}}"}}
]}
```

- 语法：`{{<stepRef>.<header|result>[.path]}}`；`stepRef` 为 `pN`（N≥1）或步骤 `alias`/`name`；`result` 支持点分路径递归下钻与数组下标；
- 渲染作用域覆盖 **URL / 请求体 / 请求头值**三处，未命中保留原文不中断压测；
- 兼容存量：`{{var}}` 扁平变量池与 extract 规则全部保留，新旧语法可混用；
- 语义：`pN` 指**步骤定义中第 N 个接口的最近一次响应**（"最新值"而非"本次链路值"），适合 token 等会话级数据；需要请求级强关联（如创建→详情的 ID）时仍用 extract；
- 启动校验产出 `warnings`（`pN` 越界、别名非法/重名）随结果 JSON 落库并在前端报告展示告警卡片；统计侧记 `renderMiss`。

设计详见 [design/scenario-step-context.md](design/scenario-step-context.md)，端到端自验：`scripts/verify-scenario-context.sh`（需 `PERF_ADMIN_PASSWORD` 与已启动的 server/demo）。

## 定时压测与 CI 性能门禁

- **定时调度**（`/api/stress/schedules`）：以压测任务为模板，按 `intervalMinutes`（1~10080）周期触发；支持启停、删除、**基线对比**（与上次触发任务或指定基线任务比对 QPS / RT / 错误率）；可配 Webhook 告警（默认阈值：QPS 下降 >10%、P95 上升 >20%、错误率上升 >5 个百分点）。
- **CI 门禁**（`POST /api/stress/gate` + `scripts/perf-gate.sh`）：触发一次压测 → 等待完成 → 与基线比对 → 返回 `passed` 结论；退出码 `0` 通过 / `1` 未通过 / `2` 调用异常。CI 侧使用静态令牌 `X-Perf-Api-Token`（平台 `perf.auth.api-token`）：

```bash
PERF_API_TOKEN=xxx GATE_URL=http://app:8081/api/user/list GATE_APP=demo-app \
GATE_CONCURRENCY=20 GATE_DURATION=60 ./scripts/perf-gate.sh
```

## Web 后台（10 个路由）

| 路由 | 页面 | 核心内容 |
| ---- | ---- | -------- |
| `/login` | 登录 | 账号密码登录，JWT 写入 localStorage；默认 `admin / admin123` |
| `/dashboard` | 总览 | 6 张指标卡 + AI 快捷指令 + 最近压测/慢 SQL/性能问题 + 快速开始入口 |
| `/quick-start` | 快速开始向导 | 七步：选项目 → 部署(可选) → 灌数据 → 发压测 → 看结果 → 分析调用链 → 问题优化；进度跨页面持久化于 sessionStorage |
| `/projects` | 项目管理 | zip / git / 本地目录三种导入并绑定应用；git 分支枚举与切换、重新同步；文件浏览与内容查看；一键清理项目历史数据 |
| `/deploy` | 部署管理 | 被测服务器 SSH 连接管理（密码 / 私钥 PEM），测试连接、按 ID 测试、删除；凭据仅落服务端，不进入 AI 上下文 |
| `/jvm` | 项目监控 | 堆/非堆/GC/线程/CPU 图表，5s 轮询可暂停；内嵌 JVM 阈值告警规则面板（按应用隔离，支持手动判定） |
| `/trace` | 调用链分析 | 三个 Tab：调用链（列表 + 调用树 + 火焰图）/ 慢命令（慢 SQL + Redis·ES·MQ 慢命令）/ CPU 热点（Top30 采样排行） |
| `/stress` | 压测中心 | 四个 Tab：压测（表单 + 实时曲线 + 报告 + 历史版本对比）/ 数据准备 / 容量建议 / 定时压测（调度 + Webhook + 基线对比） |
| `/optimize` | 性能优化 | 数据库连接（EXPLAIN）→ 问题列表（慢 SQL / 热点方法 / 压测瓶颈一键创建）→ 大模型多方案对比 → 修复提示词（复制/下载）→ 验证修复（重放压测对比 P95/QPS） |
| `/ai` | AI 助手 | SSE 流式对话（token/thought/tool/observation/done/error），工具调用卡片，ReAct 思考条，`/` 指令与 `@` 引用，支持 `?msg=&auto=1` 自动启动 |

前端工程：Vite dev 端口 5173，`/api` 代理到 `http://localhost:8080`；axios 请求拦截注入 `Bearer perf_token`，响应 401 清凭证跳登录；`npm run dev / build / test / test:mutation`（Vitest + Stryker）。

## AI 修复闭环

未配置 `ai.api-key` 时平台以**降级规则模板**演示完整闭环（基于真实库数据）：

1. 在 **压测中心** 对 `http://<被测>:8081/api/user/list?limit=200` 发起压测（如 20 并发 × 10s）；
2. 在 **项目管理** 导入被测项目源码（zip / git / 本地目录，并绑定应用名）；
3. 在 **AI 助手** 输入：
   - 「分析最近慢 SQL 的根因并给出修复方案」
   - 「当前性能能承受多少并发、何时扩容」
   - 「生成一份性能检测报告」
   - 「列出待处理的性能问题」
4. 在 **性能优化** 的问题详情中查看多方案对比，选定方案后生成**修复提示词**，复制到 AI 辅助编码工具即可落地修复；修复后可点「验证修复」重放原压测，自动对比 P95 降幅与 QPS 变化。

配置 `ai.api-key` 与 `ai.base-url`（OpenAI 兼容 API）后，AI 助手升级为 Function Calling 多轮编排（ReAct），模型自主选工具执行并汇总报告。工具清单：

| 工具 | 作用 |
| ---- | ---- |
| `stress_test` | 发起压测（支持 scenario 步骤数组） |
| `jvm_metrics` | 查询 JVM 时序指标 |
| `trace` | 查询调用链 |
| `slow_sql` | 查询慢 SQL |
| `data_gen` | 发起灌数据 |
| `suggest_data_rules` | 依表结构 + 语义提示库 + 枚举建议产出完整 ruleJson |
| `analyze_code_issues` | 关联源码做根因分析并生成多方案 |
| `generate_fix_prompt` | 按选定方案生成修复提示词 |
| `capacity_advice` | 容量评估与扩容建议 |
| `generate_report` | 生成性能检测报告 |
| `build_project` | 远程编译项目 |
| `deploy_and_start` | 远程部署并启动（含挂载探针） |
| `remote_app_status` | 查询远程应用运行状态 |
| `search_knowledge` | 检索 AI 知识库沉淀结论 |

无 API Key 时 `suggest_data_rules` 等工具自动降级为规则档。知识库结论随对话沉淀，可按应用检索与清理。

## 关键 API

> 鉴权：`/api/**` 默认需 JWT（`Authorization: Bearer`），白名单仅 `/api/auth/**` 与 `/api/agent/**`；`/api/agent/**` 另由 `X-Perf-Agent-Token` 校验；CI 可用静态令牌 `X-Perf-Api-Token`。

```
# 鉴权
POST /api/auth/login                       # 登录，返回 JWT（默认 admin / admin123）
GET  /api/auth/me                          # 会话探测

# 采集与上报
POST /api/agent/report             # agent 批量上报（JVM/调用链/慢SQL/慢命令/热点；项目不存在则丢弃）
GET  /api/apps                     # 应用实例列表
GET  /api/jvm/metrics              # JVM 指标查询
GET  /api/traces                   # 调用链列表
GET  /api/traces/{traceId}         # 调用链详情（含 trace_json 调用树）
GET  /api/slow-sqls                # 慢 SQL 查询
GET  /api/slow-commands            # 中间件慢命令（Redis/ES/MQ）
GET  /api/hotspots                 # 方法级 CPU 热点排行
POST /api/slowlog/parse            # MySQL 慢日志文本解析导入

# JVM 告警规则
GET|POST /api/jvm-alert-rules      # 规则列表 / 新增
PUT|DELETE /api/jvm-alert-rules/{id}   # 更新 / 删除
GET  /api/jvm-alert-rules/metrics  # 支持的指标键
POST /api/jvm-alert-rules/{id}/enabled  # 启停
POST /api/jvm-alert-rules/{id}/check    # 手动触发判定

# 数据库连接管理（EXPLAIN 分析 / 灌数据复用；密码 AES 加密落库）
GET|POST /api/dbconns              # 列表（仅 hasPassword 标记） / 保存
DELETE /api/dbconns/{id}           # 删除
POST /api/dbconns/test             # 测试连接（可携带 SQL 试跑 EXPLAIN）
POST /api/dbconns/{id}/test        # 按 ID 测试（凭据不出服务端）

# 压测
POST /api/stress/tasks             # 启动压测（engineType: http|jmeter；支持 scenario）
GET  /api/stress/tasks[?status]    # 任务列表
GET  /api/stress/tasks/{id}        # 任务详情
GET  /api/stress/tasks/{id}/samples    # 周期采样（实时曲线）
GET  /api/stress/tasks/{id}/report     # 压测报告（统计 + 采样点 + 容量评估 + warnings）
GET  /api/stress/trend             # 同应用同 URL 历史趋势对比
POST /api/stress/gate              # CI 性能门禁：压测→等完成→基线比对→passed 结论

# 定时压测调度
GET|POST /api/stress/schedules     # 调度列表 / 创建（intervalMinutes 1-10080 + 模板 + webhook）
PUT  /api/stress/schedules/{id}/enabled   # 启停
DELETE /api/stress/schedules/{id}  # 删除
GET  /api/stress/schedules/{id}/diff      # 基线对比

# 报告导出（不走 ApiResponse 封装，直接返回文件流）
GET  /api/reports/export?stressTaskId=&format=html|pdf|doc&printMode=

# 数据准备
POST /api/datagen/tasks            # 启动灌数据（dryRun=true 试运行；tables 表组联表；connId 复用连接）
GET  /api/datagen/tasks            # 任务列表
GET  /api/datagen/tasks/{id}       # 任务详情（进度/逐表统计/试运行报告/自检报告）
POST /api/datagen/tables           # 读取目标库表清单
POST /api/datagen/columns          # 列结构（含枚举建议值 / 疑似枚举标记）
GET  /api/datagen/suggestions      # 语义建议查询（魔法值 value / 关联 relation；status=pending 待确认）
POST /api/datagen/suggestions/{id}/adopt  # 建议采纳 / 忽略

# 源码管理
GET  /api/source/repos             # 源码仓库列表
POST /api/source/repos/zip         # 导入：zip 上传
POST /api/source/repos/git         # 导入：git 克隆
POST /api/source/repos/local       # 导入：本地目录
POST /api/source/repos/{id}/resync       # 重新同步源码并重建索引
POST /api/source/repos/{id}/cleanup-data # 清理项目历史数据
GET  /api/source/repos/{id}/endpoints    # 接口清单（源码扫描）
GET  /api/source/repos/{id}/branches     # 分支列表
POST /api/source/repos/git-branches      # 远端分支枚举（含私有仓库凭据）
POST /api/source/repos/{id}/branch       # 切换分支
POST /api/source/repos/{id}/bind-app     # 绑定应用
GET  /api/source/repos/{id}/files        # 文件树
GET  /api/source/files/{fileId}/content  # 文件内容

# 性能优化闭环
GET  /api/source/issues            # 性能问题列表
GET  /api/source/issues/{id}       # 问题详情
POST /api/source/issues/{id}/analyze     # 大模型根因分析（生成多方案）
POST /api/source/issues/{id}/suggestions/{sid}/select  # 选定方案生成修复提示词
POST /api/source/issues/{id}/verify      # 重放压测验证修复效果
GET  /api/source/issues/{id}/verify      # 查询验证状态
POST /api/source/issues/{id}/bind-repo   # 关联源码仓库
POST /api/source/issues/{id}/bind-stress # 关联压测任务
POST /api/source/issues/from-slow-sql    # 由慢 SQL 创建问题
POST /api/source/issues/from-hot-method  # 由热点方法创建问题
POST /api/source/issues/from-stress      # 由压测任务创建问题
GET  /api/source/recent-slow-sqls        # 最近慢 SQL

# 远程部署（SSH）
GET|POST /api/deploy/conns         # 连接列表 / 新增（密码或私钥 PEM）
DELETE /api/deploy/conns/{id}      # 删除
POST /api/deploy/conns/test        # 测试连接（不落库）
POST /api/deploy/conns/{id}/test   # 按 ID 测试
POST /api/deploy/build             # 远程编译（repoId + module）
POST /api/deploy/start             # 远程部署并启动（含挂载 agent）
GET  /api/deploy/status            # 远程应用运行状态

# AI 与容量
POST /api/ai/chat                  # AI 对话（降级 / Function Calling）
POST /api/ai/chat/stream           # AI 流式对话（SSE：token/thought/tool/observation/done/error）
GET  /api/ai/tools                 # Function Calling 工具清单
GET  /api/ai/knowledge             # 知识库检索
DELETE /api/ai/knowledge/{id}      # 删除知识条目
DELETE /api/ai/session/{sessionId} # 清空会话历史
POST /api/capacity/analyze         # 容量评估（目标 RT 下最大并发/QPS）
GET  /api/capacity/task/{taskId}   # 查询已落库的容量评估
GET  /api/capacity/advice          # 扩容建议 + JVM 配置建议
```

## 平台配置要点（`perf-server/src/main/resources/application.yml`）

| 配置项 | 默认值 | 说明 |
| ------ | ------ | ---- |
| `server.port` | `8080` | 平台端口 |
| `spring.datasource.*` | `MYSQL_URL/USER/PASSWORD` | 平台库，支持环境变量覆盖 |
| `perf.auth.enabled` / `username` / `password` | `true` / `admin` / `admin123` | JWT 鉴权与管理员账号（`PERF_ADMIN_USER` / `PERF_ADMIN_PASSWORD` / `PERF_JWT_SECRET`） |
| `perf.auth.agent-token` | 空 | 探针上报令牌；留空 = 不校验 |
| `perf.auth.api-token` | 空 | CI 静态令牌；留空 = 不启用 |
| `perf.crypto.secret` | 空 | DbConn 密码 / DeployConn SSH 凭据 AES 加密密钥；留空 = 不加密 |
| `perf.source.storage-dir` | `/data/perf-source` | 源码存储根目录（另有单文件 / 总量 / 文件数上限） |
| `perf.stress.*` | `http` / `5000` / `3600` | 默认引擎、并发上限、时长上限，`jmeter-home` 空则探测 PATH |
| `perf.capacity.*` | P95 200ms、CPU 80% 等 | 容量评估与扩容触发阈值 |
| `perf.ai.*` | `gpt-4o-mini`、`PERF_AI_API_KEY` | OpenAI 兼容 API，无 key 自动降级规则模式 |
| `perf.verify.*` | `300` / `2` | 修复验证超时与并发上限 |
| `perf.agent.*` | `200` / `5000` | 上报落库批大小与缓冲区上限 |

## 数据库

平台库共 20 张表（两个脚本结构一致，差异仅在是否带演示数据；`init_empty.sql` 为脚本默认）：

`app_instance`、`jvm_metric`、`trace_chain`、`slow_sql`、`slow_command`、`hotspot_method`、`stress_task`、`stress_sample`、`stress_schedule`、`data_gen_task`、`semantic_hint`、`source_repo`、`source_file`、`performance_issue`、`fix_suggestion`、`chat_message`、`db_conn`、`deploy_conn`、`jvm_alert_rule`、`ai_knowledge`。

建表脚本幂等（`CREATE TABLE IF NOT EXISTS`），重复执行安全。

## 测试与质量门禁

| 模块 | 内容 |
| ---- | ---- |
| perf-server | 31 个测试类，覆盖压测引擎（`HttpStressEngine` / `JmeterStressEngine` / 场景 / 步骤上下文 / 统计）、AI（ReAct 循环 / 流式 / 知识库 / 提示词）、鉴权（JWT / Agent Token）、基线对比、压力门禁、JVM 告警、修复验证、报告导出、凭据加密、慢命令上报等 |
| perf-datagen | 13 个测试类约 85 个用例（H2 MySQL 模式驱动全流程）；PIT 突变测试阈值 75 |
| perf-web | Vitest + jsdom 单测，Stryker 突变测试（`npm run test:mutation`） |

端到端脚本：`verify-datagen-mysql.sh`、`verify-datagen-postgres.sh`、`verify-scenario-context.sh`、`perf-gate.sh`（CI）。

## 归档约定

开发过程中的全部中间产出物归档于 `docs/`（纳入 git 管理，不写入 `.gitignore`）：

| 目录 / 文档 | 说明 |
| ---- | ---- |
| [design/requirements.md](design/requirements.md) | 需求文档：功能模块与边界说明 |
| [design/architecture.md](design/architecture.md) | 整体架构：平台与被测应用分离部署拓扑 |
| [design/tech-solution.md](design/tech-solution.md) | 技术方案：技术栈、关键设计决策、代码结构 |
| [design/database-design.md](design/database-design.md) | 数据库设计说明：核心表与分区策略 |
| [design/scenario-step-context.md](design/scenario-step-context.md) | 多接口场景步骤上下文引用设计与实现记录 |
| [handoff/](handoff/) | 交接文档：`handoff-main.md`、`environment-setup.md`、`scenario-step-context.md` |
| [plans/](plans/) | 迭代计划与优化方案（灌数引擎优化、性能改进计划等） |
| [reports/](reports/) | 性能检测报告归档（压测报告 / JVM 分析 / 容量评估） |
| [issues/](issues/) | 性能问题分析归档（证据 + 根因 + 方案对比 + 修复提示词） |
| [artifacts/](artifacts/) | 其他中间产物（SQL 导出、数据字典、联调记录） |
