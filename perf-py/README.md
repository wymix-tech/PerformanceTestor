# perf-py

PerformanceTestor 的 Python 工程层（[Issue #23](https://cnb.cool/wymix.top/PerformanceTestor/-/issues/23)）。

> 目标 1：可重复执行的性能测试脚本（场景即代码、入版本库、CI 可门禁）
> 目标 2：AI Agent 工具层（阶段 4 兑现）

## 边界（绞杀者模式）

| 模块 | 决策 |
|---|---|
| `perf-agent`（探针） | 保留 Java（ByteBuddy/JMX 只能在 JVM 内） |
| `perf-server` | 分阶段迁移，本工程逐步接管 `/api` 契约 |
| `perf-demo` | 不迁移（它是 Java 被测物） |
| `perf-web` | 零改动（保持 `/api` REST 契约） |

## 快速开始

```bash
cd perf-py
uv sync                     # 或 pip install -e .
uv run perf version
uv run pytest               # 单测
uv run ruff check .         # lint
```

## 命令

| 命令 | 说明 |
|---|---|
| `perf stress run <scenario.yaml>` | 读场景 → `POST /api/stress/gate` → 等待完成 → 输出报告 + JSON 工件 |
| `perf gate [--scenario x.yaml]` | 替代 `scripts/perf-gate.sh`，退出码 0/1/2 语义对齐 |
| `perf scenario validate <file>` | 场景 YAML 校验 |
| `perf datagen run <spec.json> [--dry-run] [--source-dir DIR]` | 灌数引擎直连执行（Dry-Run 优先/拓扑排序/批量写入），结果 camelCase JSON |
| `perf ptde analyze <dir>` | 项目源码证据快照分析（SnapshotBuilder） |
| `perf ptde profile [--collect --config c.json] [profile.json]` | 统计画像（缺省 inspect；--collect 只读采集） |
| `perf ptde plan <spec.json> [--preview / --def-key K]` | 数据集规划（离线预览 / 注册 Definition） |
| `perf ptde dataset <create/build/status/verify/list/cleanup> ...` | 数据集生命周期（READY 门禁；生产环境无构建/清理入口） |
| `perf ptde migrate-legacy` | legacy 三表 → ptde_* 存量迁移（幂等 + 行数核对报告） |
| `perf tools list` / `perf tools call <name> -a '<json>'` | Agent 工具直接调用（7 个，见下） |
| `perf mcp` | MCP server（stdio JSON-RPC 2.0），IDE / Agent 子进程接入 |
| `perf agent run <task> [-r pipeline]` | ReAct Agent / 三角色流水线（压测→分析→修复建议） |
| `perf serve [--port 8000]` | Python server（FastAPI，逐域承接 /api 契约） |
| `perf-engine <scenario.yaml> -e locust [-o result.json]` | 本地引擎发压（不经平台），产出对齐 StressTaskResult 契约的工件 |
| `perf version` | 版本号 |

## AI Agent 工具层（阶段 4 · 步骤 1）

阶段 2/3 能力包装为 6 个 Agent 工具，双形态暴露（OpenAI Function Calling schema
via `tools.registry.openai_tools_schema` / MCP server via `perf mcp`）：

| 工具 | 能力来源 | 用途 |
|---|---|---|
| `stress_result_read` | engine/result | 读压测工件：QPS/P95/P99/错误率 + 分档点 |
| `stress_run` | engine/（perf-engine 子进程） | 发起本地压测（阻塞执行，非只读） |
| `capacity_analyze` | capacity/ | 目标 P95 下最大并发/QPS、RT 拐点、R²、CI |
| `slowlog_analyze` | slowlog/ | 慢日志 Top N + 聚合（扫描行数/耗时分布） |
| `source_scan` | ptde 源码解析层 | ORM 关联 + SQL 字面量/枚举/范围谓词提示 |
| `datagen_dry_run` | ptde/（经 perf_cli.datagen_runner） | 灌数试运行（校验 + 估算，不写库） |
| `datagen_run` | ptde/（经 perf_cli.datagen_runner） | 灌数正式执行（写库，标非只读） |

约定：handler 入参 dict、出参 JSON 可序列化 dict；异常一律转为
`{"error": ...}`，不向 Agent 抛栈。典型链路：`datagen_dry_run` 造数 →
`stress_run` 发压 → `stress_result_read` 读指标 →
`capacity_analyze` + `slowlog_analyze` + `source_scan` 关联归因。

## Agent 编排（阶段 4 · 步骤 2）

`perf-py/agents/`：ReAct 循环单体（思考→工具→观察→结论，max_steps 防死循环）
+ 三角色流水线原型：

```bash
pip install -e 'perf-py[agents]'        # openai-python（懒加载）
export PERF_LLM_MODEL=... PERF_LLM_BASE_URL=... PERF_LLM_API_KEY=...
perf agent run "对 demo 场景压测并归因 P95 恶化根因" -r pipeline   # 压测→分析→修复建议
perf agent run "分析 /tmp/result.json" -r analysis --max-steps 8  # 单角色
```

角色间只传结构化上下文（AgentReport.context_block），单角色截断不阻断下游
（降级推理，`degraded` 标记）。全部测试由脚本化假 LLM 驱动，无网络依赖。

## 契约固化（阶段 5 · 步骤 1）

`tests/contract/`：对运行中的 Java perf-server 做黑盒契约捕获（本仓库无 springdoc，
以响应结构 golden 替代 OpenAPI 导出），作为按域迁移的切流门禁：

```bash
BASE=http://127.0.0.1:8080 ./scripts/capture-contract.sh   # 重新捕获（Java 侧有数据环境）
uv run pytest tests/contract/ -v                            # 契约测试
```

规范化规则：dict 保留键名、list 取首元素、标量 → 类型标记（结构与键名即契约）。
golden 含 normalized + raw 双层。AI 域已对照通过；stress/datagen/auth 域 golden
已入库，域实现落地后替换占位断言（绞杀者纪律：先契约后切流）。

## Python server（阶段 4 · 步骤 3/4）

`perf-py/server/`：FastAPI 骨架，AI 域 `/api/ai` 先行接管（绞杀者模式），契约逐端点
对齐 Java（AiController + AiChatStreamController）：

| 端点 | 说明 |
|---|---|
| `POST /api/ai/chat` | 同步对话：{reply, toolCalls, mode(llm/rule), role, rounds, steps} |
| `POST /api/ai/chat/stream` | SSE 流式对话（thought/tool/observation/token/done/error） |
| `GET /api/ai/tools` | Agent 工具清单（与 Function Calling 同源 registry） |
| `GET /api/ai/knowledge` | 知识检索（query 空时列最近；无命中如实返回空） |
| `DELETE /api/ai/knowledge/{id}`、`DELETE /api/ai/session/{sid}` | 清理入口 |

```bash
pip install -e 'perf-py[server,agents]'
export PERF_LLM_MODEL=... PERF_LLM_BASE_URL=... PERF_LLM_API_KEY=...
perf serve --port 8000 --knowledge-db ./kb.sqlite   # 未配 LLM 时 mode=rule 兜底，不 500
```

知识库：`KnowledgeStore` 协议 + SQLite 起步实现（LIKE + 命中计数评分；
沉淀阈值 40 字、bigram Jaccard ≥0.9 去重回看 50 条，语义对齐 Java
AiKnowledgeService）；MySQL FULLTEXT（ngram）与向量检索（RAG）为同契约替换实现。

测试闸门：conftest 默认阻断 locust/gevent 导入（收集期 patch 会污染全进程，
导致网络型测试挂起）；引擎族用例用 `PERF_PY_ENGINES=1 uv run pytest
tests/test_locust_engine.py` 单独运行。

## stress 写路径（阶段 5 · 步骤 6）

`POST /api/stress/tasks` 与 `POST /api/stress/gate` 迁入 Python server（校验文案、
任务落库语义、GateVerdict/基线比对文案逐字对齐 StressTaskService / StressGateService /
BaselineCompareService）：

- 执行模型：gevent 与 uvicorn 运行时不能共存——默认 `SubprocessRunner` 在 worker
  子进程（`python -m server.stress_worker`）内跑引擎，采样实时落 `stress_sample`；
  内存库（测试）自动降级 `InlineRunner` 进程内执行。
- 引擎映射：平台 `http` 引擎由 locust 引擎等价承担（不做手写线程发压，见阶段 2
  长尾实测结论）；场景模式回退规则与 Java `resolveEngine` 一致。
- 分档模式：对齐 `HttpStressEngine`——仅首档 warmup、逐档产 StagePoint、
  qps/p95/p99 取末档；`stageConcurrencies` 简写均分展开语义一致。
- 门禁超时：下限=预热+时长+爬坡+原始 stages 各档+60s 余量（简写档不计，对齐 Java）、
  硬上限 1800s；失败时 webhook 告警（飞书/钉钉/自定义自适应，尽力而为）。

## 灌数引擎（阶段 3 步骤 5）

`perf datagen run` 直连数据库执行灌数（不经 perf-server REST），宿主装配对齐 Java
`DataGenEngineConfig`：MySQL + PostgreSQL 双方言路由、MyBatis/JPA 双 ORM 解析器注册。

```bash
pip install -e 'perf-py[datagen-drivers]'        # pymysql / psycopg2-binary
perf datagen run spec.json --dry-run             # 试运行（估算 + 校验，不写入）
perf datagen run spec.json --source-dir ../perf-demo/src/main/java -o result.json
scripts/verify-datagen-py-mysql.sh               # 真实库端到端冒烟
scripts/verify-datagen-py-postgres.sh
```

spec JSON 形状对齐 Java `DataGenRequest`（dbUrl/dbUser/dbPassword/dryRun/tables，
fields 为规则 JSON，兼容旧单表 ruleJson 形式）；退出码：0 成功 / 1 引擎失败 / 2 调用异常。

## 本地引擎选择（阶段 2 实测结论）

| 引擎 | 适用 | 实测 |
|---|---|---|
| `locust`（`--extra locust`） | 场景编排/混合流量/Agent 编排/QPS 容量验证 | QPS、P50 与 Java 引擎对齐；**P95/P99 长尾系统性偏高**（Python 客户端每请求处理成本所致，多进程分摊无效，已实测） |
| `jmeter` | JVM 客户端兜底（需本机安装 jmeter CLI） | 已实现；长尾敏感场景用此引擎或平台 Java 引擎 |

统计层三引擎统一：平坦 30000 桶×1ms 直方图，逐位对齐 Java `StressStatistician`；
结果契约 `perf-py.result.v1`（camelCase，与 Java `resultJson` 结构一致，可直接导入）。

## 配置

环境变量优先，profile（`profiles/<name>.toml`）兜底：

- 平台连接：`PERF_SERVER`（默认 `http://localhost:8080`）、`PERF_API_TOKEN`
- 门禁参数：`GATE_URL` / `GATE_APP` / `GATE_METHOD` / `GATE_BODY` / `GATE_CONCURRENCY` /
  `GATE_DURATION` / `GATE_WARMUP` / `GATE_RAMP_UP` / `GATE_QPS_LIMIT` / `GATE_BASELINE` /
  `GATE_TIMEOUT` / `GATE_FAIL_NO_BASE` / `GATE_WEBHOOK`

退出码：`0` = 门禁通过；`1` = 未通过（回归/超时/失败）；`2` = 调用异常（参数/网络）。
与 `scripts/perf-gate.sh` 完全对齐，可在 CI 中直接替换。

## 目录

```
perf-py/
├── pyproject.toml
├── perf_cli/       # CLI 入口 + 配置 + 平台 HTTP 客户端 + MCP + PTDE/灌数 runner
├── scenario/       # 场景即代码：模型 + YAML 加载 + 场景文件
├── profiles/       # 环境配置（TOML）
├── engine/         # 本地压测引擎（locust/jmeter）+ 统一统计层
├── stress/         # 压测辅助（模板渲染、断言、变量提取、CSV 参数化）
├── slowlog/        # MySQL 慢日志解析
├── capacity/       # 容量分析 + 曲线拟合
├── ptde/           # 智能测试数据引擎（分析/规划/生成/装载/校验/生命周期/适配器）
├── ptde_common/    # PTDE 公共层（模型基类、ID 池、枚举发现）
├── tools/          # AI Agent 工具层（7 工具 + registry + OpenAI/MCP 双形态 schema）
├── agents/         # ReAct 循环 + 三角色流水线 + LLM 客户端
├── server/         # FastAPI 平台（逐域承接 /api 契约：auth/agent/stress/datagen/datasets/source/dbconns/ai）
├── benchmarks/     # 基准脚本
└── tests/          # 单测 + contract 契约测试
```

> 注意：默认压测目标为 perf-demo 真实端点（`http://localhost:8081/api/order/list`），
> `scripts/perf-gate.sh` 默认值已同步修正。
