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
| `perf-engine <scenario.yaml> -e locust [-o result.json]` | 本地引擎发压（不经平台），产出对齐 StressTaskResult 契约的工件 |
| `perf version` | 版本号 |

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
├── perf_cli/       # CLI 入口 + 配置 + 平台 HTTP 客户端
├── scenario/       # 场景即代码：模型 + YAML 加载 + 场景文件
├── profiles/       # 环境配置（TOML）
└── tests/          # 单测
```

> 注意：默认压测目标为 perf-demo 真实端点（`http://localhost:8081/api/order/list`），
> `scripts/perf-gate.sh` 注释中的 `18080/api/orders` 是过时默认值。
