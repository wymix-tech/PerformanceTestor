"""datagen 写路径服务（阶段 5 · 步骤 7）：灌数任务创建/执行 + 表列探测 + 建议采纳。

对齐 Java DataGenService / DataGenController：
- createAndRun：validate（方言白名单/表名正则/行数上限）→ 建任务（pending）→
  固定 2 线程池异步执行 → pending→running→finished/failed；
- runEngine：spec 装配（复用 perf_cli.datagen_runner.spec_from_request）→
  listener（进度落库 / 语义提示 upsert / 告警收集）→ detailJson
  （tableStats + warnings + dryRunReport / selfCheck）；
- listTables/listColumns：方言直连 + 枚举发现（内联 ENUM/注释）→ ColumnInfo；
- adopt：建议采纳/忽略（adopted/dismissed）。

有意差异（步骤 8 已收口）：
- 环依赖自动降级（PlanIntegrityChecker.breakWeakestCycleEdge）已接入 _run_engine；
- selfCheck 的端点扫描复用 endpoint_index_parser（Controller AST），无扫描产物时跳过。
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from server import crypto
from server.datagen_repo import DatagenRepository
from server.datagen_request import parse_datagen_request

log = logging.getLogger(__name__)

MAX_ROWS = 1_000_000
TABLE_NAME_REGEX = re.compile(r"^[A-Za-z0-9_]+$")
SELF_CHECK_ENDPOINTS = 3
# SourceFileProvider.GEN_SCAN_LIMIT：灌数扫描的文件上限
GEN_SCAN_LIMIT = 200


def _nvl(s: str | None) -> str:
    """对齐 Java nvl：null → ""，统一小写（幂等键归一）。"""
    return (s or "").lower()


def validate_db_url(db_url: str | None) -> None:
    """对齐 validateDbUrl：仅允许已注册方言库种。"""
    from datagen.dialects.mysql import MysqlDialect
    from datagen.dialects.postgres import PostgresDialect

    if not db_url or not db_url.strip() or not (
            MysqlDialect().supports(db_url) or PostgresDialect().supports(db_url)):
        raise ValueError("JDBC URL 仅支持 jdbc:mysql:// 或 jdbc:postgresql:// 前缀")


def validate_table_name(table_name: str | None) -> None:
    if not table_name or not TABLE_NAME_REGEX.match(table_name):
        raise ValueError("表名仅允许字母数字下划线")


class _ConnectError(Exception):
    """建连失败（错误文案由调用方按入口语义包装）。"""


def _connect(db_url: str, db_user: str | None, db_password: str | None):
    """按方言直连（对齐 DataGenService.connect + enrichUrl）。"""
    from datagen.dialects.mysql import MysqlDialect
    from datagen.dialects.postgres import PostgresDialect

    for dialect in (MysqlDialect(), PostgresDialect()):
        if dialect.supports(db_url):
            url = dialect.enrich_jdbc_url(db_url) if isinstance(dialect, MysqlDialect) else db_url
            try:
                conn = dialect.connect(url, db_user or "", db_password or "")
            except Exception as exc:
                raise _ConnectError(str(exc)) from exc
            return conn, dialect
    raise ValueError("JDBC URL 仅支持 jdbc:mysql:// 或 jdbc:postgresql:// 前缀")


def _load_source_files(repo, files_index: list, limit: int = GEN_SCAN_LIMIT) -> list:
    """对齐 SourceFileProvider.loadFiles：磁盘读内容，仅 .java/.xml，
    空内容与「// 文件过大」占位跳过，单文件失败跳过；仓库/路径不可读返回空。"""
    from datagen.model import SourceFile

    if repo is None or not repo.source_path or not repo.source_path.strip():
        return []
    root = Path(repo.source_path)
    if not root.is_dir():
        return []
    out = []
    for sf in files_index[:limit]:
        name = (sf.file_name or "").lower()
        if not name.endswith(".java") and not name.endswith(".xml"):
            continue
        try:
            content = (root / sf.relative_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if content and content.strip() and not content.startswith("// 文件过大"):
            out.append(SourceFile(path=sf.relative_path, content=content))
    return out


class DatagenWriteService:
    """灌数写路径门面（对齐 DataGenService 写方法 + Controller connId 覆盖）。"""

    def __init__(self, repo: DatagenRepository, engine=None,
                 executor: ThreadPoolExecutor | None = None):
        self._repo = repo
        self._engine = engine
        # executor.submit(fn, *args) 契约：生产固定 2 线程池（对齐 DATAGEN_EXECUTOR）；
        # 测试可注入同步执行器（任务随请求同步完成，无跨线程时序问题）。
        self._executor = executor or ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="datagen-runner")

    # ------------------------------------------------------------ connId 覆盖

    def apply_conn(self, body: dict) -> None:
        """对齐 Controller.applyConn：connId 空白/非数字/不存在时原样保留手填值。"""
        conn_id = self._parse_conn_id(body.get("connId"))
        if conn_id is None:
            return
        conn = self._repo.find_db_conn(conn_id)
        if conn is not None:
            body["dbUrl"] = conn.db_url
            body["dbUser"] = conn.username
            body["dbPassword"] = crypto.decrypt(conn.password)

    @staticmethod
    def _parse_conn_id(value) -> int | None:
        """Long.parseLong 语义：空白/非数字返回 None（不因辅助参数报错）。"""
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None

    # ------------------------------------------------------------ POST /tasks

    def create_and_run(self, payload: dict) -> dict:
        """对齐 createAndRun：校验 → 建任务 → 异步执行 → 返回 wire 形态。

        返回 dict 与 DataGenTask 实体 Jackson 序列化一致；createTime 为 null
        （DB 默认值仅入库，MyBatis 插入后实体未回填）。
        """
        request = parse_datagen_request(payload)
        self._validate(request)

        multi_table = bool(request.get("tables"))
        first_table = (request["tables"][0]["tableName"] if multi_table
                       else request.get("tableName"))
        total_rows = (sum(t["rowCount"] for t in request["tables"]) if multi_table
                      else request["rowCount"])
        task_name = request.get("taskName")
        if not task_name or not task_name.strip():
            task_name = "灌数据-" + str(first_table)
        tables_json = None
        if multi_table:
            # 对齐 DataGenSpecAdapter.toTablesJson：序列化 DTO bean（仅四个字段，null 透传）
            tables_json = json.dumps([
                {"tableName": t["tableName"], "rowCount": t["rowCount"],
                 "fields": t.get("fields"), "orphanRate": t.get("orphanRate")}
                for t in request["tables"]], ensure_ascii=False)

        task_id = self._repo.insert_task(
            task_name=task_name,
            db_url=request.get("dbUrl"),
            table_name=first_table,
            row_count=0 if multi_table else request["rowCount"],
            total_rows=total_rows,
            tables_json=tables_json,
            rule_json=request.get("ruleJson"),
        )
        # tablesJson 回填进执行请求（runFromPlan 路径之外的记录一致性；引擎输入不受影响）
        exec_payload = dict(payload)
        exec_payload["taskName"] = task_name
        self._executor.submit(self._run_task, task_id, exec_payload)
        return self._task_wire(task_id, task_name, request, first_table,
                               multi_table, total_rows, tables_json)

    def _task_wire(self, task_id: int, task_name: str, request: dict, table_name,
                   multi_table: bool, total_rows: int, tables_json: str | None) -> dict:
        """对齐 DataGenTask 实体 Jackson 形态（14 字段）。"""
        return {
            "id": task_id,
            "taskName": task_name,
            "dbUrl": request.get("dbUrl"),
            "tableName": table_name,
            "rowCount": 0 if multi_table else request["rowCount"],
            "ruleJson": request.get("ruleJson"),
            "status": "pending",
            "inserted": 0,
            "totalRows": total_rows,
            "tablesJson": tables_json,
            "detailJson": None,
            "costMs": 0,
            "errorMsg": None,
            "createTime": None,
        }

    def _validate(self, request: dict) -> None:
        """对齐 validate：dbUrl 方言 → 表名白名单 → 行数上下限（表组求和钳制）。"""
        validate_db_url(request.get("dbUrl"))
        tables = request.get("tables")
        if tables:
            total = 0
            for t in tables:
                validate_table_name(t.get("tableName"))
                if t["rowCount"] < 0:
                    raise ValueError(f"行数非法: {t.get('tableName')}")
                total += t["rowCount"]
            if total < 1 or total > MAX_ROWS:
                raise ValueError(f"表组总行数必须在 1-{MAX_ROWS} 之间")
        else:
            validate_table_name(request.get("tableName"))
            if request["rowCount"] < 1 or request["rowCount"] > MAX_ROWS:
                raise ValueError(f"行数必须在 1-{MAX_ROWS} 之间")

    # ------------------------------------------------------------ 引擎执行

    def _run_task(self, task_id: int, payload: dict) -> None:
        """对齐 DATAGEN_EXECUTOR 提交体：pending→running→finished/failed。"""
        import time

        start = time.monotonic()
        request = parse_datagen_request(payload)
        try:
            self._repo.update_task_status(task_id, "running")
            result = self._run_engine(request, task_id)
            cost_ms = int((time.monotonic() - start) * 1000)
            if result["success"]:
                self._repo.update_task_finished(task_id, "finished",
                                                int(result["insertedRows"]), cost_ms, None)
                log.info("datagen finished, taskId=%s, dryRun=%s, rows=%s",
                         task_id, request.get("dryRun"), result["insertedRows"])
            else:
                self._repo.update_task_finished(task_id, "failed", 0, cost_ms,
                                                result.get("errorMsg"))
        except Exception as exc:  # noqa: BLE001 — 对齐 updateFinished(failed) 兜底
            cost_ms = int((time.monotonic() - start) * 1000)
            log.error("datagen failed, taskId=%s", task_id, exc_info=exc)
            self._repo.update_task_finished(task_id, "failed", 0, cost_ms, str(exc))

    def _run_engine(self, request: dict, task_id: int) -> dict:
        """对齐 runEngine：spec 装配 → listener → 环依赖降级重试 → detailJson。"""
        from perf_cli.datagen_runner import spec_from_request

        engine = self._engine or self._build_engine()
        repo_id = request.get("repoId")
        repo = self._repo.find_repo(repo_id) if repo_id else None
        source_files = _load_source_files(
            repo, self._repo.list_indexed_files(repo_id)) if repo_id else []

        detail: dict[str, Any] = {}
        warnings: list[str] = []

        def on_progress(total_rows: int, inserted_rows: int, current_table) -> None:
            self._repo.update_progress(task_id, int(inserted_rows))

        def on_hints(magic_hints, relation_hints) -> None:
            try:
                self._persist_hints(repo_id, magic_hints, relation_hints)
            except Exception as exc:  # noqa: BLE001 — 对齐 Java：仅日志
                log.warning("persist semantic hints failed: %s", exc)

        def on_warn(message: str) -> None:
            warnings.append(message)

        spec = spec_from_request(request, source_files or None)
        try:
            result = engine.execute(spec, _Listener(on_progress, on_hints, on_warn))
        except ValueError as exc:
            # 环依赖降级（对齐 executeWithCycleFallback）：剔除环中置信度最低的一条
            # ref 边后重试一次；重试复用同一 listener 与 dryRun。置信度必须来自
            # 规则 JSON——传空表会让「最低置信度」退化为「环中第一条边」。
            msg = str(exc)
            if "循环依赖" not in msg:
                raise
            from datagen.plan_pipeline import (
                PlanTable,
                break_weakest_cycle_edge,
                edge_confidence,
            )

            tables = [PlanTable(table=t["tableName"], row_count=t["rowCount"],
                                fields=t.get("fields"), access=None)
                      for t in (request.get("tables") or [])]
            broke = break_weakest_cycle_edge(tables, msg, edge_confidence(tables))
            if not broke:
                warnings.append("检测到循环依赖且无法自动降级: " + msg)
                raise
            warnings.append("检测到循环依赖，已剔除一条最低置信度的关联边后重试；"
                            "被剔除列的外键值将为随机值")
            request["tables"] = [{"tableName": t.table, "rowCount": t.row_count,
                                  "fields": t.fields} for t in tables]
            spec = spec_from_request(request, source_files or None)
            result = engine.execute(spec, _Listener(on_progress, on_hints, on_warn))

        if not source_files and repo_id:
            warnings.append("源码不可读，魔法值仅基于数据库元数据（ENUM/CHECK/列注释），精准度下降")

        detail["tableStats"] = [{"table": s.table, "rows": s.rows}
                                for s in result.table_stats]
        detail["warnings"] = warnings
        if result.error_msg:
            detail["errorMsg"] = result.error_msg
        if request.get("dryRun"):
            detail["dryRunReport"] = {
                "estimatedMaxTxnMs": result.estimated_max_txn_ms,
                "totalRows": result.total_rows,
                "passed": result.success,
            }
        elif result.success:
            sc = self._self_check(request, repo)
            if sc is not None:
                detail["selfCheck"] = sc

        self._repo.update_detail(task_id, json.dumps(detail, ensure_ascii=False),
                                 int(result.total_rows))
        return {"success": result.success, "insertedRows": result.inserted_rows,
                "errorMsg": result.error_msg}

    def _persist_hints(self, repo_id, magic_hints, relation_hints) -> None:
        """对齐 persistHints：value → pending；relation → naming 系 pending、
        自动采纳类 adopted；null 归一小写空串保证 uk_hint 幂等键。"""
        for mh in magic_hints or []:
            self._repo.hint_upsert(
                repo_id=repo_id, hint_type="value",
                table_name=_nvl(mh.table), column_name=_nvl(mh.column),
                values_json=json.dumps(mh.values, ensure_ascii=False),
                parent_table="", parent_column="",
                source_refs=json.dumps(mh.source_refs, ensure_ascii=False),
                source=_nvl(mh.kind), confidence=None)
        for rh in relation_hints or []:
            self._repo.hint_upsert(
                repo_id=repo_id, hint_type="relation",
                table_name=_nvl(rh.child_table), column_name=_nvl(rh.child_column),
                values_json=None,
                parent_table=_nvl(rh.parent_table), parent_column=_nvl(rh.parent_column),
                source_refs=json.dumps(rh.source_refs, ensure_ascii=False),
                source=_nvl(rh.source), confidence=rh.confidence,
                status="adopted" if rh.auto_applicable() else "pending")

    def _self_check(self, request: dict, repo) -> dict | None:
        """对齐 selfCheck：抽 GET 端点实调，非空返回率写入详情；
        仓库未绑定应用 / 无实例信息 / 无 GET 端点 → 跳过（None）。"""
        if repo is None or not (repo.app_name or "").strip():
            return None
        app = self._repo.find_app_instance(repo.app_name)
        if app is None or not app.app_host or app.app_port is None:
            return None
        endpoints = self._scan_get_endpoints(request.get("repoId"))
        if not endpoints:
            return None
        import httpx

        items = []
        non_empty = 0
        for ep in endpoints:
            path = str(ep["path"])
            url = (f"http://{app.app_host}:{app.app_port}"
                   + (path if path.startswith("/") else "/" + path))
            item: dict[str, Any] = {"url": url, "path": path}
            try:
                resp = httpx.get(url, timeout=5.0)
                non_empty_body = resp.status_code == 200 and len(resp.text or "") > 2
                item["status"] = resp.status_code
                item["nonEmpty"] = non_empty_body
                if non_empty_body:
                    non_empty += 1
            except Exception as exc:  # noqa: BLE001 — 单端点失败不阻断
                item["status"] = -1
                item["nonEmpty"] = False
                item["error"] = str(exc)
            items.append(item)
        report = {"endpoints": items,
                  "nonEmptyRate": non_empty / len(items) if items else 0}
        if non_empty == 0:
            report["warning"] = "数据未命中业务谓词或服务不可达，压测结果可能失真"
        return report

    def _scan_get_endpoints(self, repo_id) -> list[dict]:
        """GET 端点抽取（EndpointScanner.scan 的 Python 等价：Controller AST 解析）。"""
        try:
            from datagen.endpoint_index_parser import EndpointIndexParser
            from perf_cli.datagen_runner import MAX_SOURCE_FILES

            repo = self._repo.find_repo(repo_id) if repo_id else None
            if repo is None:
                return []
            files = _load_source_files(
                repo, self._repo.list_indexed_files(repo_id, MAX_SOURCE_FILES),
                MAX_SOURCE_FILES)
            refs = EndpointIndexParser().parse(files)
            return [{"httpMethod": r.http_method, "path": r.path}
                    for r in refs
                    if (r.http_method or "").upper() == "GET" and r.path][:SELF_CHECK_ENDPOINTS]
        except Exception as exc:  # noqa: BLE001 — 自检尽力而为
            log.warning("datagen self-check endpoint scan failed: %s", exc)
            return []

    @staticmethod
    def _build_engine():
        from perf_cli.datagen_runner import build_default_engine

        return build_default_engine()

    # ------------------------------------------------------------ 表/列探测

    def list_tables(self, db_url: str | None, db_user: str | None,
                    db_password: str | None) -> list[str]:
        """对齐 listTables：getTables(catalog, "%", ["TABLE"]) → 表名清单。"""
        validate_db_url(db_url)
        tables: list[str] = []
        conn = None
        try:
            conn, dialect = _connect(db_url, db_user, db_password)
            cur = conn.cursor()
            try:
                if hasattr(dialect, "param") and dialect.param == "%s":  # MySQL 路径
                    cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES "
                                "WHERE TABLE_SCHEMA = DATABASE() "
                                "AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME")
                else:  # PostgreSQL 路径
                    cur.execute("SELECT table_name FROM information_schema.tables "
                                "WHERE table_schema = current_schema() "
                                "AND table_type = 'BASE TABLE' ORDER BY table_name")
                tables = [r[0] for r in cur.fetchall()]
            finally:
                cur.close()
        except ValueError:
            raise
        except _ConnectError as exc:
            raise ValueError(f"连接目标库失败: {exc}") from exc
        except Exception as exc:
            raise ValueError(f"连接目标库失败: {exc}") from exc
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        return tables

    def list_columns(self, db_url: str | None, db_user: str | None,
                     db_password: str | None, table_name: str | None) -> list[dict]:
        """对齐 listColumns：getColumns+getPrimaryKeys → ColumnInfo +
        枚举发现（无 DB 重载 discover：内联 ENUM/注释；无值时 suspectEnum 标记）。"""
        validate_db_url(db_url)
        validate_table_name(table_name)
        conn = None
        try:
            from datagen.enum_value_discoverer import discover, suspect_enum_column

            conn, dialect = _connect(db_url, db_user, db_password)
            meta = dialect.fetch_metadata(conn, table_name)
            columns = []
            for cm in meta.columns:
                values = discover(cm, None, None)
                col = {
                    "name": cm.name,
                    "typeName": cm.type_name,
                    "size": cm.size,
                    "nullable": cm.nullable,
                    "autoIncrement": cm.auto_increment,
                    "primaryKey": cm.primary_key,
                    "comment": cm.comment,
                    "suggestedValues": None,
                    "suspectEnum": False,
                }
                if values:
                    col["suggestedValues"] = values
                else:
                    col["suspectEnum"] = suspect_enum_column(cm)
                columns.append(col)
            return columns
        except ValueError:
            raise
        except _ConnectError as exc:
            # Java listColumns 的 try 块包含建连：连接失败同走「读取表结构失败」包装
            raise ValueError(f"读取表结构失败: {exc}") from exc
        except Exception as exc:
            raise ValueError(f"读取表结构失败: {exc}") from exc
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ 建议采纳

    def adopt(self, hint_id: int, adopt: bool) -> None:
        """对齐 adopt：hint 不存在抛错（code=1 包装）；状态 adopted/dismissed。"""
        hint = self._repo.hint_by_id(hint_id)
        if hint is None:
            raise ValueError(f"建议不存在: {hint_id}")
        self._repo.hint_update_status(hint_id, "adopted" if adopt else "dismissed")


class _Listener:
    """EngineListener 适配（闭包 → 抽象基类）。"""

    def __init__(self, on_progress, on_hints, on_warn):
        self._on_progress, self._on_hints, self._on_warn = on_progress, on_hints, on_warn

    def on_progress(self, total_rows: int, inserted_rows: int, current_table) -> None:
        self._on_progress(total_rows, inserted_rows, current_table)

    def on_hints(self, magic_hints: list, relation_hints: list) -> None:
        self._on_hints(magic_hints, relation_hints)

    def on_warn(self, message: str) -> None:
        self._on_warn(message)
