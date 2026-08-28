# PerformanceTestor — 性能检测智能体平台

面向 Java 应用的**性能检测智能体平台**：平台（perf-server、MySQL、源码存储）与被测应用**分离部署**在不同服务器，避免压测与监控互相干扰。通过 Java Agent 无侵入采集 JVM 指标、业务方法调用链耗时与慢 SQL；提供自研压测引擎与自动灌数据能力；支持 AI 自然语言对话自动执行检测任务；采集项目源代码，将检测结果关联到源码位置，由大模型分析根因并给出多种修改方案，选定方案后生成修复提示词供 AI 辅助编码工具直接修复。

## 模块总览

```
/workspace/
├── perf-agent/     # 监控探针（Java 17 + ByteBuddy）：方法埋点 / JDBC 拦截 / JMX 采集 / HTTP 批量上报
├── perf-server/    # 后端平台（Spring Boot 3.x + MyBatis + MySQL）：采集存储 / 压测引擎 / 灌数据 /
│                   #   容量评估 / 扩容建议 / 数据库连接管理 / 源码管理 / AI Function Calling 编排
├── perf-web/       # 前端（Vue3 + TS + Vite + ECharts）：七屏可视化
├── perf-demo/      # 示例被测应用（Spring Boot 3.x + MyBatis）：含 N+1 / 慢 SQL 等可分析问题代码
├── sql/init.sql    # 平台库全量建表脚本（幂等，含演示数据）
├── sql/init_empty.sql # 平台库全量建表脚本（幂等，无演示数据，用于一键初始化空库）
├── scripts/        # build.sh / start-all.sh(单机) / start-server.sh / start-agent.sh(多机) / link-codebuddy.sh
└── docs/           # 本文档 + design / reports / issues / artifacts 归档
```

## 快速开始（单机演示模式）

前置条件：JDK 17+、Maven、Node 18+、MySQL 8（root/root123，可改环境变量）。

```bash
# 1. 全量构建（后端 + 前端）
cd /workspace && scripts/build.sh

# 2. 一键启动（MySQL + perf-server + perf-demo(挂 agent) + perf-web）
scripts/start-all.sh
#    Web 后台: http://localhost:5173
#    被测应用: http://localhost:8081/api/user/list?limit=200   （N+1 热点接口）

# 3. 也可以在已有 MySQL 上手动启动：
#    mvn -q -pl perf-server,perf-demo package -DskipTests
#    java -jar perf-server/target/perf-server.jar &
#    java -javaagent:perf-agent/target/perf-agent.jar=reportUrl=http://127.0.0.1:8080/api/agent/report,appName=perf-demo,sampleRate=1.0 \
#         -jar perf-demo/target/perf-demo.jar &
#    (cd perf-web && npm run dev)
```

单机模式下平台库 `performance_testor` 与被测库 `demo` 隔离在同一 MySQL 实例，仅用于演示。

## 多机分离部署

平台服务器与被测服务器分离，仅需网络白名单互通两处：**平台 → 被测**（发起压测、灌数据），**被测 → 平台**（agent 上报）。

> agent 上报时自动注册应用实例（来源 IP + `appPort` 配置的端口 + 应用版本号）。压测中心选定项目后，**服务地址自动预填** `http://{appHost}:{appPort}`（用户手动修改过后不再覆盖），配合接口扫描实现"选项目 → 选接口 → 直接压测"。
>
> **版本号来源优先级**：agent 参数显式配置 `appVersion=x.y.z` > 自动读被测 jar `META-INF/MANIFEST.MF` 的 `Implementation-Version`（被测应用 pom 给 maven-jar-plugin 加 `<addDefaultImplementationEntries>true</addDefaultImplementationEntries>` 即可写入）> 空（平台保留已有值）。版本随 agent 上报维护，创建压测任务时自动快照到任务，报告页支持不同版本间的性能对比。

```
[平台服务器]  perf-server:8080 + MySQL + 源码存储 + OpenAI 兼容 API
    ↑ agent 上报（被测→平台）        ↓ 压测/灌数据（平台→被测）
[被测服务器]  perf-demo:8081 + MySQL(demo库) + -javaagent 探针
```

### 平台服务器

```bash
scripts/start-server.sh    # 端口 8080；源码上传目录默认 ./data/source
```

### 被测服务器

```bash
# REPORT_URL 必须改为平台服务器真实地址，如 http://<平台IP>:8080/api/agent/report
REPORT_URL=http://<平台IP>:8080/api/agent/report scripts/start-agent.sh
```

网络要求：被测服务器可访问平台 `8080` 端口（上报）；平台服务器可访问被测服务器 `8081` 端口（压测）与被测库 `3306` 端口（灌数据）。agent 上报采用本地队列 + 1s 批量 flush，失败指数退避重试、超时丢弃，网络抖动不影响被测业务。

## perf-demo 示例应用（可分析问题点）

| 问题类型 | 位置 | 表现 |
| -------- | ---- | ---- |
| N+1 查询（最典型） | `UserService.listUsersWithOrderCount`（循环内逐条 `countOrders`） | `GET /api/user/list?limit=N`，N 个用户产生 N+1 次 SQL |
| 慢 SQL：无索引全表扫描 | `OrderMapper.selectByStatus`（`status` 列刻意无索引） | `GET /api/order/list?status=PAID` |
| 慢 SQL：大表 GROUP BY | `OrderMapper.selectStats`（5 万行聚合） | `GET /api/order/stats` |
| 慢 SQL：无覆盖索引排序 | `OrderMapper.selectRecent`（`created_at` filesort） | `GET /api/order/recent` |

启动时自动建表并灌入 1 万用户 / 5 万订单（`DemoDataInitializer`，幂等）。压测 `GET /api/user/list?limit=200`（20 并发）可稳定复现：QPS ≈ 300、P95 ≈ 135ms、错误率 > 0.5%，同时产生 N+1 热点调用链与慢 SQL 上报。

## 七屏 Web 后台

1. **总览 Dashboard**：关键指标卡 + JVM 堆趋势 + 最近压测/慢 SQL/性能问题 + AI 快捷入口
2. **项目管理**：zip/git/本地三种方式导入源码并绑定应用（agent 上报名称一致时数据自动归属）；git 分支枚举/切换与重新同步；一键清理项目历史数据（源码与项目保留）
3. **项目监控**：JVM 堆 / 非堆 / GC / 线程 / CPU 四类图表，5s 轮询可暂停，按项目与时间范围筛选
4. **调用链分析**（含慢 SQL Tab）：调用链列表 + 调用树 + 火焰图 + 慢 SQL 明细
5. **压测中心**（含数据准备与容量建议 Tab）：压测表单（选项目自动带出服务地址与接口清单）+ 实时 QPS 曲线 + 容量评估（目标 RT 下最大并发/QPS）+ 扩容建议 + JVM 配置建议 + 灌数据
6. **性能优化**：数据库连接配置（EXPLAIN 分析用）→ 性能问题列表（慢 SQL/热点方法/压测瓶颈一键创建）→ 大模型多方案对比 → 修复提示词（复制/下载）
7. **AI 助手**：对话流 + 工具调用卡片 + 无 Key 降级规则模式演示

## AI 修复闭环演示（无 Key 降级模式）

未配置 `ai.api-key` 时平台以**降级规则模板**演示完整闭环（基于真实库数据）：

1. 在 **压测中心** 对 `http://<被测>:8081/api/user/list?limit=200` 发起压测（如 20 并发 × 10s）；
2. 在 **项目管理** 导入被测项目源码（zip 或 git，并绑定应用名）；
3. 在 **AI 助手** 输入：
   - 「分析最近慢 SQL 的根因并给出修复方案」
   - 「当前性能能承受多少并发、何时扩容」
   - 「生成一份性能检测报告」
   - 「列出待处理的性能问题」
4. 在 **性能优化** 的问题详情中查看多方案对比，选定方案后生成**修复提示词**，复制到 AI 辅助编码工具即可落地修复。

配置 `ai.api-key` 与 `ai.base-url`（OpenAI 兼容 API）后，AI 助手升级为 Function Calling 多轮编排：工具清单 `stress_test / jvm_metrics / trace / slow_sql / data_gen / analyze_code_issues / generate_fix_prompt / capacity_advice / generate_report`，模型自主选工具执行并汇总报告。

## 关键 API

```
# 采集与上报
POST /api/agent/report             # agent 批量上报（JVM/调用链/慢SQL；项目不存在自动丢弃）
GET  /api/apps                     # 应用实例列表
GET  /api/jvm/metrics              # JVM 指标查询
GET  /api/traces                   # 调用链查询（含 traceJson 调用树）
GET  /api/traces/{id}              # 调用链详情
GET  /api/slow-sqls                # 慢 SQL 查询
POST /api/slowlog/parse            # MySQL 慢日志文本解析导入

# 数据库连接管理（慢 SQL 分析 EXPLAIN / 灌数据复用）
GET  /api/dbconns                  # 连接配置列表
POST /api/dbconns                  # 保存连接
POST /api/dbconns/{id}/default     # 设为默认连接
POST /api/dbconns/test             # 测试连接（可携带 SQL 试跑 EXPLAIN）

# 压测与数据准备
POST /api/stress/tasks             # 启动压测（引擎 http/jmeter）
GET  /api/stress/tasks/{id}/report # 压测报告
POST /api/datagen/tasks            # 启动灌数据
POST /api/datagen/tables           # 读取目标库表清单

# 源码管理与性能优化
POST /api/source/repos/zip         # 源码导入：zip 上传
POST /api/source/repos/git         # 源码导入：git 克隆
POST /api/source/repos/local       # 源码导入：本地目录
GET  /api/source/repos             # 源码仓库列表
POST /api/source/repos/{id}/resync         # 重新同步源码并重建索引
POST /api/source/repos/{id}/cleanup-data   # 清理项目历史数据
POST /api/source/issues/{id}/analyze       # 大模型根因分析（生成多方案）
POST /api/source/issues/{id}/suggestions/{sid}/select  # 选定方案生成修复提示词

# AI 与容量
POST /api/ai/chat                  # AI 对话（降级/Function Calling）
POST /api/capacity/analyze         # 容量评估（目标 RT 下最大并发/QPS）
GET  /api/capacity/advice          # 容量与扩容建议
```

## 归档约定

开发过程中的全部中间产出物归档于 `docs/`（纳入 git 管理，不写入 `.gitignore`）：

| 文档 | 说明 | 状态 |
| ---- | ---- | ---- |
| [design/requirements.md](design/requirements.md) | 需求文档：7 大功能模块与边界说明 | 已定稿 |
| [design/architecture.md](design/architecture.md) | 整体架构：平台与被测应用分离部署拓扑 | 已定稿 |
| [design/tech-solution.md](design/tech-solution.md) | 技术方案：技术栈、关键设计决策、代码结构 | 已定稿 |
| [design/database-design.md](design/database-design.md) | 数据库设计说明：核心表、新增五张表、分区策略 | 已定稿 |
