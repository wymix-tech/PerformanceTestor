"""locust_engine 纯逻辑测试（不实际启动 runner）+ assertor 测试。

涉及 locust 运行时的用例用 importorskip 守护；CI 在 python job 中
以 --extra locust 安装后全部执行。
"""

import pytest

from engine.assertor import evaluate
from engine.locust_engine import pacing_seconds_for, spawn_rate_for
from scenario.model import (
    AssertionSpec,
    LoadSpec,
    RequestSpec,
    Scenario,
    StepSpec,
)

pytest.importorskip("locust", reason="未安装 locust（uv sync --extra locust）")

from engine.locust_engine import build_user_class, run_locust  # noqa: E402
from engine.stats import HistogramStatistician  # noqa: E402


def _scenario(**load) -> Scenario:
    return Scenario(
        name="t",
        app="demo-app",
        request=RequestSpec(url="http://localhost:8081/api/order/recent?limit=10"),
        load=LoadSpec(**load),
    )


def test_spawn_rate_instant_without_ramp_up():
    assert spawn_rate_for(_scenario(concurrency=50)) == 50.0


def test_spawn_rate_linear_with_ramp_up():
    # ramp_up 需落在 warmup 窗口内（场景模型校验）
    assert spawn_rate_for(_scenario(concurrency=60, warmup_sec=30, ramp_up_sec=30)) == pytest.approx(2.0)


def test_pacing_qps_limit():
    # pacing = concurrency / qpsLimit → 全局 QPS ≈ qpsLimit
    assert pacing_seconds_for(_scenario(concurrency=20, qps_limit=100)) == pytest.approx(0.2)
    assert pacing_seconds_for(_scenario(concurrency=20)) is None


def test_build_user_class_shape():
    scenario = _scenario(concurrency=5, qps_limit=50)
    stats = HistogramStatistician()
    cls = build_user_class(scenario, stats, {"formal_start": None})
    assert cls.host == "http://localhost:8081"
    assert len(cls.tasks) == 1
    assert cls.wait_time is not None  # qps_limit>0 → constant_pacing


def test_build_user_class_steps_weights():
    scenario = Scenario(
        name="steps",
        app="demo-app",
        steps=[
            StepSpec(name="a", url="http://localhost:8081/api/order/recent", weight=3),
            StepSpec(name="b", url="http://localhost:8081/api/user/list?limit=10", weight=1),
        ],
    )
    cls = build_user_class(scenario, HistogramStatistician(), {"formal_start": None})
    # 类创建时 locust 元类已把 (task, weight) 按权重复制展开进任务列表
    # （weight=3 → 3 份同一函数对象，weight=1 → 1 份），共 3+1=4 项
    from collections import Counter

    counts = Counter(id(t) for t in cls.tasks)
    assert sorted(counts.values()) == [1, 3]
    assert cls.host == "http://localhost:8081"


# ---------------------------------------------------------------- assertor
def test_assertor_status_eq():
    ok = [AssertionSpec(source="status", op="eq", expected="200")]
    assert evaluate(ok, 200, "", {}) == []
    assert evaluate(ok, 500, "", {})


def test_assertor_json_path():
    a = [AssertionSpec(source="json", key="data.code", op="eq", expected="0")]
    assert evaluate(a, 200, '{"data":{"code":0}}', {}) == []
    assert "路径不存在" in evaluate(a, 200, '{"other":1}', {})[0]
    assert "非 JSON" in evaluate(a, 200, "not-json", {})[0]


def test_assertor_header_case_insensitive():
    a = [AssertionSpec(source="header", key="X-Req-Id", op="contains", expected="abc")]
    assert evaluate(a, 200, "", {"x-req-id": "xxabcxx"}) == []


def test_assertor_gt_lt_numeric():
    gt = [AssertionSpec(source="json", key="latency", op="gt", expected="10")]
    assert evaluate(gt, 200, '{"latency": 11}', {}) == []
    assert evaluate(gt, 200, '{"latency": 5}', {})
    assert "需要数值" in evaluate(gt, 200, '{"latency": "abc"}', {})[0]


def test_assertor_regex_and_ne():
    a = [
        AssertionSpec(source="body", op="regex", expected="id\":\\d+"),
        AssertionSpec(source="status", op="ne", expected="404"),
    ]
    assert evaluate(a, 200, '{"id":42}', {}) == []


# ---------------------------------------------------------------- run_locust（真实小压测，仅在有 locust 时执行）
def test_run_locust_smoke_against_placeholder():
    """对 httpbin.org 的 2 秒极小压测——验证 runner 链路；无外网环境时跳过。"""
    scenario = Scenario(
        name="smoke",
        app="t",
        request=RequestSpec(url="https://httpbin.org/status/200"),
        load=LoadSpec(concurrency=2, duration_sec=2, warmup_sec=0, sample_interval_sec=1),
    )
    result = run_locust(scenario)
    assert result.engine_type == "locust"
    assert result.total_requests >= 0
    assert result.samples  # 有采样产出
