"""QW3 生成后验证最小集单测：verify_min 三项检查 + 服务执行后挂钩 + 状态显式标注。

- 行数核对 / 外键孤儿抽检 / 枚举合法率：sqlite 直跑（声明类型 ENUM('A','B')
  由 sqlite 原样保留 type_name，内联枚举解析可复用）；
- 服务挂钩：报告落 data_gen_task.detail_json；验证失败任务状态显式标注
  finished_verify_failed（不伪装成功）；连接失败仅日志不影响任务。
连接用鸭子类型/内存 sqlite，不连真库。
"""

import json
import sqlite3

import pytest
from sqlalchemy.orm import sessionmaker

import server.datagen_service as dsvc
from ptde.models.wire import ColumnMeta, EngineResult, SchemaMetadata, TableStat
from ptde.validator.verify_min import ENUM_LEGAL_MIN, verify_min
from server.datagen_repo import Base, DatagenRepository
from server.datagen_service import DatagenWriteService
from server.db import make_engine


class SqliteDialect:
    """最小 sqlite 测试方言（内省列/主键/外键）。"""

    param = "?"

    def quote_identifier(self, name):
        return '"' + name + '"'

    def fetch_metadata(self, conn, table_name):
        from ptde.models.wire import FkMeta

        meta = SchemaMetadata(table_name=table_name)
        cur = conn.cursor()
        try:
            cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name = ?",
                        (table_name,))
            ddl = (cur.fetchone() or ("",))[0] or ""
            cur.execute(f"PRAGMA table_info({table_name})")
            for _cid, name, ctype, notnull, _dflt, pk in cur.fetchall():
                meta.columns.append(ColumnMeta(
                    name=name, type_name=ctype, nullable=not notnull,
                    auto_increment=pk == 1 and "AUTOINCREMENT" in ddl.upper()))
                if pk:
                    meta.primary_keys.add(name.lower())
            cur.execute(f"PRAGMA foreign_key_list({table_name})")
            fk_rows = cur.fetchall()
        finally:
            cur.close()
        for _id, seq, pk_table, fk_col, pk_col, *_ in fk_rows:
            if seq == 0:
                meta.foreign_keys.append(FkMeta(fk_col, pk_table, pk_col))
        return meta


# ---------------------------------------------------------------- verify_min


def _verify_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE perf_user (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "status TEXT)")
    conn.execute("CREATE TABLE perf_order (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "user_id BIGINT, status VARCHAR(16), "
                 "CONSTRAINT fk_o_u FOREIGN KEY (user_id) REFERENCES perf_user(id))")
    return conn


class MysqlLikeDialect(SqliteDialect):
    """模拟 MySQL：perf_user.status 的 TYPE_NAME 为内联 ENUM（sqlite DDL 不支持）。"""

    ENUM_TYPE = "ENUM('ACTIVE','FROZEN')"

    def fetch_metadata(self, conn, table_name):
        meta = super().fetch_metadata(conn, table_name)
        if table_name == "perf_user":
            for col in meta.columns:
                if col.name == "status":
                    col.type_name = self.ENUM_TYPE
        return meta


def _seed(conn, user_rows=30, order_valid=40, order_orphan=1, order_null=2):
    for _ in range(user_rows):
        conn.execute("INSERT INTO perf_user (status) VALUES (?)", ("ACTIVE",))
    for i in range(1, order_valid + 1):
        conn.execute("INSERT INTO perf_order (user_id, status) VALUES (?, ?)",
                     ((i % user_rows) + 1, "PAID"))
    for _ in range(order_orphan):
        conn.execute("INSERT INTO perf_order (user_id, status) VALUES (?, ?)",
                     (999999, "PAID"))
    for _ in range(order_null):
        conn.execute("INSERT INTO perf_order (user_id, status) VALUES (?, ?)",
                     (None, "PAID"))
    conn.commit()


def test_verify_min_all_pass():
    conn = _verify_db()
    _seed(conn, order_orphan=0)
    report = verify_min(conn, MysqlLikeDialect(), [
        {"table": "perf_user", "expected": 30},
        {"table": "perf_order", "expected": 42},
    ])
    assert report["passed"] is True
    kinds = [c["kind"] for c in report["checks"]]
    assert kinds.count("row_count") == 2
    assert kinds.count("fk_orphan") == 1
    assert kinds.count("enum_legal") == 1  # 仅 perf_user.status 为内联 ENUM
    fk = next(c for c in report["checks"] if c["kind"] == "fk_orphan")
    assert fk["orphans"] == 0 and fk["parent"] == "perf_user.id"
    assert fk["sampled"] == 40, "非空外键全部纳入抽检（NULL 不计孤儿）"


def test_verify_min_row_count_mismatch_fails():
    conn = _verify_db()
    _seed(conn)
    report = verify_min(conn, MysqlLikeDialect(), [{"table": "perf_user", "expected": 100}])
    rc = report["checks"][0]
    assert rc["kind"] == "row_count" and rc["actual"] == 30 and rc["ok"] is False
    assert report["passed"] is False


def test_verify_min_fk_orphan_fails():
    conn = _verify_db()
    _seed(conn)
    report = verify_min(conn, MysqlLikeDialect(), [{"table": "perf_order", "expected": 43}])
    fk = next(c for c in report["checks"] if c["kind"] == "fk_orphan")
    assert fk["orphans"] == 1 and fk["ok"] is False
    assert report["passed"] is False


def test_verify_min_enum_legal_rate():
    conn = _verify_db()
    _seed(conn)
    report = verify_min(conn, MysqlLikeDialect(), [{"table": "perf_order", "expected": 43}])
    # perf_order.status 为 VARCHAR（无内联枚举）：无 enum_legal 检查项
    assert [c["kind"] for c in report["checks"]].count("enum_legal") == 0
    conn.execute("INSERT INTO perf_user (status) VALUES ('ILLEGAL')")
    conn.commit()
    report2 = verify_min(conn, MysqlLikeDialect(), [{"table": "perf_user", "expected": 31}])
    en = next(c for c in report2["checks"] if c["kind"] == "enum_legal")
    assert en["legalRate"] < ENUM_LEGAL_MIN and en["ok"] is False
    assert report2["passed"] is False


def test_verify_min_missing_table_recorded_failed():
    conn = _verify_db()
    report = verify_min(conn, MysqlLikeDialect(), [{"table": "nope", "expected": 1}])
    assert report["passed"] is False
    assert report["checks"][0]["error"] == "表不存在或无可见列"


def test_verify_min_empty_tables_pass():
    conn = _verify_db()
    report = verify_min(conn, MysqlLikeDialect(), [])
    assert report == {"passed": True, "checks": [], "sampleLimit": 200}


def test_verify_min_row_count_baseline_delta_passes():
    """目标库已有存量时，行数核对按本次新增行数（COUNT(*) - baseline）。"""
    conn = _verify_db()
    _seed(conn, user_rows=30, order_orphan=0)
    for _ in range(10):
        conn.execute("INSERT INTO perf_user (status) VALUES ('ACTIVE')")
    conn.commit()
    report = verify_min(conn, MysqlLikeDialect(), [
        {"table": "perf_user", "expected": 10, "baseline": 30},
    ])
    rc = report["checks"][0]
    assert report["passed"] is True
    assert rc["mode"] == "increment" and rc["baseline"] == 30
    assert rc["actual"] == 10 and rc["actualTotal"] == 40


def test_verify_min_row_count_baseline_delta_fails():
    conn = _verify_db()
    _seed(conn, user_rows=30, order_orphan=0)
    report = verify_min(conn, MysqlLikeDialect(), [
        {"table": "perf_user", "expected": 10, "baseline": 35},
    ])
    rc = report["checks"][0]
    assert report["passed"] is False
    assert rc["mode"] == "increment" and rc["actual"] == 0 and rc["ok"] is False


# ---------------------------------------------------------------- 服务挂钩


class SyncExecutor:
    def submit(self, fn, *args):
        fn(*args)


class StubEngine:
    """成功桩：返回固定 EngineResult（验证由真实 sqlite conn 完成）。"""

    def execute(self, spec, listener=None):
        r = EngineResult()
        r.success = True
        r.dry_run = spec.dry_run
        r.total_rows = sum(t.row_count for t in spec.tables)
        r.inserted_rows = 0 if spec.dry_run else r.total_rows
        r.table_stats = [TableStat(t.table_name, t.row_count) for t in spec.tables]
        return r


class InsertingStubEngine(StubEngine):
    """在目标 sqlite 文件库中按比例插入测试行，模拟真实灌数后的增量行数。"""

    def __init__(self, db_file, insert_factor: float = 1.0):
        self.db_file = db_file
        self.insert_factor = insert_factor

    def execute(self, spec, listener=None):
        if not spec.dry_run:
            conn = sqlite3.connect(str(self.db_file))
            try:
                for t in spec.tables:
                    rows = int(t.row_count * self.insert_factor)
                    for _ in range(rows):
                        conn.execute("INSERT INTO user (status) VALUES ('ACTIVE')")
                conn.commit()
            finally:
                conn.close()
        return super().execute(spec, listener)


def _svc_with_sqlite(rows_in_table: int, tmp_path, insert_factor: float | None = None):
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    import server.collector_repo  # noqa: F401
    import server.datagen_repo  # noqa: F401
    Base.metadata.create_all(factory.kw["bind"])
    # 文件库：水位捕获/范围落库/验证各自建连（对齐生产每步独立连接语义）
    db_file = tmp_path / "target.db"
    engine = (InsertingStubEngine(db_file, insert_factor)
              if insert_factor is not None else StubEngine())
    svc = DatagenWriteService(DatagenRepository(factory), engine=engine,
                              executor=SyncExecutor())
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE user (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "status VARCHAR(16))")
    for _ in range(rows_in_table):
        conn.execute("INSERT INTO user (status) VALUES ('ACTIVE')")
    conn.commit()
    conn.close()
    return svc, db_file


def _sqlite_connect(db_file):
    return lambda db_url, user, pwd: (sqlite3.connect(str(db_file)), MysqlLikeDialect())


def test_verify_hook_report_passed_and_status_finished(monkeypatch, tmp_path):
    svc, db_file = _svc_with_sqlite(rows_in_table=10, tmp_path=tmp_path,
                                    insert_factor=1.0)
    monkeypatch.setattr(dsvc, "_connect", _sqlite_connect(db_file))
    data = svc.create_and_run({"dbUrl": "jdbc:mysql://stub/db",
                               "tableName": "user", "rowCount": 10})
    task = svc._repo.task_by_id(data["id"])
    assert task.status == "finished"
    detail = json.loads(task.detail_json)
    assert detail["verifyMin"]["passed"] is True
    rc = next(c for c in detail["verifyMin"]["checks"] if c["kind"] == "row_count")
    assert rc["mode"] == "increment" and rc["baseline"] == 10
    assert rc["expected"] == 10 and rc["actual"] == 10 and rc["actualTotal"] == 20


def test_verify_hook_failure_marks_status_explicitly(monkeypatch, tmp_path):
    """行数不符：verifyMin.passed=false + 状态 finished_verify_failed（不伪装成功）。"""
    svc, db_file = _svc_with_sqlite(rows_in_table=5, tmp_path=tmp_path,
                                    insert_factor=0.5)
    monkeypatch.setattr(dsvc, "_connect", _sqlite_connect(db_file))
    data = svc.create_and_run({"dbUrl": "jdbc:mysql://stub/db",
                               "tableName": "user", "rowCount": 10})
    task = svc._repo.task_by_id(data["id"])
    assert task.status == "finished_verify_failed"
    assert task.error_msg is None, "数据已灌入，不并入失败路径"
    detail = json.loads(task.detail_json)
    assert detail["verifyMin"]["passed"] is False
    rc = next(c for c in detail["verifyMin"]["checks"] if c["kind"] == "row_count")
    assert rc["mode"] == "increment" and rc["baseline"] == 5
    assert rc["expected"] == 10 and rc["actual"] == 5 and rc["ok"] is False


def test_verify_hook_connect_failure_invisible_to_task(tmp_path):
    """目标库不可达：验证跳过（仅日志），任务不受影响、无 verifyMin 键。"""
    svc, _db_file = _svc_with_sqlite(rows_in_table=0, tmp_path=tmp_path)

    def _boom(db_url, user, pwd):
        raise dsvc._ConnectError("refused")

    orig = dsvc._connect
    dsvc._connect = _boom
    try:
        data = svc.create_and_run({"dbUrl": "jdbc:mysql://stub/db",
                                   "tableName": "user", "rowCount": 10})
    finally:
        dsvc._connect = orig
    task = svc._repo.task_by_id(data["id"])
    assert task.status == "finished"
    detail = json.loads(task.detail_json)
    assert "verifyMin" not in detail


def test_verify_hook_skipped_for_dry_run(monkeypatch, tmp_path):
    svc, db_file = _svc_with_sqlite(rows_in_table=10, tmp_path=tmp_path)
    monkeypatch.setattr(dsvc, "_connect", _sqlite_connect(db_file))
    data = svc.create_and_run({"dbUrl": "jdbc:mysql://stub/db",
                               "tableName": "user", "rowCount": 10,
                               "dryRun": True})
    task = svc._repo.task_by_id(data["id"])
    assert task.status == "finished"
    detail = json.loads(task.detail_json)
    assert "verifyMin" not in detail and "dryRunReport" in detail


@pytest.mark.parametrize("rows,expected,insert_factor,status", [
    (10, 10, 1.0, "finished"),
    (5, 10, 0.5, "finished_verify_failed")])
def test_verify_status_matrix(monkeypatch, tmp_path, rows, expected, insert_factor,
                              status):
    svc, db_file = _svc_with_sqlite(rows_in_table=rows, tmp_path=tmp_path,
                                    insert_factor=insert_factor)
    monkeypatch.setattr(dsvc, "_connect", _sqlite_connect(db_file))
    data = svc.create_and_run({"dbUrl": "jdbc:mysql://stub/db",
                               "tables": [{"tableName": "user",
                                           "rowCount": expected}]})
    assert svc._repo.task_by_id(data["id"]).status == status
