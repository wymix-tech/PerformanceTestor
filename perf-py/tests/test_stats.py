"""stats.py 测试：与 Java StressStatistician 语义对齐的关键断言。"""

from engine.stats import (
    BUCKET_COUNT,
    HistogramStatistician,
    histogram_max,
    percentile,
    round2,
)


def _hist_from(values):
    h = [0] * BUCKET_COUNT
    for v in values:
        h[int(min(max(v, 0), BUCKET_COUNT - 1))] += 1
    return h


def test_percentile_returns_bucket_index_like_java():
    # 100 个样本：1ms×60，5ms×30，100ms×10 → P95 target=95 → 累计到第 95 个
    # 桶（5ms 桶内第 35 个）时 cum=95 → 返回桶下标 5
    h = _hist_from([1] * 60 + [5] * 30 + [100] * 10)
    assert percentile(h, 50) == 1
    assert percentile(h, 95) == 5
    assert percentile(h, 99) == 100  # P99 target=99 → 落在 100ms 桶
    assert percentile(h, 100) == 100


def test_percentile_empty_hist():
    assert percentile([0] * 10, 50) == 0


def test_histogram_max_last_nonzero_bucket():
    h = _hist_from([1, 2, 25000, 31000])  # 31000 钳位进最后桶
    assert histogram_max(h) == BUCKET_COUNT - 1
    assert histogram_max([0] * 10) == 0


def test_record_clamps_to_bucket_range():
    s = HistogramStatistician()
    s.record(-5, error=False)      # 负值 → 桶 0
    s.record(99999, error=True)    # 超界 → 最后桶
    snap = s.stage_snapshot()
    assert snap["histogram"][0] == 1
    assert snap["histogram"][BUCKET_COUNT - 1] == 1
    assert snap["total"] == 2
    assert snap["error_count"] == 1


def test_collect_window_diff_semantics():
    s = HistogramStatistician()
    s.record(10, False)
    w1 = s.collect_window()
    assert w1["count"] == 1
    assert w1["histogram"][10] == 1
    s.record(20, False)
    s.record(21, True)
    w2 = s.collect_window()
    # 第二窗口只含新增样本（累计差值 → 各周期独立）
    assert w2["count"] == 2
    assert w2["histogram"][10] == 0
    assert w2["histogram"][20] == 1
    assert w2["histogram"][21] == 1
    assert w2["error_count"] == 1


def test_round2_matches_java_math_round():
    # Java Math.round(-200.5) = -200（向正无穷取半）→ -2.0
    assert round2(-2.005) == -2.0
    assert round2(2.005) == 2.01
    assert round2(37.0777777) == 37.08
    assert round2(0.125) == 0.13


def test_build_summary_aligns_http_engine_stage_outcome():
    # 对齐 HttpStressEngine StageOutcome 组装：
    # qps = total/formalElapsedSec；errorRate 为百分数；avg/max 来自直方图
    s = HistogramStatistician()
    for _ in range(95):
        s.record(100, False)
    for _ in range(5):
        s.record(200, True)
    summary = s.build_summary(formal_elapsed_sec=10.0)
    assert summary["total"] == 100
    assert summary["errors"] == 5
    assert summary["qps"] == 10.0
    assert summary["error_rate"] == 5.0  # 百分数
    assert summary["p50"] == 100
    assert summary["p95"] == 100
    assert summary["p99"] == 200
    assert summary["max"] == 200
    # avg = (95*100+5*200)/100 = 105.0
    assert summary["avg"] == 105.0


def test_build_summary_elapsed_floor_one_sec():
    s = HistogramStatistician()
    s.record(5, False)
    summary = s.build_summary(formal_elapsed_sec=0.1)  # 低于 1s 抬到 1s
    assert summary["qps"] == 1.0
