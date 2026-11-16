"""探针上报处理服务（阶段 5 · 步骤 3）：逐行对齐 Java AgentReportService。

上报 body（与 agent 端 MetricsReporter 约定一致）：
    {"type":"trace|jvm|slowSql|slowCommand|hotspot","appName":"...",
     "targetProjectName":"...","items":[...],"appPort":...,"appVersion":"...","dropped":{...}}

容错语义逐条保留：
- appName 缺失 → 忽略整批（仅日志）；项目不存在 → Controller 层 404（见 collector_api）；
- 单条 item 解析失败不影响整批；单批落库失败不影响后续上报（异常在 router 层兜底）；
- trace 树 / methodChain 序列化为紧凑 JSON（对齐 Jackson 默认无空格形态）；
- slowCommand：type 必填（跳过空 type 条目）、类型小写、command 超 1000 截断加 "..."；
- slowSql：source 缺省 "jdbc"、dbHost 缺省 ""、同批同库地址只自动注册一次连接；
- hotspot：嵌套聚合 {sampleTime, items:[{method, samples}]}，method 空或 samples<=0 跳过。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from server.collector_repo import (
    CollectorRepository,
    DbConnModel,
    HotspotMethodModel,
    JvmMetricModel,
    SlowCommandModel,
    SlowSqlModel,
    TraceChainModel,
)

log = logging.getLogger(__name__)

AGENT_VERSION = "1.0.0"
MAX_COMMAND_LEN = 1000


def to_local_datetime(epoch_ms: int) -> datetime:
    """epoch 毫秒 → 本地时区 datetime（对齐 Java Instant.atZone(systemDefault)）。"""
    return datetime.fromtimestamp(epoch_ms / 1000.0)


def compact_json(value: Any) -> str:
    """对齐 Jackson writeValueAsString：紧凑分隔符、非 ASCII 不转义。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _str_val(o) -> str | None:
    return None if o is None else str(o)


def _long_val(o) -> int:
    return int(o) if isinstance(o, (int, float)) and not isinstance(o, bool) else 0


def _int_val(o) -> int:
    return _long_val(o)


def _double_val(o) -> float:
    return float(o) if isinstance(o, (int, float)) and not isinstance(o, bool) else 0.0


def handle_report(body: dict, remote_host: str, repo: CollectorRepository) -> None:
    """处理一次批量上报（对齐 Java handleReport 主流程）。"""
    report_type = _str_val(body.get("type"))
    app_name = _str_val(body.get("appName")) or ""
    if not app_name:
        log.warning("agent report ignored: missing appName, type=%s", report_type)
        return
    # 项目准入（Controller 已预检，此处双保险）：targetProjectName 缺省回退 appName
    target_project = _str_val(body.get("targetProjectName"))
    if target_project is None or not target_project.strip():
        target_project = app_name
    if not repo.project_exists(target_project):
        return
    # 心跳注册应用实例（appPort/appVersion 供压测地址自动预填与版本快照）
    register_instance(app_name, remote_host, body, repo)
    items = body.get("items")
    if not isinstance(items, list) or not items:
        return
    try:
        if report_type == "trace":
            save_traces(app_name, items, repo)
        elif report_type == "jvm":
            save_jvm_metrics(app_name, items, repo)
        elif report_type == "slowSql":
            save_slow_sqls(app_name, items, repo)
        elif report_type == "slowCommand":
            save_slow_commands(app_name, items, repo)
        elif report_type == "hotspot":
            save_hotspots(app_name, items, repo)
        else:
            log.warning("agent report ignored: unknown type=%s, app=%s",
                        report_type, app_name)
    except Exception:  # noqa: BLE001 单批失败不阻断后续上报（对齐 Java catch log）
        log.exception("agent report handle failed, type=%s, app=%s",
                      report_type, app_name)


def register_instance(app_name: str, remote_host: str, body: dict,
                      repo: CollectorRepository) -> None:
    """应用注册 upsert 心跳（appPort/appVersion 可选，未携带时保留库中已有值）。"""
    try:
        port = _int_val(body.get("appPort"))
        version = body.get("appVersion")
        repo.upsert_app_instance(
            app_name=app_name,
            app_host=remote_host or "",
            app_port=port if port > 0 else None,
            app_version="" if version is None else str(version),
            agent_version=AGENT_VERSION,
            last_report_time=datetime.now(),
        )
    except Exception:  # noqa: BLE001 心跳失败不阻断数据落库
        log.exception("register app instance failed, app=%s", app_name)


def save_traces(app_name: str, items: list, repo: CollectorRepository) -> None:
    rows: list[TraceChainModel] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            tree = item.get("tree")
            rows.append(TraceChainModel(
                app_name=app_name,
                trace_id=_str_val(item.get("traceId")),
                root_method=_str_val(item.get("rootMethod")),
                total_ms=_double_val(item.get("totalMs")),
                start_time=to_local_datetime(_long_val(item.get("startTime"))),
                trace_json="{}" if tree is None else compact_json(tree),
            ))
        except Exception:  # noqa: BLE001 单条解析失败不影响整批
            log.warning("parse trace failed, app=%s", app_name, exc_info=True)
    if rows:
        repo.save_traces(rows)
    log.info("agent report: saved %d traces, app=%s", len(rows), app_name)


def save_jvm_metrics(app_name: str, items: list, repo: CollectorRepository) -> None:
    rows: list[JvmMetricModel] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            rows.append(JvmMetricModel(
                app_name=app_name,
                collect_time=to_local_datetime(_long_val(item.get("collectTime"))),
                heap_used_mb=_double_val(item.get("heapUsedMb")),
                heap_max_mb=_double_val(item.get("heapMaxMb")),
                non_heap_used_mb=_double_val(item.get("nonHeapUsedMb")),
                young_gc_count=_long_val(item.get("youngGcCount")),
                young_gc_ms=_long_val(item.get("youngGcMs")),
                full_gc_count=_long_val(item.get("fullGcCount")),
                full_gc_ms=_long_val(item.get("fullGcMs")),
                thread_count=_int_val(item.get("threadCount")),
                cpu_usage=_double_val(item.get("cpuUsage")),
                system_cpu=_double_val(item.get("systemCpu")),
            ))
        except Exception:  # noqa: BLE001
            log.warning("parse jvm metric failed, app=%s", app_name, exc_info=True)
    if rows:
        repo.save_jvm_metrics(rows)
    log.info("agent report: saved %d jvm metrics, app=%s", len(rows), app_name)


def save_slow_sqls(app_name: str, items: list, repo: CollectorRepository) -> None:
    rows: list[SlowSqlModel] = []
    auto_regged: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            source = _str_val(item.get("source"))
            db_host = _str_val(item.get("dbHost"))
            chain = item.get("methodChain")
            rows.append(SlowSqlModel(
                app_name=app_name,
                sql_text=_str_val(item.get("sqlText")),
                cost_ms=_double_val(item.get("costMs")),
                source="jdbc" if source is None else source,
                db_host="" if db_host is None else db_host,
                method_chain=None if chain is None else compact_json(chain),
                execute_time=to_local_datetime(_long_val(item.get("executeTime"))),
            ))
            # agent 上报了库地址且该应用尚未关联连接 → 自动注册（每批同地址仅一次）
            if db_host and db_host.strip() and db_host not in auto_regged:
                auto_regged.add(db_host)
                ensure_auto_db_conn(app_name, db_host, repo)
        except Exception:  # noqa: BLE001
            log.warning("parse slow sql failed, app=%s", app_name, exc_info=True)
    repo.save_slow_sqls(rows)
    log.info("agent report: saved %d slow sqls, app=%s", len(rows), app_name)


def save_slow_commands(app_name: str, items: list, repo: CollectorRepository) -> None:
    rows: list[SlowCommandModel] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            cmd_type = _str_val(item.get("type"))
            if cmd_type is None or not cmd_type.strip():
                continue
            chain = item.get("methodChain")
            rows.append(SlowCommandModel(
                app_name=app_name,
                type=cmd_type.lower(),
                command=truncate_command(_str_val(item.get("command"))),
                cost_ms=_double_val(item.get("costMs")),
                trace_id=_str_val(item.get("traceId")),
                method_chain=None if chain is None else compact_json(chain),
                execute_time=to_local_datetime(_long_val(item.get("executeTime"))),
            ))
        except Exception:  # noqa: BLE001
            log.warning("parse slow command failed, app=%s", app_name, exc_info=True)
    repo.save_slow_commands(rows)
    log.info("agent report: saved %d slow commands, app=%s", len(rows), app_name)


def truncate_command(command: str | None) -> str:
    """命令文本落库截断（command 列 VARCHAR(1024)，超长直接报错故截断加 ...）。"""
    if command is None:
        return ""
    return command[:MAX_COMMAND_LEN] + "..." if len(command) > MAX_COMMAND_LEN else command


def ensure_auto_db_conn(app_name: str, db_host: str, repo: CollectorRepository) -> None:
    """自动注册数据库连接：该应用尚未关联任何连接（appName 绑定或 host 匹配）时插入。"""
    if not db_host or not db_host.strip():
        return
    try:
        for conn in repo.find_conns_for(app_name, db_host):
            app_bound = conn.app_name == app_name
            host_match = bool(conn.host) and (db_host in conn.host or conn.host in db_host)
            if app_bound or host_match:
                return  # 已有关联，不重复自动插入
        repo.insert_db_conn(DbConnModel(
            conn_name="自动关联-" + app_name,
            host=db_host,
            app_name=app_name,
            db_url="jdbc:mysql://" + db_host
                   + "?useUnicode=true&characterEncoding=utf8"
                     "&serverTimezone=Asia/Shanghai&rewriteBatchedStatements=true",
            username="",  # agent 不携带凭据，由用户后补
            password="",
            is_default=0,
        ))
        log.info("agent report: 自动注册数据库连接, app=%s, host=%s", app_name, db_host)
    except Exception:  # noqa: BLE001
        log.warning("auto register db conn failed, app=%s, host=%s",
                    app_name, db_host, exc_info=True)


def save_hotspots(app_name: str, items: list, repo: CollectorRepository) -> None:
    """CPU 热点：元素为单批聚合 {sampleTime, items:[{method, samples}]}。"""
    rows: list[HotspotMethodModel] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            sample_time = to_local_datetime(_long_val(item.get("sampleTime")))
            methods = item.get("items")
            if not isinstance(methods, list) or not methods:
                continue
            for mo in methods:
                if not isinstance(mo, dict):
                    continue
                method_name = _str_val(mo.get("method"))
                samples = _long_val(mo.get("samples"))
                if method_name is not None and samples > 0:
                    rows.append(HotspotMethodModel(
                        app_name=app_name, method_name=method_name,
                        samples=samples, sample_time=sample_time))
        except Exception:  # noqa: BLE001
            log.warning("parse hotspot failed, app=%s", app_name, exc_info=True)
    if rows:
        repo.save_hotspots(rows)
    log.info("agent report: saved %d hotspot methods, app=%s", len(rows), app_name)
