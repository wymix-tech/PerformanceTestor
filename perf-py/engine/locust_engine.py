"""Locust 引擎：场景 → Locust User/Task 生成，本地执行，产出对齐契约的结果。

架构决策（Issue #23 阶段 2）：
- 不手写 Python 线程发压（GIL 风险），复用 Locust/gevent 的协程发压
- 进程内 programmatic 运行（而非子进程 locustfile），以便用本工程的
  HistogramStatistician 收集逐请求延迟，保证与 Java 侧统计语义逐位一致
- gevent monkey-patch 必须发生在进程最早时刻——由 perf-cli-engine 入口负责，
  本模块只在函数体内延迟 import locust/gevent

与 HttpStressEngine 的语义对齐点：
- warmup 期请求不计入统计（listener 按时间过滤）
- samples 覆盖全程（含 warmup，与 Java sampler 一致），QPS = count×1000/intervalMs
- 最终统计基于正式期累计直方图，qps = total/formalElapsedSec（≥1s）
- QPS 上限：wait_time = constant_pacing(concurrency/qpsLimit)（全局整形近似）
- ramp-up：spawn_rate = concurrency/rampUpSec（爬坡期与预热期解耦）
- steps 场景支持 {{var}} 渲染 / extract 变量提取 / 步骤上下文引用（复用 stress/ 移植层，
  语义对齐 TemplateRenderer + StepContextStore + ResponseVarExtractor）
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from engine.result import Sample, StressTaskResult
from engine.stats import HistogramStatistician, percentile, round2
from scenario.model import Scenario
from stress.response_assertor import matches as assert_matches
from stress.response_var_extractor import create_pool, extract
from stress.template_renderer import (
    StepContextStore,
    build_alias_index,
    render,
)


def spawn_rate_for(scenario: Scenario) -> float:
    """每秒拉起用户数：爬坡期线性铺开，否则立即全量（对齐 rampUpSec 语义）。"""
    ramp = scenario.load.ramp_up_sec
    if ramp > 0:
        return max(0.1, scenario.load.concurrency / ramp)
    return float(scenario.load.concurrency)


def pacing_seconds_for(scenario: Scenario) -> float | None:
    """QPS 上限 → 每 worker 固定步进间隔（秒）；不限速返回 None。

    全局 QPS ≈ concurrency / pacing，故 pacing = concurrency / qpsLimit。
    """
    if scenario.load.qps_limit > 0:
        return scenario.load.concurrency / scenario.load.qps_limit
    return None


def _host_of(url: str) -> str:
    # "http://h:8080/api/x" → "http://h:8080"（Locust HttpUser 需要 host；绝对 URL 请求不受影响）
    parts = url.split("/", 3)
    return "/".join(parts[:3]) if len(parts) >= 3 and url.startswith("http") else url


def build_user_class(
    scenario: Scenario,
    stats: HistogramStatistician,
    state: dict,
    pool: dict[str, str],
    ctx: StepContextStore | None = None,
) -> type:
    """构造 Locust User 类（纯构造，不启动 runner——便于单测断言类属性）。

    state 约定：state["formal_start"]（epoch 秒，None=warmup 未结束）由 run() 填写；
    request listener 据此过滤 warmup 期样本。
    pool/ctx：变量池与步骤上下文池（steps 场景传入 ctx；单接口模式可为 None）。
    """
    import gevent.monkey  # noqa: F401  # 确保调用方已 patch（缺失时下方 import 会警告）
    from locust import HttpUser, constant_pacing

    # (url模板, method, headers模板, body模板, name, 断言, 权重, extract规则, 槽位下标)
    plans: list[tuple[str, str, dict[str, str], str | None, str, list, int, list | None, int | None]] = []
    host = ""

    if scenario.request:
        req = scenario.request
        host = _host_of(req.url)
        plans.append((req.url, req.method, req.headers, req.body, req.url, req.assertions, 1, None, None))
    else:
        first = scenario.steps[0]
        host = _host_of(first.url)
        for i, step in enumerate(scenario.steps):
            plans.append(
                (
                    step.url,
                    step.method,
                    step.headers,
                    step.body,
                    step.name or step.url,
                    step.assertions,
                    step.weight,
                    step.extract or None,
                    i,
                )
            )

    def _make_task(
        url_t: str,
        method: str,
        headers_t: dict[str, str],
        body_t: str | None,
        name: str,
        assertions: list,
        extract_rules: list | None,
        slot: int | None,
    ):
        def task_fn(user) -> None:  # noqa: ANN001
            # 双池渲染：上下文引用 → 扁平变量 → 保留原文（对齐 TemplateRenderer）
            miss = [0]
            url = render(url_t, pool, ctx, miss)
            headers = {k: render(v, pool, ctx, miss) for k, v in headers_t.items()}
            body = render(body_t, pool, ctx, miss) if body_t is not None else None
            with user.client.request(
                method=method,
                url=url,
                headers=headers or None,
                data=body,
                name=name,
                catch_response=True,
            ) as resp:
                status = resp.status_code or 0
                text = resp.text or ""
                resp_headers = {k: [v] for k, v in (resp.headers or {}).items()}
                if not assert_matches(assertions, status, resp_headers, text):
                    resp.failure(f"assertions failed on {name}")
                # 变量提取 + 上下文入池（仅真正收到响应时；错误响应照常入池，对齐 Java 契约）
                if extract_rules:
                    extract(text, resp_headers, extract_rules, pool, 65536)
                if ctx is not None and slot is not None:
                    ctx.put(slot, resp_headers, text)

        task_fn.__name__ = f"task_{name}"
        return task_fn

    # 注意：Locust 的 tasks 展开（含 (callable, weight) 元组）只发生在
    # 类创建时刻（UserMeta 从 class_dict 收集），类创建后再赋值 tasks 会让
    # 元组原样进入任务队列导致 'tuple' object is not callable。
    # 因此必须用 type() 把 tasks 放进 class_dict 动态建类。
    class_attrs: dict = {
        "tasks": [(_make_task(*item[:5], item[5], item[7], item[8]), item[6]) for item in plans],
        "host": host,
    }
    pacing = pacing_seconds_for(scenario)
    if pacing is not None:
        class_attrs["wait_time"] = constant_pacing(pacing)
    return type("ScenarioUser", (HttpUser,), class_attrs)


def run_locust(
    scenario: Scenario,
    *,
    on_sample: Callable[[Sample], None] | None = None,
) -> StressTaskResult:
    """本地执行 Locust 压测（阻塞至完成），返回对齐 StressTaskResult 的结果。"""
    import gevent
    from locust.env import Environment
    from locust.runners import LocalRunner

    stats = HistogramStatistician()
    samples: list[Sample] = []
    load = scenario.load
    state: dict[str, Any] = {"formal_start": None}

    def on_request(**kwargs) -> None:
        formal_start = state["formal_start"]
        if formal_start is None:
            return  # warmup 期不计入统计（对齐 Java）
        stats.record(kwargs.get("response_time") or 0, kwargs.get("exception") is not None)

    # 变量池 + 步骤上下文池（steps 场景启用上下文引用；别名登记告警写入结果）
    warnings: list[str] = []
    pool = create_pool(scenario.variables)
    if scenario.steps:
        alias_index = build_alias_index(scenario.steps, warnings)
        ctx: StepContextStore | None = StepContextStore(len(scenario.steps), alias_index, 65536)
    else:
        ctx = None

    user_class = build_user_class(scenario, stats, state, pool, ctx)
    env = Environment(user_classes=[user_class])
    env.events.request.add_listener(on_request)
    runner = LocalRunner(env)

    def sampler_greenlet() -> None:
        """周期采样：窗口差值 → Sample（全程采样含 warmup，对齐 Java sampler）。"""
        interval_ms = max(1, load.sample_interval_sec) * 1000
        while runner.state not in ("stopped", "stopping"):
            gevent.sleep(load.sample_interval_sec)
            w = stats.collect_window()
            s = Sample(
                sample_time=int(time.time() * 1000),
                concurrency=load.concurrency,
                qps=round2(w["count"] * 1000.0 / interval_ms),
                p50_ms=percentile(w["histogram"], 50),
                p95_ms=percentile(w["histogram"], 95),
                p99_ms=percentile(w["histogram"], 99),
                error_count=w["error_count"],
            )
            samples.append(s)
            if on_sample:
                try:
                    on_sample(s)
                except Exception:  # noqa: BLE001 — 监听器异常不影响压测（对齐 Java 行为）
                    pass

    started = time.time()
    runner.start(user_count=load.concurrency, spawn_rate=spawn_rate_for(scenario))
    state["formal_start"] = started + load.warmup_sec
    gevent.spawn(sampler_greenlet)
    gevent.spawn_later(load.warmup_sec + load.duration_sec + 2, runner.quit)
    runner.greenlet.join()

    formal_elapsed = max(1.0, time.time() - state["formal_start"])
    summary = stats.build_summary(formal_elapsed)

    return StressTaskResult(
        engine_type="locust",
        target_url=scenario.request.url if scenario.request else "",
        concurrency=load.concurrency,
        duration_sec=load.duration_sec,
        warmup_sec=load.warmup_sec,
        ramp_up_sec=load.ramp_up_sec,
        total_requests=summary["total"],
        error_count=summary["errors"],
        error_rate=summary["error_rate"],
        qps=summary["qps"],
        p50_ms=summary["p50"],
        p95_ms=summary["p95"],
        p99_ms=summary["p99"],
        max_ms=summary["max"],
        avg_ms=summary["avg"],
        samples=samples,
        warnings=warnings,
    )


class LocustEngine:
    """Engine SPI 实现：type=locust。"""

    @property
    def type(self) -> str:
        return "locust"

    def run(self, scenario: Scenario, listener: Any | None = None) -> StressTaskResult:
        return run_locust(scenario, on_sample=listener)
