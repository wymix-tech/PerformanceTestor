# PerformanceTestor P2 复核缺陷精准修复提示词（封版收口）

> **适用基线**：`main` 分支提交 `31d74d07`  
> **修复目标**：收口 P2 代码复核发现的 **2 处高危 Bug**（k6 双倍计数与预热时长不足、未采样流量跨服务掉染色）与 **3 处收尾细节**（Locust 饱和采样偏移、三角色流水线挂载火焰图工具、修复验证分类型验收联动），完成 P0–P2 全周期封版。

---

## 一、精准修复清单

### Fix-1（高危）：修复 `k6_engine.py` 双倍计数、0ms 污染分位与预热时长不足

- **文件**：`perf-py/engine/k6_engine.py`（及对应单测 `perf-py/tests/test_k6_engine.py`）
- **改动点**：
  1. **`parse_k6_json_line()`（L292–320）**：
     - k6 `run --out json=-` 对每次 HTTP 请求都会同时输出 `http_req_duration` 与 `http_req_failed` 两条 `Point`，当前两者都调用 `stats.record()` 会导致总请求数与 QPS 虚高 2 倍，且 50% 样本为 `0ms` 严重拉垮 P50/P95/P99。
     - **改法**：仅处理 `metric == K6_METRIC_DURATION`，直接忽略 `K6_METRIC_FAILED`（请求成功/失败已由 `_point_failed(data.get("tags"))` 从 `tags.expected_response` 准确判定）。
  2. **`_executor_of()`（L38–62）**：
     - `constant-vus` 与 `constant-arrival-rate` 的 `duration` 改为 `f"{load.warmup_sec + load.duration_sec}s"`（当前仅写了 `load.duration_sec`，导致 `formal_start_ms` 过滤预热期后正式采集时长短缺 `warmup_sec` 秒）；`ramping-vus` 若 `warmup_sec > ramp_up_sec`，补足正式期持续时间确保正式期完整达到 `load.duration_sec`。
- **回归单测（`test_k6_engine.py`）**：
  - 模拟单次请求同时输出一条 `http_req_duration`（`value=42.5`）和一条 `http_req_failed`（`value=0`）两行 JSON，断言 `stats` 的 `total == 1`、`p50 == 42.5`（而非 `total == 2`、`p50 == 0`）。
  - 断言 `warmup_sec=10, duration_sec=30` 时生成的 k6 `options` 总时长为 `40s`。

---

### Fix-2（高危）：修复 `HttpClientSupport.java` 未采样流量注入 `null` 导致跨服务掉染色

- **文件**：`perf-agent/src/main/java/com/perfagent/instrumentation/HttpClientSupport.java`（L90–175）
- **改动点**：
  - 当 `sampleRate < 1.0` 且压测请求未命中采样时，`TraceContext.currentStressTaskId()` 非空而 `TraceContext.currentTraceparent()` 返回 `null`。当前 `injectApache`、`injectRequest`、`injectSpring` 均无条件调用 `add/set("traceparent", null)`，在 OkHttp 上直接抛 `NPE` 被外层 `catch` 吞掉，连带丢弃紧随其后的 `X-Perf-Stress-Task-Id`。
  - **改法**：三处注入器均先判空再注入 `traceparent`，与 `X-Perf-Stress-Task-Id` 独立：
    ```java
    String tp = TraceContext.currentTraceparent();
    if (tp != null && !tp.isBlank()) {
        // 仅非空时写入 traceparent（判重后注入）
        setter.invoke(target, "traceparent", tp);
    }
    injectStressHeader(...); // 独立于 traceparent，未采样压测流量照常透传染色头
    ```
- **回归单测（`HttpClientSupportTest.java`）**：
  - 新增用例：在 `sampleRate=0.0`（`currentTraceparent() == null`、`currentStressTaskId() == "88"`）下，使用对 `null` header 值直接抛 `NullPointerException` 的严格假 OkHttp/Apache/Spring 请求对象，断言 `X-Perf-Stress-Task-Id: 88` 仍成功注入且未设置 `traceparent` 头。

---

### Fix-3（中低）：修复 `locust_engine.py` CPU 采样时间点偏移

- **文件**：`perf-py/engine/locust_engine.py`（L251–267）
- **改动点**：
  - 在 `saturation_watch()` 循环中，将 `last_times = os.times()` 移到 `gevent.sleep(1.0)` **之后**（当前在 `sleep` 之前读取，导致第 1 秒 `cpu_delta ≈ 0`、后续每秒错位滞后 1 秒）：
    ```python
    t0 = time.monotonic()
    gevent.sleep(1.0)
    now_cpu = os.times()
    drift_ms = (time.monotonic() - t0 - 1.0) * 1000
    cpu_delta = ((now_cpu.user + now_cpu.system)
                 - (last_cpu.user + last_cpu.system))
    last_cpu = now_cpu
    ```

---

### Fix-4（中低）：将 `profile_flamegraph_summary` 接入 `ROLE_ANALYSIS`

- **文件**：`perf-py/agents/roles.py`
- **改动点**：
  - 在 `ROLE_ANALYSIS.tool_names` 中追加 `"profile_flamegraph_summary"`；
  - 在 `ROLE_ANALYSIS.system_prompt` 中补充指引：存在 CPU / 分配 / 锁热点时优先调用 `profile_flamegraph_summary` 获取完整调用路径，再结合 `source_scan` 定位根因代码。

---

### Fix-5（可选收尾）：`issue_verify_service.py` 联动分类型验收

- **文件**：`perf-py/server/issue_verify_service.py`（L206–260）
- **改动点**：
  - 保留 `slow_sql` / `hot_method` 默认沿用 `P95` 下降 ≥ `30%` 且无回归（保证既有单测 100% 兼容）；
  - 当 `issue.issue_type == "stress"`（吞吐/容量瓶颈）时，支持满足「`P95` 下降 ≥ `30%` **或** `QPS` 提升 ≥ `15%`（且无回归）」即判 `passed = True`，并在 `verify_json` 中附加 `criteria` 字段说明命中规则。

---

## 二、封版验证命令

```bash
# 1. Java 探针全量单测
mvn -pl perf-agent test

# 2. Python 全量单测 + lint
cd perf-py && uv run pytest && uv run ruff check .

# 3. 压测引擎专项测试（含 k6 / locust / jmeter）
cd perf-py && PERF_PY_ENGINES=1 uv run pytest tests/test_locust_engine.py tests/test_jmeter_engine.py tests/test_k6_engine.py
```

完成后请输出改动文件清单及上述三条命令的实际通过数即可封版。
