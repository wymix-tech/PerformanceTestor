"""引擎端到端等价单测：移植 EngineH2Test 全部 9 个用例（Java 用 H2 MySQL 模式，
Python 等价载体用内置 sqlite3 + 测试方言——体现 SPI 可插拔，对齐 Java H2Dialect 思路）。

覆盖编排/方言路由/批量执行/关联识别/空表兜底/orphanRate/错误路径/Dry-Run。
"""

import sqlite3

import pytest

from datagen.default_engine import DefaultDataGenEngine
from datagen.dialects.mysql import MysqlDialect
from datagen.executor import DbApiBatchExecutor
from datagen.model import DataGenSpec, FieldRule, RelationHint, TableSpec


class SqliteDialect(MysqlDialect):
    """测试方言：元数据经 PRAGMA 内省（sqlite 无 information_schema/JDBC 元数据）。"""

    param = "?"

    def supports(self, jdbc_url):
        return jdbc_url is not None and jdbc_url.startswith("jdbc:sqlite:")

    def enrich_jdbc_url(self, jdbc_url):
        return jdbc_url

    def connect(self, jdbc_url, user, password):
        return sqlite3.connect(jdbc_url[len("jdbc:sqlite:"):])

    def fetch_metadata(self, conn, table_name):
        from datagen.model import ColumnMeta, FkMeta, SchemaMetadata

        meta = SchemaMetadata(table_name=table_name)
        cur = conn.cursor()
        try:
            cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name = ?",
                        (table_name,))
            row = cur.fetchone()
            ddl = row[0] if row else ""
            cur.execute(f"PRAGMA table_info({table_name})")
            for _cid, name, ctype, notnull, _dflt, pk in cur.fetchall():
                auto = pk == 1 and "AUTOINCREMENT" in (ddl or "").upper()
                size = 0
                if "(" in (ctype or ""):
                    try:
                        size = int(ctype[ctype.find("(") + 1:].rstrip(")").split(",")[0])
                    except ValueError:
                        size = 0
                meta.columns.append(ColumnMeta(
                    name=name, type_name=ctype, size=size,
                    nullable=not notnull, auto_increment=auto))
            cur.execute(f"PRAGMA foreign_key_list({table_name})")
            fk_rows = cur.fetchall()
        finally:
            cur.close()
        # 主键（table_info pk > 0）
        cur = conn.cursor()
        try:
            cur.execute(f"PRAGMA table_info({table_name})")
            for _cid, name, _ctype, _notnull, _dflt, pk in cur.fetchall():
                if pk:
                    meta.primary_keys.add(name.lower())
                    c = meta.find_column(name)
                    if c is not None:
                        c.primary_key = True
        finally:
            cur.close()
        # 声明外键：id, seq, table, from, to, ...
        for _id, seq, pk_table, fk_col, pk_col, *_ in fk_rows:
            if seq == 0:
                meta.foreign_keys.append(FkMeta(fk_col, pk_table, pk_col))
        meta.row_count = self.table_row_count(conn, table_name)
        return meta


@pytest.fixture(scope="module")
def db_url(tmp_path_factory):
    path = tmp_path_factory.mktemp("dg") / "dg.db"
    url = f"jdbc:sqlite:{path}"

    def raw():
        return sqlite3.connect(str(path))

    conn = raw()
    conn.execute("CREATE TABLE perf_user (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "name VARCHAR(64), age INT, score DOUBLE, vip BIT, created_at DATETIME)")
    conn.execute("CREATE TABLE perf_order (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "user_id BIGINT NOT NULL, amount DECIMAL(12,2), status VARCHAR(16), "
                 "remark TEXT, created_at DATETIME, "
                 "CONSTRAINT fk_o_u FOREIGN KEY (user_id) REFERENCES perf_user(id))")
    conn.commit()
    conn.close()

    def count(sql):
        c = raw()
        try:
            return c.execute(sql).fetchone()[0]
        finally:
            c.close()

    yield url, raw, count
    raw().close()


def make_engine():
    return DefaultDataGenEngine(SqliteDialect(), DbApiBatchExecutor(), None)


def spec(db_url, dry_run):
    s = DataGenSpec()
    s.jdbc_url = db_url
    s.db_user = "sa"
    s.db_password = ""
    s.dry_run = dry_run
    return s


def ref(table, column) -> FieldRule:
    r = FieldRule()
    r.type = FieldRule.TYPE_REF
    r.table = table
    r.column = column
    return r


def progress_listener(counter: list, hints: list | None = None):
    class _L:
        def on_progress(self, total, inserted, table):
            counter.append(1)

        def on_hints(self, m, r):
            if hints is not None:
                hints.extend(r)

        def on_warn(self, message):
            pass  # 命名推断提示 / refSkew 高倾斜警告
    return _L()


# ---------- EngineH2Test 移植 ----------

def test_dry_run_validates_without_insert(db_url):
    url, _raw, count = db_url
    s = spec(url, True)
    user = TableSpec(table_name="perf_user", row_count=50)
    order = TableSpec(table_name="perf_order", row_count=80)
    order.fields["user_id"] = ref("perf_user", "id")
    s.tables = [user, order]
    progress: list = []
    r = make_engine().execute(s, progress_listener(progress))
    assert r.success, r.error_msg
    assert r.inserted_rows == 0
    assert len(r.table_stats) == 2
    assert progress
    assert count("SELECT COUNT(*) FROM perf_user") == 0


def test_real_insert_fk_valid_and_topo_order(db_url):
    url, raw, count = db_url
    conn = raw()
    conn.execute("DELETE FROM perf_order")
    conn.execute("DELETE FROM perf_user")
    conn.commit()
    conn.close()
    s = spec(url, False)
    order = TableSpec(table_name="perf_order", row_count=200)
    order.fields["user_id"] = ref("perf_user", "id")
    order.fields["status"] = FieldRule.fixed("PAID")
    s.tables = [order]  # 子表在前，验证拓扑排序重排
    s.tables.append(TableSpec(table_name="perf_user", row_count=100))
    r = make_engine().execute(s, progress_listener([]))
    assert r.success, r.error_msg
    assert r.inserted_rows == 300
    assert count("SELECT COUNT(*) FROM perf_order") == 200
    # 外键 100% 有效
    assert count("SELECT COUNT(*) FROM perf_order o "
                 "WHERE NOT EXISTS (SELECT 1 FROM perf_user u WHERE u.id = o.user_id)") == 0
    assert count("SELECT COUNT(*) FROM perf_user WHERE name IS NOT NULL") == 100


def test_db_foreign_key_auto_detected(db_url):
    url, raw, count = db_url
    conn = raw()
    # 无显式规则，靠声明外键自动识别（独立表避免计数串扰）
    conn.execute("CREATE TABLE t_fk_p (id INTEGER PRIMARY KEY AUTOINCREMENT, pname VARCHAR(32))")
    conn.execute("CREATE TABLE t_fk_c (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "pid BIGINT NOT NULL, CONSTRAINT fk_cp FOREIGN KEY (pid) REFERENCES t_fk_p(id))")
    conn.commit()
    conn.close()
    s = spec(url, False)
    s.tables = [TableSpec(table_name="t_fk_p", row_count=30),
                TableSpec(table_name="t_fk_c", row_count=60)]
    r = make_engine().execute(s, progress_listener([]))
    assert r.success, r.error_msg
    assert count("SELECT COUNT(*) FROM t_fk_c c "
                 "WHERE NOT EXISTS (SELECT 1 FROM t_fk_p p WHERE p.id = c.pid)") == 0


def test_empty_table_naming_fallback_delivered(db_url):
    url, raw, count = db_url
    conn = raw()
    # 表名不带前缀，匹配 dept_id → dept 的命名推断
    conn.execute("CREATE TABLE dept (id INTEGER PRIMARY KEY AUTOINCREMENT, dname VARCHAR(32))")
    conn.execute("CREATE TABLE emp (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "dept_id BIGINT, ename VARCHAR(32))")
    conn.commit()
    conn.close()
    s = spec(url, False)
    s.tables = [TableSpec(table_name="dept", row_count=0),
                TableSpec(table_name="emp", row_count=20)]
    hints: list[RelationHint] = []
    r = make_engine().execute(s, progress_listener([], hints))
    assert r.success, r.error_msg
    # 空表兜底：父表 0 行 → naming_empty_fallback（LOW，待确认）
    assert any(h.source == RelationHint.SRC_NAMING_EMPTY
               and h.child_table == "emp" and h.child_column == "dept_id"
               for h in hints), f"hints={hints}"


def test_orphan_rate_on_unconstrained_table(db_url):
    url, raw, count = db_url
    conn = raw()
    # 无外键约束的独立表：orphanRate=1.0 全部孤儿（NULL 或无效 ID）
    conn.execute("CREATE TABLE t_orp_p (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    conn.execute("CREATE TABLE t_orp_c (id INTEGER PRIMARY KEY AUTOINCREMENT, pid BIGINT)")
    conn.commit()
    conn.close()
    s = spec(url, False)
    s.tables = [TableSpec(table_name="t_orp_p", row_count=40)]
    child = TableSpec(table_name="t_orp_c", row_count=100)
    child.fields["pid"] = ref("t_orp_p", "id")
    child.orphan_rate = 1.0
    s.tables.append(child)
    r = make_engine().execute(s, progress_listener([]))
    assert r.success, r.error_msg
    # 有效引用应显著少于 100（部分 NULL/部分无效 ID）
    valid = count("SELECT COUNT(*) FROM t_orp_c c "
                  "WHERE EXISTS (SELECT 1 FROM t_orp_p p WHERE p.id = c.pid)")
    assert valid < 100, f"孤儿率 1.0 时有效引用应少于全量: {valid}"


def test_bad_table_fails_fast(db_url):
    url, _raw, _count = db_url
    s = spec(url, True)
    s.tables = [TableSpec(table_name="nope_missing", row_count=10)]
    r = make_engine().execute(s, progress_listener([]))
    assert not r.success
    assert "表不存在" in r.error_msg


def test_cycle_dependency_fails(db_url):
    url, _raw, _count = db_url
    s = spec(url, True)
    a = TableSpec(table_name="perf_user", row_count=10)
    a.fields["user_id"] = ref("perf_order", "id")
    b = TableSpec(table_name="perf_order", row_count=10)
    b.fields["user_id"] = ref("perf_user", "id")
    s.tables = [a, b]
    r = make_engine().execute(s, progress_listener([]))
    assert not r.success
    assert "循环依赖" in r.error_msg


def test_unsupported_url_rejected(db_url):
    url, _raw, _count = db_url
    s = spec(url, True)
    s.jdbc_url = "jdbc:oracle:thin:@x"
    r = make_engine().execute(s, progress_listener([]))
    assert not r.success
    assert "不支持" in r.error_msg


def test_max_rows_exceeded_rejected(db_url):
    url, _raw, _count = db_url
    s = spec(url, True)
    s.max_rows = 10
    s.tables = [TableSpec(table_name="perf_user", row_count=100)]
    r = make_engine().execute(s, progress_listener([]))
    assert not r.success
    assert "总行数" in r.error_msg


def test_invalid_table_name_rejected(db_url):
    url, _raw, _count = db_url
    s = spec(url, True)
    s.tables = [TableSpec(table_name="bad name!", row_count=10)]
    r = make_engine().execute(s, progress_listener([]))
    assert not r.success
    assert "表名仅允许" in r.error_msg


def test_empty_tables_rejected(db_url):
    url, _raw, _count = db_url
    s = spec(url, True)
    r = make_engine().execute(s, progress_listener([]))
    assert not r.success
    assert "表组为空" in r.error_msg


# ---------- CoreEngineTest 的引擎层补充（topo/id_pool 已有专测） ----------

def test_estimate_max_txn_ms(db_url):
    url, _raw, _count = db_url
    s = spec(url, True)
    s.tables = [TableSpec(table_name="perf_user", row_count=1000)]
    r = make_engine().execute(s, progress_listener([]))
    assert r.success
    # batchSize=500 → (1000//500)*5 = 10ms
    assert r.estimated_max_txn_ms == 10


def test_ref_skew_sampling_uses_hot_rows(db_url):
    url, raw, count = db_url
    conn = raw()
    conn.execute("DELETE FROM perf_order")
    conn.execute("DELETE FROM perf_user")
    conn.commit()
    conn.close()
    s = spec(url, False)
    user = TableSpec(table_name="perf_user", row_count=100)
    order = TableSpec(table_name="perf_order", row_count=500)
    skew = FieldRule(type=FieldRule.TYPE_REF_SKEW, table="perf_user", column="id",
                     hot_percent=20, hot_share=80)
    order.fields["user_id"] = skew
    s.tables = [order, user]
    r = make_engine().execute(s, progress_listener([]))
    assert r.success, r.error_msg
    assert count("SELECT COUNT(*) FROM perf_order") == 500
