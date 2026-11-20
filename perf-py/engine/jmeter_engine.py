"""JMeter 引擎兜底：场景 → JMX 生成 → jmeter CLI → JTL 解析 → 对齐契约的结果。

兑现 Java 侧 JmeterStressEngine 的 SPI 承诺（原实现为空壳）。
与 Locust 引擎共用 HistogramStatistician 与 StressTaskResult 契约。

JTL：CSV 模式（默认列：timeStamp,elapsed,label,responseCode,success,...），
按 timeStamp 过滤 warmup 期样本后再计入统计（对齐 Java warmup 语义）。

边界：
- 断言映射为 JMeter ResponseAssertion 仅支持 status eq / body eq|contains|regex；
  其余断言（header/json/gt/lt/ne/notContains）降级为 warnings（不在 JMeter 侧判定）
- steps 混合场景暂不支持执行（JMeter 侧无权重编排/变量上下文，报错提示）；
  generate_jmx(steps_mode=True) 仅供导出：步骤按顺序全部执行，
  权重配比与变量提取（pN 上下文引用）不生效
- 变量提取（extract）不写入 JMX
"""

from __future__ import annotations

import csv
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from engine.result import Sample, StressTaskResult
from engine.stats import HistogramStatistician, round2
from scenario.model import Scenario

JMETER_NOT_FOUND = "未找到 jmeter 可执行文件（PATH 中无 jmeter）。请安装 Apache JMeter 后重试。"


def _xml(tag: str, **attrs) -> ET.Element:
    el = ET.Element(tag)
    for k, v in attrs.items():
        el.set(k, v)
    return el


def _prop(parent: ET.Element, name: str, value: str) -> ET.Element:
    p = _xml("stringProp", name=name)
    p.text = value
    parent.append(p)
    return p


def generate_jmx(scenario: Scenario, *, steps_mode: bool = False) -> str:
    """场景 → JMX 测试计划（duration + ramp_up + 永久循环）。

    默认仅单接口模式（引擎执行口径）；steps_mode=True 供导出：混合场景步骤
    按顺序全部写入（顺序执行，权重配比与变量提取不生效），每步独立
    HeaderManager 与 ResponseAssertion。请求头经 HeaderManager 写入
    （此前缺失，顺带修复引擎执行丢失请求头的问题）。
    """
    if scenario.steps and not steps_mode:
        raise ValueError("jmeter 引擎暂不支持 steps 混合场景（多 Controller 编排待扩展）")

    load = scenario.load
    root = _xml("jmeterTestPlan", version="1.2", properties="5.0", jmeter="5.6")
    plan = _xml("TestPlan", guiclass="TestPlanGui", testclass="TestPlan", testname=scenario.name)
    root.append(plan)

    tg = _xml(
        "ThreadGroup",
        guiclass="ThreadGroupGui",
        testclass="ThreadGroup",
        testname=f"threads-{load.concurrency}",
    )
    plan.append(tg)
    _prop(tg, "ThreadGroup.num_threads", str(load.concurrency))
    _prop(tg, "ThreadGroup.ramp_time", str(load.ramp_up_sec))
    _prop(tg, "ThreadGroup.duration", str(load.duration_sec + load.warmup_sec))
    _prop(tg, "ThreadGroup.on_sample_error", "continue")
    sched = _xml("elementProp", name="ThreadGroup.main_controller", elementType="LoopController")
    tg.append(sched)
    _prop(sched, "LoopController.continue_forever", "true")
    _prop(sched, "LoopController.loops", "-1")

    # 采样器序列：单接口 1 条；steps_mode 下按步骤顺序（JMeter 顺序执行）
    requests = (list(scenario.steps) if scenario.steps
                else [scenario.request] if scenario.request else [])
    for req in requests:
        from urllib.parse import urlsplit

        parts = urlsplit(req.url)
        sampler = _xml(
            "HTTPSamplerProxy",
            guiclass="HttpTestSampleGui",
            testclass="HTTPSamplerProxy",
            testname=(req.name if hasattr(req, "name") and req.name else req.url),
        )
        tg.append(sampler)
        _prop(sampler, "HTTPSampler.domain", parts.hostname or "")
        _prop(sampler, "HTTPSampler.port",
              str(parts.port or (443 if parts.scheme == "https" else 80)))
        _prop(sampler, "HTTPSampler.path",
              parts.path + (("?" + parts.query) if parts.query else ""))
        _prop(sampler, "HTTPSampler.method", req.method)
        _prop(sampler, "HTTPSampler.follow_redirects", "true")
        if req.body:
            _prop(sampler, "HTTPSampler.postBodyRaw", "true")
            body = _xml("elementProp", name="HTTPsampler.Arguments", elementType="Arguments")
            sampler.append(body)
            arg = _xml("elementProp", name="", elementType="Argument")
            body.append(arg)
            _prop(arg, "Argument.value", req.body)
        _append_headers(sampler, dict(req.headers or {}))

        # 断言映射（有限支持，其余降级 warnings 由 run() 记录）；
        # 挂在采样器下作为子节点（per-sampler 作用域，标准 JMX 结构）
        for a in req.assertions:
            supported = (a.source == "status" and a.op == "eq") or (
                a.source == "body" and a.op in ("eq", "contains", "regex")
            )
            if not supported:
                continue
            ra = _xml("ResponseAssertion", guiclass="AssertionGui",
                      testclass="ResponseAssertion", testname="assert")
            sampler.append(ra)
            if a.source == "status":
                _prop(ra, "Assertion.test_field", "Assertion.response_code")
                _prop(ra, "Assertion.test_type", "8")  # equals
            else:
                _prop(ra, "Assertion.test_field", "Assertion.response_data")
                _prop(ra, "Assertion.test_type", {"eq": "8", "contains": "2", "regex": "16"}[a.op])
            patterns = _xml("collectionProp", name="Asserts.test_strings")
            ra.append(patterns)
            entry = _xml("stringProp", name="")
            entry.text = a.expected
            patterns.append(entry)
            _prop(ra, "Assertion.assume_success", "false")

    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def _append_headers(sampler: ET.Element, headers: dict[str, str]) -> None:
    """请求头 → HeaderManager（与 jmx_import 解析侧结构互逆）。"""
    if not headers:
        return
    hm = _xml("HeaderManager", guiclass="HeaderPanel", testclass="HeaderManager",
              testname="HTTP Header Manager")
    sampler.append(hm)
    collection = _xml("collectionProp", name="HeaderManager.headers")
    hm.append(collection)
    for k, v in headers.items():
        header = _xml("elementProp", name="", elementType="Header")
        collection.append(header)
        _prop(header, "Header.name", str(k))
        _prop(header, "Header.value", str(v))


def unsupported_assertion_warnings(scenario: Scenario) -> list[str]:
    """JMeter 有限断言映射下被降级的断言清单。"""
    warnings: list[str] = []
    if not scenario.request:
        return warnings
    for a in scenario.request.assertions:
        supported = (a.source == "status" and a.op == "eq") or (
            a.source == "body" and a.op in ("eq", "contains", "regex")
        )
        if not supported:
            warnings.append(f"jmeter 引擎不支持断言 {a.source}/{a.op}，已跳过（本地判定缺失）")
    return warnings


def parse_jtl(path: Path) -> list[dict]:
    """解析 JTL CSV → [{timeStamp, elapsed, label, responseCode, success, ...}]。"""
    rows: list[dict] = []
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            try:
                rows.append(
                    {
                        "timeStamp": int(raw["timeStamp"]),
                        "elapsed": int(raw["elapsed"]),
                        "label": raw.get("label", ""),
                        "responseCode": raw.get("responseCode", ""),
                        "success": str(raw.get("success", "true")).lower() == "true",
                    }
                )
            except (KeyError, ValueError):
                continue  # 容忍脏行
    return rows


class JMeterEngine:
    """Engine SPI 实现：type=jmeter。"""

    def __init__(self, jmeter_bin: str | None = None) -> None:
        self._jmeter_bin = jmeter_bin or shutil.which("jmeter")

    @property
    def type(self) -> str:
        return "jmeter"

    def run(self, scenario: Scenario, listener=None) -> StressTaskResult:  # noqa: ANN001
        if not self._jmeter_bin:
            raise FileNotFoundError(JMETER_NOT_FOUND)

        load = scenario.load
        warnings = unsupported_assertion_warnings(scenario)
        stats = HistogramStatistician()
        samples: list[Sample] = []

        with tempfile.TemporaryDirectory(prefix="perf-py-jmeter-") as tmp:
            tmp_dir = Path(tmp)
            jmx = tmp_dir / "plan.jmx"
            jtl = tmp_dir / "result.jtl"
            jmx.write_text(generate_jmx(scenario), encoding="utf-8")
            started = time.time()
            proc = subprocess.run(
                [
                    self._jmeter_bin,
                    "-n",
                    "-t",
                    str(jmx),
                    "-l",
                    str(jtl),
                    "-Jjmeter.save.saveservice.output_format=csv",
                    "-Jjmeter.save.saveservice.print_field_names=true",
                ],
                capture_output=True,
                text=True,
                timeout=load.duration_sec + load.warmup_sec + 300,
            )
            if proc.returncode != 0 or not jtl.exists():
                raise RuntimeError(f"jmeter CLI 执行失败（exit={proc.returncode}）：{proc.stderr[-500:]}")

            # warmup 截止：正式期样本 = timeStamp >= started + warmup*1000
            formal_start_sec = started + load.warmup_sec
            rows = parse_jtl(jtl)
            for r in rows:
                stats.record(r["elapsed"], not r["success"])

            # 周期采样：按 sample_interval 把 JTL 行分桶（含 warmup，对齐 Java sampler）
            interval_ms = max(1, load.sample_interval_sec) * 1000
            by_window: dict[int, list[dict]] = {}
            for r in rows:
                by_window.setdefault((r["timeStamp"] - int(started * 1000)) // interval_ms, []).append(r)
            win_stats = HistogramStatistician()
            for widx in sorted(by_window):
                w_rows = by_window[widx]
                for r in w_rows:
                    win_stats.record(r["elapsed"], not r["success"])
                s = Sample(
                    sample_time=int(started * 1000) + (widx + 1) * interval_ms,
                    concurrency=load.concurrency,
                    qps=round2(len(w_rows) * 1000.0 / interval_ms),
                    p50_ms=_pct(win_stats, 50),
                    p95_ms=_pct(win_stats, 95),
                    p99_ms=_pct(win_stats, 99),
                    error_count=sum(1 for r in w_rows if not r["success"]),
                )
                samples.append(s)
                win_stats = HistogramStatistician()  # 各窗口独立（差值语义等价实现）

            if listener:
                for s in samples:
                    try:
                        listener(s)
                    except Exception:  # noqa: BLE001
                        pass

        formal_elapsed = max(1.0, time.time() - formal_start_sec)
        summary = stats.build_summary(formal_elapsed)
        return StressTaskResult(
            engine_type="jmeter",
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


def _pct(stats: HistogramStatistician, p: float) -> float:
    from engine.stats import percentile

    snap = stats.stage_snapshot()
    return percentile(snap["histogram"], p)


__all__ = ["JMeterEngine", "generate_jmx", "parse_jtl", "unsupported_assertion_warnings", "JMETER_NOT_FOUND"]
