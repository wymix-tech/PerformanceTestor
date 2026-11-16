"""datagen 写路径契约测试（阶段 5 · 步骤 7）：POST /tasks /tables /columns /suggestions adopt。

- wire 形态：任务实体 14 字段（createTime=null 对齐 MyBatis 插入后未回填）、
  ApiResponse 包装、校验错误文案原样（HTTP 200 + code=1）；
- 生命周期：pending → running → finished/failed，detailJson（tableStats/warnings/
  dryRunReport）与 semantic_hint 落库；
- connId 覆盖手填连接（applyConn 语义）。
（本环境无真实目标库/Java server——tables/columns 走错误路径与校验契约；
  引擎以可编程桩注入。Java 侧恢复后可用 capture-contract.sh 扩展 POST 捕获补强。）
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from datagen.model import EngineResult, MagicHint, RelationHint, TableStat
from server.app import create_app
from server.datagen_repo import Base, DatagenRepository
from server.datagen_service import DatagenWriteService
from server.db import make_engine

JDBC = "jdbc:mysql://127.0.0.1:3306/demo"


class SyncExecutor:
    """同步执行器：任务随请求同步完成（测试确定性；生产为 2 线程池）。"""

    def submit(self, fn, *args):
        fn(*args)


class StubEngine:
    """可编程桩引擎：固定结果 + 回调 hints/进度/告警，可注失败。"""

    def __init__(self, success=True, fail_msg=None, hints=True, raise_cycle=False):
        self._success, self._fail_msg = success, fail_msg
        self._hints, self._raise_cycle = hints, raise_cycle
        self.last_spec = None
        self.last_listener = None

    def execute(self, spec, listener):  # noqa: ANN001
        self.last_spec, self.last_listener = spec, listener
        if self._raise_cycle:
            raise ValueError("检测到循环依赖: a → b → a（定位环中表名）")
        if listener is not None:
            listener.on_progress(10, 5, "user")
            if self._hints:
                listener.on_hints(
                    [MagicHint(table="user", column="status", kind="literal",
                               values=["PAID", "NEW"], source_refs=["a.java:1"])],
                    [RelationHint("order", "user_id", "user", "id", "db_fk", 0.95)])
            listener.on_warn("[警告] 采样警告")
        return EngineResult(
            success=self._success, dry_run=spec.dry_run, total_rows=10,
            inserted_rows=0 if spec.dry_run else 10,
            estimated_max_txn_ms=12 if spec.dry_run else 0,
            table_stats=[TableStat(table="user", rows=10)],
            error_msg=self._fail_msg)


@pytest.fixture()
def db_url(tmp_path) -> str:
    """每测试独立文件库：worker 线程与轮询会话并发使用同一 StaticPool 单连接
    （内存库）存在事务可见性竞争，文件库多连接与生产 MySQL 语义一致。"""
    return f"sqlite:///{tmp_path}/datagen.db"


def _make_client(engine, seed_conn: bool = False, seed_hint: bool = False,
                 db_url: str = "sqlite:///:memory:"):
    factory = sessionmaker(bind=make_engine(db_url), expire_on_commit=False)
    Base.metadata.create_all(factory.kw["bind"])
    repo = DatagenRepository(factory)
    if seed_conn:
        from datetime import datetime

        from server.collector_repo import DbConnModel

        with factory() as s:
            s.add(DbConnModel(id=5, conn_name="本地库", db_url=JDBC,
                              username="root", password="ENC:xxx", is_default=0,
                              create_time=datetime(2026, 11, 16, 10, 0, 0)))
            s.commit()
    if seed_hint:
        repo.hint_upsert(repo_id=None, hint_type="value", table_name="user",
                         column_name="status", values_json='["A"]', parent_table="",
                         parent_column="", source_refs="[]", source="literal",
                         confidence=None)
    app = create_app(datagen_repo=repo, auth_enabled=False)
    app.state.datagen_service = DatagenWriteService(
        repo, engine=engine, executor=SyncExecutor())
    return TestClient(app), repo


def factory_rows(repo, tid):
    raw = repo._sessions.kw["bind"].connect().exec_driver_sql(
        "SELECT id, task_name FROM data_gen_task").fetchall()
    return raw


def _post_tasks(client, **overrides) -> dict:
    payload = {"dbUrl": JDBC, "tableName": "user", "rowCount": 10, **overrides}
    return client.post("/api/datagen/tasks", json=payload).json()


# ---------------------------------------------------------------- POST /tasks


def test_start_response_wire_shape(db_url):
    """响应：任务实体 14 字段 + taskName 兜底「灌数据-表名」+ createTime=null。"""
    client, _ = _make_client(StubEngine())
    body = _post_tasks(client)
    assert body["code"] == 0 and body["message"] == "success"
    data = body["data"]
    assert set(data) == {"id", "taskName", "dbUrl", "tableName", "rowCount", "ruleJson",
                         "status", "inserted", "totalRows", "tablesJson", "detailJson",
                         "costMs", "errorMsg", "createTime"}
    assert data["taskName"] == "灌数据-user" and data["status"] == "pending"
    assert data["createTime"] is None and data["totalRows"] == 10
    assert data["rowCount"] == 10 and data["inserted"] == 0


def test_start_lifecycle_detail_and_hints_persisted(db_url):
    """/tasks 生命周期：finished + detailJson（tableStats/warnings）+ 语义提示落库。"""
    client, repo = _make_client(StubEngine())
    task_id = _post_tasks(client)["data"]["id"]
    detail = client.get(f"/api/datagen/tasks/{task_id}").json()["data"]
    d = json.loads(detail["detailJson"])
    assert d["tableStats"] == [{"table": "user", "rows": 10}]
    assert d["warnings"] == ["[警告] 采样警告"]
    assert "dryRunReport" not in d
    assert detail["inserted"] == 10 and detail["status"] == "finished"
    # 进度回调落库（onProgress → inserted=5 被最终 updateFinished 覆盖为 10）
    # 语义提示：value → pending；relation(db_fk) → adopted
    hints = {h.hint_type: h for h in repo.suggestions(None, None, None, None)}
    assert hints["value"].values_json == '["PAID", "NEW"]'
    assert hints["value"].status == "pending" and hints["value"].source == "literal"
    assert hints["relation"].status == "adopted" and hints["relation"].confidence == 0.95
    assert hints["relation"].table_name == "order" and hints["relation"].parent_table == "user"


def test_start_dry_run_report_in_detail(db_url):
    """dryRun=true：detailJson 含 dryRunReport（estimatedMaxTxnMs/totalRows/passed）。"""
    client, _ = _make_client(StubEngine())
    task_id = _post_tasks(client, dryRun=True)["data"]["id"]
    d = json.loads(client.get(f"/api/datagen/tasks/{task_id}").json()["data"]["detailJson"])
    assert d["dryRunReport"] == {"estimatedMaxTxnMs": 12, "totalRows": 10, "passed": True}


def test_start_engine_failure_marks_failed(db_url):
    client, _ = _make_client(StubEngine(success=False, fail_msg="表不存在: user"))
    task_id = _post_tasks(client)["data"]["id"]
    detail = client.get(f"/api/datagen/tasks/{task_id}").json()["data"]
    assert detail["status"] == "failed"
    assert detail["errorMsg"] == "表不存在: user" and detail["inserted"] == 0


def test_start_cycle_dependency_fails_with_located_message(db_url):
    """环依赖：步骤 7 无自动降级 → 任务失败，错误信息定位环中表名（Java 引擎同文案）。"""
    client, _ = _make_client(StubEngine(raise_cycle=True))
    task_id = _post_tasks(client)["data"]["id"]
    detail = client.get(f"/api/datagen/tasks/{task_id}").json()["data"]
    assert detail["status"] == "failed"
    assert "循环依赖" in detail["errorMsg"]


def test_start_validation_error_wire_exact(db_url):
    client, _ = _make_client(StubEngine())
    resp = client.post("/api/datagen/tasks",
                       json={"dbUrl": "jdbc:oracle://x", "tableName": "t"})
    assert resp.status_code == 200
    assert resp.json() == {"code": 1,
                           "message": "JDBC URL 仅支持 jdbc:mysql:// 或 jdbc:postgresql:// 前缀",
                           "data": None}
    resp2 = client.post("/api/datagen/tasks",
                        json={"dbUrl": JDBC, "tableName": "bad-name"})
    assert resp2.json()["message"] == "表名仅允许字母数字下划线"


def test_start_conn_id_overrides_manual_conn(db_url):
    """connId 命中时覆盖手填连接（applyConn 语义）；未命中保留手填。"""
    client, repo = _make_client(StubEngine(), seed_conn=True, db_url=db_url)
    data = _post_tasks(client, connId="5", dbUrl="jdbc:mysql://wrong/x")["data"]
    assert data["dbUrl"] == JDBC, "connId 命中 → 覆盖手填"
    assert repo.task_by_id(data["id"]).db_url == JDBC
    # connId 不存在：静默保留手填
    data2 = _post_tasks(client, connId="999", dbUrl=JDBC)["data"]
    assert data2["dbUrl"] == JDBC


# ---------------------------------------------------------------- 表/列探测


def test_tables_unreachable_error_wrapper(db_url):
    """连不上目标库：HTTP 200 + code=1「连接目标库失败: …」（无 404，对齐 Java）。"""
    client, _ = _make_client(StubEngine())
    resp = client.post("/api/datagen/tables",
                       json={"dbUrl": "jdbc:mysql://127.0.0.1:1/nope"})
    body = resp.json()
    assert resp.status_code == 200 and body["code"] == 1 and body["data"] is None
    assert body["message"].startswith("连接目标库失败: ")


def test_tables_invalid_url_message_exact(db_url):
    client, _ = _make_client(StubEngine())
    body = client.post("/api/datagen/tables", json={"dbUrl": "jdbc:xx://x"}).json()
    assert body["message"] == "JDBC URL 仅支持 jdbc:mysql:// 或 jdbc:postgresql:// 前缀"


def test_columns_table_name_validated_before_connect(db_url):
    """表名校验先于连库：非法表名直接文案拒绝。"""
    client, _ = _make_client(StubEngine())
    body = client.post("/api/datagen/columns",
                       json={"dbUrl": JDBC, "tableName": "drop; --"}).json()
    assert body["message"] == "表名仅允许字母数字下划线"


def test_columns_unreachable_error_wrapper(db_url):
    client, _ = _make_client(StubEngine())
    body = client.post("/api/datagen/columns",
                       json={"dbUrl": "jdbc:mysql://127.0.0.1:1/nope",
                             "tableName": "user"}).json()
    assert body["code"] == 1 and body["message"].startswith("读取表结构失败: ")


# ---------------------------------------------------------------- 建议采纳


def test_adopt_wire_null_data_and_missing_error(db_url):
    client, repo = _make_client(StubEngine(), seed_hint=True)
    hid = repo.suggestions(None, None, None, None)[0].id
    body = client.post(f"/api/datagen/suggestions/{hid}/adopt").json()
    assert body == {"code": 0, "message": "success", "data": None}, "返回 data=null"
    assert repo.hint_by_id(hid).status == "adopted"
    # adopt=false → dismissed
    client.post(f"/api/datagen/suggestions/{hid}/adopt", params={"adopt": "false"})
    assert repo.hint_by_id(hid).status == "dismissed"
    # 不存在：code=1 + 文案
    body = client.post("/api/datagen/suggestions/999/adopt").json()
    assert body["message"] == "建议不存在: 999"
