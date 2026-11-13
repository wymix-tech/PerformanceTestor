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

## 命令（阶段 1）

| 命令 | 状态 | 说明 |
|---|---|---|
| `perf stress run <scenario.yaml>` | 步骤 3 | 读场景 → `POST /api/stress/gate` → 等待完成 → 输出报告 |
| `perf gate` | 步骤 3 | 替代 `scripts/perf-gate.sh`，退出码 0/1/2 语义对齐 |
| `perf scenario validate <file>` | 步骤 2 | 场景 YAML 校验 |
| `perf version` | 可用 | 版本号 |

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
