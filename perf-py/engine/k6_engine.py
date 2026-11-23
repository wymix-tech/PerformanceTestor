"""k6 高精度发压引擎（P2-4）：解决 Locust（Python/gevent）长尾 P95/P99 系统性偏高。

架构：
- generate_k6_script(scenario)：负载 → k6 scenarios 执行器
  （qps_limit>0 → constant-arrival-rate；ramp_up>0 → ramping-vus；否则 constant-vus），
  支持单接口与 steps 多步场景、染色头（X-Perf-Stress-Task-Id 由 to_scenario 注入）、
  JSON body、变量提取（跨步骤上下文）、基本状态码/正文/JSON 断言、CSV SharedArray 参数化
  （按 shard_rows 协议的 workerIndex/workerCount 分片预留，多 Worker 不重复打热点行）。
- run(scenario, listener)：k6 run --out json=- 流式按行读取 Point
  （metric == http_req_duration / http_req_failed），过滤 warmup 期样本后喂入统一的
  HistogramStatistician（与 Locust/JMeter 完全一致的统计口径），周期触发 listener(Sample)
  实时落库 stress_sample。
- 与 LocustEngine/JMeterEngine 共用同一 Engine 协议（type="k6"）与 StressTaskResult 契约。

k6 未安装时由 stress_service.engine_available 探测 shutil.which("k6")，给出明确提示。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.result import Sample, StressTaskResult
from engine.stats import HistogramStatistician, percentile, round2
from scenario.model import Scenario

K6_METRIC_DURATION = "http_req_duration"
K6_METRIC_FAILED = "http_req_failed"


def _executor_of(scenario: Scenario) -> tuple[str, dict]:
    """负载参数 → k6 executor（类型 + 参数）。"""
    load = scenario.load
    if load.qps_limit > 0:
        return "constant-arrival-rate", {
            "rate": load.qps_limit,
            "timeUnit": "1s",
            "duration": f"{load.duration_sec}s",
            "preAllocatedVUs": max(1, load.concurrency),
            "maxVUs": max(1, load.concurrency),
        }
    if load.ramp_up_sec > 0:
        return "ramping-vus", {
            "startVUs": 0,
            "stages": [
                {"duration": f"{load.ramp_up_sec}s", "target": load.concurrency},
                {"duration": f"{load.duration_sec}s", "target": load.concurrency},
            ],
            "gracefulRampDown": "0s",
        }
    return "constant-vus", {
        "vus": load.concurrency,
        "duration": f"{load.duration_sec}s",
    }


def _js_str(s) -> str:
    """JS 字符串字面量转义（用户 headers/body/csv 内容原样嵌入，防脚本注入）。"""
    return json.dumps(s if s is not None else "", ensure_ascii=False)


def _js_header_value(v: str) -> str:
    """单头值 → JS 表达式：{{var}} 模板替换为 vars 引用（前后缀字符串拼接）。"""
    import re

    m = re.search(r"\{\{([^{}]+)\}\}", v)
    if not m:
        return _js_str(v)
    var = _js_str(m.group(1).strip())
    prefix = v[:m.start()]
    suffix = v[m.end():]
    expr = f"vars[{var}] ?? {_js_str(v)}"
    if prefix:
        expr = f"{_js_str(prefix)} + {expr}"
    if suffix:
        expr = f"{expr} + {_js_str(suffix)}"
    return expr


def _js_headers(headers: dict[str, str]) -> str:
    """headers dict → JS 对象字面量（值经 {{var}} 模板替换为 vars 引用）。"""
    parts = []
    for k, v in (headers or {}).items():
        parts.append(f"    {_js_str(k)}: {_js_header_value(v)}")
    return "{" + ",\n".join(parts) + "}" if parts else "{}"


def _assertion_checks(assertions: list) -> list[str]:
    """AssertionSpec 列表 → k6 check 断言 JS 片段（status/body/header/json）。"""
    checks = []
    for a in assertions or []:
        op = a.op or "eq"
        expected = _js_str(a.expected or "")
        if a.source == "status":
            checks.append(f"check(res, {{'status {op}': (r) => "
                          f"_cmp(r.status, {expected}, {_js_str(op)}) }});")
        elif a.source == "body":
            if op in ("contains", "notContains"):
                neg = "!" if op == "notContains" else ""
                checks.append(f"check(res, {{'body {op}': (r) => "
                              f"{neg}r.body.includes({expected}) }});")
            else:
                checks.append(f"check(res, {{'body {op}': (r) => "
                              f"_cmp(r.body, {expected}, {_js_str(op)}) }});")
        elif a.source == "header":
            checks.append(f"check(res, {{'header {a.key} {op}': (r) => "
                          f"_cmp(String(r.headers[{_js_str(a.key)}] ?? ''), "
                          f"{expected}, {_js_str(op)}) }});")
        elif a.source == "json":
            checks.append(f"check(res, {{'json {a.key} {op}': (r) => "
                          f"_cmp(String(_json_path(r.json(), {_js_str(a.key)}) ?? ''), "
                          f"{expected}, {_js_str(op)}) }});")
    return checks


def _extract_js(extract_rules: list) -> list[str]:
    """VarExtractSpec 列表 → 提取 JS 片段（写入跨步骤 vars 池）。"""
    out = []
    for e in extract_rules or []:
        if (e.source or "body") == "header":
            out.append(f"  vars[{_js_str(e.var)}] = String(res.headers[{_js_str(e.path)}] ?? '');")
        else:
            out.append(f"  vars[{_js_str(e.var)}] = "
                       f"String(_json_path(res.json(), {_js_str(e.path)}) ?? '');")
    return out


def _csv_block(scenario: Scenario) -> tuple[list[str], list[str]]:
    """CSV 参数化 → SharedArray 常量与迭代内取行 JS。
    行按 WORKER_COUNT/WORKER_INDEX 分片（P2-4 分布式协议，防多机重复打热点行）。"""
    decls: list[str] = []
    reads: list[str] = []
    sources: list[tuple[str, object, str]] = []
    if scenario.request and scenario.request.csv:
        sources.append(("req_csv", scenario.request.csv, "REQ_CSV"))
    for i, step in enumerate(scenario.steps):
        if step.csv:
            sources.append((f"step_csv_{i}", step.csv, f"STEP{i}_CSV"))
    for _var, spec, const in sources:
        rows = [r for r in (spec.text or "").strip().splitlines() if r.strip()]
        if len(rows) <= 1:
            continue
        header = rows[0]
        data_rows = rows[1:]
        decls.append(
            f"const {const} = new SharedArray('{const}', function () {{\n"
            f"  const all = [\n" +
            "".join(f"    {_js_str(r)},\n" for r in data_rows) +
            "  ];\n"
            "  // P2-4 分片：row_idx % WORKER_COUNT === WORKER_INDEX（单机时全量）\n"
            "  return all.filter((r, i) => i % WORKER_COUNT === WORKER_INDEX);\n"
            "});")
        reads.append(
            f"  const {const}_line = {const}[__ITER % {const}.length];\n"
            f"  if ({const}_line !== undefined) "
            f"Object.assign(vars, _parse_csv_row({const}_line, {_js_str(header)}));")
    return decls, reads


def _single_request_fn(scenario: Scenario, csv_reads: str) -> str:
    """单接口模式 default_fn（染色头已含在 req.headers）。"""
    req = scenario.request
    body_js = f"  const body = _render_body({_js_str(req.body)}, vars);\n" if req.body else ""
    body_arg = "body !== null ? {body: body} : {}"
    checks = "\n".join("  " + c for c in _assertion_checks(req.assertions))
    return f"""
function default_fn() {{
  const vars = Object.assign({{}}, VARS);
{csv_reads}
{body_js}  const res = http.req({_js_str(req.url)}, Object.assign({{
    method: {_js_str(req.method)},
    headers: {_js_headers(req.headers)},
    tags: {{name: {_js_str(req.url)}}},
  }}, {body_arg}));
{checks}
  return res;
}}
"""



def _steps_request_fn(scenario: Scenario, csv_reads: str) -> str:
    """steps 多步场景 default_fn：顺序执行各步骤（共享 vars 池实现跨步骤提取引用）。"""
    blocks = []
    for i, step in enumerate(scenario.steps):
        body_js = (f"  const body{i} = _render_body({_js_str(step.body)}, vars);\n"
                   if step.body else "")
        body_arg = f"body{i} !== null ? {{body: body{i}}} : {{}}"
        checks = "\n".join("  " + c for c in _assertion_checks(step.assertions))
        extract = "\n".join(_extract_js(step.extract))
        first_csv = (csv_reads + "\n") if i == 0 else ""
        blocks.append(f"""  // 步骤 {i + 1}：{step.name or step.url}（weight={step.weight}）
{first_csv}{body_js}  let res{i} = http.req({_js_str(step.url)}, Object.assign({{
    method: {_js_str(step.method)},
    headers: {_js_headers(step.headers)},
    tags: {{name: {_js_str(step.name or step.url)}}},
  }}, {body_arg}));
{checks}
{extract}""")
        if i == len(scenario.steps) - 1:
            blocks.append(f"  return res{i};")
    body = "\n".join(blocks)
    return f"""
function default_fn() {{
  const vars = Object.assign({{}}, VARS);
{body}
}}
"""


def generate_k6_script(scenario: Scenario, *, worker_index: int = 0,
                       worker_count: int = 1) -> str:
    """场景 → k6 脚本（JS）。单接口与 steps 多步场景均支持。

    worker_index/worker_count：P2-4 分布式分片协议（CSV SharedArray 行按
    row_idx % workerCount === workerIndex 切分；单机默认全量）。
    """
    if not scenario.request and not scenario.steps:
        raise ValueError("k6 场景必须定义 request（单接口）或 steps（多步）之一")
    executor, exec_params = _executor_of(scenario)
    csv_decls, csv_reads = _csv_block(scenario)
    decls = "\n".join(csv_decls)
    iter_csv = "\n".join(csv_reads)
    request_fn = (_steps_request_fn(scenario, iter_csv) if scenario.steps
                  else _single_request_fn(scenario, iter_csv))
    header = f"""// 由 perf-platform generate_k6_script 生成（P2-4 高精度引擎）
import http from 'k6/http';
import {{ check }} from 'k6';
import {{ SharedArray }} from 'k6/data';

const WORKER_INDEX = __ENV.WORKER_INDEX ? Number(__ENV.WORKER_INDEX) : {worker_index};
const WORKER_COUNT = __ENV.WORKER_COUNT ? Number(__ENV.WORKER_COUNT) : {worker_count};
const VARS = {_js_str(dict(scenario.variables or {}))};
{decls}

function _parse_csv_row(line, header) {{
  const cols = header.split(',');
  const vals = line.split(',');
  const row = {{}};
  cols.forEach((c, i) => {{ row[c.trim()] = (vals[i] ?? '').trim(); }});
  return row;
}}

function _cmp(actual, expected, op) {{
  switch (op) {{
    case 'eq': return String(actual) === String(expected);
    case 'ne': return String(actual) !== String(expected);
    case 'gt': return Number(actual) > Number(expected);
    case 'lt': return Number(actual) < Number(expected);
    case 'regex': return new RegExp(expected).test(String(actual));
    case 'contains': return String(actual).includes(String(expected));
    case 'notContains': return !String(actual).includes(String(expected));
    default: return false;
  }}
}}

function _json_path(obj, path) {{
  // 支持 $.a.b.c 风格 JSON 路径（k6 场景常用子集）
  const keys = String(path).replace(/^[$]\\./, '').split('.');
  let cur = obj;
  for (const k of keys) {{
    if (cur === null || cur === undefined) return undefined;
    cur = cur[k];
  }}
  return cur;
}}

function _render_body(tpl, vars) {{
  if (tpl === null || tpl === undefined) return null;
  return String(tpl).replace(/\\{{\\{{([^}}]+)\\}}\\}}/g, (m, k) => vars[k.trim()] ?? m);
}}
"""
    options = f"""
export const options = {{
  scenarios: {{
    stress: Object.assign({{ executor: {_js_str(executor)}, exec: 'default_fn' }},
                          {_js_str(exec_params)}),
  }},
}};
"""
    return header + options + request_fn


def parse_k6_json_line(line: str, stats: HistogramStatistician,
                       formal_start_ms: int) -> bool:
    """解析 k6 JSON 输出一行；Point 命中 http_req_duration/failed 时记录统计。

    返回是否计入正式期（warmup 期样本按时间过滤，不计入统计，对齐 Locust/JMeter）。
    """
    line = line.strip()
    if not line.startswith("{"):
        return False
    try:
        obj = json.loads(line)
    except ValueError:
        return False
    if obj.get("type") != "Point":
        return False
    data = obj.get("data") or {}
    metric = data.get("metric")
    if metric not in (K6_METRIC_DURATION, K6_METRIC_FAILED):
        return False
    ts_ms = _point_time_ms(data.get("time"))
    if ts_ms is not None and ts_ms < formal_start_ms:
        return False  # warmup 期不计入统计（对齐 Java/Locust 语义）
    value = data.get("value")
    if metric == K6_METRIC_FAILED:
        stats.record(0, bool(value))
    else:
        # 失败请求的 duration 也计入直方图（与 Locust on_request 语义一致）
        stats.record(float(value or 0), _point_failed(data.get("tags")))
    return True


def _point_failed(tags: dict | None) -> bool:
    """k6 http_req_duration 的 expected_response 标签（true=预期响应）。"""
    if not tags:
        return False
    expected = tags.get("expected_response")
    return expected is not None and str(expected) not in ("true", "1")


def _point_time_ms(raw) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw if raw > 1e11 else raw * 1000)  # 秒/毫秒自适应
    try:
        t = str(raw).replace("Z", "+00:00")
        return int(datetime.fromisoformat(t).timestamp() * 1000)
    except ValueError:
        return None


class K6Engine:
    """Engine SPI 实现：type=k6（k6 二进制缺失时 run 抛 FileNotFoundError）。"""

    def __init__(self, k6_bin: str | None = None) -> None:
        self._k6_bin = k6_bin or shutil.which("k6")

    @property
    def type(self) -> str:
        return "k6"

    def run(self, scenario: Scenario, listener: Any | None = None) -> StressTaskResult:
        if not self._k6_bin:
            raise FileNotFoundError(
                "k6 未安装或不在 PATH（https://k6.io/install/；或 K6_BIN 指定二进制路径）")
        load = scenario.load
        script = generate_k6_script(scenario)
        with tempfile.TemporaryDirectory(prefix="perf-k6-") as tmp:
            script_path = Path(tmp) / "stress_scenario.js"
            script_path.write_text(script, encoding="utf-8")
            stats = HistogramStatistician()
            samples: list[Sample] = []
            started = time.time()
            formal_start_ms = int(started * 1000) + load.warmup_sec * 1000
            env = {**os.environ, "WORKER_INDEX": "0", "WORKER_COUNT": "1"}
            proc = subprocess.Popen(
                [self._k6_bin, "run", "--out", "json=-", "--quiet", str(script_path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                errors="replace", env=env, cwd=str(Path(__file__).resolve().parent.parent))

            def sampler() -> None:
                """周期采样：窗口差值 → Sample（全程含 warmup，对齐 Java sampler）。"""
                interval_ms = max(1, load.sample_interval_sec) * 1000
                while proc.poll() is None:
                    time.sleep(load.sample_interval_sec)
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
                    if listener:
                        try:
                            listener(s)
                        except Exception:  # noqa: BLE001 — 监听器异常不影响压测
                            pass

            threading.Thread(target=sampler, daemon=True).start()
            assert proc.stdout is not None
            for line in proc.stdout:
                parse_k6_json_line(line, stats, formal_start_ms)
            proc.wait()
            stderr_tail = ""
            if proc.stderr:
                try:
                    stderr_tail = (proc.stderr.read() or "")[-500:]
                except Exception:  # noqa: BLE001
                    pass
            if proc.returncode != 0:
                raise RuntimeError(
                    f"k6 执行失败（exit={proc.returncode}）：{stderr_tail.strip()}")

        formal_elapsed = max(1.0, time.time() - started - load.warmup_sec)
        summary = stats.build_summary(formal_elapsed)
        return StressTaskResult(
            engine_type="k6",
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
            warnings=[],
        )
