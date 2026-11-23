"""P2-3 科学化回归门禁测试：防抖基线选择、verdict 枚举、分验收类型判定与资源维度。"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from server.baseline_compare import (
    DEBOUNCE_MIN_HISTORY,
    diff,
    pick_debounced_baseline,
)

BASE_TIME = datetime(2026, 11, 20, 10, 0, 0)


def _task(tid, qps, p95, err_rate, finish_offset_min):
    return SimpleNamespace(
        id=tid, app_name="demo-app", target_url="/api/list", status="finished",
        create_time=BASE_TIME - timedelta(minutes=finish_offset_min + 10),
        finish_time=BASE_TIME - timedelta(minutes=finish_offset_min),
        result_json=f'{{"qps": {qps}, "p95Ms": {p95}, "p99Ms": {p95 * 1.5}, '
                    f'"errorRate": {err_rate}}}',
    )


class FakeRepo:
    def __init__(self, tasks):
        self._tasks = tasks

    def query(self, status, limit):
        return [t for t in self._tasks if t.status == status][:limit]

    def find_by_id(self, tid):
        return next((t for t in self._tasks if t.id == tid), None)


class FakeJvmRepo:
    """jvm_metric 查询替身：按窗口返回计数器行。

    peak_fn(start, end) -> (heap_peak, cpu_peak, gc_span)：按窗口区分基线/当前指标。
    """

    def __init__(self, peak_fn):
        self._peak_fn = peak_fn

    def query_jvm_metrics(self, app, start, end, cursor_pair, limit_plus_one):
        heap_peak, cpu_peak, gc_span = self._peak_fn(start, end)
        rows = []
        for i in range(3):
            young = 100 + int(gc_span * (i / 2))
            rows.append(SimpleNamespace(
                heap_used_mb=heap_peak * (0.8 + 0.2 * i / 2), heap_max_mb=1024.0,
                non_heap_used_mb=100.0, young_gc_count=young, young_gc_ms=10.0,
                full_gc_count=2, full_gc_ms=200.0, thread_count=50,
                cpu_usage=cpu_peak * (0.8 + 0.2 * i / 2), system_cpu=10.0,
                collect_time=start, id=i, app_name=app, extra_metrics=None))
        return rows


# ---------------------------------------------------------------- 防抖基线

def test_debounced_baseline_with_enough_history():
    # 三个历史任务：QPS 1000 / 950 / 500（500 为噪声离群），中位数 950
    tasks = [_task(1, 1000, 100, 0.1, 30), _task(2, 950, 105, 0.1, 20),
             _task(3, 500, 110, 0.1, 10)]
    current = _task(9, 980, 102, 0.1, 0)
    repo = FakeRepo(tasks)
    baseline, mode = pick_debounced_baseline(repo, current)
    assert mode == "debounced" and baseline.id == 2  # 最接近中位数向量的任务


def test_latest_fallback_with_insufficient_history():
    tasks = [_task(1, 1000, 100, 0.1, 30), _task(2, 500, 110, 0.1, 10)]
    current = _task(9, 980, 102, 0.1, 0)
    baseline, mode = pick_debounced_baseline(FakeRepo(tasks), current)
    assert mode == "latest" and baseline.id == 2  # 最近一次（完成时间最近）
    assert len(tasks) < DEBOUNCE_MIN_HISTORY


def test_no_history_returns_none():
    baseline, mode = pick_debounced_baseline(FakeRepo([]), _task(9, 980, 102, 0.1, 0))
    assert baseline is None and mode == ""


# ---------------------------------------------------------------- verdict 枚举与分类型

def test_verdict_regressed_throughput():
    tasks = [_task(1, 1000, 100, 0.1, 10), _task(9, 880, 102, 0.1, 0)]  # QPS -12%
    d = diff(FakeRepo(tasks), 9, 0)
    assert d["verdict"] == "regressed" and d["regressed"] is True
    assert d["types"]["throughput"] == "regressed"
    assert d["types"]["latency"] == "passed"
    assert d["baselineMode"] == "latest"


def test_verdict_regressed_latency_boundary():
    tasks = [_task(1, 1000, 100, 0.1, 10), _task(9, 990, 121, 0.1, 0)]  # P95 +21%
    d = diff(FakeRepo(tasks), 9, 0)
    assert d["verdict"] == "regressed" and d["types"]["latency"] == "regressed"
    # P99 不参与回归判定（既有语义保留）
    tasks2 = [_task(1, 1000, 100, 0.1, 10), _task(9, 990, 101, 0.1, 0)]
    d2 = diff(FakeRepo(tasks2), 9, 0)
    assert d2["verdict"] == "passed"


def test_verdict_regressed_stability():
    tasks = [_task(1, 1000, 100, 0.5, 10), _task(9, 995, 102, 6.0, 0)]  # 错误率 +5.5pp
    d = diff(FakeRepo(tasks), 9, 0)
    assert d["verdict"] == "regressed" and d["types"]["stability"] == "regressed"


def test_verdict_no_baseline_and_insufficient_data():
    d = diff(FakeRepo([]), 9, 0)
    assert d["verdict"] == "no_baseline" and d["noBaseline"] is True
    # 基线存在但双方 QPS 均为 0（引擎无 QPS 口径）
    t1 = _task(1, 0, 100, 0.1, 10)
    t9 = _task(9, 0, 102, 0.1, 0)
    d2 = diff(FakeRepo([t1, t9]), 9, 0)
    assert d2["verdict"] == "insufficient_data"
    assert d2["rows"], "rows 仍输出供人工复核"


def test_explicit_baseline_mode():
    tasks = [_task(1, 1000, 100, 0.1, 30), _task(2, 950, 105, 0.1, 10),
             _task(3, 940, 106, 0.1, 5), _task(9, 980, 102, 0.1, 0)]
    d = diff(FakeRepo(tasks), 9, 1)  # 显式指定基线任务 1
    assert d["baselineMode"] == "explicit" and d["baseline"].id == 1


# ---------------------------------------------------------------- 资源维度

def test_resource_dimension_regressed_on_heap_peak():
    tasks = [_task(1, 1000, 100, 0.1, 10), _task(9, 990, 102, 0.1, 0)]
    # 当前任务堆峰值较基线 +50%（超 30% 阈值），GC 增量 / CPU 正常
    jvm = FakeJvmRepo(lambda s, e: ((400, 40, 5) if e < BASE_TIME
                                    else (600, 45, 5)))
    d = diff(FakeRepo(tasks), 9, 0, jvm_repo=jvm)
    assert d["types"]["resource"] == "regressed"
    assert d["verdict"] == "regressed"
    labels = [r["label"] for r in d["rows"]]
    assert "堆峰值 (MB)" in labels and "GC 次数 (增量)" in labels


def test_resource_dimension_passed_and_skipped():
    tasks = [_task(1, 1000, 100, 0.1, 10), _task(9, 990, 102, 0.1, 0)]
    # 当前窗口堆峰值 ~+4%（低于 30% 阈值）
    jvm = FakeJvmRepo(lambda s, e: ((400, 40, 3) if e < BASE_TIME
                                    else (420, 42, 3)))
    d = diff(FakeRepo(tasks), 9, 0, jvm_repo=jvm)
    assert d["types"]["resource"] == "passed" and d["verdict"] == "passed"
    # 未注入 jvm_repo：resource=skipped，不影响既有判定
    d2 = diff(FakeRepo(tasks), 9, 0)
    assert d2["types"]["resource"] == "skipped" and d2["verdict"] == "passed"
