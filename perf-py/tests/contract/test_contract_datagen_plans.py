"""datagen plans 写路径契约测试（阶段 5 · 步骤 8）：analyze/adopt/execute/delete 全链。

以 Java EndpointDataPlanService + DataGenService.runFromPlan 源码为契约：
- analyze：校验文案、pending 落库、异步分析（inline 同步模式）→ updateAnalysis 回写、
  分析失败 → failed + 「分析失败: 」前缀；
- adopt/delete：状态机与 wire；
- execute：parseTables → 补父表 → 刷 seqStart → connId 解密 → 组装 → 引擎执行 →
  updateTaskId（dryRun 不回写）→ updateWarnings；rowOverrides 优先与上限保护；
  连接校验文案；环依赖降级重试。
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from ptde.models.wire import EngineResult, TableStat
from server.app import create_app
from server.datagen_plan_service import DatagenPlanService, is_detail_endpoint
from server.datagen_repo import Base, DatagenRepository
from server.datagen_service import DatagenWriteService
from server.db import make_engine

JDBC = "jdbc:mysql://127.0.0.1:3306/demo"


class StubEngine:
    """可编程桩引擎：固定结果 + 可注环依赖（首抛后重试成功）与失败。"""

    def __init__(self, success=True, fail_msg=None, cycle_once=False):
        self._success, self._fail_msg = success, fail_msg
        self._cycle_once = cycle_once
        self.calls = 0

    def execute(self, spec, listener):  # noqa: ANN001
        self.calls += 1
        if self._cycle_once and self.calls == 1:
            raise ValueError("表组存在循环依赖，无法确定生成顺序: perf_order -> perf_user -> perf_order")
        if self._fail_msg:
            return EngineResult(success=False, total_rows=0, inserted_rows=0,
                                table_stats=[], error_msg=self._fail_msg)
        return EngineResult(
            success=True,
            total_rows=sum(t.row_count for t in spec.tables),
            inserted_rows=0 if spec.dry_run else sum(t.row_count for t in spec.tables),
            estimated_max_txn_ms=5 if spec.dry_run else 0,
            table_stats=[TableStat(table=t.table_name, rows=t.row_count)
                         for t in spec.tables])


class SyncExecutor:
    def submit(self, fn, *args):
        fn(*args)


def _seed(factory, *, with_source: bool = True, source_dir: str | None = None):
    """种子：源码仓库 + 磁盘源码（Controller + Mapper）+ 压测历史。"""
    from datetime import datetime

    from server.collector_repo import SourceRepoModel
    from server.source_repo import SourceFileModel
    from server.stress_repo import StressSampleModel, StressTaskModel

    with factory() as s:
        repo = SourceRepoModel(id=1, repo_name="demo-repo", app_name="demo-app",
                               source_path=source_dir or str(_SOURCE_DIR),
                               status="ready", file_count=3)
        s.add(repo)
        if with_source:
            for rel in ("OrderController.java", "OrderMapper.java", "OrderMapper.xml"):
                s.add(SourceFileModel(repo_id=1, relative_path=rel, file_name=rel,
                                      language="java", size=100, create_time=datetime.now()))
        s.add(StressTaskModel(
            id=9, task_name="GET /api/order/list", engine_type="http",
            target_url="/api/order/list", app_name="demo-app", app_version="",
            method="GET", concurrency=1, duration_sec=60, qps_limit=0,
            status="finished", result_json=json.dumps({"avgMs": 100.0}),
            error_msg=None, create_time=datetime(2026, 11, 16, 9, 0, 0),
            finish_time=datetime(2026, 11, 16, 9, 1, 0)))
        for i in range(3):
            s.add(StressSampleModel(task_id=9, sample_time=datetime(2026, 11, 16, 9, i, 0),
                                    concurrency=1, qps=10.0, p50_ms=50.0, p95_ms=60.0,
                                    p99_ms=70.0, error_count=0))
        s.commit()


def _make(tmp_path: Path, engine=None, with_source: bool = True):
    factory = sessionmaker(bind=make_engine(f"sqlite:///{tmp_path}/plan.db"),
                           expire_on_commit=False)
    Base.metadata.create_all(factory.kw["bind"])
    source_dir = str(_SOURCE_DIR) if with_source else str(tmp_path / "nonexistent")
    _seed(factory, with_source=with_source, source_dir=source_dir)
    repo = DatagenRepository(factory)
    stress_repo = __import__("server.stress_repo", fromlist=["StressRepository"]) \
        .StressRepository(factory)
    task_service = DatagenWriteService(repo, engine=engine,
                                       executor=type("S", (), {"submit": lambda self, fn, *a: fn(*a)})())
    app = create_app(datagen_repo=repo, stress_repo=stress_repo, auth_enabled=False)
    plan_service = DatagenPlanService(repo, stress_repo, task_service=task_service,
                                      inline_analyze=True)
    app.state.datagen_plan_service = plan_service
    return TestClient(app), plan_service


_SOURCE_DIR = Path(__file__).parent / "_source_demo"
_SOURCE_DIR.mkdir(exist_ok=True)
if not (_SOURCE_DIR / "OrderController.java").exists():
    (_SOURCE_DIR / "OrderController.java").write_text(
        "/**\n * @RestController\n */\n"
        "@RestController\n@RequestMapping(\"/api/order\")\n"
        "public class OrderController {\n"
        "    private final OrderMapper orderMapper;\n"
        "    @GetMapping(\"/list\")\n"
        "    public java.util.List<com.demo.Order> list() {\n"
        "        return orderMapper.listOrders();\n"
        "    }\n"
        "    @GetMapping(\"/{id}\")\n"
        "    public com.demo.Order detail(Long id) {\n"
        "        return orderMapper.getOrder(id);\n"
        "    }\n"
        "    @PostMapping(\"/create\")\n"
        "    public void create(com.demo.Order o) {\n"
        "        orderMapper.insertOrder(o);\n"
        "    }\n"
        "}\n", encoding="utf-8")
if not (_SOURCE_DIR / "OrderMapper.java").exists():
    (_SOURCE_DIR / "OrderMapper.java").write_text(
        "@Mapper\npublic interface OrderMapper {\n"
        "    java.util.List<com.demo.Order> listOrders();\n"
        "    com.demo.Order getOrder(Long id);\n"
        "    void insertOrder(com.demo.Order o);\n"
        "}\n", encoding="utf-8")
if not (_SOURCE_DIR / "OrderMapper.xml").exists():
    (_SOURCE_DIR / "OrderMapper.xml").write_text(
        "<?xml version=\"1.0\"?>\n<mapper namespace=\"OrderMapper\">\n"
        "<select id=\"listOrders\" resultType=\"com.demo.Order\">"
        "SELECT o.*, u.name FROM perf_order o JOIN perf_user u ON o.user_id = u.id "
        "WHERE o.status = 'PAID' LIMIT 20</select>\n"
        "<select id=\"getOrder\" resultType=\"com.demo.Order\">"
        "SELECT * FROM perf_order WHERE id = #{id} LIMIT 1</select>\n"
        "<insert id=\"insertOrder\">INSERT INTO perf_order(user_id, order_no) "
        "VALUES(#{userId}, #{orderNo})</insert>\n"
        "</mapper>\n", encoding="utf-8")


def _analyze_body(**kw):
    return {"repoId": kw.pop("repoId", 1), "planName": kw.pop("planName", None),
            "endpoints": kw.pop("endpoints", [
                {"httpMethod": "GET", "path": "/api/order/list", "weight": 1}]),
            "stress": {"concurrency": 10, "durationSec": 60, "qpsLimit": 0,
                       "assumedRtMs": 100}, "connId": kw.pop("connId", None),
            "dbUrl": kw.pop("dbUrl", None), "taskId": kw.pop("taskId", None),
            "llmEnhance": kw.pop("llmEnhance", False)}


def test_is_detail_endpoint():
    assert is_detail_endpoint("/api/order/{id}") is True
    assert is_detail_endpoint("/api/order/list") is False
    assert is_detail_endpoint(None) is False


# ---------------------------------------------------------------- analyze


def test_analyze_wire_and_success_flow(tmp_path):
    """analyze 全链：pending 落库 → 源码分析 → tablesJson 回写 + 告警。"""
    client, _ = _make(tmp_path)
    body = client.post("/api/datagen/plans/analyze", json=_analyze_body()).json()
    assert body["code"] == 0
    data = body["data"]
    assert set(data) == {"id", "repoId", "planName", "endpointJson", "stressJson",
                         "connId", "dbUrl", "tablesJson", "warningsJson", "status",
                         "taskId", "createTime", "updateTime"}
    assert data["status"] == "pending" and data["createTime"] is None
    assert data["planName"] == "数据方案-GET /api/order/list"

    detail = client.get("/api/datagen/plans/1").json()["data"]
    assert detail["status"] == "pending", "pending 同时表示「分析中/待采纳」"
    tables = json.loads(detail["tablesJson"])
    assert {t["table"] for t in tables} == {"perf_order", "perf_user"}, \
        "JOIN ON 边识别父表并自动补入"
    order = next(t for t in tables if t["table"] == "perf_order")
    assert order["access"] in ("READ", "WRITE")
    fields = json.loads(order["fields"])
    assert fields["status"]["type"] == "enum", "WHERE 谓词 → enum 规则"
    assert "PAID" in fields["status"]["values"]
    assert fields["user_id"]["type"] in ("ref", "refSkew"), "JOIN 边 → ref 规则"
    assert any("响应时间基线取值来源" in w for w in json.loads(detail["warningsJson"]))


def test_analyze_validation_messages_exact(tmp_path):
    client, _ = _make(tmp_path)
    resp = client.post("/api/datagen/plans/analyze", json=_analyze_body(repoId=None))
    assert resp.json()["message"] == "源码仓库 ID 必填"
    resp2 = client.post("/api/datagen/plans/analyze", json=_analyze_body(endpoints=[]))
    assert resp2.json()["message"] == "至少选择一个压测接口"
    resp3 = client.post("/api/datagen/plans/analyze",
                        json=_analyze_body(connId=1, dbUrl=JDBC))
    assert resp3.json()["message"] == "数据库连接配置 ID 与 JDBC URL 只能提供其一"


def test_analyze_endpoint_not_located_warning(tmp_path):
    """接口未定位：占位权重 + 告警（不做静默猜测）。"""
    client, _ = _make(tmp_path)
    client.post("/api/datagen/plans/analyze", json=_analyze_body(endpoints=[
        {"httpMethod": "GET", "path": "/api/missing/x", "weight": 1}]))
    detail = client.get("/api/datagen/plans/1").json()["data"]
    warnings = json.loads(detail["warningsJson"])
    assert any("接口未定位: GET /api/missing/x（可手工补表后执行）" in w for w in warnings)


def test_analyze_no_source_warning(tmp_path):
    client, _ = _make(tmp_path, with_source=False)
    client.post("/api/datagen/plans/analyze", json=_analyze_body())
    detail = client.get("/api/datagen/plans/1").json()["data"]
    warnings = json.loads(detail["warningsJson"])
    assert any("源码不可读" in w for w in warnings)
    assert detail["tablesJson"] in (None, "[]"), "无源码 → 无表清单"


# ---------------------------------------------------------------- adopt/delete


def test_adopt_status_machine(tmp_path):
    client, _ = _make(tmp_path, with_source=False)
    client.post("/api/datagen/plans/analyze", json=_analyze_body())
    body = client.post("/api/datagen/plans/1/adopt").json()
    assert body["code"] == 0 and body["data"]["status"] == "adopted"
    assert client.get("/api/datagen/plans/1").json()["data"]["status"] == "adopted"
    body2 = client.post("/api/datagen/plans/1/adopt", params={"adopt": "false"}).json()
    assert body2["data"]["status"] == "discarded"


def test_delete_and_missing_detail(tmp_path):
    client, _ = _make(tmp_path, with_source=False)
    client.post("/api/datagen/plans/analyze", json=_analyze_body())
    body = client.delete("/api/datagen/plans/1").json()
    assert body == {"code": 0, "message": "success", "data": None}
    assert client.get("/api/datagen/plans/1").json()["message"] == "方案不存在: 1"
    # 对齐 Java deleteById：DELETE 不存在的方案也返回 success（无存在性检查）
    assert client.delete("/api/datagen/plans/1").json() == {"code": 0, "message": "success", "data": None}


# ---------------------------------------------------------------- execute


def _seed_analyzed_plan(client, tmp_path, conn_id=None, db_url=None,
                        fields=None, row_count=1000):
    """直接落一个已分析的方案（tablesJson 可控），避免依赖源码分析细节。"""
    tables = [{"table": "perf_order", "rowCount": row_count,
               "fields": fields or json.dumps({"order_no": {"type": "seq", "seqPrefix": "ON_"}}),
               "access": "WRITE", "evidence": []}]
    tables_json = json.dumps(tables, ensure_ascii=False)
    client.app.state.datagen_repo.insert_plan(
        repo_id=1, plan_name="数据方案-GET /api/order/list",
        endpoint_json="[]", stress_json="{}",
        conn_id=conn_id, db_url=db_url)
    client.app.state.datagen_repo.update_analysis(1, tables_json, "[]")
    return tables_json


def test_execute_full_flow_with_seq_refresh_and_taskid(tmp_path):
    """execute 全链：connId 解密 → seq 刷新 → 引擎执行 → updateTaskId（executed）。"""
    engine = StubEngine()
    client, plan_service = _make(tmp_path, engine=engine)
    factory = client.app.state.datagen_repo._sessions
    import base64
    import hashlib
    from datetime import datetime

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from server.collector_repo import DbConnModel

    secret = "s3cret"
    iv = b"0123456789ab"
    ct = AESGCM(hashlib.sha256(secret.encode()).digest()).encrypt(
        iv, b"real-pass", None)
    enc = "ENC:" + base64.b64encode(iv + ct).decode()
    with factory() as s:
        s.add(DbConnModel(id=5, conn_name="本地库", db_url=JDBC, username="root",
                          password=enc, is_default=0,
                          create_time=datetime(2026, 11, 16, 10, 0, 0)))
        s.commit()
    _seed_analyzed_plan(client, tmp_path, conn_id=5)
    monkey_secret = pytest.MonkeyPatch()
    monkey_secret.setenv("PERF_CRYPTO_SECRET", secret)
    try:
        # 连接校验放在 execute 内部——连不上目标库会在 seq 刷新处降级为告警，
        # 灌数任务本体仍会创建（与 Java 一致：刷新失败不阻断）
        body = client.post("/api/datagen/plans/1/execute",
                           params={"dryRun": "false"},
                           json={"connId": "5"}).json()
        assert body["code"] == 0, body
        task = body["data"]
        assert task["dbUrl"] == JDBC, "connId → db_conn 三元组解析（密码解密在服务内部）"
        assert task["taskName"] == "数据方案-GET /api/order/list"
        assert task["status"] == "pending", "wire 是插入时形态（create_and_run 契约）"
        assert task["totalRows"] == 1000, "tables 传入 → rowCount=0、totalRows=1000"
        # 同步执行器 → 任务随请求完成；detail 校验终态
        detail = client.get(f"/api/datagen/tasks/{task['id']}").json()["data"]
        assert detail["status"] == "finished" and detail["inserted"] == 1000
        plan = client.get("/api/datagen/plans/1").json()["data"]
        assert plan["status"] == "executed" and plan["taskId"] == task["id"]
        warnings = json.loads(plan["warningsJson"])
        assert any("序号起始值刷新失败" in w for w in warnings), \
            "连不上目标库 → seq 探测降级告警（不阻断）"
        assert engine.calls == 1
    finally:
        monkey_secret.undo()


def test_execute_dry_run_does_not_mark_executed(tmp_path):
    """dryRun=true：不回写 taskId/status（防状态失真），告警仍回写。"""
    client, _ = _make(tmp_path, engine=StubEngine())
    _seed_analyzed_plan(client, tmp_path, db_url=JDBC)
    body = client.post("/api/datagen/plans/1/execute", json={}).json()
    assert body["code"] == 0 and body["data"]["inserted"] == 0
    plan = client.get("/api/datagen/plans/1").json()["data"]
    assert plan["status"] == "pending" and plan["taskId"] is None


def test_execute_row_overrides_and_caps(tmp_path):
    """rowOverrides 优先（上限 MAX_ROWS 保护）；非法值按方案落库值执行。"""
    client, _ = _make(tmp_path, engine=StubEngine())
    _seed_analyzed_plan(client, tmp_path, db_url=JDBC, row_count=500)
    body = client.post("/api/datagen/plans/1/execute", json={
        "rowOverrides": [{"table": "PERF_ORDER", "rowCount": 2000},
                         {"table": "bad", "rowCount": -1}]}).json()
    assert body["data"]["totalRows"] == 2000
    body2 = client.post("/api/datagen/plans/1/execute", json={
        "rowOverrides": [{"table": "perf_order", "rowCount": 2000000}]}).json()
    assert body2["data"]["totalRows"] == 1000000, "MAX_ROWS 上限保护"


def test_execute_connection_validation_messages(tmp_path):
    client, _ = _make(tmp_path, engine=StubEngine())
    _seed_analyzed_plan(client, tmp_path)  # 无连接
    resp = client.post("/api/datagen/plans/1/execute", json={})
    assert resp.json()["message"] == \
        "方案尚未指定数据库连接，请先选择连接配置或填写 JDBC URL"
    # 回填连接后可执行
    resp2 = client.post("/api/datagen/plans/1/execute", json={"dbUrl": JDBC})
    assert resp2.json()["code"] == 0
    assert client.get("/api/datagen/plans/1").json()["data"]["dbUrl"] == JDBC


def test_execute_only_row_overrides_skips_connection_backfill(tmp_path):
    """仅带 rowOverrides 的请求不回填连接：报「方案未指定连接」而非「必须且只能其一」。"""
    client, _ = _make(tmp_path, engine=StubEngine())
    _seed_analyzed_plan(client, tmp_path)
    resp = client.post("/api/datagen/plans/1/execute", json={
        "rowOverrides": [{"table": "perf_order", "rowCount": 10}]})
    assert resp.json()["message"] == \
        "方案尚未指定数据库连接，请先选择连接配置或填写 JDBC URL"


def test_execute_cycle_fallback_retry(tmp_path):
    """环依赖：剔除最低置信边后重试一次 → 任务成功 + 告警；失败信息不直抛。"""
    engine = StubEngine(cycle_once=True)
    client, _ = _make(tmp_path, engine=engine)
    fields = json.dumps({
        "user_id": {"type": "ref", "table": "perf_user", "column": "id",
                    "confidence": 0.6},
        "order_no": {"type": "seq", "seqPrefix": "ON_"}})
    tables = [{"table": "perf_order", "rowCount": 100, "fields": fields, "access": "WRITE"},
              {"table": "perf_user", "rowCount": 100, "fields": None, "access": "READ"}]
    client.app.state.datagen_repo.insert_plan(
        repo_id=1, plan_name="环依赖方案", endpoint_json="[]", stress_json="{}",
        conn_id=None, db_url=JDBC)
    client.app.state.datagen_repo.update_analysis(1, json.dumps(tables), "[]")
    body = client.post("/api/datagen/plans/1/execute",
                       params={"dryRun": "false"}, json={}).json()
    assert body["code"] == 0, body
    assert engine.calls == 2, "剔除边后重试一次"
    detail = client.get(f"/api/datagen/tasks/{body['data']['id']}").json()["data"]
    assert detail["status"] == "finished"
    # 对齐 Java：降级文案在 task 的 detailJson.warnings（runEngine 局部 warnings）
    task_warnings = json.loads(detail["detailJson"])["warnings"]
    assert any("已剔除一条最低置信度的关联边后重试" in w for w in task_warnings)
