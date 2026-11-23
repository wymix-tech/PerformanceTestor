"""P2-4 测试：k6 脚本生成、模拟 JSON 输出统计一致性、分布式直方图合并、饱和告警。"""

import json
import random

import pytest

from engine.k6_engine import (
    K6Engine,
    _executor_of,
    generate_k6_script,
    parse_k6_json_line,
)
from engine.locust_engine import saturation_warning
from engine.stats import HistogramStatistician, shard_rows
from scenario.model import (
    AssertionSpec,
    CsvSpec,
    LoadSpec,
    RequestSpec,
    Scenario,
    StepSpec,
)


def _scenario(**load_kw) -> Scenario:
    load = LoadSpec.model_construct(
        concurrency=load_kw.get("concurrency", 10),
        duration_sec=load_kw.get("duration_sec", 30),
        warmup_sec=load_kw.get("warmup_sec", 5),
        ramp_up_sec=load_kw.get("ramp_up_sec", 0),
        qps_limit=load_kw.get("qps_limit", 0),
        sample_interval_sec=5,
    )
    return Scenario.model_construct(
        name="t", description="", engine="k6", app="demo-app", labels=[], gate=None,
        load=load, variables=load_kw.get("variables", {}),
        stages=[], stage_concurrencies=[],
        request=load_kw.get("request"), steps=load_kw.get("steps", []),
    )


# ---------------------------------------------------------------- 脚本生成

def test_generate_single_request_constant_vus():
    req = RequestSpec.model_construct(
        url="http://h/api/list", method="POST",
        headers={"X-Perf-Stress-Task-Id": "42", "Content-Type": "application/json"},
        body='{"kw": "{{kw}}"}', assertions=[], csv=None)
    script = generate_k6_script(_scenario(request=req))
    assert 'executor: "constant-vus"' in script and '"vus": 10' in script
    assert "X-Perf-Stress-Task-Id" in script  # 染色头透传
    assert "_render_body(" in script and "{{kw}}" in script  # JSON body 模板
    assert 'http.req("http://h/api/list"' in script


def test_generate_qps_limit_uses_arrival_rate_and_ramp_uses_ramping():
    req = RequestSpec.model_construct(url="http://h/a", method="GET",
                                      headers={}, body=None, assertions=[], csv=None)
    s1 = _scenario(request=req, qps_limit=200)
    assert _executor_of(s1)[0] == "constant-arrival-rate"
    assert '"rate": 200' in generate_k6_script(s1)
    s2 = _scenario(request=req, ramp_up_sec=10)
    assert _executor_of(s2)[0] == "ramping-vus"
    assert generate_k6_script(s2).count('"target": 10') == 2


def test_generate_steps_with_assertions_and_extract():
    step1 = StepSpec.model_construct(
        name="登录", alias="login", url="http://h/login", method="POST",
        headers={}, body='{"u":"admin"}', weight=2,
        extract=[__import__("scenario.model", fromlist=["VarExtractSpec"])
                 .VarExtractSpec.model_construct(var="token", source="body", path="$.token")],
        assertions=[AssertionSpec.model_construct(source="status", op="eq", expected="200")],
        csv=None)
    step2 = StepSpec.model_construct(
        name="查询", url="http://h/orders", method="GET",
        headers={"Authorization": "Bearer {{token}}"}, body=None, weight=1,
        extract=[], assertions=[], csv=None)
    script = generate_k6_script(_scenario(steps=[step1, step2]))
    assert script.count("http.req(") == 2
    assert '_json_path(res.json(), "$.token")' in script  # 变量提取
    assert 'vars["token"]' in script  # 后续步骤引用提取变量
    assert "check(res, {'status eq'" in script  # 断言生成


def test_generate_csv_sharedarray_with_worker_shard():
    csv = CsvSpec.model_construct(mode="sequential", text="id,name\n1,a\n2,b\n3,c\n4,d")
    req = RequestSpec.model_construct(url="http://h/x", method="GET", headers={},
                                      body=None, assertions=[], csv=csv)
    script = generate_k6_script(_scenario(request=req), worker_index=1, worker_count=2)
    assert "SharedArray('REQ_CSV'" in script
    assert "i % WORKER_COUNT === WORKER_INDEX" in script  # 分片协议
    assert "_parse_csv_row(line" in script or "_parse_csv_row(" in script


# ---------------------------------------------------------------- 模拟 JSON 输出统计

def _point(metric, value, time_s, tags=None):
    data = {"metric": metric, "value": value, "time": time_s}
    if tags:
        data["tags"] = tags
    return json.dumps({"type": "Point", "data": data})


def test_parse_k6_points_matches_direct_recording():
    """模拟 k6 JSON 输出：统计结果与 HistogramStatistician 直接 record 逐位一致。"""
    random.seed(11)
    delays = [random.randint(1, 800) for _ in range(500)]
    formal_start_ms = 1000
    stats = HistogramStatistician()
    ref = HistogramStatistician()
    for i, d in enumerate(delays):
        # 时间从 1.0s 开始（全部正式期），奇数序号模拟失败
        line = _point("http_req_duration", d, 1.5 + i * 0.01,
                      {"expected_response": "true" if i % 2 == 0 else "false"})
        assert parse_k6_json_line(line, stats, formal_start_ms) is True
        ref.record(d, i % 2 == 1)
    got, want = stats.build_summary(10), ref.build_summary(10)
    for k in ("total", "errors", "p50", "p95", "p99", "max", "avg", "error_rate", "qps"):
        assert got[k] == want[k], f"{k} 不一致"


def test_parse_k6_filters_warmup_and_bad_lines():
    stats = HistogramStatistician()
    assert parse_k6_json_line(_point("http_req_duration", 100, 0.5), stats, 1000) is False
    assert parse_k6_json_line(_point("http_req_duration", 100, 1.2), stats, 1000) is True
    assert parse_k6_json_line(_point("http_req_failed", 1, 1.3), stats, 1000) is False  # failed 指标忽略
    assert parse_k6_json_line(_point("vus", 10, 1.4), stats, 1000) is False  # 非目标指标
    assert parse_k6_json_line("not-json", stats, 1000) is False  # 脏行容错
    assert parse_k6_json_line(json.dumps({"type": "Metric", "name": "x"}), stats, 1000) is False
    s = stats.build_summary(10)
    assert s["total"] == 1 and s["errors"] == 0


def test_parse_k6_ignores_failed_point_no_double_counting():
    """Fix-1 回归：k6 每次请求同时输出 duration + failed 两条 Point，
    仅统计 duration——total == 1、p50 为请求真实耗时（而非 total == 2、p50 == 0）。"""
    stats = HistogramStatistician()
    assert parse_k6_json_line(_point("http_req_duration", 42, 2.0,
                                     {"expected_response": "true"}), stats, 1000) is True
    assert parse_k6_json_line(_point("http_req_failed", 0, 2.0), stats, 1000) is False
    s = stats.build_summary(1)
    assert s["total"] == 1 and s["errors"] == 0
    assert s["p50"] == 42 and s["p95"] == 42 and s["avg"] == 42.0


def test_executor_duration_includes_warmup():
    """Fix-1 回归：executor 总时长 = warmup + duration，正式期过滤预热后仍跑满 duration。"""
    # constant-vus：warmup 10 + duration 30 → 40s
    s1 = _scenario(duration_sec=30, warmup_sec=10)
    assert _executor_of(s1)[1]["duration"] == "40s"
    # constant-arrival-rate 同语义
    s2 = _scenario(qps_limit=100, duration_sec=30, warmup_sec=10,
                   request=RequestSpec.model_construct(
                       url="http://h/a", method="GET", headers={}, body=None,
                       assertions=[], csv=None))
    assert _executor_of(s2)[1]["duration"] == "40s"
    assert '"duration": "40s"' in generate_k6_script(s2)
    # ramping-vus：warmup(10) > ramp_up(5) → 第二阶段补足 10+30-5=35s
    s3 = _scenario(ramp_up_sec=5, warmup_sec=10, duration_sec=30)
    stages = _executor_of(s3)[1]["stages"]
    assert stages[0]["duration"] == "5s" and stages[1]["duration"] == "35s"
    # ramping-vus：warmup(5) ≤ ramp_up(10) → 第二阶段维持 duration 30s
    s4 = _scenario(ramp_up_sec=10, warmup_sec=5, duration_sec=30)
    assert _executor_of(s4)[1]["stages"][1]["duration"] == "30s"


# ---------------------------------------------------------------- 分布式合并

def test_merge_shards_exactly_equal_full_statistic():
    """10,000 条延迟随机拆 4 分片统计后合并，P50/P95/P99 与单机全量逐位相等。"""
    random.seed(7)
    delays = [random.randint(1, 5000) for _ in range(10000)]
    full = HistogramStatistician()
    for d in delays:
        full.record(d, d % 17 == 0)
    shards = [HistogramStatistician() for _ in range(4)]
    for i, d in enumerate(delays):
        shards[i % 4].record(d, d % 17 == 0)
    merged = HistogramStatistician.merge([s.export_buckets() for s in shards])
    a, b = full.build_summary(20), merged.build_summary(20)
    for k in ("total", "errors", "p50", "p95", "p99", "max", "avg", "error_rate"):
        assert a[k] == b[k], f"{k} 不一致（禁止对分片分位数求平均/取最大）"
    assert a["p50"] > 0 and a["p99"] > a["p95"]


def test_merge_empty_and_mismatched_bucket_counts():
    assert HistogramStatistician.merge([]).build_summary(1)["total"] == 0
    small = HistogramStatistician(bucket_count=100)
    small.record(50, False)
    big = HistogramStatistician()
    big.record(2000, False)
    merged = HistogramStatistician.merge([small.export_buckets(), big.export_buckets()])
    assert merged.build_summary(1)["total"] == 2
    assert merged.build_summary(1)["p99"] == 2000


def test_shard_rows_protocol():
    rows = list(range(10))
    assert shard_rows(rows, 0, 4) == [0, 4, 8]
    assert shard_rows(rows, 1, 4) == [1, 5, 9]
    parts = [shard_rows(rows, i, 4) for i in range(4)]
    assert sorted(x for p in parts for x in p) == rows  # 无重叠无遗漏
    assert shard_rows(rows, 4, 4) == []  # 越界分片为空
    assert shard_rows(rows, 0, 1) == rows  # 单机全量
    assert shard_rows(rows, 0, 0) == rows  # 非法 worker_count 视为单机


# ---------------------------------------------------------------- 饱和告警

def test_saturation_warning_thresholds():
    assert saturation_warning([5.0, 8.0, 10.0]) is None  # 未超 20ms
    assert saturation_warning([25.0, 10.0, 5.0]) is None  # 超 20ms 不足 3 次
    w = saturation_warning([25.0, 30.0, 40.0])
    assert w and "最大漂移 40ms" in w and "切换 k6/JMeter 引擎" in w
    w2 = saturation_warning([25.0, 30.0, 40.0], cpu_peak_pct=96.0)
    assert w2 and "CPU 峰值 96%" in w2


# ---------------------------------------------------------------- 可用性探测

def test_k6_engine_missing_binary_raises():
    engine = K6Engine(k6_bin=None)  # 显式 None → PATH 探测（测试环境无 k6）
    if engine._k6_bin:
        pytest.skip("测试机已安装 k6")
    with pytest.raises(FileNotFoundError, match="k6 未安装"):
        engine.run(_scenario())
