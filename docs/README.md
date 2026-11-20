# PerformanceTestor — 性能检测智能体平台

面向 Java 应用的**性能检测智能体平台**：通过 Java Agent 无侵入采集 JVM 指标、方法调用链耗时、慢 SQL、中间件慢命令与 CPU 热点；平台提供压测引擎、灌数据引擎、容量评估与 AI 修复闭环。

> **当前架构状态（绞杀者迁移完成）**：Java 版 `perf-server`（Spring Boot 平台）与 `perf-datagen`（灌数引擎）已由 Python 工程层 **`perf-py`** 重构取代（历史 Java 代码归档于 tag `v-java-final`）。平台 `/api` 契约由 `perf-py/server`（FastAPI）承接，前端 `perf-web` 零改动；Java 探针 `perf-agent` 因只能在 JVM 内工作而保留 Java。

## 模块总览

```
/workspace/
├── perf-agent/     # 监控探针（Java 17 + ByteBuddy）：方法埋点 / JDBC 拦截 / 中间件拦截 /
│                   #   JMX 采集 / CPU 热点采样 / HTTP 批量上报
├── perf-py/        # Python 工程层（主力）：
│                   #   server/    FastAPI 平台（/api 契约：auth/agent/stress/datagen/
│                   #               datasets/source/dbconns/ai 八个域）
│                   #   perf_cli/  perf CLI（压测/门禁/灌数/Agent 工具/MCP）
│                   #   engine/    本地压测引擎（locust/jmeter，统计层逐位对齐 Java）
│                   #   ptde/      智能测试数据引擎 PTDE（分析/规划/生成/装载/生命周期）
│                   #   tools/ agents/  AI Agent 工具层（7 工具，OpenAI/MCP 双形态）+ ReAct 编排
│                   #   scenario/ stress/ slowlog/ capacity/  场景即代码 / 模板渲染 / 慢日志 / 容量
├── perf-web/       # 前端（Vue3 + TS + Vite + Tailwind + ECharts），零改动对接 Python server
├── perf-demo/      # 示例被测应用（Spring Boot 3.2 + MyBatis）：N+1 / 慢 SQL 演示热点
├── sql/            # init.sql（含演示数据）/ init_empty.sql（空表，脚本默认）
├── scripts/        # build.sh / start-all.sh / start-server.sh / start-agent.sh /
│                   #   perf-gate.sh(CI 门禁) / verify-datagen-*{,-py}-*.sh / capture-contract.sh
└── docs/           # 本文档 + design / handoff / plans / reports / issues / artifacts 归档
```

## 快速开始（单机演示模式）

前置条件：JDK 17+、Maven、Node ≥22、Python ≥3.12（推荐 uv）、MySQL 8（默认 `root/root123`，可用环境变量覆盖）。

```bash
# 1. 全量构建（Java 两模块 + perf-py 安装 + 前端 dist）
cd /workspace && scripts/build.sh          # --skip-backend / --skip-frontend / --skip-python 可分段构建

# 2. 一键启动（MySQL + Python server + perf-demo(挂 agent) + perf-web）
scripts/start-all.sh
#    Web 后台:   http://localhost:5173      （/api 经 VITE_API_TARGET 代理到 Python server :8000）
#    API 服务:   http://localhost:8000/api  （Python server，FastAPI）
#    被测应用:   http://localhost:8081/api/order/list?status=PAID   （慢 SQL 热点接口）

# 3. 手动分步启动（等价）：
#    mvn -q -pl perf-agent,perf-demo package -DskipTests
#    (cd perf-py && PERF_PY_DATABASE_URL="mysql+pymysql://root:root123@127.0.0.1/performance_testor?charset=utf8mb4" uv run perf serve --port 8000)
#    java -javaagent:perf-agent/target/perf-agent.jar=reportUrl=http://127.0.0.1:8000/api/agent/report,appName=perf-demo,appPort=8081,sampleRate=1.0 \
#         -jar perf-demo/target/perf-demo.jar --server.port=8081 &
#    (cd perf-web && VITE_API_TARGET=http://localhost:8000 npm run dev)
```

关键环境变量：

| 变量 | 默认值 | 说明 |
| ---- | ------ | ---- |
| `SERVER_PORT` / `WEB_PORT` / `DEMO_PORT` | `8000` / `5173` / `8081` | Python server / 前端 / 被测应用端口 |
| `PERF_PY_DATABASE_URL` | sqlite 内存库 | Python server SQLAlchemy 连接（生产口径 `mysql+pymysql://user:pwd@host/performance_testor`） |
| `MYSQL_HOST\|PORT\|USER\|PASSWORD` | `127.0.0.1` / `3306` / `root` / `root123` | 被测库与建库脚本连接 |
| `SQL_FILE` | `sql/init_empty.sql` | 需要首屏演示数据改用 `sql/init.sql` |
| `VITE_API_TARGET` | `http://localhost:8080` | 前端 dev 代理目标（指向 Python server 时设 `http://localhost:8000`） |
| `PERF_LLM_MODEL/BASE_URL/API_KEY` | 空 | OpenAI 兼容 API；未配置时 AI 对话以 `mode=rule` 兜底，不影响启动 |

## Python CLI（perf-py）

```bash
cd perf-py
uv sync --all-extras        # 或 pip install -e .（extras 按需：locust/datagen-drivers/agents/server/security）
uv run perf version
uv run pytest               # 单测（引擎族需 PERF_PY_ENGINES=1 单独运行）
uv run ruff check .         # lint
```

| 命令 | 说明 |
|---|---|
| `perf stress run <scenario.yaml> [-p profile] [-o out.json]` | 读场景 → `POST /api/stress/gate` → 等待完成 → 输出报告 + JSON 工件 |
| `perf gate [-s scenario.yaml]` | CI 性能门禁（替代 `scripts/perf-gate.sh`），退出码 0/1/2 语义一致 |
| `perf scenario validate <file>` | 场景 YAML 校验（结构/负载参数/断言规则） |
| `perf datagen run <spec.json> [--dry-run] [--seed N] [-o out.json]` | 灌数执行（执行体路由到 PTDE 真库管线，目标库 jdbc:mysql://） |
| `perf ptde analyze <dir>` | 项目源码证据快照分析 |
| `perf ptde profile [--collect --config c.json] [profile.json]` | 统计画像（inspect / 只读采集） |
| `perf ptde plan <spec.json> [--preview / --def-key K]` | 数据集规划（离线预览 / 注册 Definition） |
| `perf ptde dataset <create/build/status/verify/list/cleanup> ...` | 数据集生命周期（READY 门禁；生产环境无构建/清理入口） |
| `perf ptde migrate-legacy` | legacy 三表 → ptde_* 存量迁移（幂等 + 核对报告） |
| `perf tools list` / `perf tools call <name> -a '<json>'` | Agent 工具直接调用（7 个，双形态暴露） |
| `perf mcp` | MCP server（stdio JSON-RPC 2.0），IDE / Agent 子进程接入 |
| `perf agent run <task> [-r pipeline]` | ReAct Agent / 三角色流水线（压测→分析→修复建议） |
| `perf serve [--port 8000] [--knowledge-db kb.sqlite]` | 启动 Python server（FastAPI） |
| `perf-engine <scenario.yaml> -e locust [-o result.json]` | 本地引擎发压（不经平台），产出对齐 StressTaskResult 契约的工件 |

退出码约定（stress/gate/datagen/agent 全线一致）：`0` = 成功/通过；`1` = 未通过（回归/失败/截断）；`2` = 调用异常（参数/网络/配置缺失）。

## 多机分离部署

平台服务器与被测服务器分离，仅需三处网络互通：**平台 → 被测**（发起压测、灌数据）、**被测 → 平台**（agent 上报 `:8000/api/agent/report`）、**平台 → 被测库**（灌数据 3306）。

```bash
scripts/start-server.sh     # 平台侧：Python server + perf-web
REPORT_URL=http://<平台IP>:8000/api/agent/report scripts/start-agent.sh   # 被测侧
```

`start-agent.sh` 可配置项：`REPORT_URL` / `DEMO_PORT` / `SAMPLE_RATE`(演示 1.0，生产建议 0.1) / `SLOW_SQL_THRESHOLD_MS`(200) / `MIDDLEWARE_ENABLED`(false) / `SLOW_COMMAND_THRESHOLD_MS`(100) / `AGENT_TOKEN`（需与平台侧一致）。

> agent 上报时以 `appName` 匹配平台「源码仓库」项目名做准入校验（ collector 域）；上报自动注册应用实例（来源 IP + `appPort` + 版本号），压测中心选定项目后服务地址自动预填。

### 探针低开销设计（对被测应用影响最小化）

- 埋点范围：显式 `includePackages` > 被测 jar `Start-Class` 包名自动推导（推荐零配置）> Spring 注解兜底；
- POJO/数据类不埋点（包名启发 + 结构判断 + 枚举/record 三层排除）；构造器/getter/setter/桥接合成/main 不埋；
- 业务线程零转换：调用链原生树 O(1) 入队，序列化全部在 reporter 线程完成；单 ThreadLocal 上下文，未采样链路零分配；
- 采样率（比例）+ 每秒令牌桶（`maxTracesPerSec`，默认 50/s）+ trace 队列条数与字节预算（`traceBufferBytes`，默认 8MB）三重控制；
- JDBC 惰性 SQL：enter 仅取参数引用，超阈值才做 toString 兜底与截断；
- Servlet Filter 根模式（默认开）消除采样稀释与半截链路；中间件拦截与 CPU 热点采样默认关闭，按需开启。

### Agent 参数表（`-javaagent:perf-agent.jar=k=v,...`）

| 参数 | 默认值 | 说明 |
| ---- | ------ | ---- |
| `reportUrl` | `http://127.0.0.1:8080/api/agent/report` | 平台上报地址（多机部署必改，Python server 默认 8000） |
| `appName` | `unknown-app` | 应用名，需与平台项目名一致 |
| `targetProjectName` | 空 | 目标项目名，优先于 `appName` 做准入匹配 |
| `appPort` | `0` | 被测 HTTP 端口，供压测表单预填服务地址 |
| `appVersion` | 空 | 版本号，留空自动读 jar MANIFEST |
| `sampleRate` | `0.1` | 调用链采样率 0~1 |
| `includePackages` | 空 | 显式埋点包前缀（逗号分隔），留空自动推导 |
| `slowSqlThresholdMs` / `slowCommandThresholdMs` | `500` / `100` | 慢 SQL / 中间件慢命令阈值 |
| `middlewareEnabled` / `hotspotEnabled` | `false` | Redis·ES·MQ 拦截 / CPU 热点采样开关 |
| `methodTraceEnabled` / `jdbcEnabled` / `jmxEnabled` / `filterRootEnabled` | `true` | 各类采集开关 |
| `batchSize` / `bufferSize` | `500` / `5000` | 上报批次 / 缓冲区上限（超出丢弃最旧） |
| `maxTracesPerSec` / `traceBufferBytes` | `50` / `8388608` | 每秒 trace 上限 / 队列字节预算 |
| `agentToken` | 空 | 上报令牌，需与平台侧一致 |

## perf-demo 示例应用（可分析问题点）

| 问题类型 | 位置 | 表现 |
| -------- | ---- | ---- |
| N+1 查询（最典型） | `UserService.listUsersWithOrderCount`（循环内逐条 `countOrders`） | `GET /api/user/list?limit=N`，N 个用户产生 N+1 次 SQL |
| 慢 SQL：无索引全表扫描 | `OrderMapper.selectByStatus`（`status` 列刻意无索引） | `GET /api/order/list?status=PAID` |
| 慢 SQL：大表 GROUP BY | `OrderMapper.selectStats`（5 万行聚合） | `GET /api/order/stats` |
| 慢 SQL：无覆盖索引排序 | `OrderMapper.selectRecent`（`created_at` filesort） | `GET /api/order/recent` |

启动时自动建表并灌入 1 万用户 / 5 万订单（幂等，仅空表灌入；`demo.data.enabled=false` 可只建表）。压测 `GET /api/user/list?limit=200`（20 并发）可稳定复现 N+1 热点调用链与慢 SQL 上报。

## 灌数引擎（PTDE）

灌数能力沉淀为 `perf-py/ptde/`（智能测试数据引擎）：源码分析（SnapshotBuilder 证据快照）→ 统计画像 → 数据集规划（规模/分布/预算）→ 生成与装载（MySQL 原生 `LOAD DATA`/批插降级链）→ 生命周期（create/build/verify/cleanup，READY 门禁）。`perf datagen run` 直连数据库执行（Dry-Run 优先/拓扑排序/批量写入），规格 JSON 对齐 Java `DataGenRequest` 形状。端到端验收：`scripts/verify-datagen-py-mysql.sh` / `verify-datagen-py-postgres.sh`（直连）；`verify-datagen-mysql.sh` / `-postgres.sh`（经平台 REST）。

## Web 后台

`perf-web`（Vue3 + Vite dev 端口 5173）通过 `/api` 代理对接平台（`VITE_API_TARGET` 切换目标），页面：登录 / 总览 / 快速开始向导 / 项目管理 / 数据库连接 / 部署管理 / 项目监控(JVM) / 调用链分析 / 压测中心 / 数据集 / 性能优化 / AI 助手。前端零改动对接 Python server（契约测试保证 `/api` 结构兼容）。

## AI Agent 闭环

- **工具层**（`perf-py/tools/`）：7 个工具双形态暴露（OpenAI Function Calling schema + MCP server）：`stress_result_read` / `stress_run` / `capacity_analyze` / `slowlog_analyze` / `source_scan` / `datagen_dry_run` / `datagen_run`。handler 出参 JSON 可序列化，异常一律转 `{"error": ...}` 不向 Agent 抛栈。
- **编排**（`perf-py/agents/`）：ReAct 循环单体（max_steps 防死循环）+ 三角色流水线（压测→分析→修复建议），角色间只传结构化上下文，单角色截断不阻断下游（`degraded` 标记）。配置 `PERF_LLM_MODEL / PERF_LLM_BASE_URL / PERF_LLM_API_KEY` 后运行：

```bash
perf agent run "对 demo 场景压测并归因 P95 恶化根因" -r pipeline
```

- **知识库**：`KnowledgeStore` 协议 + SQLite 实现（LIKE + 命中计数评分，沉淀阈值 40 字、bigram Jaccard ≥0.9 去重）；MySQL FULLTEXT 与向量检索为同契约替换实现。

## 关键 API（Python server 承接的 `/api` 契约域）

> 鉴权：`/api/auth` 登录签发 JWT；`/api/agent/**` 由探针令牌校验。游标分页端点返回 `{ items, nextCursor, hasMore }`。

```
/api/auth/**                # 登录 / 会话探测（auth 域）
/api/agent/report           # agent 批量上报（JVM/调用链/慢SQL/慢命令/热点；collector 域）
/api/stress/**              # 压测任务 / gate 门禁 / jmx 导入导出 / trend / samples / report（stress 域）
/api/datagen/**             # 灌数 plan analyze/adopt/execute / tasks / tables / columns / suggestions
/api/datasets/**            # PTDE 数据集生命周期（plan/create/build/verify/cleanup）
/api/source/**              # 源码仓库（zip/git/local 导入、分支、文件树）与性能问题闭环
/api/dbconns/**             # 数据库连接管理（EXPLAIN / 灌数复用）
/api/ai/**                  # AI 对话（同步 + SSE 流式）/ 工具清单 / 知识库
```

## 数据库

平台库建表脚本：`sql/init.sql`（含演示数据）/ `sql/init_empty.sql`（空表，脚本默认），均幂等。Python server 数据层走 SQLAlchemy（ORM `create_all` 建齐 stress / collector 域模型；分区表 DDL 由 init 脚本维护）。

## 测试与质量门禁

| 模块 | 内容 |
| ---- | ---- |
| perf-py | ~102 个测试文件 + 12 个 contract 契约测试（golden 双层黑盒对照，AI/stress/datagen/auth/source/issue 域）；ruff lint；引擎族用例 `PERF_PY_ENGINES=1` 单独运行；真库用例 `PTDE_REALDB_URL` 门控 |
| perf-agent | JUnit 5 单测（探针核心链路）；reporter/jmx/hotspot 包覆盖较薄 |
| perf-web | Vitest + jsdom 单测；Stryker 突变测试（`npm run test:mutation`，已覆盖代码杀死率阈值 70） |

契约纪律（绞杀者模式）：`tests/contract/` 黑盒契约是迁移切流门禁——先契约后切流，`scripts/capture-contract.sh` 可对运行中的 server 重新捕获 golden。

## 归档约定

开发过程中的全部中间产出物归档于 `docs/`：

| 目录 / 文档 | 说明 |
| ---- | ---- |
| [design/](design/) | 需求 / 架构 / 技术方案 / 数据库设计（部分描述 Java 时代架构，属历史归档） |
| [handoff/](handoff/) | 交接文档（Java 时代为主，历史归档） |
| [plans/](plans/) | 迭代计划与优化方案（PTDE R0–R7 / B4~B7 系列交接等，历史过程文档） |
| [reports/](reports/) | 性能检测报告归档 |
| [review/](review/) | 架构/闭环评审报告（如 closed-loop-audit：main 分支性能测试闭环诊断与改进方案） |
| [issues/](issues/) / [artifacts/](artifacts/) | 性能问题分析 / 其他中间产物 |

> Java 时代历史文档（含 perf-server / perf-datagen 相关 design、handoff、plans）按原样保留供追溯；理解当前架构以本 README 与 `perf-py/README.md` 为准。
