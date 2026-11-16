"""版本基线对比服务（阶段 5 · 写路径）：逐行对齐 Java BaselineCompareService。

回归判定阈值：QPS 下降超 10%、P95 上升超 20%、错误率上升超 5 个百分点
（注意：P99 行不参与 regressed 判定、错误率行 regressed 恒 False——由
diff 末尾的 (errC - errB) > 5.0 兜底，均为 Java 现状语义，不"修正"）。
"""

from __future__ import annotations

import json
import math

QPS_DROP_PCT = 10.0
P95_RISE_PCT = 20.0
ERROR_RATE_RISE_PCT = 5.0


def _fmt(v: float) -> str:
    """对齐 Java fmt：整值去小数，否则两位小数。"""
    return str(int(v)) if v == math.floor(v) else f"{v:.2f}"


def _delta_pct(cur: float, base: float) -> str:
    """对齐 Java deltaPct：base=0 → 空串；带符号一位小数百分比。"""
    if base == 0:
        return ""
    v = (cur - base) / abs(base) * 100
    return f"{'+' if v > 0 else ''}{v:.1f}%"


def _row(label: str, base: float, cur: float, higher_better: bool) -> dict:
    """对齐 DiffRow 构造：delta 非空且 base!=0 时才判定 improved/regressed。"""
    delta = _delta_pct(cur, base)
    improved = False
    regressed = False
    if delta and base != 0:
        pct = (cur - base) / abs(base) * 100
        improved = pct > 0 if higher_better else pct < 0
        if higher_better:
            regressed = -pct > QPS_DROP_PCT
        elif label.startswith("P95"):
            regressed = pct > P95_RISE_PCT
    return {"label": label, "base": _fmt(base), "cur": _fmt(cur),
            "deltaPct": delta, "improved": improved, "regressed": regressed}


def _parse_result(result_json: str | None) -> dict | None:
    if not result_json or not result_json.strip():
        return None
    try:
        return json.loads(result_json)
    except (ValueError, TypeError):
        return None


def auto_baseline(repo, current) -> object | None:
    """对齐 autoBaseline：同应用同 URL、完成时间早于当前任务创建时间的最近完成任务。"""
    finished = repo.query("finished", 200)
    best = None
    for t in finished:
        if t.id is None or t.id == current.id:
            continue
        same_app = (not current.app_name or current.app_name == t.app_name)
        same_url = current.target_url is not None and current.target_url == t.target_url
        earlier = (t.finish_time is not None
                   and (current.create_time is None
                        or not t.finish_time > current.create_time))
        newer = (best is None
                 or (best.finish_time is not None and t.finish_time is not None
                     and t.finish_time > best.finish_time))
        if same_app and same_url and earlier and newer:
            best = t
    return best


def diff(repo, task_id: int, baseline_task_id: int) -> dict:
    """对齐 BaselineDiff：{baseline, rows, regressed, noBaseline}（baseline 为 ORM 行或 None）。"""
    result: dict = {"baseline": None, "rows": [], "regressed": False, "noBaseline": False}
    current = repo.find_by_id(task_id)
    if current is None:
        result["noBaseline"] = True
        return result
    baseline = repo.find_by_id(baseline_task_id) if baseline_task_id > 0 \
        else auto_baseline(repo, current)
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

    result["regressed"] = any(r["regressed"] for r in rows) \
        or (err_c - err_b) > ERROR_RATE_RISE_PCT
    return result
