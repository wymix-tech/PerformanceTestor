"""容量评估与扩容建议路由：补齐 POST /api/capacity/analyze 与 GET /api/capacity/advice。

复用 capacity.analyzer.analyze() 拟合 stress_task.result_json 中的 stagePoints，
并补齐前端 CapacityAdvicePanel.vue 消费的 advices 与 jvmAdvice 字段；
结果回写 result_json["capacity"]，使 /api/stress/tasks/{id}/report 自动带出容量结果。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, Request

from capacity.analyzer import analyze as analyze_capacity
from engine.result import StagePoint

router = APIRouter(prefix="/api/capacity")


def _ok(data) -> dict:
    return {"code": 0, "message": "success", "data": data}


def _build_jvm_advice(collector_repo, app_name: str | None) -> dict | None:
    """最近 1 小时 jvm_metric 峰值 → 堆内存/GC 建议（无数据返回 None）。"""
    if not app_name:
        return None
    end_dt = datetime.now()
    rows = collector_repo.query_jvm_metrics(
        app=app_name, start=end_dt - timedelta(hours=1), end=end_dt,
        cursor_pair=None, limit_plus_one=500,
    )
    if not rows:
        return None
    peak_heap = max(r.heap_used_mb for r in rows)
    heap_max = max((r.heap_max_mb for r in rows), default=0.0)
    ratio = round(peak_heap / heap_max * 100, 1) if heap_max > 0 else 0.0
    if ratio >= 80:
        suggested_gb = max(2, int(round((heap_max * 1.5) / 1024.0)))
        heap_size = (f"-Xms{suggested_gb}g -Xmx{suggested_gb}g"
                     f"（当前峰值占比偏高，建议扩容堆内存）")
    else:
        heap_size = (f"当前堆上限 {int(heap_max)}MB 仍充足，"
                     f"可保持 -Xms 与 -Xmx 等值避免动态扩缩容")
    return {
        "peakHeapUsedMb": round(peak_heap, 1),
        "heapMaxMb": round(heap_max, 1),
        "peakHeapRatio": ratio,
        "heapSize": heap_size,
        "gcCollector": "G1GC（JDK 17 默认，高吞吐兼顾可控停顿）",
        "params": ["-XX:+UseG1GC", "-XX:MaxGCPauseMillis=200",
                   "-XX:+HeapDumpOnOutOfMemoryError"],
    }


def _build_scale_advices(cap_dict: dict, jvm_advice: dict | None) -> list[dict]:
    advices: list[dict] = []
    max_qps = cap_dict.get("maxQps", 0)
    max_conc = cap_dict.get("maxConcurrency", 0)
    if cap_dict.get("extrapolated"):
        advices.append({
            "title": "未触达目标 P95 上限，可继续探测更高并发档位",
            "description": (f"当前安全容量估计为并发 {max_conc} / QPS {max_qps}，"
                            f"建议追加更高分档确认拐点。"),
            "scaleType": "容量探测",
            "riskLevel": "low",
        })
    else:
        advices.append({
            "title": f"单实例安全容量约 {max_qps} QPS（并发 {max_conc}）",
            "description": (f"目标 P95={cap_dict.get('targetP95Ms')}ms 下已逼近拐点，"
                            f"业务峰值超出该值时建议水平扩容。"),
            "scaleType": "水平扩容",
            "riskLevel": "medium",
            "suggestedInstances": 2,
        })
    if jvm_advice and jvm_advice["peakHeapRatio"] >= 80:
        advices.append({
            "title": f"JVM 堆峰值占比达 {jvm_advice['peakHeapRatio']}%，"
                     f"存在 GC 停顿风险",
            "description": "建议先排查大对象分配与缓存无界增长，再按需提升堆上限"
                           "或分摊实例数。",
            "scaleType": "垂直调优",
            "riskLevel": "high",
        })
    return advices


@router.post("/analyze")
def analyze(request: Request, body: dict = Body(...)) -> dict:
    task_id = int(body.get("taskId") or 0)
    target_p95 = float(body.get("targetP95Ms") or 500.0)
    stress_repo = request.app.state.stress_repo
    task = stress_repo.find_by_id(task_id)
    if task is None:
        return {"code": 1, "message": f"stress task not found: {task_id}", "data": None}

    result_obj = stress_repo.parse_result_json(task) or {}
    raw_points = result_obj.get("stagePoints") or []
    points = [
        StagePoint(
            concurrency=int(p.get("concurrency") or 0),
            qps=float(p.get("qps") or 0),
            p50_ms=float(p.get("p50Ms") or 0),
            p95_ms=float(p.get("p95Ms") or 0),
            p99_ms=float(p.get("p99Ms") or 0),
            error_rate=float(p.get("errorRate") or 0),
            total_requests=int(p.get("totalRequests") or 0),
        )
        for p in raw_points
    ]
    cap = analyze_capacity(points, target_p95).to_java_json()
    app_name = body.get("app") or task.app_name
    jvm_advice = _build_jvm_advice(request.app.state.collector_repo, app_name)
    cap["advices"] = _build_scale_advices(cap, jvm_advice)
    if jvm_advice is not None:
        cap["jvmAdvice"] = jvm_advice

    # 回写 stress_task.result_json，使 /api/stress/tasks/{id}/report 直接包含 capacity
    result_obj["capacity"] = cap
    stress_repo.update_result_json(
        task_id, json.dumps(result_obj, ensure_ascii=False, separators=(",", ":"))
    )
    return _ok(cap)


@router.get("/advice")
def advice(request: Request, app: str | None = None) -> dict:
    """最近一次已评估容量任务（含 advices/jvmAdvice，analyze 时已回写）。"""
    stress_repo = request.app.state.stress_repo
    tasks = stress_repo.query("finished", 50)
    for t in tasks:
        if app and t.app_name != app:
            continue
        parsed = stress_repo.parse_result_json(t) or {}
        if parsed.get("capacity"):
            return _ok({"taskId": t.id, "appName": t.app_name,
                        "capacity": parsed["capacity"]})
    return _ok({"taskId": None, "appName": app or "", "capacity": None})
