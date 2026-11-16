"""datagen plans 写路径服务（阶段 5 · 步骤 8）：analyze / adopt / execute / delete。

对齐 Java EndpointDataPlanService（分析编排）+ DataGenService.runFromPlan（执行链）：
- analyze：校验 → 建 plan（pending）→ 分析线程池异步 doAnalyze → 专用 updateAnalysis
  （防冲掉并发状态变更）；失败 markAnalyzeFailed（failed + 原因）；
  排队满同样置 failed（Java AbortPolicy 语义）。
- doAnalyze：源码加载（ANALYSIS_LIMIT=5000）→ 接口索引 → 调用链 → SQL 解析 →
  访问事实（写优先/谓词合并/详情标记）→ FK 命名兜底 → RT 基线 → 行数推导 →
  规则合成 → 补父表。
- runFromPlan：validatePlanConnection → parseTables → 补父表 → 刷 seqStart →
  组装请求 → createAndRun（环依赖降级重试 1 次，置信度来自规则 JSON）→
  updateTaskId（dryRun 不回写，防状态失真）→ updateWarnings。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Any

from datagen.call_graph_analyzer import TRIPPED_CONFIDENCE, CallGraphAnalyzer
from datagen.endpoint_index_parser import EndpointIndexParser
from datagen.model import EndpointRef, SourceFile, SqlAccess
from datagen.mybatis_sql_resolver import MyBatisSqlResolver
from datagen.plan_pipeline import (
    MIN_MAIN_ROWS,
    EndpointTableFact,
    JoinRef,
    PlanTable,
    StressParams,
    TableContext,
    TableDemand,
    break_weakest_cycle_edge,
    complete_parents,
    compose,
    describe_rt_source,
    edge_confidence,
    estimate,
    llm_enhance,
    resolve_rt_baseline,
)
from datagen.sql_predicate_extractor import extract as extract_predicates
from datagen.sql_table_extractor import (
    extract as extract_tables,
)
from datagen.sql_table_extractor import (
    extract_columns_by_table,
    extract_join_edges,
    is_fk_column,
)
from server import crypto
from server.datagen_repo import DataGenPlanModel, DatagenRepository

log = logging.getLogger(__name__)

ANALYZE_QUEUE_CAPACITY = 16
DEFAULT_LIST_LIMIT = 50
ANALYZE_FAILED_PREFIX = "分析失败: "
# SourceFileProvider.ANALYSIS_LIMIT：按接口分析必须走 5000 而非灌数扫描的 200 上限
ANALYSIS_LIMIT = 5000
MAX_ROWS = 1_000_000


def _nvl_lower(s: str | None) -> str:
    return (s or "").lower()


def is_detail_endpoint(path: str | None) -> bool:
    """详情类接口判定：路径末段为路径变量（如 /api/order/{id}）。"""
    if not path or not path.strip():
        return False
    segs = path.split("/")
    last = segs[-1].strip() if segs else ""
    return last.startswith("{") and last.endswith("}")


# ---------------------------------------------------------------- 请求模型


@dataclass
class EndpointInput:
    """接口选择项（前端传入）。"""

    http_method: str | None = None
    path: str | None = None
    weight: int = 1
    className: str | None = None
    methodName: str | None = None

    def __post_init__(self) -> None:
        if self.weight < 1:
            self.weight = 1


@dataclass
class AnalyzeRequest:
    """分析请求（stress 为压测参数快照）。"""

    repoId: int | None = None
    planName: str | None = None
    endpoints: list[EndpointInput] = field(default_factory=list)
    stress: StressParams = field(default_factory=StressParams)
    connId: int | None = None
    dbUrl: str | None = None
    taskId: int | None = None
    llmEnhance: bool = False


def _opt_int(v: Any, default: int | None = None) -> int | None:
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def parse_analyze_request(payload: dict) -> AnalyzeRequest:
    """camelCase 请求体 → AnalyzeRequest（未知字段忽略；stress 快照补齐默认值）。"""
    stress_raw = payload.get("stress") or {}
    stages: list = []
    for s in stress_raw.get("stages") or []:
        if isinstance(s, dict):
            from datagen.plan_pipeline import Stage

            stages.append(Stage(
                concurrency=_opt_int(s.get("concurrency"), 1) or 1,
                duration_sec=_opt_int(s.get("durationSec"), 60) or 1))
    stress = StressParams(
        concurrency=max(1, _opt_int(stress_raw.get("concurrency"), 1) or 1),
        duration_sec=max(1, _opt_int(stress_raw.get("durationSec"), 60) or 1),
        qps_limit=float(stress_raw.get("qpsLimit") or 0),
        assumed_rt_ms=float(stress_raw.get("assumedRtMs") or 0) or 50.0,
        stages=stages)
    endpoints: list[EndpointInput] = []
    for e in payload.get("endpoints") or []:
        if isinstance(e, dict):
            endpoints.append(EndpointInput(
                http_method=e.get("httpMethod"), path=e.get("path"),
                weight=_opt_int(e.get("weight"), 1) or 1,
                className=e.get("className"), methodName=e.get("methodName")))
    return AnalyzeRequest(
        repoId=_opt_int(payload.get("repoId")),
        planName=payload.get("planName"),
        endpoints=endpoints,
        stress=stress,
        connId=_opt_int(payload.get("connId")),
        dbUrl=payload.get("dbUrl"),
        taskId=_opt_int(payload.get("taskId")),
        llmEnhance=bool(payload.get("llmEnhance")),
    )


# ---------------------------------------------------------------- 分析执行器


class _AnalyzerWorker:
    """分析线程池等价物：单 daemon 线程 + 有界队列（对齐 Java AbortPolicy 语义）。

    队列满 → put_nowait 抛 queue.Full，由 analyze 捕获并置 failed（排队已满文案）。
    测试可注入同步执行（run_inline=True 直接调用）。
    """

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue(maxsize=ANALYZE_QUEUE_CAPACITY)
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="datagen-plan-analyzer")
        self._thread.start()

    def _loop(self) -> None:
        while True:
            fn, args = self._queue.get()
            try:
                fn(*args)
            except Exception:  # noqa: BLE001 — worker 兜底（任务内部已捕获）
                log.exception("plan analyzer worker error")
            finally:
                self._queue.task_done()

    def submit(self, fn, *args) -> None:
        self._queue.put_nowait((fn, args))  # 队列满 → queue.Full


_ANALYZER: _AnalyzerWorker | None = None
_ANALYZER_LOCK = threading.Lock()


def _analyzer() -> _AnalyzerWorker:
    global _ANALYZER
    with _ANALYZER_LOCK:
        if _ANALYZER is None:
            _ANALYZER = _AnalyzerWorker()
        return _ANALYZER


# ---------------------------------------------------------------- 服务


class DatagenPlanService:
    """plans 写路径门面（对齐 EndpointDataPlanService + runFromPlan）。"""

    def __init__(self, repo: DatagenRepository, stress_repo, task_service=None,
                 inline_analyze: bool = False, llm=None):
        self._repo = repo
        self._stress_repo = stress_repo
        self._llm = llm
        # task_service：DatagenWriteService（execute 复用 create_and_run，含引擎桩注入）
        self._task_service = task_service
        self._inline = inline_analyze

    # ------------------------------------------------------------ 请求校验/装配

    @staticmethod
    def _validate_connection(request: AnalyzeRequest) -> None:
        """连接参数校验：不允许同时提供 connId 与 dbUrl（允许都为空，执行期才强校验）。"""
        has_conn = request.connId is not None
        has_url = bool(request.dbUrl and request.dbUrl.strip())
        if has_conn and has_url:
            raise ValueError("数据库连接配置 ID 与 JDBC URL 只能提供其一")

    @staticmethod
    def _default_plan_name(request: AnalyzeRequest) -> str:
        if request.planName and request.planName.strip():
            return request.planName
        first = request.endpoints[0]
        name = (first.http_method.upper() + " " if first.http_method else "") \
            + (first.path or "")
        return "数据方案-" + name.strip()

    # ------------------------------------------------------------ analyze

    def analyze(self, payload: dict) -> dict:
        """分析并落库（异步执行于分析线程池）；返回已落库的方案 wire 形态。"""
        request = parse_analyze_request(payload)
        if request.repoId is None:
            raise ValueError("源码仓库 ID 必填")
        self._validate_connection(request)
        if not request.endpoints:
            raise ValueError("至少选择一个压测接口")

        endpoint_json = json.dumps(
            [{"httpMethod": e.http_method, "path": e.path, "weight": e.weight,
              "className": e.className, "methodName": e.methodName}
             for e in request.endpoints], ensure_ascii=False)
        stress_json = json.dumps(
            {"concurrency": request.stress.concurrency,
             "durationSec": request.stress.duration_sec,
             "qpsLimit": request.stress.qps_limit,
             "assumedRtMs": request.stress.assumed_rt_ms,
             "stages": [{"concurrency": s.concurrency, "durationSec": s.duration_sec}
                        for s in request.stress.stages]}, ensure_ascii=False)
        plan_id = self._repo.insert_plan(
            repo_id=request.repoId,
            plan_name=self._default_plan_name(request),
            endpoint_json=endpoint_json,
            stress_json=stress_json,
            conn_id=request.connId,
            db_url=request.dbUrl,
        )
        try:
            if self._inline:
                self._analyze_task(plan_id, request)
            else:
                _analyzer().submit(self._analyze_task, plan_id, request)
        except queue.Full:
            # 排队已满：方案已落库但没有分析线程会再碰它，必须写清结论
            log.warning("plan analyze rejected, queue full, planId=%s", plan_id)
            self._mark_analyze_failed(
                plan_id, f"分析排队已满（上限 {ANALYZE_QUEUE_CAPACITY}），请稍后重试")
        return self._plan_wire(plan_id, request)

    def _plan_wire(self, plan_id: int, request: AnalyzeRequest) -> dict:
        """对齐 DataGenPlan 实体 Jackson 形态（14 字段；时间字段 DB 默认值仅入库）。"""
        return {
            "id": plan_id,
            "repoId": request.repoId,
            "planName": self._default_plan_name(request),
            "endpointJson": json.dumps(
                [{"httpMethod": e.http_method, "path": e.path, "weight": e.weight,
                  "className": e.className, "methodName": e.methodName}
                 for e in request.endpoints], ensure_ascii=False),
            "stressJson": json.dumps(
                {"concurrency": request.stress.concurrency,
                 "durationSec": request.stress.duration_sec,
                 "qpsLimit": request.stress.qps_limit,
                 "assumedRtMs": request.stress.assumed_rt_ms,
                 "stages": [{"concurrency": s.concurrency, "durationSec": s.duration_sec}
                            for s in request.stress.stages]}, ensure_ascii=False),
            "connId": request.connId,
            "dbUrl": request.dbUrl,
            "tablesJson": None,
            "warningsJson": None,
            "status": "pending",
            "taskId": None,
            "createTime": None,
            "updateTime": None,
        }

    def _mark_analyze_failed(self, plan_id: int, reason: str) -> None:
        """分析失败落库：failed + 原因（走专用 SQL 防覆盖并发状态变更）。"""
        self._repo.update_warnings(
            plan_id, json.dumps([ANALYZE_FAILED_PREFIX + reason], ensure_ascii=False))
        self._repo.update_plan_status(plan_id, "failed")

    def _analyze_task(self, plan_id: int, request: AnalyzeRequest) -> None:
        try:
            warnings: list[str] = []
            tables = self.do_analyze(request, warnings)
            self._repo.update_analysis(
                plan_id, json.dumps([{
                    "table": t.table, "rowCount": t.row_count, "fields": t.fields,
                    "access": t.access, "evidence": t.evidence} for t in tables],
                    ensure_ascii=False),
                json.dumps(warnings, ensure_ascii=False))
            log.info("plan analyzed: planId=%s, tables=%s", plan_id, len(tables))
        except Exception as exc:  # noqa: BLE001 — 对齐 markAnalyzeFailed 兜底
            log.error("plan analyze failed, planId=%s", plan_id, exc_info=exc)
            self._mark_analyze_failed(plan_id, str(exc) or exc.__class__.__name__)

    # ------------------------------------------------------------ doAnalyze

    def do_analyze(self, request: AnalyzeRequest, warnings: list[str]) -> list[PlanTable]:
        """核心分析流程（对齐 doAnalyze；同步执行，供单测直接调用）。"""
        files = self._load_sources(request.repoId, warnings)
        java_files = self._filter(files, ".java")
        xml_files = self._filter(files, ".xml")

        index = EndpointIndexParser().parse(java_files)
        if not index:
            warnings.append("源码中未解析到任何 Controller 接口，请确认导入的仓库包含 Controller 源码")

        sql_resolver = MyBatisSqlResolver(xml_files)
        sql_resolver.index_annotations(java_files)

        analyzer = CallGraphAnalyzer()
        facts_by_table: dict[str, EndpointTableFact] = {}
        demands: list[TableDemand] = []
        weights: list[int] = []

        endpoint_index = 0
        for endpoint_input in request.endpoints:
            matched = self._match(index, endpoint_input, warnings)
            if matched is None:
                warnings.append("接口未定位: " + (endpoint_input.http_method or "") + " "
                                + (endpoint_input.path or "") + "（可手工补表后执行）")
                # 未定位接口也要占位：weights 是按接口下标的紧凑列表
                weights.append(endpoint_input.weight)
                endpoint_index += 1
                continue
            graph = analyzer.analyze(java_files, matched)
            for reason in graph.unresolved:
                warnings.append("调用链未覆盖: " + reason)
            if graph.tripped:
                warnings.append("调用链分析触发熔断，涉及表可能不完整")
            weights.append(endpoint_input.weight)
            self._collect_facts(graph, sql_resolver, facts_by_table, demands,
                                warnings, graph.tripped, endpoint_index,
                                is_detail_endpoint(endpoint_input.path))
            endpoint_index += 1

        # 行数推导（RT 基线三级取值）
        params = request.stress
        repo = self._repo.find_repo(request.repoId) if request.repoId else None
        app_name = repo.app_name if repo is not None and repo.app_name else None
        baseline = resolve_rt_baseline(
            self._stress_repo, app_name, self._first_path(request), request.taskId)
        params.assumed_rt_ms = baseline.rt_ms
        warnings.append("响应时间基线取值来源: " + describe_rt_source(baseline.source)
                        + f"（{round(baseline.rt_ms * 100) / 100}ms）")

        est = estimate(params, demands, weights)
        warnings.extend(est.warnings)

        # LLM 可选增强（无 Key 静默降级）
        enhancement = llm_enhance(list(facts_by_table.values()), request.llmEnhance,
                                  self._llm)
        if not enhancement.applied and request.llmEnhance:
            warnings.append(enhancement.reason)
        warnings.extend(enhancement.warnings)

        if not facts_by_table:
            warnings.append("未从源码分析出该接口涉及的数据库表"
                            "（接口可能不访问数据库，或调用链未覆盖到 Mapper），请手动补表后执行")
        else:
            self._resolve_fk_by_naming(facts_by_table)

        # 规则合成 + 父表补齐（必须在构建 spec 之前，否则补了也不生效）
        contexts: dict[str, TableContext] = {}
        for table, fact in facts_by_table.items():
            ctx = TableContext()
            ctx.detail_lookup = fact.detail_lookup
            ctx.join_refs.extend(fact.join_refs)
            contexts[table] = ctx
        composed = compose(list(facts_by_table.values()), contexts)
        warnings.extend(composed.warnings)

        tables: list[PlanTable] = []
        for table, fact in facts_by_table.items():
            pt = PlanTable(
                table=table,
                row_count=est.rows_by_table.get(table, MIN_MAIN_ROWS),
                fields=composed.fields_by_table.get(table),
                access=fact.access.value if fact.access else "READ")
            pt.evidence.extend(fact.source_refs)
            tables.append(pt)
        checked = complete_parents(tables)
        warnings.extend(checked.warnings)
        return checked.tables

    def _load_sources(self, repo_id: int | None, warnings: list[str]) -> list[SourceFile]:
        """加载分析用源码（ANALYSIS_LIMIT=5000；截断显式告警，防误判「接口不查库」）。"""
        files = self._load_files(repo_id, ANALYSIS_LIMIT)
        if not files:
            warnings.append("源码不可读，无法按接口推导涉及表（请确认仓库已导入且磁盘路径可访问）")
            return files
        count = self._count_indexed(repo_id)
        if count > len(files):
            warnings.append(
                f"源码文件 {count} 个超过分析上限 {ANALYSIS_LIMIT} 个，仅分析了其中 {len(files)} 个，涉及表可能不完整")
        return files

    def _load_files(self, repo_id: int | None, limit: int) -> list[SourceFile]:
        repo = self._repo.find_repo(repo_id) if repo_id else None
        if repo is None or not repo.source_path or not repo.source_path.strip():
            return []
        from pathlib import Path

        root = Path(repo.source_path)
        if not root.is_dir():
            return []
        out: list[SourceFile] = []
        for sf in self._repo.list_indexed_files(repo_id, limit)[:limit]:
            try:
                content = (root / sf.relative_path).read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                continue
            if content and content.strip() and not content.startswith("// 文件过大"):
                out.append(SourceFile(path=sf.relative_path, content=content))
        return out

    def _count_indexed(self, repo_id: int | None) -> int:
        if not repo_id:
            return 0
        try:
            return len(self._repo.list_indexed_files(repo_id, 5000))
        except Exception:  # noqa: BLE001
            return 0

    @staticmethod
    def _filter(files: list[SourceFile], suffix: str) -> list[SourceFile]:
        return [f for f in files
                if f.path and f.path.lower().endswith(suffix)]

    @staticmethod
    def _first_path(request: AnalyzeRequest) -> str | None:
        return request.endpoints[0].path if request.endpoints else None

    # ------------------------------------------------------------ 接口匹配

    def _match(self, index: list[EndpointRef], endpoint_input: EndpointInput,
               warnings: list[str]) -> EndpointRef | None:
        """三级降级匹配：精确 → 仅 path → 类#方法（命中层级写入告警）。"""
        if not index or endpoint_input is None:
            return None
        want_key = ((endpoint_input.http_method or "").upper() + " "
                    + (endpoint_input.path or ""))
        for r in index:
            if r.key() == want_key:
                return r
        for r in index:
            if r.path_key() == endpoint_input.path:
                warnings.append("接口按路径匹配（HTTP 方法不一致）: " + (endpoint_input.path or ""))
                return r
        if endpoint_input.className and endpoint_input.className.strip() \
                and endpoint_input.methodName and endpoint_input.methodName.strip():
            want_member = endpoint_input.className + "#" + endpoint_input.methodName
            for r in index:
                if want_member == r.class_member_key():
                    warnings.append("接口按 类#方法 匹配（路径不一致）: " + want_member)
                    return r
        return None

    # ------------------------------------------------------------ 事实收集

    def _collect_facts(self, graph, sql_resolver: MyBatisSqlResolver,
                       facts_by_table: dict[str, EndpointTableFact],
                       demands: list[TableDemand], warnings: list[str], tripped: bool,
                       endpoint_index: int, detail: bool) -> None:
        for inv in graph.invocations:
            sql = sql_resolver.resolve_invocation(inv)
            if sql is None:
                warnings.append("Mapper 方法未定位到 SQL: " + inv.mapper_type + "#"
                                + inv.method_name + "（" + inv.call_site + "）")
                continue
            extracted = extract_tables(sql)
            if extracted.warning:
                warnings.append(inv.mapper_type + "#" + inv.method_name + "："
                                + extracted.warning)
            if not extracted.tables:
                warnings.append("SQL 未识别出表名: " + inv.mapper_type + "#" + inv.method_name)
                continue
            columns_by_table = extract_columns_by_table(sql)
            join_edges = extract_join_edges(sql)

            for ta in extracted.tables:
                fact = facts_by_table.setdefault(ta.table, EndpointTableFact(ta.table, ta.access))
                fact.source_refs.append(inv.call_site)
                if tripped:
                    fact.confidence = TRIPPED_CONFIDENCE
                # 同一张表被不同 Mapper 以不同方式访问时，写优先（写链路覆盖读链路需求）
                if ta.access == SqlAccess.WRITE:
                    fact.access = SqlAccess.WRITE
                for p in extract_predicates(sql, ta.table):
                    self._merge_predicate(fact, p)
                if extracted.page_size > fact.page_size:
                    fact.page_size = extracted.page_size
                for other in extracted.table_names():
                    if other != ta.table and other not in fact.join_tables:
                        fact.join_tables.append(other)
                # 详情类接口：该表按 id 反复查单行，生成数据应做热点倾斜
                if detail:
                    fact.detail_lookup = True
                # 累积列名：N+1 形态下外键列可能只在单条 SQL 里出现
                for col in columns_by_table.get(ta.table) or []:
                    if col not in fact.columns:
                        fact.columns.append(col)
                self._collect_join_refs(fact, ta.table, join_edges)
                demands.append(TableDemand(
                    ta.table, ta.access == SqlAccess.WRITE, extracted.page_size,
                    endpoint_index))

    @staticmethod
    def _collect_join_refs(fact: EndpointTableFact, table: str,
                           join_edges) -> None:
        """收集 JOIN ON 外键引用边（方向明确的证据，置信度 0.9/0.7）。"""
        for edge in join_edges:
            if table != edge.child_table:
                continue
            fact.join_refs.append(JoinRef(
                edge.child_column, edge.parent_table, edge.parent_column,
                edge.confidence))

    @staticmethod
    def _merge_predicate(fact: EndpointTableFact, incoming: Any) -> None:
        """谓词合并：同列同算子合并值集（多步骤 union，避免漏造导致查询不命中）。"""
        for p in fact.predicates:
            if p.column != incoming.column or p.op != incoming.op:
                continue
            for v in incoming.values:
                if v not in p.values:
                    p.values.append(v)
            if incoming.min is not None and (p.min is None or incoming.min < p.min):
                p.min = incoming.min
            if incoming.max is not None and (p.max is None or incoming.max > p.max):
                p.max = incoming.max
            return
        fact.predicates.append(incoming)

    @staticmethod
    def _resolve_fk_by_naming(facts_by_table: dict[str, EndpointTableFact]) -> None:
        """外键命名兜底：xxx_id → 同方案的 xxx 表（置信度 0.6），仅无 JOIN 证据时。"""
        for table, fact in facts_by_table.items():
            if fact.join_refs or not fact.columns:
                continue
            for other_table, _other in facts_by_table.items():
                if other_table == table:
                    continue
                for col in fact.columns:
                    if is_fk_column(col, other_table):
                        fact.join_refs.append(JoinRef(col, other_table, "id", 0.6))
                        break

    # ------------------------------------------------------------ 状态流转

    def detail(self, plan_id: int) -> DataGenPlanModel:
        plan = self._repo.plan_by_id(plan_id)
        if plan is None:
            raise ValueError(f"方案不存在: {plan_id}")
        return plan

    def adopt(self, plan_id: int, adopt: bool) -> DataGenPlanModel:
        """采纳/放弃（返回方案对象，status 为更新后值——对齐 Java adopt）。"""
        plan = self.detail(plan_id)
        self._repo.update_plan_status(plan_id, "adopted" if adopt else "discarded")
        plan.status = "adopted" if adopt else "discarded"
        return plan

    def delete(self, plan_id: int) -> None:
        self._repo.delete_plan(plan_id)

    # ------------------------------------------------------------ parseTables

    def parse_tables(self, tables_json: str | None) -> list[PlanTable]:
        """解析方案表清单 JSON（fields 兼容文本/对象节点——Java asText 语义）。"""
        out: list[PlanTable] = []
        if not tables_json or not tables_json.strip():
            return out
        try:
            arr = json.loads(tables_json)
        except (ValueError, TypeError):
            log.warning("parse plan tables failed")
            return out
        if not isinstance(arr, list):
            return out
        for n in arr:
            if not isinstance(n, dict):
                continue
            fields_node = n.get("fields")
            if fields_node is None or isinstance(fields_node, str):
                fields = fields_node
            else:
                fields = json.dumps(fields_node, ensure_ascii=False)
            t = PlanTable(
                table=n.get("table"),
                row_count=_opt_int(n.get("rowCount"), MIN_MAIN_ROWS) or MIN_MAIN_ROWS,
                fields=fields,
                access=n.get("access") or "READ",
                evidence=[e for e in (n.get("evidence") or []) if isinstance(e, str)])
            if t.table:
                out.append(t)
        return out

    # ------------------------------------------------------------ runFromPlan

    def run_from_plan(self, plan_id: int, dry_run: bool,
                      row_overrides: dict[str, int] | None) -> dict:
        """对齐 runFromPlan：读方案 → 补父表 → 刷 seqStart → 组装 → 执行 → 回写。"""
        plan = self.detail(plan_id)
        self._validate_plan_connection(plan)

        tables = self.parse_tables(plan.tables_json)
        if not tables:
            raise ValueError("方案尚未分析出表清单（可能分析失败或接口未定位）")

        checked = complete_parents(tables)
        warnings = list(checked.warnings)
        final_tables = checked.tables

        # ③ 刷新 seqStart（连接前先做方言校验语义由目标连接失败暴露）
        if plan.conn_id is not None or plan.db_url:
            self._refresh_seq_starts(plan, final_tables, warnings)

        resolved = self._resolve_plan_conn(plan)
        specs = []
        for t in final_tables:
            override = (row_overrides or {}).get((t.table or "").lower())
            # 用户改过的行数优先（同样受上下限保护）
            rows = min(override, MAX_ROWS) if override is not None and override > 0 \
                else max(1, t.row_count)
            specs.append({"tableName": t.table, "rowCount": rows, "fields": t.fields})
        exec_request = {
            "taskName": plan.plan_name,
            "connId": plan.conn_id,
            "dbUrl": resolved[0],
            "dbUser": resolved[1],
            "dbPassword": resolved[2],
            "repoId": plan.repo_id,
            "dryRun": dry_run,
            "tables": specs,
        }
        task = self._create_and_run_with_cycle_fallback(exec_request, warnings)
        # 试运行不回写：updateTaskId 固定置 executed，dryRun 回写会让状态失真
        if not dry_run and task is not None and task.get("id") is not None:
            try:
                self._repo.update_task_id(plan_id, task["id"])
            except Exception as exc:  # noqa: BLE001 — 对齐 Java：仅日志
                log.warning("update plan taskId failed, planId=%s: %s", plan_id, exc)
        if warnings:
            try:
                self._repo.update_warnings(plan_id, json.dumps(warnings, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001
                log.warning("persist plan warnings failed, planId=%s: %s", plan_id, exc)
        return task

    def _create_and_run_with_cycle_fallback(self, exec_request: dict,
                                            warnings: list[str]) -> dict:
        """执行引擎，遇环依赖时降级重试一次（置信度来自规则 JSON）。"""
        assert self._task_service is not None, "execute 需要 task_service"
        attempt = dict(exec_request)
        try:
            return self._task_service.create_and_run(attempt)
        except ValueError as exc:
            msg = str(exc)
            if "循环依赖" not in msg:
                raise
            tables = [PlanTable(table=t["tableName"], row_count=t["rowCount"],
                                fields=t.get("fields"), access=None)
                      for t in attempt["tables"]]
            broke = break_weakest_cycle_edge(tables, msg, edge_confidence(tables))
            if not broke:
                warnings.append("检测到循环依赖且无法自动降级: " + msg)
                raise
            warnings.append("检测到循环依赖，已剔除一条最低置信度的关联边后重试；"
                            "被剔除列的外键值将为随机值")
            # 重建请求（引用规则已变），复用同一 dryRun
            attempt["tables"] = [{"tableName": t.table, "rowCount": t.row_count,
                                  "fields": t.fields} for t in tables]
            return self._task_service.create_and_run(attempt)

    def _validate_plan_connection(self, plan: DataGenPlanModel) -> None:
        """连接参数强校验：「两者都无」单独给文案（执行期最常见情形）。"""
        has_conn = plan.conn_id is not None
        has_url = bool(plan.db_url and plan.db_url.strip())
        if not has_conn and not has_url:
            raise ValueError("方案尚未指定数据库连接，请先选择连接配置或填写 JDBC URL")
        if has_conn and has_url:
            raise ValueError("方案的数据库连接配置 ID 与 JDBC URL 只能提供其一")
        if has_conn and self._repo.find_db_conn(plan.conn_id) is None:
            raise ValueError(f"数据库连接配置不存在: {plan.conn_id}")

    def plan_needs_connection(self, plan_id: int) -> bool:
        plan = self.detail(plan_id)
        return plan.conn_id is None and not (plan.db_url and plan.db_url.strip())

    def apply_plan_connection(self, plan_id: int, conn: dict) -> None:
        """回填方案连接（connId 与 dbUrl 二选一，用于分析后补连接）。"""
        plan = self.detail(plan_id)
        conn_id = _opt_int(conn.get("connId"))
        db_url = str(conn.get("dbUrl")) if conn.get("dbUrl") is not None else None
        has_conn = conn_id is not None
        has_url = bool(db_url and db_url.strip())
        if has_conn == has_url:
            raise ValueError("数据库连接配置 ID 与 JDBC URL 必须且只能提供其一")
        if has_conn and self._repo.find_db_conn(conn_id) is None:
            raise ValueError(f"数据库连接配置不存在: {conn_id}")
        if has_conn:
            plan.conn_id = conn_id
            plan.db_url = None
        else:
            plan.conn_id = None
            plan.db_url = db_url
        self._repo.update_plan(plan)

    def _resolve_plan_conn(self, plan: DataGenPlanModel) -> tuple[str | None, str | None,
                                                                  str | None]:
        """连接三元组解析：connId → db_conn（密码解密）；否则原样 dbUrl。"""
        if plan.conn_id is not None:
            conn = self._repo.find_db_conn(plan.conn_id)
            if conn is None:
                raise ValueError(f"数据库连接配置不存在: {plan.conn_id}")
            return conn.db_url, conn.username, crypto.decrypt(conn.password)
        return plan.db_url, None, None

    def _refresh_seq_starts(self, plan: DataGenPlanModel,
                            tables: list[PlanTable], warnings: list[str]) -> None:
        """刷新方案中所有 seq 规则的起始值（探测失败回退 1 并写告警）。"""
        from datagen.plan_pipeline import refresh_seq_starts

        resolved = self._resolve_plan_conn(plan)
        db_url, db_user, db_password = resolved
        if not db_url:
            return
        conn = None
        try:
            from server.datagen_service import _connect

            conn, _dialect = _connect(db_url, db_user, db_password)
        except Exception as exc:  # noqa: BLE001 — 对齐 Java：刷新失败仅告警不阻断
            warnings.append("序号起始值刷新失败: " + str(exc))
            return
        try:
            for t in tables:
                if not t.fields or not t.fields.strip():
                    continue
                fields, warns = refresh_seq_starts(conn, db_url or "", t.table or "",
                                                   t.fields)
                t.fields = fields
                warnings.extend(warns)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
