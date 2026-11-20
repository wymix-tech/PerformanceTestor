# Java 应用性能测试闭环诊断与改进方案（main 分支评估）

> 评估范围：`main` 分支 499 个文件（`perf-agent`、`perf-py`、`perf-web`、`scripts`）源码级交叉核验。
> 数据来源：`perf-web/src/api/index.ts`、`perf-py/server/*_api.py`、`perf-agent`、`scripts/`。
> 评估日期：2026-11-20。

**总体结论**：平台已完成 Java `perf-server` / `perf-datagen` 退役并建立起 PTDE 智能数据引擎与 CLI/MCP 能力，但绞杀者迁移留下的接口断层与运行时接线缺口，导致「接入 → 造数 → 压测 → 观测 → 归因 → 修复验证」在 Web 端与真库执行链路上尚未真正闭环。

**核心指标**：六大闭环环节中有 **4 个环节**存在直接阻断端到端流转的 P0/P1 断点；其中前端 **11 组接口**在 Python 服务端尚未注册。

---

## 〇、当前工作区修复进度（评估后跟进 · P0 已全部收尾）

| 评审断点 | 进度 | 说明 |
| --- | --- | --- |
| P0-1 前端接口缺失 | 🟢 已修复 | 观测查询：`metrics_api.py`（apps/jvm/traces/slow-sqls/slow-commands/hotspots，游标分页 `(time_col, id)` 复合键）；容量：`capacity_api.py`（analyze/advice + `result_json.capacity` 回写）；告警：`jvm_alert_api.py`（`/api/jvm-alert-rules` CRUD + 手动判定）；调度：`schedule_api.py` + `schedule_service.py`（`/api/stress/schedules` CRUD/diff + 守护线程触发）；报告：`report_api.py`（`/api/reports/export` html/doc/pdf 打印视图）；部署：`deploy_api.py` + `deploy_service.py`（`/api/deploy/**` SSH 探活/构建/启动/状态，paramiko 懒加载可选依赖 `perf-py[deploy]`）；`sql/init_empty.sql` 时序表 `idx_app_time` 扩为 `(app_name, <time_col>, id)` |
| P0-2 PTDE 未接 `perf serve` | 🟢 已修复 | `DatasetLifecycleService` 支持 `pipeline_factory` 动态工厂（`dataset_pipeline_factory.py`：DSN 优先级 显式 → `PTDE_REALDB_URL` → `db_conn` 默认连接；字段/外键经 `information_schema` 实探）；`create_app` 在配置 `PTDE_REALDB_URL` 时自动接线；知识库缺省持久化 `~/.perftestor/knowledge.sqlite`（`PERF_KNOWLEDGE_DB` 可覆盖） |
| P0-3 启动脚本漏 `export` | 🟢 已修复 | `scripts/start-server.sh` / `start-all.sh` 已补 `export PERF_PY_DATABASE_URL` + 启动日志掩码打印连接串 |
| P0-4 修复验证重放丢上下文 | 🟢 已修复 | `stress_task` 新增 `request_json` 列（init_empty.sql 建表 + init.sql 幂等 ALTER）；`create_and_run` 持久化完整请求快照；`_to_request()` 优先反序列化快照（body/headers/scenario/warmupSec/stages 等全量还原），坏 JSON 回退字段重建；`requestJson` 不进 wire 契约（Java 实体无此字段，golden 契约纪律） |

## 一、六大闭环环节现状总览

| 环节 | 状态 | 已通能力 | 断点/风险 |
| --- | --- | --- | --- |
| 1. 应用接入与部署 | 🔴 P0 | 源码导入 / 端点扫描已通 | × `/api/deploy/**` 全部 404；× 启动脚本漏 export DB URL |
| 2. 测试数据准备 (PTDE) | 🔴 P0 | CLI 真库灌数与 5 层验证已通 | × `perf serve` 未接 pipeline；△ 绑定未喂给压测参数化 |
| 3. 压测编排与执行 | 🟡 P1 | Locust/JMeter 与分档执行已通 | △ 无流量染色、无运行中止；× `/stress/schedules` 缺失 |
| 4. Java 探针可观测性 | 🔴 P0 | Agent 上报 `/agent/report` 已通 | × 查询 API(JVM/Trace) 全 404；△ 缺异步链路与安全点偏差 |
| 5. 根因归因与容量评估 | 🔴 P0 | EXPLAIN + 源码定位 + ReAct | × `/capacity` & `/reports/export` 缺失；△ AI 工具无法查 JVM/Trace |
| 6. 修复验证与 CI 门禁 | 🟢 基本可用 | `perf gate` 与基线比对已通 | × 验证重放丢 body/场景/预热；△ P99 不入回归门禁、缺去噪 |

> 闭环反馈：知识库沉淀（当前默认内存态，重启即清空）

## 二、P0 级硬断点：4 处核心接线尚未闭合

### P0-1 前端 11 组接口在后端缺失（P0 · 阻塞）

- **源码证据**：`perf-web/src/api/index.ts` 调用 `/api/apps`、`/api/jvm/metrics`、`/api/traces`、`/api/slow-sqls`、`/api/slow-commands`、`/api/hotspots`、`/api/jvm-alert-rules`、`/api/capacity/*`、`/api/reports/export`、`/api/stress/schedules`、`/api/deploy/*`；但 `perf-py/server/*_api.py` 均未注册。
- **实际破坏**：`collector_api.py` 只收 `/api/agent/report` 写入，没有任何查询路由。Web 端总览、JVM 监控、调用链与慢 SQL 分析、部署管理、容量评估、报告导出全部 404，观测闭环完全不可见。
- **落地修复**：优先新增 `metrics_api.py`（复用已有 `CollectorRepository` 实现 `(time, id)` 游标分页查询）、`capacity_api.py`（包装已有 `capacity/`）与 `report_api.py`；对暂未迁移的定时任务/SSH 部署模块补充后端路由或前端功能开关。

### P0-2 PTDE 真库执行体未接入 `perf serve`（P0 · 阻塞）

- **源码证据**：`perf_cli/main.py:454` 调用 `create_app(llm=llm, knowledge=knowledge)` 未传 `dataset_pipeline`；`DatasetLifecycleService` 默认挂载 `UnwiredPipeline`。`realdb_pipeline.py` 仅在 CLI 与单测中引用。
- **实际破坏**：在 Web 端「数据集管理」点击构建、校验或清理（`/api/datasets/{id}/build`）必然报错「数据集执行体未接线」，测试数据生命周期无法在平台服务侧运行。
- **落地修复**：在 `app.py` 注入动态工厂：按数据集绑定的 `dbConnId`（或 `PTDE_REALDB_URL`）按需调用 `build_realdb_pipeline()`，并显式配置可降级层集（如 `DISTRIBUTION`）。

### P0-3 启动脚本漏 `export` 且知识库默认内存态（P0 · 阻塞）

- **源码证据**：`scripts/start-all.sh:25` 与 `start-server.sh:20` 仅写 `PERF_PY_DATABASE_URL=...` 未加 `export`；`perf serve` 缺省 `knowledge_db=":memory:"`。
- **实际破坏**：`uv run perf serve` 继承不到 MySQL 连接串，静默退化为 `sqlite:///:memory:`，探针数据与任务状态无法落盘，且 `SubprocessRunner` 遇到内存库直接拒绝执行压测；知识库重启即清空。
- **落地修复**：在两份启动脚本中改为 `export PERF_PY_DATABASE_URL`，为 `perf serve` 增加 `--knowledge-db` 默认持久化路径（或直连平台 MySQL 的 `ai_knowledge` 表），并在启动日志打印实际生效的数据库方言。

### P0-4 修复验证重放丢失压测上下文（P0 · 失真）

- **源码证据**：`issue_verify_service.py` 的 `_to_request(snapshot)` 仅复制 `targetUrl`、`method`、`concurrency`、`durationSec`、`qpsLimit`，未保存也未还原 `body`、`headers`、`scenario`、`warmupSec`、`rampUpSec`、`stages`、`csv` 与 `dataset`。
- **实际破坏**：任何 POST/JSON 请求、带鉴权头接口、多步混合场景或分档压测，在点击「一键验证」时都会变成裸发无预热单接口请求，导致修复验证直接报错或 P95 对比严重失真。
- **落地修复**：在 `stress_task` 表新增 `request_json` 列持久化完整压测请求快照；`IssueVerifyService._to_request()` 直接反序列化原快照并覆盖 `taskName`，保证验证压测与基线压测 100% 同构。

## 三、不必推倒重来：4 块高完成度资产可直接复用

问题集中在**跨模块接线与闭环编排**，而非底层算法缺失。

| 资产 | 完成度说明 |
| --- | --- |
| **PTDE 数据引擎内核** | 5 层校验：MySQL/PG 双方言、MyBatis/JPA 源码解析、Zipf/相关性采样、分片断点装载与五层验证门禁，单测覆盖充分 |
| **统一压测统计与容量拟合** | 30,000 桶：`HistogramStatistician` 与多项式容量曲线拟合、RT 拐点检测、95% CI 已移植到 `perf-py` |
| **perf-agent 采集骨架** | 低开销埋点：单 `ThreadLocal` 栈、Servlet Filter 根采样、字节预算限流、精确 GC 名称映射及安全 `EXPLAIN` 防注入机制扎实 |
| **场景即代码与门禁** | CLI + MCP：YAML 场景、`perf gate` 退出码 0/1/2 契约、OpenAI/MCP 双协议工具注册表已打通 |

## 四、P1 级闭环断层：压测、观测、数据与 AI 之间缺少「因果锚点」

即使把 P0 接口和接线补齐，当前平台仍偏向「多工具拼接」而非「因果联动闭环」——压测结果不知道对应哪些慢 SQL 与 GC 波动，AI 看不到探针采集的运行态数据，数据集也未真正参与压测流量与状态重置。

### 4.1 压测流量未染色，无法与探针数据精准关联

`locust_engine.py` 与 `jmeter_engine.py` 发压时未自动注入任务标识头（如 `X-Perf-Stress-Task-Id`），`perf-agent` 的 `FilterRootAdvice` 也未读取该请求头。后果是：当同一应用同时存在多轮压测或后台定时任务时，平台无法精确切出「本次压测触发的慢 SQL、慢命令与调用链」，也无法支撑后续的影子库/压测隔离路由。

**解法**：引擎层默认注入 `X-Perf-Stress-Task-Id` 与 `X-Perf-Traffic-Tag: stress`；探针在 `FilterRootAdvice` 捕获并写入 `TraceContext`，随 Trace / SlowSQL / SlowCommand 一并上报入库，压测报告页自动按 `stress_task_id` 与时间窗聚合运行态瓶颈。

### 4.2 AI Agent 工具集「盲视」探针数据，归因链断开

`perf-py/tools/registry.py` 仅注册了 7 个工具（压测、容量、离线慢日志、源码扫描、造数），**完全没有查询平台库中 JVM 指标、Trace 树、在线慢 SQL、慢命令、CPU 热点的工具**。`ROLE_ANALYSIS` 在无 MySQL 慢日志文件时只能对着压测总指标空谈，无法真正把「P95 飙升 → 某时段 Full GC 频繁 / 某条 SQL 扫全表 → 对应 Mapper 代码」串成自动化证据链。

**解法**：新增 `jvm_metrics_query`、`trace_query`、`slow_sql_query`、`hotspot_query`、`db_explain_run` 5 个只读工具并注入 `ROLE_ANALYSIS`，按 `appName + stressTaskId/时间窗` 自动拉取全链路运行态证据。

### 4.3 数据集绑定止步于元数据，缺参数化联动与状态重置

`StressTaskService._resolve_dataset_binding()` 虽然校验了数据集为 `READY` 并记录版本快照，但压测引擎并未消费该数据集中的真实 ID 池或热点样本（仍需手工准备 CSV）；更重要的是，写接口压测后**没有自动回滚或重建机制**，导致第二轮回归压测面对的数据量、索引碎片和业务状态已发生变化，基线对比失去单一变量前提。

**解法**：在 PTDE `ExecutionManifest` 中增加「压测参数样本导出（热/温/冷主键 CSV）」供压测场景自动挂载；为写场景压测增加可选的 `beforeRun / afterRun` 数据重置钩子（对接已有 `cleanup` 档位）。

### 4.4 压测引擎缺少中止控制、长尾精度受限且仅支持单机

`stress_api.py` 没有停止运行中任务的接口，`SubprocessRunner` 仅 `Popen` 后丢弃进程引用，既无法取消失控压测，也未在正常结束后清理 `perf-py-task-*.json` 临时文件，且无全局并发任务上限。同时 `perf-py/README.md` 已实测证实 Locust 在 Python 侧会导致 P95/P99 系统性偏高，而 JMeter 引擎目前不支持 `steps` 多步场景与变量提取。

**解法**：维护任务 PID 注册表并新增 `POST /api/stress/tasks/{id}/cancel`、临时文件 `finally` 清理及并发槽位限制；对长尾敏感与高并发场景，补齐 JMeter `steps`/`extract` 生成或引入 k6 / Gatling 作为高精度发压引擎，并规划分布式施压（Master-Worker 聚合）。

## 五、P1/P2 级技术纵深：Java 探针剖析盲区与门禁科学性不足

针对真实生产级 Java 应用（Spring Boot 3、高并发线程池、微服务与复杂中间件），当前 `perf-agent` 与回归门禁在四个关键维度仍停留在演示级精度，容易在高压场景下出现漏报或误判。

| 维度 | 当前实现与位置 | 真实 Java 压测下的风险 | 演进改造建议 |
| --- | --- | --- | --- |
| **异步与跨服务调用链**（P1 · 盲区） | `TraceContext.java` 仅依赖单线程 `ThreadLocal<Ctx>`，`FilterRootAdvice` 只覆盖 Servlet Filter | `CompletableFuture`、`@Async`、业务线程池提交任务后链路立即断裂；WebFlux/Netty 与 Dubbo/Feign 跨服务调用无法串联 | ByteBuddy 包装 `Executor`/`Runnable`/`Callable` 实现上下文快照传递（参考 TransmittableThreadLocal 思路），HTTP Client 透传 W3C `traceparent` |
| **CPU 热点与锁竞争剖析**（P1 · 失真） | `HotspotSampler.java` 每 10ms 调用 `ThreadMXBean.getThreadInfo(ids, 8)` 抓全线程栈 | 高频全线程抓栈持续触发 Global Safepoint（STW），高压下显著拉高被测应用 P99（观测者效应）；存在 Safepoint Bias，无法定位锁阻塞与分配热点，无火焰图 | 集成 **JFR** 或 `async-profiler` 动态启停采集（按压测时间窗自动触发），输出 CPU / Allocation / Lock 三类折叠栈交由前端渲染火焰图 |
| **JVM 与容器/池化指标**（P1 · 缺失） | `JvmCollector.java` 仅采集 Heap/NonHeap、累计 GC 次数/耗时、总线程数与 CPU | 无法识别三类最常见 Java 瓶颈：**HikariCP 连接池打满（pending）**、**Tomcat 工作线程队列堆积**、**DirectBuffer/Metaspace 泄漏**与**单次 GC 长停顿（STW 尖刺）** | JMX MBean 补充 HikariCP/Druid、Tomcat ThreadPool、BufferPoolMXBean、Metaspace、线程状态分布（RUNNABLE/BLOCKED/WAITING）及 `NotificationListener` 捕获单次 GC 停顿明细 |
| **回归门禁与验收策略**（P2 · 粗糙） | `baseline_compare.py` 中 P99 不参与 `regressed` 判定；`issue_verify_service.py` 硬编码 P95 下降 ≥ 30% 才算修复通过 | 长尾退化（P99 翻倍但 P95 未超 20%）会逃逸门禁；单次压测受云主机抖动易误报；吞吐提升型或内存/GC 优化类问题无法按单一规则验收 | P99 纳入可配置门禁策略并支持多轮中位数防抖；`IssueVerifyService` 按问题类型匹配验收条件（慢 SQL 看 P95/SQL 耗时、容量瓶颈看 QPS、内存问题看 Full GC/堆水位） |

> **核心观点**：性能测试平台的分水岭不在于"能不能发压或造数"，而在于每一次压测结束后，系统能否在 60 秒内用同一份因果证据回答：慢在哪一行代码、哪一条 SQL、哪一次 GC，以及改完后是否真的无回归。

## 六、分阶段落地路线图

**节奏**：先止血打通 Web/真库端到端（P0）→ 再建立压测/探针/数据/AI 因果联动（P1）→ 最后补齐生产级剖析与门禁精度（P2），避免在基础接口未闭环前继续横向堆叠外围功能。

**闭环完整度预估**（按「接入部署、数据闭环、压测执行、观测可见性、AI 归因联动、验证与门禁」六大阶段加权估算）：

- 当前 main 分支：45%
- 阶段 1（P0 止血）：68%
- 阶段 2（P1 贯通）：88%
- 阶段 3（P2 深化）：97%

| 实施阶段 | 关键交付事项 | 主要涉及模块/文件 | 闭环验收标准 |
| --- | --- | --- | --- |
| **阶段 1 · P0 止血**（1–2 周） | 补齐观测查询/容量/报告导出 API；`perf serve` 动态接入 `realdb_pipeline` 与持久化知识库；脚本补 `export`；`stress_task` 持久化 `request_json` 修复验证重放；增加压测取消与临时文件清理 | `metrics_api.py`(新)、`capacity_api.py` / `report_api.py`(新)、`app.py`、`issue_verify_service.py`、`scripts/start-all.sh` | 单机执行 `start-all.sh` 后，Web 端所有主视图零 404；可在页面完成 PTDE 造数 → 场景压测 → 查看 JVM/Trace/慢 SQL → 生成报告 → 一键验证完整重放 |
| **阶段 2 · P1 贯通**（2–4 周） | 压测引擎注入 `X-Perf-Stress-Task-Id`，探针采集透传并按任务聚合运行态瓶颈；补齐 5 个 AI 观测查询工具接入 `ROLE_ANALYSIS`；PTDE 导出参数样本 CSV 并支持写压测前后数据重置；补齐高精度发压引擎（k6/JMeter 完整场景） | `perf-agent`（`FilterRootAdvice` / `TraceContext`）、`perf-py/tools/` & `agents/roles.py`、`perf-py/engine/` & `stress_service.py` | 压测报告自动展示该次任务关联的慢 SQL Top N 与 JVM 峰值；AI Agent 无需手工传慢日志文件即可基于在线观测数据输出根因与修复代码提示词 |
| **阶段 3 · P2 深化**（4–8 周） | 以 JFR / `async-profiler` 替代 10ms `ThreadMXBean` 轮询并输出火焰图；补齐异步线程池/WebFlux/W3C Trace 透传与 HikariCP/Tomcat/GC 停顿指标；升级基线门禁（纳入 P99、防抖与分类型验收）及分布式施压 | `perf-agent`（`JfrProfiler` / `JvmCollector` / `TraceEnhancer`）、`perf-py/server/baseline_compare.py`、`perf-web` 火焰图视图 | 高压下探针 CPU 开销 < 3%、无 Safepoint 引起的 P99 尖刺；异步与微服务链路不中断；CI 门禁能稳定捕获 P99 退化且零误报 |
