# PerformanceTestor P2 阶段改造执行提示词（生产级深度剖析、科学门禁与高精度施压）

> **适用对象**：AI 编码助手（CodeBuddy / Claude Code / Cursor 等）或后端/探针/前端工程师  
> **前置假设**：P0 与 P1 阶段已完成（观测查询与容量 API、真库数据集管线、压测 `X-Perf-Stress-Task-Id` 染色、AI 在线观测工具、压测取消与孤儿清理、基础异步上下文与 JMX 扩展指标已就绪）。  
> **本阶段目标**：将平台从「闭环跑通」升级为「**高压下低开销、数据可信、可支撑真实微服务与 CI 自动化门禁**」的生产级 Java 性能测试平台。

---

## 一、角色与全局约束（执行前必读）

你正在参与 **PerformanceTestor** 仓库的 P2 阶段深度演进。本阶段跨越 `perf-agent`（Java 17）、`perf-py`（Python FastAPI / 引擎 / 门禁）、`perf-web`（Vue 3 + TS）与 `sql/` 四个目录。

### 必须遵守的硬性红线
1. **探针零外部依赖与低开销承诺**：
   - `perf-agent/pom.xml` 继续保持零业务/中间件/Servlet 编译依赖；连续剖析优先使用 JDK 17 标准内置 **JFR（`jdk.jfr.*`）** 与标准 JMX API（`com.sun.management.*`），不强依赖平台专有 `.so/.dylib` 动态库（`async-profiler` 仅作为可选探测插件预留）。
   - 高压下探针额外 CPU 占用需控制在 **< 3%**，严禁在 GC 通知线程、JFR 事件回调线程或 ByteBuddy Advice 热路径中执行网络 IO、复杂正则或大对象序列化。
2. **数学严谨性（统计与门禁）**：
   - 分布式施压聚合 P50/P95/P99 必须**逐桶合并 30,000 桶直方图数组**后再计算分位数，**严禁对多个 Worker 的 P95/P99 求算术平均**。
   - 门禁阈值与验收策略升级必须保持无 `policy` 参数时的向后兼容，不无故破坏 `tests/contract/` 与既有单测。
3. **跨服务 Schema 演进安全**：
   - `trace_chain` 扩展 `span_id`/`parent_span_id` 支持微服务串联时，单服务存量数据与旧版探针必须无缝兼容（缺省 `span_id="root"`）。
4. **分批验证闸门**：每完成一个 Batch 必须跑通：
   - Java 探针：`mvn -pl perf-agent test`
   - Python 单测与 lint：`cd perf-py && uv run pytest && uv run ruff check .`
   - 引擎测试：`cd perf-py && PERF_PY_ENGINES=1 uv run pytest tests/test_locust_engine.py tests/test_jmeter_engine.py`
   - 前端构建与单测（涉及前端 Batch）：`cd perf-web && npm run build && npm run test`

---

## 二、分批改造清单与详细实现要求

### Batch 1：JFR 低开销连续剖析与火焰图闭环（P2-1）

#### 目标
彻底替代 `HotspotSampler.java` 基于 `ThreadMXBean.getThreadInfo()` 的高频 Safepoint 抓栈方案，利用 JDK 17 原生 JFR 实现 CPU / 对象分配 / 锁竞争三维度低开销采样，产出标准折叠栈（Folded Stacks）并在前端渲染可交互火焰图。

#### 具体改动点

1. **Java 探针：新增 `JfrProfiler.java`（`perf-agent`）**
   - `perf-agent/src/main/java/com/perfagent/config/AgentConfig.java`：
     - 新增配置项：`profilerMode`（`jfr` / `threadmx` / `off`，默认 `jfr`；当 `hotspotEnabled=true` 时按此模式启动）、`jfrCpuIntervalMs`（默认 `20`）、`jfrAllocThrottle`（默认 `150/s`）、`jfrLockThresholdMs`（默认 `10`）。
   - 新增 `perf-agent/src/main/java/com/perfagent/hotspot/JfrProfiler.java`：
     - 基于 `jdk.jfr.consumer.RecordingStream` 开启三类事件：
       - `jdk.ExecutionSample`（周期 `jfrCpuIntervalMs`）→ `profileType = "cpu"`
       - `jdk.ObjectAllocationSample`（`throttle = jfrAllocThrottle`）→ `profileType = "alloc"`（累加分配字节数 `weight`）
       - `jdk.JavaMonitorEnter`（`threshold = jfrLockThresholdMs`）→ `profileType = "lock"`（累加阻塞耗时毫秒）
     - **折叠栈格式化**：将事件 `RecordedStackTrace` 从根帧到叶帧拼接为 `pkg.Class.method;pkg.Class2.method2`，过滤 `com.perfagent.`、`net.bytebuddy.`、`jdk.jfr.` 内部帧；单条栈深度上限 `64`、每种 `profileType` 的唯一栈条数上限 `MAX_STACKS = 4096`（超限丢弃新栈防 OOM）。
     - **双轨上报兼容**：按 `hotspotReportSec`（默认 30s）快照并清零聚合 Map，一方面调用 `reporter.reportProfile(profileType, foldedLines, totalSamples, sampleTimeMs)` 上报折叠栈，另一方面自动提取 `cpu` 栈的叶帧 Top N 兼容调用原有 `reporter.reportHotspot(...)`，确保现有热点排行与 AI `hotspot_query` 零破坏。
     - 在 `AgentPremain.java` 的 shutdown hook 中调用 `jfrProfiler.stop()`（关闭 `RecordingStream` 并释放磁盘临时 chunk）。
   - **关键注意点**：
     - `RecordingStream.startAsync()` 会在后台线程消费事件；必须设置 `stream.setMaxAge(Duration.ofSeconds(60))` 与 `stream.setMaxSize(32 * 1024 * 1024)`，防止长时压测在 `/tmp` 堆积 JFR 磁盘段。
     - 预留 `ProfilerBackend` 接口，方便后续在 Linux 宿主机有 `libasyncProfiler.so` 时无缝接入原生栈（Native + Java 混合火焰图），默认走 JFR 零外部依赖。

2. **服务端存储与查询（`sql/` + `perf-py/server/`）**
   - `sql/init_empty.sql` & `sql/init.sql` 新增 `profile_snapshot` 表：
     ```sql
     CREATE TABLE IF NOT EXISTS profile_snapshot (
         id             BIGINT AUTO_INCREMENT PRIMARY KEY,
         app_name       VARCHAR(128) NOT NULL,
         stress_task_id BIGINT NULL,
         profile_type   VARCHAR(16) NOT NULL,  -- cpu / alloc / lock
         folded_stacks  MEDIUMTEXT NOT NULL,   -- 每行 "frame1;frame2 count"
         total_samples  BIGINT NOT NULL DEFAULT 0,
         sample_time    DATETIME NOT NULL,
         INDEX idx_profile_app_type_time (app_name, profile_type, sample_time),
         INDEX idx_profile_stress_task (stress_task_id, profile_type)
     );
     ```
   - `collector_repo.py` & `collector_service.py`：新增 `ProfileSnapshotModel` 与 `type == "profile"` 上报处理分支。
   - `metrics_api.py`：新增 `GET /api/profiles?app=&profileType=cpu&stressTaskId=&start=&end=`，将时间窗内多批 `folded_stacks` 在服务端按栈路径合并累加后返回（支持返回标准折叠文本或聚合树 JSON，限制最大节点数防前端卡顿）。
   - `perf-py/tools/observability_tools.py`：为 AI Agent 新增 `profile_flamegraph_summary` 工具，返回 CPU / 分配 / 锁竞争 Top 10 热点调用路径（而非只看叶子方法），大幅提升定位深层调用链瓶颈的准确度。

3. **前端火焰图视图（`perf-web/`）**
   - 新增轻量组件 `perf-web/src/components/charts/FlameGraph.vue`（纯 Canvas 或 SVG 渲染 folded stacks，支持按 `cpu / alloc / lock` 切换、点击帧放大、关键字搜索高亮与悬停显示自身/总占比，不引入重型第三方依赖）。
   - 在 `TraceAnalyzerView.vue` 的「CPU 热点」Tab 接入火焰图与列表双视图切换。

#### Batch 1 验收标准
- `JfrProfiler` 单测能实际捕获人工构造的 CPU 热点/对象分配/synchronized 锁竞争并输出合法折叠栈；`stop()` 后无残留线程与 JFR 临时文件泄漏。
- 压测 `perf-demo` 期间，前端可正常切换展示 CPU / 内存分配 / 锁竞争火焰图。

---

### Batch 2：W3C 跨服务链路追踪、单次 GC 停顿明细与 SQL 指纹脱敏（P2-2）

#### 目标
打通微服务跨进程调用链，捕获单次 GC STW 尖刺，并对慢 SQL 实行指纹归类与敏感参数脱敏。

#### 具体改动点

1. **W3C `traceparent` 跨服务透传（`perf-agent`）**
   - `TraceContext.java` & `FilterRootAdvice.java`：
     - 入口除读取 `X-Perf-Stress-Task-Id` 外，同时尝试读取 `traceparent` 头（格式 `00-<32hex traceId>-<16hex parentSpanId>-<2hex flags>`）。
     - 若合法则继承 `traceId` 并记录 `parentSpanId`，同时为本服务生成 16 位 hex `spanId`；若无则生成新 32 位 hex `traceId` 与 `spanId`。
     - 新增 `public static String currentTraceparent()`，供出口拦截器注入下游请求。
   - `TraceEnhancer.java`：
     - 新增开关 `httpClientEnabled`（默认 `false`），按类型名匹配常见同步 HTTP 客户端（Apache `HttpClient`/`HttpRequest`、OkHttp `Request.Builder`、Spring `ClientHttpRequest`、JDK `HttpRequest.Builder`），在请求发出前注入 `traceparent` 与 `X-Perf-Stress-Task-Id`。
   - **数据库与仓储关键改造（防止多服务覆盖）**：
     - `sql/init_empty.sql` 中 `trace_chain` 表当前 `trace_id` 是 `UNIQUE` 键——微服务场景下 A 调 B 会共享同一个 `trace_id`，必须调整：
       - 新增 `span_id VARCHAR(32) NOT NULL DEFAULT 'root'` 与 `parent_span_id VARCHAR(32) NULL`；
       - 将唯一键从 `UNIQUE(trace_id)` 改为 `UNIQUE(trace_id, app_name, span_id)`。
     - `collector_repo.py` 的 `save_traces()` 同步改为按 `(trace_id, app_name, span_id)` 判定幂等更新；`/api/traces/{traceId}` 兼容返回主链路及关联的 `childSpans` 列表。

2. **单次 GC 停顿明细采集（`JvmCollector.java`）**
   - 在 `JvmCollector.start()` 中遍历 `ManagementFactory.getGarbageCollectorMXBeans()`，对实现 `NotificationEmitter` 的 Bean 注册监听器监听 `GarbageCollectionNotificationInfo.GARBAGE_COLLECTION_NOTIFICATION`：
     - 仅在监听器内用无锁 `LongAccumulator(Math::max, 0)` 记录本周期 `maxGcPauseMs`，用 `AtomicReference<String>` 记录最近一次 `lastGcCause` 与 `lastGcAction`（绝不做 IO 或字符串大拼接）。
     - 在 `collectOnce()` 读取并原子归零，写入 `extraMetrics`（`maxGcPauseMs`、`lastGcCause`）随 `jvm` 指标上报；`stop()` 时注销监听器防止重复注册。

3. **慢 SQL 指纹归类与敏感参数脱敏（`JdbcInterceptor.java` + `collector_service.py`）**
   - 仅在慢 SQL 命中后（服务端 `collector_service.py` 侧处理，进一步减轻探针热路径负担）：
     - 脱敏：对单引号字符串中的手机号、身份证号、邮箱及 `password/token/secret` 赋值做掩码处理；
     - 指纹化：将数字字面量、字符串字面量与 `IN (...)` 列表归一化为 `?` 后计算 SHA-1/MD5 前 16 位存入 `slow_sql.sql_fingerprint`（新增可空列 + 索引）。
   - 在 `metrics_api.py` 与 `observability_tools.py` 支持按 `sql_fingerprint` 聚合展示「同模板慢 SQL 次数、平均耗时、P95 耗时与样本 SQL」，避免同一条 N+1 或全表扫描 SQL 因参数不同刷屏。

#### Batch 2 验收标准
- 两个服务（A → B）模拟调用时，A 与 B 的 `trace_chain` 共享 `trace_id` 且 `B.parent_span_id == A.span_id`，入库互不覆盖。
- 触发 Full GC 后，`jvm_metric` 扩展字段能准确记录单次 `maxGcPauseMs` 与 `gcCause`。
- 同模板不同参数的 100 条慢 SQL 生成相同 `sql_fingerprint`，且敏感字面量已脱敏。

---

### Batch 3：科学化回归门禁、防抖基线与分类型修复验收（P2-3）

#### 目标
让 CI 性能门禁与问题「一键验证」具备统计可信度：捕获 P99 长尾退化、过滤云主机偶发抖动、按问题类型匹配验收指标。

#### 具体改动点

1. **升级 `perf-py/server/baseline_compare.py`**
   - 新增策略结构 `GatePolicy`（所有字段均有默认值，保证不传 `policy` 时行为向后兼容）：
     ```python
     @dataclass(frozen=True)
     class GatePolicy:
         qps_drop_pct: float = 10.0
         p95_rise_pct: float = 20.0
         p99_rise_pct: float | None = None   # 门禁新模式默认 25.0；None 保持历史契约兼容
         error_rate_rise_pct: float = 5.0
         min_samples: int = 100
         baseline_mode: str = "last"         # "last" | "median_k"
         baseline_window: int = 3
     ```
   - **`median_k` 基线防抖**：当 `baseline_mode="median_k"` 且未显式指定 `baseline_task_id` 时，查询同应用同 URL 最近 `k` 次（默认 3 次）`finished` 任务，分别计算 QPS / P95 / P99 / 错误率的中位数构造虚拟稳定基线，并在返回体中标注 `baselineTaskIds: [...]` 与 `baselineMode`。
   - **P99 门禁与低样本告警**：当 `policy.p99_rise_pct is not None` 时，P99 涨幅超限将 `row["regressed"] = True` 并触发总 `regressed = True`；当当前任务或基线 `totalRequests < min_samples` 时，返回 `lowConfidence = True` 与提示文案。
   - `perf-py/server/stress_api.py`（`POST /api/stress/gate`）与 `perf-py/perf_cli/gate_runner.py`：支持通过查询参数/YAML `gate` 字段/环境变量（`GATE_P99_RISE_PCT`、`GATE_BASELINE_MODE`、`GATE_MIN_SAMPLES`）配置策略。

2. **升级 `perf-py/server/issue_verify_service.py`（分类型验收策略）**
   - 将硬编码的 `drop_pct >= P95_DROP_PCT and not d["regressed"]` 抽离为策略函数 `_evaluate_verify_criteria(issue, snapshot, finished, diff_res, collector_repo)`：
     - **`slow_sql` / `hot_method`**：P95 下降 ≥ 25%（可配）**且**无回归；若前后两次压测均有染色 `slow_sql` 数据，附加检查同指纹慢 SQL 平均耗时下降 ≥ 30% 并写入对比明细。
     - **`stress`（容量/吞吐瓶颈）**：满足「P95 下降 ≥ 20% **或** QPS 提升 ≥ 15%」且错误率无恶化、无回归。
     - **`jvm_gc` / `memory`**：通过 `collector_repo` 对比两次压测时间窗的 Full GC 耗时或堆峰值占比下降 ≥ 15%，且 QPS/P95 无回归。
   - 在 `verify_json` 中新增 `criteria` 字段（记录采用规则与各项实测通过情况），保留原有 `passed`、`p95Before`、`p95After`、`p95DropPct`、`rows` 等全部字段确保前端零改动兼容。

#### Batch 3 验收标准
- 单测验证：P95 仅涨 5% 但 P99 暴涨 60% 时，启用 `p99_rise_pct=25` 的门禁能准确拦截（`passed=False`）。
- 单测验证：3 次历史基线中混入 1 次异常抖动点，`median_k` 模式下门禁不会被误触发。
- 单测验证：吞吐提升型问题（QPS +35%、P95 持平）在 `stress` 策略下可验证通过，不再被「P95 必须降 30%」误杀。

---

### Batch 4：高精度发压引擎（k6）与分布式施压聚合（P2-4）

#### 目标
解决 Locust（Python/gevent）在长尾延迟（P95/P99）上的系统性偏高问题，补齐施压端饱和自检，并奠定多 Worker 分布式压测的精确聚合基础。

#### 具体改动点

1. **施压端饱和度自检（`perf-py/engine/locust_engine.py` & `stress_worker.py`）**
   - 在 `locust_engine.py` 运行期间，后台采样协程每 1 秒测量一次事件循环调度漂移（`gevent.sleep(1.0)` 的实际耗时减去 `1.0s`）以及进程 CPU 占比（`os.times()` 或 `psutil` 若可用）。
   - 若正式压测期间出现事件循环漂移 > 20ms 超过 3 次，自动向 `StressTaskResult.warnings` 写入明确告警：「施压端事件循环饱和（最大漂移 XXms），当前 P95/P99 可能包含客户端排队延迟，建议切换 k6/JMeter 引擎或降低单机并发」。

2. **接入高精度 `k6` 引擎（`perf-py/engine/k6_engine.py`）**
   - 新增 `K6Engine`，在 `perf-py/engine/registry.py` 注册 `"k6"`，与 `LocustEngine` / `JMeterEngine` 共用完全一致的 `run(scenario, listener) -> StressTaskResult` 契约：
     - **脚本生成（`generate_k6_script(scenario)`）**：
       - 将 `load`（`concurrency`/`duration_sec`/`warmup_sec`/`ramp_up_sec`/`qps_limit`）映射为 k6 `scenarios`（`constant-vus` / `ramping-vus` / `constant-arrival-rate`）；
       - 支持单接口与 `steps` 多步场景（含 `headers` 染色头、JSON `body`、`extract` 变量提取、基本状态码/正文断言、`csv` SharedArray 参数化）。
     - **流式采集与统一统计**：执行 `k6 run --out json=<pipe_or_file>`，按行读取 `Point`（`metric == "http_req_duration"` / `"http_req_failed"`），过滤 `warmup` 期样本后喂入统一的 `HistogramStatistician`，每秒触发 `listener(Sample)` 实时落库 `stress_sample`。
   - `perf-py/server/stress_service.py` 的 `engine_available()` 与 `_default_engine()` 补齐 `"k6"` 分支，探测 `shutil.which("k6")`；未安装时返回明确提示文案。

3. **分布式施压直方图精确合并协议（`perf-py/engine/stats.py`）**
   - 在 `HistogramStatistician` 中新增序列化与合并方法：
     - `export_buckets() -> dict`：导出 `buckets`（压缩非零桶 `[idx, count]` 列表）、`total`、`errors`、`sum_ms`、`max_ms`；
     - `HistogramStatistician.merge(snapshots: list[dict]) -> HistogramStatistician`：逐桶累加合并，再统一计算全局精确 `P50 / P95 / P99 / avg / max / QPS / errorRate`。
   - 预留分布式 Worker 任务分片协议（`workerIndex / workerCount`）：CSV 参数化按 `row_idx % workerCount == workerIndex` 切分，避免多台施压机重复请求同一批热点 ID。

#### Batch 4 验收标准
- `generate_k6_script` 单测覆盖单接口、多步场景、染色头、断言与 CSV 参数化生成；使用模拟 k6 JSON 输出验证 `K6Engine` 统计结果与 `HistogramStatistician` 逐位一致。
- `HistogramStatistician.merge()` 单测验证：将 10,000 条延迟随机拆分给 4 个分片统计后再合并，计算出的 P50/P95/P99 与单机全量统计结果 **100% 精确相等**。
- 模拟施压端阻塞时，`StressTaskResult.warnings` 自动产出饱和告警。

---

## 三、常见踩坑与自检清单（提交前逐项核对）

| 检查项 | 容易踩的坑 | 核对方式 |
| --- | --- | --- |
| `trace_chain` 微服务主键冲突 | 仍保留 `UNIQUE(trace_id)`，跨服务 A→B 共享 `trace_id` 时 B 把 A 覆盖 | DDL 与 `save_traces` 升级为 `(trace_id, app_name, span_id)` 联合唯一 |
| JFR 临时文件或内存泄漏 | `RecordingStream` 未设 `maxAge`/`maxSize` 或 shutdown 未 `close()` | 长时间压测后检查 `/tmp` 磁盘占用与 JVM 退出时 `JfrProfiler.stop()` 执行日志 |
| GC 监听器阻塞应用 | 在 `NotificationListener` 中做同步上报、JSON 序列化或锁等待 | 审查监听器代码仅含 `LongAccumulator` / `AtomicReference` 无锁写入 |
| 分布式分位数算错 | 将各 Worker 的 P95/P99 求平均或取最大值作为全局指标 | 强制走 `HistogramStatistician.merge()` 逐桶累加后统一求分位 |
| 多 Worker 重复打热点 Key | 分布式施压时每个 Worker 都从 CSV 第 1 行开始读，导致缓存命中率虚高 | 校验 CSV 读取按 `workerIndex / workerCount` 分片偏移 |
| 门禁默认策略破坏旧契约 | 直接把 P99 硬切入默认 `diff()` 导致既有 `test_stress_write` / 契约测试失败 | 通过可选 `GatePolicy` 控制新规则，或同步评审更新契约基线 |
| 施压机饱和导致误诊 | Locust 客户端 CPU 跑满造成假 P99 飙升，开发白查 Java 代码 | 确认 `warnings` 正确透传至压测报告与 AI 分析上下文 |

---

## 四、建议交付物格式

每完成一个 Batch，请输出：
1. **改动文件清单**（标明 `[NEW]` / `[MODIFY]` 及对应 Batch 编号）。
2. **测试与性能开销验证结果**：
   - `mvn -pl perf-agent test`、`uv run pytest`、`uv run ruff check .`、`PERF_PY_ENGINES=1` 引擎测试与前端构建通过数；
   - Batch 1 完成后补充一份简短开销对比（开启 JFR vs 关闭探针在 `perf-demo` 下的 QPS 与 P99 差异）。
3. **DDL 迁移与向后兼容说明**（列出 `ALTER TABLE` / 新增表及旧探针降级行为）。
