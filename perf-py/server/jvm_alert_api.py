"""JVM 阈值告警路由：补齐 /api/jvm-alert-rules（CRUD + 手动判定）。

契约对齐 perf-web jvmAlertApi（index.ts）与 JvmAlertRulePanel.vue：
wire 形态 {id, appName, metricKey, threshold, windowSec, quietSec, enabled,
webhook, lastAlertTime, createTime}。metric_key 取值口径与 jvm_alert_rule
表注释一致：heapUsedRatio / fullGcCountDelta / threadCount。
判定数据源为最近 window_sec 的 jvm_metric 时序（CollectorRepository 复用）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Body, Query, Request
from sqlalchemy import desc, select

from server.collector_repo import JvmAlertRuleModel, JvmMetricModel

router = APIRouter(prefix="/api/jvm-alert-rules")

METRICS: dict[str, str] = {
    "heapUsedRatio": "堆使用率 %（heap_used/heap_max*100，窗口内峰值）",
    "fullGcCountDelta": "Full GC 次数增量（窗口内 max-min）",
    "threadCount": "JVM 线程数（窗口内峰值）",
}


def _ok(data) -> dict:
    return {"code": 0, "message": "success", "data": data}


def _err(message: str) -> dict:
    return {"code": 1, "message": message, "data": None}


def _fmt(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt is not None else None


def _to_camel(r: JvmAlertRuleModel) -> dict:
    return {
        "id": r.id, "appName": r.app_name, "metricKey": r.metric_key,
        "threshold": r.threshold, "windowSec": r.window_sec,
        "quietSec": r.quiet_sec, "enabled": r.enabled, "webhook": r.webhook,
        "lastAlertTime": _fmt(r.last_alert_time),
        "createTime": _fmt(r.create_time),
    }


def _sessions(request: Request):
    """同域会话缝：告警规则与 jvm_metric 同属 collector 仓储管理。"""
    return request.app.state.collector_repo._sessions  # noqa: SLF001


def _load_rule(request: Request, rule_id: int) -> JvmAlertRuleModel | None:
    with _sessions(request)() as s:
        return s.get(JvmAlertRuleModel, rule_id)


def _evaluate(rule: JvmAlertRuleModel, metrics: list[JvmMetricModel]) -> float | None:
    """窗口内指标值：heapUsedRatio / fullGcCountDelta / threadCount。"""
    if not metrics:
        return None
    key = rule.metric_key
    if key == "heapUsedRatio":
        ratios = [m.heap_used_mb / m.heap_max_mb * 100.0
                  for m in metrics if m.heap_max_mb and m.heap_max_mb > 0]
        return round(max(ratios), 1) if ratios else None
    if key == "fullGcCountDelta":
        counts = [m.full_gc_count for m in metrics]
        return float(max(counts) - min(counts))
    if key == "threadCount":
        return float(max(m.thread_count for m in metrics))
    return None


@router.get("")
def list_rules(request: Request, appName: str | None = None,
               limit: int = 100) -> dict:
    stmt = select(JvmAlertRuleModel).order_by(desc(JvmAlertRuleModel.id))
    if appName:
        stmt = stmt.where(JvmAlertRuleModel.app_name == appName)
    stmt = stmt.limit(max(1, min(limit, 200)))
    with _sessions(request)() as s:
        rows = list(s.scalars(stmt))
    return _ok([_to_camel(r) for r in rows])


@router.get("/metrics")
def metrics() -> dict:
    return _ok([{"key": k, "label": v} for k, v in METRICS.items()])


def _apply_payload(rule: JvmAlertRuleModel, body: dict) -> str | None:
    """camelCase payload → 规则字段；非法指标返回错误文案。"""
    if "appName" in body and body["appName"]:
        rule.app_name = body["appName"]
    if "metricKey" in body:
        if body["metricKey"] not in METRICS:
            return f"未知指标: {body['metricKey']}（支持：{', '.join(METRICS)}）"
        rule.metric_key = body["metricKey"]
    if "threshold" in body:
        rule.threshold = float(body["threshold"])
    if "windowSec" in body:
        rule.window_sec = max(1, int(body["windowSec"]))
    if "quietSec" in body:
        rule.quiet_sec = max(0, int(body["quietSec"]))
    if "webhook" in body:
        rule.webhook = body["webhook"]
    if "enabled" in body:
        rule.enabled = 1 if body["enabled"] else 0
    return None


@router.post("")
def add_rule(request: Request, body: dict = Body(...)) -> dict:
    if not body.get("appName") or not body.get("metricKey"):
        return _err("appName 与 metricKey 必填")
    err = _apply_payload(JvmAlertRuleModel(), body)
    if err:
        return _err(err)
    rule = JvmAlertRuleModel(
        app_name=body["appName"], metric_key=body["metricKey"],
        threshold=float(body.get("threshold") or 0),
        window_sec=max(1, int(body.get("windowSec") or 300)),
        quiet_sec=max(0, int(body.get("quietSec") or 900)),
        enabled=1 if body.get("enabled") else 0,
        webhook=body.get("webhook"),
    )
    with _sessions(request)() as s:
        dup = s.scalars(select(JvmAlertRuleModel)
                        .where(JvmAlertRuleModel.app_name == rule.app_name,
                               JvmAlertRuleModel.metric_key == rule.metric_key)
                        .limit(1)).first()
        if dup is not None:
            return _err(f"规则已存在：{rule.app_name}/{rule.metric_key}（uk_app_metric）")
        s.add(rule)
        s.commit()
        s.refresh(rule)
    return _ok(_to_camel(rule))


@router.put("/{rule_id}")
def update_rule(request: Request, rule_id: int, body: dict = Body(...)) -> dict:
    """加载/变更/提交同一会话内完成（跨会话操作 detached 实体会报错）。"""
    with _sessions(request)() as s:
        rule = s.get(JvmAlertRuleModel, rule_id)
        if rule is None:
            return _err(f"jvm alert rule not found: {rule_id}")
        err = _apply_payload(rule, body)
        if err:
            return _err(err)
        s.commit()
        s.refresh(rule)
        return _ok(_to_camel(rule))


@router.delete("/{rule_id}")
def remove_rule(request: Request, rule_id: int) -> dict:
    with _sessions(request)() as s:
        rule = s.get(JvmAlertRuleModel, rule_id)
        if rule is None:
            return _err(f"jvm alert rule not found: {rule_id}")
        s.delete(rule)
        s.commit()
    return _ok(None)


@router.post("/{rule_id}/enabled")
def set_enabled(request: Request, rule_id: int,
                enabled: bool = Query(...)) -> dict:
    with _sessions(request)() as s:
        rule = s.get(JvmAlertRuleModel, rule_id)
        if rule is None:
            return _err(f"jvm alert rule not found: {rule_id}")
        rule.enabled = 1 if enabled else 0
        s.commit()
        s.refresh(rule)
        return _ok(_to_camel(rule))


@router.post("/{rule_id}/check")
def check_rule(request: Request, rule_id: int) -> dict:
    """手动触发一次判定：读最近 window_sec 的 jvm_metric 求指标值对比阈值。

    triggered 时刷新 last_alert_time（quiet_sec 静默期内不重复刷新，
    webhook 通知节奏对齐）；webhook 已配置时经 server.notify 发送。
    """
    with _sessions(request)() as s:
        rule = s.get(JvmAlertRuleModel, rule_id)
        if rule is None:
            return _err(f"jvm alert rule not found: {rule_id}")
        if not rule.enabled:
            return _err(f"规则已停用（id={rule_id}），启用后再判定")
        end = datetime.now()
        start = end - timedelta(seconds=max(1, rule.window_sec))
        rows = request.app.state.collector_repo.query_jvm_metrics(
            app=rule.app_name, start=start, end=end,
            cursor_pair=None, limit_plus_one=500,
        )
        current = _evaluate(rule, rows)
        if current is None:
            return _ok({"triggered": False, "metricKey": rule.metric_key,
                        "currentValue": None, "threshold": rule.threshold,
                        "reason": f"窗口内无 {rule.app_name} 的 JVM 指标"})
        triggered = current >= rule.threshold
        app_name, metric_key, threshold = rule.app_name, rule.metric_key, rule.threshold
        if triggered:
            quiet_until = (rule.last_alert_time or datetime.min) \
                + timedelta(seconds=max(0, rule.quiet_sec))
            if quiet_until <= end or rule.last_alert_time is None:
                rule.last_alert_time = end
                s.commit()
            webhook = rule.webhook
        else:
            webhook = None
        last_alert = _fmt(rule.last_alert_time)
    if triggered and webhook:
        from server.notify import send as notify_send

        notify_send(webhook, "[JVM 告警]",
                    f"{app_name} {metric_key}={current} ≥ 阈值 {threshold}")
    return _ok({"triggered": triggered, "metricKey": rule.metric_key,
                "currentValue": current, "threshold": rule.threshold,
                "lastAlertTime": last_alert})
