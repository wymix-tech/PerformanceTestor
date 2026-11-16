"""stress 写路径单测（阶段 5 · 步骤 6）：请求解析 / 校验文案 / 场景桥接 / 基线比对。

全部文案断言取自 Java StressTaskService / BaselineCompareService 源码字符串，
逐字对齐（含 Java null 字符串拼接语义与「不修正」的 P99 不参与回归判定）。
"""

import pytest

from server.baseline_compare import _delta_pct, _fmt, _row, diff
from server.stress_request import (
    StageData,
    parse_stress_request,
    to_scenario,
    validate_stress_request,
)

# ---------------------------------------------------------------- 解析：缺省 / null 语义

def test_parse_defaults_match_java_field_init():
    """字段缺省 → Java 字段初始化默认值。"""
    r = parse_stress_request({"targetUrl": "http://x/api"})
    assert r.engine_type == "http" and r.method == "GET"
    assert r.concurrency == 1 and r.duration_sec == 60 and r.qps_limit == 0
    assert r.warmup_sec == 10 and r.ramp_up_sec == 0 and r.sample_interval_sec == 5
    assert r.task_name is None and r.app_name is None and r.body is None


def test_parse_explicit_null_int_becomes_zero_like_jackson():
    """显式 null 整数 → Jackson 基本类型收敛 0（并发 0 会被校验拒绝）。"""
    r = parse_stress_request({"targetUrl": "http://x/api", "concurrency": None,
                              "durationSec": None, "warmupSec": None})
    assert r.concurrency == 0 and r.duration_sec == 0 and r.warmup_sec == 0
    with pytest.raises(ValueError, match="并发数必须在 1-1000 之间"):
        validate_stress_request(r)


def test_parse_unknown_fields_ignored_and_stage_short_hand():
    """未知字段忽略（Spring Boot 默认）；stageConcurrencies 简写按档数均分时长。"""
    r = parse_stress_request({
        "targetUrl": "http://x/api", "unknownField": 1,
        "durationSec": 90, "stageConcurrencies": [10, 50, 100],
    })
    stages = r.resolve_stages()
    assert [s.concurrency for s in stages] == [10, 50, 100]
    assert all(s.duration_sec == 30 for s in stages), "90s / 3 档均分"


def test_resolve_stages_prefers_full_stages_and_filters_nonpositive():
    r = parse_stress_request({"stages": [{"concurrency": 5, "durationSec": 30}],
                              "stageConcurrencies": [1, 2]})
    assert r.resolve_stages() == [StageData(concurrency=5, duration_sec=30)]
    r2 = parse_stress_request({"stageConcurrencies": [0, -1]})
    assert r2.resolve_stages() is None


# ---------------------------------------------------------------- 校验：文案逐字对齐

def test_validate_url_message_exact():
    r = parse_stress_request({"targetUrl": "ftp://x"})
    with pytest.raises(ValueError) as ei:
        validate_stress_request(r)
    assert str(ei.value) == "压测目标 URL 必须以 http:// 或 https:// 开头"


@pytest.mark.parametrize("payload, message", [
    ({"targetUrl": "http://x", "concurrency": 1001}, "并发数必须在 1-1000 之间"),
    ({"targetUrl": "http://x", "durationSec": 3601}, "压测时长必须在 1-3600 秒之间"),
    ({"targetUrl": "http://x", "qpsLimit": 100001}, "QPS 上限必须在 0-100000 之间"),
    ({"targetUrl": "http://x", "warmupSec": 301}, "预热时长必须在 0-300 秒之间"),
    ({"targetUrl": "http://x", "rampUpSec": 601}, "并发爬坡时长必须在 0-600 秒之间"),
    ({"targetUrl": "http://x", "stages": [{"concurrency": 1, "durationSec": 1}] * 11},
     "分档数量不能超过 10"),
    ({"targetUrl": "http://x", "stages": [{"concurrency": 1001, "durationSec": 1}]},
     "分档并发数必须在 1-1000 之间"),
    ({"targetUrl": "http://x", "stages": [{"concurrency": 1, "durationSec": 601}]},
     "分档时长必须在 1-600 秒之间"),
])
def test_validate_common_messages_exact(payload, message):
    with pytest.raises(ValueError) as ei:
        validate_stress_request(parse_stress_request(payload))
    assert str(ei.value) == message


def test_validate_scenario_messages_exact():
    steps = [{"url": "http://x/a", "weight": 0},
             {"url": "http://x/b", "weight": 0}]
    r = parse_stress_request({"scenario": steps})
    with pytest.raises(ValueError) as ei:
        validate_stress_request(r)
    assert str(ei.value) == "场景至少需要 1 个权重大于 0 的步骤"

    r2 = parse_stress_request({"scenario": [{"url": "no-scheme", "weight": 1}]})
    with pytest.raises(ValueError) as ei:
        validate_stress_request(r2)
    assert str(ei.value) == "场景步骤「None」的 URL 必须以 http:// 或 https:// 开头" \
        or str(ei.value) == "场景步骤「null」的 URL 必须以 http:// 或 https:// 开头"

    r3 = parse_stress_request({"scenario": [{"url": "http://x", "weight": 1,
                                             "extract": [{"var": "t"}]}]})
    with pytest.raises(ValueError) as ei:
        validate_stress_request(r3)
    assert str(ei.value) == "场景步骤「None」的变量提取规则必须同时提供 var 与 path" \
        or str(ei.value) == "场景步骤「null」的变量提取规则必须同时提供 var 与 path"

    r4 = parse_stress_request({"scenario": [{"url": "http://x", "weight": 1}] * 21})
    with pytest.raises(ValueError) as ei:
        validate_stress_request(r4)
    assert str(ei.value) == "场景步骤数量不能超过 20"


# ---------------------------------------------------------------- 场景桥接

def test_to_scenario_single_mode_maps_request_and_load():
    r = parse_stress_request({"targetUrl": "http://x/api", "method": "post",
                              "headers": {"A": "b"}, "body": "x",
                              "concurrency": 8, "durationSec": 20,
                              "assertions": [{"source": "json", "key": "code", "op": "eq",
                                              "expected": "0"}]})
    s = to_scenario(r)
    assert s.engine == "locust"
    assert s.request.url == "http://x/api" and s.request.method == "post"
    assert s.request.assertions[0].key == "code"
    assert s.load.concurrency == 8 and s.load.duration_sec == 20
    assert not s.steps


def test_to_scenario_mode_maps_steps_with_weight_and_extract():
    r = parse_stress_request({"scenario": [
        {"url": "http://x/login", "weight": 2,
         "extract": [{"var": "t", "source": "body", "path": "data.token"}]},
        {"url": "http://x/list", "weight": 1},
    ]})
    s = to_scenario(r)
    assert [st.weight for st in s.steps] == [2, 1]
    assert s.steps[0].extract[0].path == "data.token"
    assert s.request is None


# ---------------------------------------------------------------- 基线比对

def test_fmt_and_delta_pct_match_java():
    assert _fmt(100.0) == "100" and _fmt(31.7) == "31.70"
    assert _delta_pct(110.0, 100.0) == "+10.0%"
    assert _delta_pct(80.0, 100.0) == "-20.0%"
    assert _delta_pct(5.0, 0.0) == ""


def test_row_regressed_thresholds_match_java():
    # QPS 下降超 10% 判回归；上升判 improved
    qps_drop = _row("QPS", 100, 80, True)
    assert qps_drop["regressed"] and not qps_drop["improved"]
    assert _row("QPS", 100, 110, True)["improved"]
    assert not _row("QPS", 100, 91, True)["regressed"], "恰 9% 未超阈值"
    # P95 上升超 20% 判回归
    assert _row("P95 (ms)", 100, 125, False)["regressed"]
    assert not _row("P95 (ms)", 100, 115, False)["regressed"]
    # P99 行不参与回归判定（Java 现状语义，不修正）
    assert not _row("P99 (ms)", 100, 500, False)["regressed"]
    # 错误率行 regressed 恒 False（由 diff 末尾 errC-errB>5 兜底）
    assert not _row("错误率 (%)", 0, 50, False)["regressed"]


def _task_with_result(result: dict):
    class T:
        pass

    t = T()
    t.result_json = __import__("json").dumps(result)
    return t


def test_diff_no_baseline_paths():
    class Repo:
        def __init__(self, rows):
            self._rows = rows

        def find_by_id(self, tid):
            return self._rows.get(tid)

        def query(self, status, limit):
            return list(self._rows.values())

    # 当前任务缺失 → noBaseline
    empty = diff(Repo({}), 1, 0)
    assert empty["noBaseline"] and not empty["rows"]
    # 基线缺失 → noBaseline
    cur = _task_with_result({"qps": 1})
    cur.id, cur.app_name, cur.target_url, cur.create_time, cur.finish_time = 1, "", "", None, None
    d = diff(Repo({1: cur}), 1, 0)
    assert d["noBaseline"]
