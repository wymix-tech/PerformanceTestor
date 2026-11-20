"""QW2 按任务清理最小版单测：两档清理 + 所有权门禁 + 审计留痕。

- 档位 task_rows：按 data_gen_task_scope 所有权范围 (watermark_from, watermark_to]
  标记删除；无所有权标记的共享表拒绝并报告（总方案 §12.5）；
- 档位 rebuild：逐表 TRUNCATE，仅限独占目标（exclusive=true 门禁确认）；
- 生产标识 URL 一律拒绝；拒绝/失败/成功均落 data_gen_cleanup_audit。
连接用 FakeConn/FakeCursor 鸭子类型替身（不连真库，对齐 ptde 测试风格）。
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import server.datagen_service as dsvc
from ptde.models.wire import ColumnMeta, SchemaMetadata
from server.app import create_app
from server.datagen_repo import Base, DatagenRepository
from server.datagen_service import DatagenWriteService
from server.db import make_engine

JDBC = "jdbc:mysql://127.0.0.1:3306/perf_test"


# ---------------------------------------------------------------- 测试替身

class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = -1

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        head = sql.strip().split(None, 1)[0].upper()
        if head == "SELECT":
            self._result = self._conn.select_results.pop(0)
        elif head == "DELETE":
            self.rowcount = self._conn.delete_rowcounts.pop(0)
        return self

    def fetchone(self):
        return self._result

    def close(self):
        pass


class FakeConn:
    def __init__(self, select_results=(), delete_rowcounts=()):
        self.executed = []
        self.select_results = list(select_results)
        self.delete_rowcounts = list(delete_rowcounts)
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


class FakeDialect:
    """最小方言替身：单自增整型主键 id。"""

    param = "?"

    def quote_identifier(self, name):
        return "`" + name + "`"

    def fetch_metadata(self, conn, table_name):
        meta = SchemaMetadata(table_name=table_name)
        meta.columns.append(ColumnMeta(name="id", type_name="BIGINT",
                                       auto_increment=True, primary_key=True))
        meta.columns.append(ColumnMeta(name="status", type_name="VARCHAR(16)"))
        meta.primary_keys.add("id")
        return meta


# ---------------------------------------------------------------- 装配

def _repo_only():
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    import server.collector_repo  # noqa: F401  注册模型
    import server.datagen_repo  # noqa: F401
    Base.metadata.create_all(factory.kw["bind"])
    return DatagenRepository(factory)


def _finished_task(repo, tables_json=None, db_url=JDBC, table_name="perf_user",
                   row_count=100):
    tid = repo.insert_task(task_name="灌数据-perf_user", db_url=db_url,
                           table_name=table_name, row_count=row_count,
                           total_rows=row_count, tables_json=tables_json,
                           rule_json=None)
    repo.update_task_finished(tid, "finished", row_count, 10, None)
    return tid


@pytest.fixture()
def service():
    repo = _repo_only()
    svc = DatagenWriteService(repo)
    return svc


def _fake_connect(monkeypatch, conn=None):
    conn = conn or FakeConn()
    monkeypatch.setattr(dsvc, "_connect",
                        lambda db_url, user, pwd: (conn, FakeDialect()))
    return conn


# ---------------------------------------------------------------- service 层


def test_cleanup_task_rows_deletes_only_scope_range(service, monkeypatch):
    """档位 task_rows：按任务所有权范围删除，参数绑定 (from, to]。"""
    tid = _finished_task(service._repo, tables_json=json.dumps([
        {"tableName": "perf_user", "rowCount": 100}]))
    service._repo.replace_scope(tid, [{"table_name": "perf_user", "pk_column": "id",
                                       "watermark_from": 100, "watermark_to": 150,
                                       "row_count": 50}])
    conn = _fake_connect(monkeypatch, FakeConn(delete_rowcounts=[50]))
    out = service.cleanup_task(tid, mode="task_rows", confirm=True)
    assert out["taskId"] == tid and out["deletedRows"] == 50
    assert out["tables"] == [{"table": "perf_user", "action": "delete_range",
                              "rows": 50, "from": 100, "to": 150}]
    delete_sqls = [s for s, _p in conn.executed if s.upper().startswith("DELETE")]
    assert len(delete_sqls) == 1 and "WHERE `id` > ? AND `id` <= ?" in delete_sqls[0]
    assert conn.committed
    audits = service._repo.cleanup_audits()
    assert len(audits) == 1
    a = audits[0]
    assert a.task_id == tid and a.mode == "task_rows" and a.status == "success"
    assert a.deleted_rows == 50 and json.loads(a.tables_json) == ["perf_user"]


def test_cleanup_task_rows_missing_scope_rejected(service, monkeypatch):
    """无所有权标记的共享表：整体拒绝并报告，不执行任何 DELETE。"""
    tid = _finished_task(service._repo, tables_json=json.dumps([
        {"tableName": "perf_user", "rowCount": 100},
        {"tableName": "shared_dict", "rowCount": 10}]))
    service._repo.replace_scope(tid, [{"table_name": "perf_user", "pk_column": "id",
                                       "watermark_from": 0, "watermark_to": 100,
                                       "row_count": 100}])
    with pytest.raises(ValueError, match="无所有权标记.*shared_dict"):
        service.cleanup_task(tid, mode="task_rows", confirm=True)
    audits = service._repo.cleanup_audits()
    assert len(audits) == 1 and audits[0].status == "rejected"
    assert "shared_dict" in audits[0].message


def test_cleanup_rebuild_gates_and_truncate(service, monkeypatch):
    """档位 rebuild：缺 confirm / 缺 exclusive 均拒绝；齐备后逐表 TRUNCATE。"""
    tid = _finished_task(service._repo, tables_json=json.dumps([
        {"tableName": "perf_user", "rowCount": 100},
        {"tableName": "perf_order", "rowCount": 200}]))
    with pytest.raises(ValueError, match="显式确认"):
        service.cleanup_task(tid, mode="rebuild", confirm=False, exclusive=True)
    with pytest.raises(ValueError, match="独占"):
        service.cleanup_task(tid, mode="rebuild", confirm=True, exclusive=False)
    audits_before = len(service._repo.cleanup_audits())
    assert audits_before == 2, "拒绝亦留审计"
    conn = _fake_connect(monkeypatch, FakeConn(select_results=[(100,), (200,)]))
    out = service.cleanup_task(tid, mode="rebuild", confirm=True, exclusive=True)
    assert out["deletedRows"] == 300, "rebuild 审计口径 = 清理前行数合计"
    truncates = [s for s, _p in conn.executed if s.upper().startswith("TRUNCATE")]
    assert len(truncates) == 2 and all("TRUNCATE TABLE" in s for s in truncates)
    a = service._repo.cleanup_audits()[0]
    assert a.status == "success" and a.mode == "rebuild" and a.deleted_rows == 300


def test_cleanup_prod_url_refused(service):
    """生产标识库（构造）：一律拒绝并留审计（总方案 §13.1）。"""
    tid = _finished_task(service._repo,
                         db_url="jdbc:mysql://prod-db.internal:3306/biz",
                         tables_json=json.dumps([{"tableName": "t", "rowCount": 1}]))
    with pytest.raises(ValueError, match="生产标识"):
        service.cleanup_task(tid, mode="task_rows", confirm=True)
    assert service._repo.cleanup_audits()[0].status == "rejected"


def test_cleanup_prerequisites(service):
    """前置门禁：任务不存在 / 未完成 / 档位非法 / 未确认。"""
    with pytest.raises(ValueError, match="任务不存在: 999"):
        service.cleanup_task(999)
    tid_running = service._repo.insert_task(
        task_name="t", db_url=JDBC, table_name="perf_user", row_count=10,
        total_rows=10, tables_json=None, rule_json=None)
    with pytest.raises(ValueError, match="任务未完成"):
        service.cleanup_task(tid_running, confirm=True)
    tid = _finished_task(service._repo)
    with pytest.raises(ValueError, match="清理档位仅支持"):
        service.cleanup_task(tid, mode="nuke", confirm=True)
    with pytest.raises(ValueError, match="显式确认"):
        service.cleanup_task(tid, mode="task_rows", confirm=False)


# ---------------------------------------------------------------- 水位捕获 / 范围落库


def test_watermark_capture_and_scope_record(service, monkeypatch):
    """灌数前后主键水位 → data_gen_task_scope (from, to]；空表水位回退 0。"""
    request = {"dbUrl": JDBC, "dbUser": "u", "dbPassword": "p",
               "tables": [{"tableName": "perf_user", "rowCount": 50},
                          {"tableName": "perf_order", "rowCount": 30}]}
    # 灌数前：perf_user MAX=100；perf_order 空表 MAX=None → 0
    _fake_connect(monkeypatch, FakeConn(select_results=[(100,), (None,)]))
    watermarks = service._capture_watermarks(request)
    assert watermarks == {"perf_user": ("id", 100), "perf_order": ("id", 0)}
    # 灌数后：150 / 20
    conn2 = FakeConn(select_results=[(150,), (20,)])
    monkeypatch.setattr(dsvc, "_connect", lambda db_url, user, pwd: (conn2, FakeDialect()))
    service._record_scope(7, request, watermarks)
    scope = service._repo.scope_for_task(7)
    assert [(r.table_name, r.pk_column, r.watermark_from, r.watermark_to, r.row_count)
            for r in scope] == [("perf_order", "id", 0, 20, 20),
                                ("perf_user", "id", 100, 150, 50)]


def test_scope_capture_failure_is_non_blocking(service, monkeypatch):
    """水位捕获/范围落库失败仅日志告警，不阻断灌数任务（尽力而为）。"""
    request = {"dbUrl": JDBC, "dbUser": "u", "dbPassword": "p",
               "tables": [{"tableName": "perf_user", "rowCount": 50}]}

    def _boom(db_url, user, pwd):
        raise dsvc._ConnectError("down")

    monkeypatch.setattr(dsvc, "_connect", _boom)
    assert service._capture_watermarks(request) == {}
    service._record_scope(7, request, {"perf_user": ("id", 0)})  # 不抛


def test_int_pk_watermark_skips_non_integer_pk(monkeypatch):
    """无单自增整型主键的表不标记所有权（档位 2 将拒绝）。"""
    from server.datagen_service import _int_pk_watermark

    class NoPkDialect(FakeDialect):
        def fetch_metadata(self, conn, table_name):
            meta = SchemaMetadata(table_name=table_name)
            meta.columns.append(ColumnMeta(name="code", type_name="VARCHAR(32)"))
            meta.primary_keys.add("code")
            return meta

    conn = FakeConn()
    assert _int_pk_watermark(conn, NoPkDialect(), "t") is None


# ---------------------------------------------------------------- API 层（非契约端点）


class SyncExecutor:
    def submit(self, fn, *args):
        fn(*args)


def _client_with_repo():
    repo = _repo_only()
    app = create_app(datagen_repo=repo, auth_enabled=False)
    app.state.datagen_service = DatagenWriteService(repo, executor=SyncExecutor())
    return TestClient(app), repo


def test_cleanup_endpoint_wire_and_audits_query(monkeypatch):
    client, repo = _client_with_repo()
    tid = _finished_task(repo, tables_json=json.dumps(
        [{"tableName": "perf_user", "rowCount": 100}]))
    repo.replace_scope(tid, [{"table_name": "perf_user", "pk_column": "id",
                              "watermark_from": 0, "watermark_to": 100,
                              "row_count": 100}])
    _fake_connect(monkeypatch, FakeConn(delete_rowcounts=[100]))
    resp = client.post(f"/api/datagen/tasks/{tid}/cleanup",
                       params={"mode": "task_rows", "confirm": "true"}, json={})
    body = resp.json()
    assert body["code"] == 0 and body["message"] == "success"
    assert body["data"]["deletedRows"] == 100 and body["data"]["taskId"] == tid
    # 审计可查
    audits = client.get("/api/datagen/cleanup-audits").json()["data"]
    assert len(audits) == 1 and audits[0]["status"] == "success"
    assert audits[0]["taskId"] == tid and audits[0]["mode"] == "task_rows"


def test_cleanup_endpoint_error_wire(monkeypatch):
    """拒绝走 code=1 + 文案原样（对齐域内错误形态）。"""
    client, repo = _client_with_repo()
    tid = _finished_task(repo)
    resp = client.post(f"/api/datagen/tasks/{tid}/cleanup",
                       params={"mode": "task_rows"}, json={})
    body = resp.json()
    assert resp.status_code == 200 and body["code"] == 1
    assert body["message"] == "破坏性操作需显式确认（confirm=true）"
    resp2 = client.post("/api/datagen/tasks/999/cleanup",
                        params={"confirm": "true"}, json={})
    assert resp2.json()["message"] == "任务不存在: 999"
