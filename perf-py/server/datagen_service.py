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

B7 接管切换（T7.7，续批次 10）：``_build_engine`` 经
``perf_cli.datagen_runner.build_default_engine`` 路由到 PTDE 背书引擎
（端点/请求/响应 wire 形状不变；语义提示不再由灌数任务产生——PTDE 通道
为 Schema 驱动，建议面板沿用存量提示）；``engine=`` 注入缝保留（契约
测试以可编程桩注入，不受路由影响）。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
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
# QW2 清理档位（落地方案 §6.1 / 总方案 §12.5）
CLEANUP_MODE_REBUILD = "rebuild"
CLEANUP_MODE_TASK_ROWS = "task_rows"
CLEANUP_MODES = (CLEANUP_MODE_REBUILD, CLEANUP_MODE_TASK_ROWS)
# 总方案 §13.1：URL 含生产标识的库一律拒绝清理
PROD_URL_MARKERS = ("prod", "production")


def _nvl(s: str | None) -> str:
    """对齐 Java nvl：null → ""，统一小写（幂等键归一）。"""
    return (s or "").lower()


def validate_db_url(db_url: str | None) -> None:
    """对齐 validateDbUrl：仅允许已注册方言库种。"""
    from ptde.adapters.direct.dialects.mysql import MysqlDialect
    from ptde.adapters.direct.dialects.postgres import PostgresDialect

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
    from ptde.adapters.direct.dialects.mysql import MysqlDialect
    from ptde.adapters.direct.dialects.postgres import PostgresDialect

    for dialect in (MysqlDialect(), PostgresDialect()):
        if dialect.supports(db_url):
            url = dialect.enrich_jdbc_url(db_url) if isinstance(dialect, MysqlDialect) else db_url
            try:
                conn = dialect.connect(url, db_user or "", db_password or "")
            except Exception as exc:
                raise _ConnectError(str(exc)) from exc
            return conn, dialect
    raise ValueError("JDBC URL 仅支持 jdbc:mysql:// 或 jdbc:postgresql:// 前缀")


def _max_pk(conn, dialect, table: str, pk_column: str) -> int | None:
    """表主键当前最大值（空表返回 None）。"""
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT MAX({dialect.quote_identifier(pk_column)}) "
                    f"FROM {dialect.quote_identifier(table)}")
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None
    finally:
        cur.close()


def _scalar_count(conn, dialect, table: str) -> int | None:
    """单表当前行数（空表/异常返回 None，调用方决定是否兜底）。"""
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) FROM {dialect.quote_identifier(table)}")
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None
    finally:
        cur.close()


def _int_pk_watermark(conn, dialect, table: str | None) -> tuple[str, int] | None:
    """单自增整型主键表返回 (主键列, 当前 MAX(pk))；否则 None（无法标记所有权）。"""
    if not table:
        return None
    from ptde.generator.primitive import base_type

    meta = dialect.fetch_metadata(conn, table)
    if not meta.columns or len(meta.primary_keys) != 1:
        return None
    pk = next(iter(meta.primary_keys))
    col = meta.find_column(pk)
    if col is None or base_type(col.type_name or "") not in ("INT", "BIGINT"):
        return None
    max_val = _max_pk(conn, dialect, table, pk)
    return pk, max_val if max_val is not None else 0


def _table_count(conn, dialect, table: str) -> int:
    """表当前行数（审计口径：rebuild 清理前行数合计）。"""
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) FROM {dialect.quote_identifier(table)}")
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        cur.close()


def _load_source_files(repo, files_index: list, limit: int = GEN_SCAN_LIMIT) -> list:
    """对齐 SourceFileProvider.loadFiles：磁盘读内容，仅 .java/.xml，
    空内容与「// 文件过大」占位跳过，单文件失败跳过；仓库/路径不可读返回空。"""
    from ptde.models.wire import SourceFile

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
        """对齐 DATAGEN_EXECUTOR 提交体：pending→running→finished/failed。

        QW2：非 dryRun 成功后按灌数前后主键水位落 data_gen_task_scope
        （所有权范围，档位 2 按任务清理依据）；失败仅日志，不影响任务状态。
        """
        import time

        start = time.monotonic()
        request = parse_datagen_request(payload)
        try:
            self._repo.update_task_status(task_id, "running")
            watermarks = self._capture_watermarks(request)
            row_baselines = self._capture_row_baselines(request)
            result = self._run_engine(request, task_id, row_baselines,
                                      watermarks)
            cost_ms = int((time.monotonic() - start) * 1000)
            if result["success"]:
                if not request.get("dryRun"):
                    self._record_scope(task_id, request, watermarks)
                # QW3：验证失败显式标注任务状态（数据已灌入，不伪装成功/失败）
                status = ("finished_verify_failed"
                          if result.get("verifyPassed") is False else "finished")
                self._repo.update_task_finished(task_id, status,
                                                int(result["insertedRows"]), cost_ms, None)
                log.info("datagen finished, taskId=%s, dryRun=%s, rows=%s, verify=%s",
                         task_id, request.get("dryRun"), result["insertedRows"],
                         result.get("verifyPassed"))
            else:
                self._repo.update_task_finished(task_id, "failed", 0, cost_ms,
                                                result.get("errorMsg"))
        except Exception as exc:  # noqa: BLE001 — 对齐 updateFinished(failed) 兜底
            cost_ms = int((time.monotonic() - start) * 1000)
            log.error("datagen failed, taskId=%s", task_id, exc_info=exc)
            self._repo.update_task_finished(task_id, "failed", 0, cost_ms, str(exc))

    # ------------------------------------------------------------ QW2 所有权范围

    @staticmethod
    def _task_tables(request: dict) -> list[str]:
        """任务灌数范围表清单（表组形态或旧单表形态）。"""
        tables = request.get("tables")
        if tables:
            return [t.get("tableName") for t in tables if t.get("tableName")]
        name = request.get("tableName")
        return [name] if name else []

    def _capture_watermarks(self, request: dict) -> dict[str, tuple[str, int]]:
        """灌数前逐表捕获自增主键水位（表名 → (主键列, 灌数前 MAX(pk))）。

        尽力而为：无单自增整型主键的表不标记（档位 2 清理时将拒绝）；
        任何失败仅日志告警，不阻断灌数任务。
        """
        out: dict[str, tuple[str, int]] = {}
        db_url = request.get("dbUrl")
        if request.get("dryRun") or not db_url:
            return out
        try:
            conn, dialect = _connect(db_url, request.get("dbUser"),
                                     request.get("dbPassword"))
        except Exception as exc:  # noqa: BLE001 — 尽力而为
            log.warning("capture watermarks failed: %s", exc)
            return out
        try:
            for table in self._task_tables(request):
                info = _int_pk_watermark(conn, dialect, table)
                if info is not None:
                    out[table.lower()] = info
        except Exception as exc:  # noqa: BLE001 — 尽力而为
            log.warning("capture watermarks failed: %s", exc)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        return out

    def _record_scope(self, task_id: int, request: dict,
                      watermarks: dict[str, tuple[str, int]]) -> None:
        """灌数成功后逐表复测 MAX(pk)，落所有权范围 (from, to]（幂等替换）。"""
        if not watermarks:
            return
        db_url = request.get("dbUrl")
        rows = []
        try:
            conn, dialect = _connect(db_url, request.get("dbUser"),
                                     request.get("dbPassword"))
        except Exception as exc:  # noqa: BLE001 — 尽力而为
            log.warning("record task scope failed: %s", exc)
            return
        try:
            for table, (pk_col, old_max) in watermarks.items():
                new_max = _max_pk(conn, dialect, table, pk_col)
                if new_max is None or new_max <= old_max:
                    continue
                rows.append({"table_name": table, "pk_column": pk_col,
                             "watermark_from": old_max, "watermark_to": new_max,
                             "row_count": int(new_max - old_max)})
        except Exception as exc:  # noqa: BLE001 — 尽力而为
            log.warning("record task scope failed: %s", exc)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if rows:
            try:
                self._repo.replace_scope(task_id, rows)
            except Exception as exc:  # noqa: BLE001 — 尽力而为
                log.warning("persist task scope failed: %s", exc)

    def _run_engine(self, request: dict, task_id: int,
                    row_baselines: dict[str, int] | None = None,
                    watermarks: dict[str, tuple[str, int]] | None = None) -> dict:
        """对齐 runEngine：spec 装配 → listener → 环依赖降级重试 → detailJson。

        row_baselines 为灌数前逐表行数基线；watermarks 为灌数前逐表主键水位。
        验证钩子据此按「本次新增行数」核对，避免目标库已有存量数据时把
        总行数/历史脏数据误判为本次灌数结果。
        """
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
            from ptde.planner.workload import (
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
            # QW3：生成后验证最小集（报告落 detailJson；失败显式标注，不伪装成功）
            verify = self._verify_after_gen(request, row_baselines or {},
                                            watermarks or {})
            if verify is not None:
                detail["verifyMin"] = verify

        self._repo.update_detail(task_id, json.dumps(detail, ensure_ascii=False),
                                 int(result.total_rows))
        verify = detail.get("verifyMin")
        return {"success": result.success, "insertedRows": result.inserted_rows,
                "errorMsg": result.error_msg,
                "verifyPassed": verify["passed"] if verify else None}

    def _verify_after_gen(self, request: dict,
                          row_baselines: dict[str, int] | None = None,
                          watermarks: dict[str, tuple[str, int]] | None = None) -> dict | None:
        """灌数成功后的最小验证（QW3）：行数核对 + 外键孤儿抽检 + 枚举合法率。

        行数核对优先按增量口径：``灌数后总行数 - 灌数前基线`` 对比本次期望行数；
        枚举合法率提供主键水位时只核对本次新增行。未捕获到基线/水位时退回总行数/
        全表口径。连接失败/执行异常仅日志（目标库此刻不可达等），不影响任务结果；
        dryRun 由调用方保证不进入本路径。
        """
        from ptde.validator.verify_min import verify_min

        tables = []
        for t, row_count in self._expected_rows(request):
            info = (watermarks or {}).get(t.lower())
            tables.append({
                "table": t,
                "expected": row_count,
                "baseline": (row_baselines or {}).get(t.lower()),
                "pk_watermark": ({"column": info[0], "max": int(info[1])}
                                 if info else None),
            })
        if not tables:
            return None
        try:
            conn, dialect = _connect(request.get("dbUrl"), request.get("dbUser"),
                                     request.get("dbPassword"))
        except Exception as exc:  # noqa: BLE001 — 验证尽力而为
            log.warning("verify after gen skipped (connect failed): %s", exc)
            return None
        try:
            return verify_min(conn, dialect, tables)
        except Exception as exc:  # noqa: BLE001 — 验证尽力而为
            log.warning("verify after gen failed: %s", exc)
            return None
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _capture_row_baselines(self, request: dict) -> dict[str, int]:
        """灌数前逐表捕获行数基线（表名小写 → COUNT(*)）。

        用于灌后按「本次新增行数」校验；连接失败/表不可读仅日志，
        缺基线的表退回总行数核对。
        """
        out: dict[str, int] = {}
        db_url = request.get("dbUrl")
        if request.get("dryRun") or not db_url:
            return out
        try:
            conn, dialect = _connect(db_url, request.get("dbUser"),
                                     request.get("dbPassword"))
        except Exception as exc:  # noqa: BLE001 — 尽力而为
            log.warning("capture row baselines failed: %s", exc)
            return out
        try:
            for table in self._task_tables(request):
                if not table:
                    continue
                try:
                    cnt = _scalar_count(conn, dialect, table)
                except Exception as exc:  # noqa: BLE001 — 单表失败不影响其他表
                    log.warning("capture row baseline failed, table=%s: %s", table, exc)
                    continue
                if cnt is not None:
                    out[table.lower()] = int(cnt)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        return out

    @staticmethod
    def _expected_rows(request: dict) -> list[tuple[str, int]]:
        """验证目标行数（与任务灌数范围一致：表组形态或旧单表形态）。"""
        tables = request.get("tables")
        if tables:
            return [(t.get("tableName"), int(t.get("rowCount") or 0))
                    for t in tables if t.get("tableName")]
        name = request.get("tableName")
        return [(name, int(request.get("rowCount") or 0))] if name else []

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
        items, non_empty = self._probe_get_endpoints(endpoints, app)
        report = {"endpoints": items,
                  "nonEmptyRate": non_empty / len(items) if items else 0}
        if non_empty == 0:
            report["warning"] = "数据未命中业务谓词或服务不可达，压测结果可能失真"
        return report

    def _probe_get_endpoints(self, endpoints: list[dict], app) -> tuple[list[dict], int]:
        """GET 端点实调（selfCheck 尽力而为：单端点失败计未命中，不抛异常）。"""
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
        return items, non_empty

    def self_check_probe(self, repo_id) -> Callable[[], dict[str, int]]:
        """selfCheck 端点抽检能力 → ``endpoint_probe`` 适配（B6 §7F 接线缝）。

        产出 ``validate_db_ready(endpoint_probe=...)`` 协议回调
        ``() -> {"checked": n, "nonEmpty": m}``（注入 OfflinePipeline/
        DatasetLifecycleService 的 db_ready 验证）。语义与 legacy selfCheck
        对齐：仓库未绑定应用/无实例信息/无 GET 端点 → checked=0（验证层
        按「无端点可查」WARN）；抽检从不阻断（单端点失败计未命中）。
        """
        def probe() -> dict[str, int]:
            repo = self._repo.find_repo(repo_id) if repo_id else None
            if repo is None or not (repo.app_name or "").strip():
                return {"checked": 0, "nonEmpty": 0}
            app = self._repo.find_app_instance(repo.app_name)
            if app is None or not app.app_host or app.app_port is None:
                return {"checked": 0, "nonEmpty": 0}
            endpoints = self._scan_get_endpoints(repo_id)
            items, non_empty = self._probe_get_endpoints(endpoints, app)
            return {"checked": len(items), "nonEmpty": non_empty}

        return probe

    def _scan_get_endpoints(self, repo_id) -> list[dict]:
        """GET 端点抽取（EndpointScanner.scan 的 Python 等价：Controller AST 解析）。"""
        try:
            from perf_cli.datagen_runner import MAX_SOURCE_FILES
            from ptde.analyzer.lang_java.endpoint_index_parser import EndpointIndexParser

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
                tables = [t for t in (r[0] for r in cur.fetchall())
                          if not str(t).lower().startswith("ptde_")]
                # ptde_ 前缀为 PTDE 引擎内部命名空间（检查点表等自管理），
                # 不作为灌数目标暴露——前端表清单不应出现 ptde_shard_checkpoint
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
            from ptde_common.enum_value_discoverer import discover, suspect_enum_column

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

    # ------------------------------------------------------------ QW2 按任务清理

    def cleanup_task(self, task_id: int, mode: str = CLEANUP_MODE_TASK_ROWS,
                     confirm: bool = False, exclusive: bool = False,
                     body: dict | None = None) -> dict:
        """按任务清理（QW2 最小版，非契约内新端点；总方案 §12.5 清理策略）。

        档位 rebuild：整库/Schema 重建（逐表 TRUNCATE，仅限独占目标 exclusive=true
        门禁确认）；档位 task_rows：按所有权范围 (watermark_from, watermark_to]
        标记删除。无所有权标记的共享表拒绝并报告；拒绝/失败/成功均落审计。
        """
        task = self._repo.task_by_id(task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        mode = (mode or "").strip().lower()
        if mode not in CLEANUP_MODES:
            raise ValueError("清理档位仅支持 rebuild（整库/Schema 重建）"
                             "或 task_rows（按任务标记删除）")
        if task.status != "finished":
            raise ValueError(f"任务未完成，无法清理（当前状态: {task.status}）")
        db_url = task.db_url or ""
        if any(m in db_url.lower() for m in PROD_URL_MARKERS):
            self._audit_cleanup(task_id, mode, db_url, None, 0, "rejected",
                                "目标库 URL 含生产标识，禁止清理")
            raise ValueError("目标库 URL 含生产标识，禁止清理")
        if not confirm:
            self._audit_cleanup(task_id, mode, db_url, None, 0, "rejected",
                                "破坏性操作需显式确认（confirm=true）")
            raise ValueError("破坏性操作需显式确认（confirm=true）")
        if mode == CLEANUP_MODE_REBUILD and not exclusive:
            self._audit_cleanup(task_id, mode, db_url, None, 0, "rejected",
                                "整库重建仅限独占目标，需显式确认独占（exclusive=true）")
            raise ValueError("整库重建仅限独占目标，需显式确认独占（exclusive=true）")
        tables = self._task_scope_tables(task)
        if not tables:
            raise ValueError("任务灌数范围为空，无可清理目标")

        scope_by_table: dict = {}
        if mode == CLEANUP_MODE_TASK_ROWS:
            for r in self._repo.scope_for_task(task_id):
                scope_by_table[r.table_name.lower()] = r
            missing = [t for t in tables if t.lower() not in scope_by_table]
            if missing:
                msg = "以下表无所有权标记，拒绝清理: " + ", ".join(missing)
                self._audit_cleanup(task_id, mode, db_url, tables, 0, "rejected", msg)
                raise ValueError(msg)

        creds = self._resolve_cleanup_creds(body)
        try:
            conn, dialect = _connect(db_url, creds.get("dbUser"), creds.get("dbPassword"))
        except Exception as exc:
            self._audit_cleanup(task_id, mode, db_url, tables, 0, "failed",
                                f"连接目标库失败: {exc}")
            raise ValueError(f"连接目标库失败: {exc}") from exc

        results: list[dict] = []
        deleted = 0
        try:
            cur = conn.cursor()
            try:
                if mode == CLEANUP_MODE_REBUILD:
                    for t in tables:
                        count = _table_count(conn, dialect, t)
                        cur.execute("TRUNCATE TABLE " + dialect.quote_identifier(t))
                        results.append({"table": t, "action": "truncate", "rows": count})
                        deleted += count
                else:
                    for t in tables:
                        r = scope_by_table[t.lower()]
                        ph = getattr(dialect, "param", "?")
                        q = dialect.quote_identifier
                        cur.execute(
                            f"DELETE FROM {q(r.table_name)} WHERE {q(r.pk_column)} > {ph} "
                            f"AND {q(r.pk_column)} <= {ph}",
                            (r.watermark_from, r.watermark_to))
                        n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                        results.append({"table": r.table_name, "action": "delete_range",
                                        "rows": n, "from": r.watermark_from,
                                        "to": r.watermark_to})
                        deleted += n
                conn.commit()
            finally:
                cur.close()
        except ValueError:
            self._safe_rollback(conn)
            self._audit_cleanup(task_id, mode, db_url, tables, deleted, "failed",
                                "清理执行失败（范围校验）")
            raise
        except Exception as exc:  # noqa: BLE001 — 失败留审计后包装上抛
            self._safe_rollback(conn)
            self._audit_cleanup(task_id, mode, db_url, tables, deleted, "failed",
                                f"清理执行失败: {exc}")
            raise ValueError(f"清理执行失败: {exc}") from exc
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

        audit_id = self._audit_cleanup(task_id, mode, db_url, tables, deleted,
                                       "success", f"清理完成（{mode}，{len(results)} 张表）")
        return {"taskId": task_id, "mode": mode, "deletedRows": deleted,
                "tables": results, "auditId": audit_id}

    def _audit_cleanup(self, task_id: int, mode: str, db_url: str,
                       tables: list[str] | None, deleted_rows: int,
                       status: str, message: str) -> int:
        """清理审计（拒绝/失败/成功均留痕；审计失败仅日志，不掩盖原错误）。"""
        try:
            return self._repo.insert_cleanup_audit(
                task_id=task_id, mode=mode, db_url=db_url,
                tables_json=json.dumps(tables, ensure_ascii=False) if tables else None,
                deleted_rows=deleted_rows, status=status, message=message)
        except Exception as exc:  # noqa: BLE001
            log.warning("persist cleanup audit failed: %s", exc)
            return 0

    def _task_scope_tables(self, task) -> list[str]:
        """任务灌数范围表清单（tablesJson 表组形态优先，回退单表 table_name）。"""
        if task.tables_json:
            try:
                rows = json.loads(task.tables_json)
                names = [t.get("tableName") for t in rows
                         if isinstance(t, dict) and t.get("tableName")]
                if names:
                    return names
            except (TypeError, ValueError):
                pass
        return [task.table_name] if task.table_name else []

    def _resolve_cleanup_creds(self, body: dict | None) -> dict:
        """清理凭据：body 手填 dbUser/dbPassword，或 connId 命中时覆盖（对齐 applyConn）。"""
        body = body or {}
        creds = {"dbUser": body.get("dbUser"), "dbPassword": body.get("dbPassword")}
        conn_id = self._parse_conn_id(body.get("connId"))
        if conn_id is not None:
            conn = self._repo.find_db_conn(conn_id)
            if conn is not None:
                creds = {"dbUser": conn.username,
                         "dbPassword": crypto.decrypt(conn.password)}
        return creds

    @staticmethod
    def _safe_rollback(conn) -> None:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass


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
