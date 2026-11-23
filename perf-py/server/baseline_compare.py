"""版本基线对比服务（阶段 5 · 写路径）：逐行对齐 Java BaselineCompareService。

回归判定阈值：QPS 下降超 10%、P95 上升超 20%、错误率上升超 5 个百分点
（注意：P99 行不参与 regressed 判定、错误率行 regressed 恒 False——由
diff 末尾的 (errC - errB) > 5.0 兜底，均为 Java 现状语义，不"修正"）。

P2-3 科学化升级（向后兼容）：
- 防抖基线：同场景（app+url）历史完成任务 ≥ DEBOUNCE_MIN_HISTORY 时，
  按 QPS/P95/错误率三维中位数向量选最接近的任务作基线（mode=debounced），
  削弱单次噪声（GC 抖动/邻机负载）导致的误判；不足 3 次回退最近一次（mode=latest）；
- 分验收类型：verdict 从 bool 升级为枚举 passed/regressed/no_baseline/
  insufficient_data，types 给出 throughput/latency/stability/resource 分类判定；
- 资源维度：两侧任务 JVM 窗口（创建→完成）聚合堆峰值/GC 增量/CPU 峰值对比
  （jvm_repo 未注入或窗口无数据时 resource=skipped，不影响既有判定）。
"""

from __future__ import annotations

import json
import math
import statistics

QPS_DROP_PCT = 10.0
P95_RISE_PCT = 20.0
ERROR_RATE_RISE_PCT = 5.0
# P2-3 资源维度阈值
HEAP_PEAK_RISE_PCT = 30.0
GC_COUNT_RISE_PCT = 50.0
CPU_PEAK_RISE_PCT = 20.0
# 防抖基线最少历史任务数（含当前任务之前的同场景完成任务）
DEBOUNCE_MIN_HISTORY = 3


def _fmt(v: float) -> str:
    """对齐 Java fmt：整值去小数，否则两位小数。"""
    return str(int(v)) if v == math.floor(v) else f"{v:.2f}"


def _delta_pct(cur: float, base: float) -> str:
    """对齐 Java deltaPct：base=0 → 空串；带符号一位小数百分比。"""
    if base == 0:
        return ""
    v = (cur - base) / abs(base) * 100
    return f"{'+' if v > 0 else ''}{v:.1f}%"


def _row(label: str, base: float, cur: float, higher_better: bool,
         rise_threshold: float | None = None) -> dict:
    """对齐 DiffRow 构造：delta 非空且 base!=0 时才判定 improved/regressed。

    P2-3：阈值参数化（P95 用 P95_RISE_PCT，资源维度传各自阈值；
    传 None 时保持旧行为——仅 QPS/P95 两类有 regressed 判定）。
    """
    delta = _delta_pct(cur, base)
    improved = False
    regressed = False
    if delta and base != 0:
        pct = (cur - base) / abs(base) * 100
        improved = pct > 0 if higher_better else pct < 0
        threshold = rise_threshold if rise_threshold is not None else (
            QPS_DROP_PCT if higher_better else
            (P95_RISE_PCT if label.startswith("P95") else None))
        if threshold is not None:
            regressed = (-pct > threshold) if higher_better else (pct > threshold)
    return {"label": label, "base": _fmt(base), "cur": _fmt(cur),
            "deltaPct": delta, "improved": improved, "regressed": regressed}


def _parse_result(result_json: str | None) -> dict | None:
    if not result_json or not result_json.strip():
        return None
    try:
        return json.loads(result_json)
    except (ValueError, TypeError):
        return None


def _scene_candidates(repo, current) -> list:
    """同场景（同应用+同 URL）、完成时间早于当前任务创建时间的历史完成任务，
    按完成时间倒序（auto_baseline 候选集的超集）。"""
    finished = repo.query("finished", 200)
    out = []
    for t in finished:
        if t.id is None or t.id == current.id:
            continue
        same_app = (not current.app_name or current.app_name == t.app_name)
        same_url = current.target_url is not None and current.target_url == t.target_url
        earlier = (t.finish_time is not None
                   and (current.create_time is None
                        or not t.finish_time > current.create_time))
        if same_app and same_url and earlier:
            out.append(t)
    out.sort(key=lambda t: t.finish_time or t.create_time, reverse=True)
    return out


def _metric_vector(result: dict | None) -> tuple[float, float, float] | None:
    if result is None:
        return None
    try:
        return (float(result.get("qps") or 0), float(result.get("p95Ms") or 0),
                float(result.get("errorRate") or 0))
    except (TypeError, ValueError):
        return None


def pick_debounced_baseline(repo, current) -> tuple[object | None, str]:
    """防抖基线选择（P2-3）。

    历史候选 ≥ DEBOUNCE_MIN_HISTORY：计算各任务 (QPS, P95, 错误率) 向量，
    取逐维中位数作参考点，选与中位数最近（归一化欧氏距离）的任务作基线
    （mode="debounced"）；不足时回退最近一次完成任务（mode="latest"）；
    无候选返回 (None, "")。
    """
    candidates = [t for t in _scene_candidates(repo, current)
                  if _metric_vector(_parse_result(t.result_json)) is not None]
    if not candidates:
        return None, ""
    if len(candidates) < DEBOUNCE_MIN_HISTORY:
        return candidates[0], "latest"
    vectors = [_metric_vector(_parse_result(t.result_json)) for t in candidates]
    medians = tuple(statistics.median(v[i] for v in vectors) for i in range(3))
    best_idx, best_dist = 0, float("inf")
    for i, vec in enumerate(vectors):
        # 逐维归一化（中位数量级差异大：QPS 可能上千而错误率是个位数小数）
        dist = 0.0
        for j in range(3):
            scale = abs(medians[j]) or 1.0
            dist += ((vec[j] - medians[j]) / scale) ** 2
        if dist < best_dist:
            best_dist, best_idx = dist, i
    return candidates[best_idx], "debounced"


def auto_baseline(repo, current) -> object | None:
    """对齐 autoBaseline：同应用同 URL、完成时间早于当前任务创建时间的最近完成任务。
    （保留旧入口：语义 = pick_debounced_baseline 的 latest 模式。）"""
    candidates = _scene_candidates(repo, current)
    return candidates[0] if candidates else None


def _jvm_summary(jvm_repo, app: str, start, end) -> dict | None:
    """任务时间窗内 JVM 聚合（P2-3 资源维度）：堆峰值 / GC 增量 / CPU 峰值。"""
    try:
        rows = jvm_repo.query_jvm_metrics(app=app, start=start, end=end,
                                          cursor_pair=None, limit_plus_one=2000)
    except Exception:  # noqa: BLE001 — jvm_repo 未接线/查询失败时资源维度跳过
        return None
    if not rows:
        return None
    young = [r.young_gc_count or 0 for r in rows]
    full = [r.full_gc_count or 0 for r in rows]
    return {
        "heapPeakMb": max((r.heap_used_mb or 0.0) for r in rows),
        # 窗口内 GC 计数增量（首尾差，兼容计数器单调递增口径）
        "gcCountDelta": (max(young) - min(young)) + (max(full) - min(full)),
        "cpuPeakPct": max((r.cpu_usage or 0.0) for r in rows),
    }


def diff(repo, task_id: int, baseline_task_id: int, jvm_repo=None) -> dict:
    """对齐 BaselineDiff（P2-3 扩展）：新增 verdict 枚举 / baselineMode / types，
    保留 rows / regressed / noBaseline / baseline 旧字段（向后兼容）。"""
    result: dict = {"baseline": None, "rows": [], "regressed": False, "noBaseline": False,
                    "verdict": "no_baseline", "baselineMode": "", "types": {}}
    current = repo.find_by_id(task_id)
    if current is None:
        result["noBaseline"] = True
        return result
    # 基线来源优先级：显式 baselineTaskId（调度/CI 指定）> 防抖基线 > 最近一次
    if baseline_task_id > 0:
        baseline = repo.find_by_id(baseline_task_id)
        result["baselineMode"] = "explicit"
    else:
        baseline, result["baselineMode"] = pick_debounced_baseline(repo, current)
    if baseline is None or baseline.id is None or baseline.id == current.id:
        result["noBaseline"] = True
        return result
    result["baseline"] = baseline

    base = _parse_result(baseline.result_json)
    cur = _parse_result(current.result_json)
    if base is None or cur is None:
        result["noBaseline"] = True
        return result

    rows = result["rows"]
    rows.append(_row("QPS", float(base.get("qps") or 0), float(cur.get("qps") or 0), True))
    rows.append(_row("P95 (ms)", float(base.get("p95Ms") or 0),
                     float(cur.get("p95Ms") or 0), False))
    rows.append(_row("P99 (ms)", float(base.get("p99Ms") or 0),
                     float(cur.get("p99Ms") or 0), False))
    err_b = float(base.get("errorRate") or 0)
    err_c = float(cur.get("errorRate") or 0)
    rows.append(_row("错误率 (%)", err_b, err_c, False))

    # ---- P2-3 资源维度：JVM 窗口聚合对比（两侧任务创建→完成窗口） ----
    resource_available = False
    if jvm_repo is not None and baseline.app_name == current.app_name:
        base_jvm = _jvm_summary(jvm_repo, baseline.app_name,
                                baseline.create_time, baseline.finish_time)
        cur_jvm = _jvm_summary(jvm_repo, current.app_name,
                               current.create_time, current.finish_time)
        if base_jvm and cur_jvm:
            resource_available = True
            rows.append(_row("堆峰值 (MB)", base_jvm["heapPeakMb"], cur_jvm["heapPeakMb"],
                             False, HEAP_PEAK_RISE_PCT))
            rows.append(_row("GC 次数 (增量)", float(base_jvm["gcCountDelta"]),
                             float(cur_jvm["gcCountDelta"]), False, GC_COUNT_RISE_PCT))
            rows.append(_row("JVM CPU 峰值 (%)", base_jvm["cpuPeakPct"],
                             cur_jvm["cpuPeakPct"], False, CPU_PEAK_RISE_PCT))

    # ---- 分验收类型判定（P2-3） ----
    def _type_regressed(row_label: str) -> bool:
        return any(r["regressed"] for r in rows if r["label"] == row_label)

    types = {
        "throughput": "regressed" if _type_regressed("QPS") else "passed",
        "latency": "regressed" if _type_regressed("P95 (ms)") else "passed",
        "stability": "regressed" if (err_c - err_b) > ERROR_RATE_RISE_PCT else "passed",
        "resource": ("regressed" if any(_type_regressed(lbl) for lbl in
                    ("堆峰值 (MB)", "GC 次数 (增量)", "JVM CPU 峰值 (%)"))
                    else "passed") if resource_available else "skipped",
    }
    result["types"] = types

    # 关键指标缺失（基线与当前均无有效 QPS）→ insufficient_data，不武断判定
    if float(base.get("qps") or 0) <= 0 and float(cur.get("qps") or 0) <= 0:
        result["verdict"] = "insufficient_data"
        return result

    regressed = any(r["regressed"] for r in rows) or (err_c - err_b) > ERROR_RATE_RISE_PCT
    result["regressed"] = regressed
    result["verdict"] = "regressed" if regressed else "passed"
    return result
