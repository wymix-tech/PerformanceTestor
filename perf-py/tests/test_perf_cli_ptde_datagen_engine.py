"""PTDE 路由灌数引擎协议级测试（B7 续批次 10：T7.6/T7.7 接管切换）。

覆盖（FakeConn 目标库 + sqlite 元数据库，不连真库）：
- 方言路由（非 jdbc:mysql:// 显式失败，legacy「未注册匹配方言」文案）；
- 表缺失/非法表名显式失败（legacy 同文案）；
- dry-run：完整内省/装配不写库，估算公式与 tableStats；
- 正式执行：追加区间（existing_max）+ 自增列剔除 + 装载行数记账；
- ref 语义：父表在任务清单 → 关系四元组声明（零孤儿 span）；
  存量父表 → 存量 ID 域采样（不声明关系）；
- orphan_rate 缺省链 → 关系降级 logical（故意孤儿）；
- seq 规则（非 id 列）row_index 语义（worker row_index 上下文接线）；
- seed 缺省随机分配落库（可追溯）+ 0 行表跳过。
"""

import json

from sqlalchemy.orm import sessionmaker

import server.realdb_pipeline  # noqa: F401  monkeypatch 目标
from perf_cli.ptde_datagen_engine import PtdeDatagenEngine
from ptde.models.wire import DataGenSpec, FieldRule, TableSpec
from server.db import Base, make_engine
from server.ptde_repo import PtdeDatasetDefinitionModel, PtdeDatasetRepository

JDBC = "jdbc:mysql://127.0.0.1:3306/demo"


def _col(name, data_type, nullable="YES", key="", extra=""):
    return (name, data_type, nullable, key, extra)


class FakeCursor:
    """``%s`` paramstyle 假游标：内省/MAX/ID 域/装载探测/INSERT 记账。"""

    def __init__(self, conn) -> None:
        self._conn = conn
        self.rowcount = 0
        self._rows: list[tuple] = []

    def execute(self, sql, params=None) -> None:
        conn = self._conn
        conn.executed.append(" ".join(sql.split()))
        s = " ".join(sql.split()).strip()
        self._rows = []
        if "information_schema.COLUMNS" in s:
            self._rows = list(conn.columns.get(params[0], []))
        elif s.startswith("SELECT @@local_infile"):
            self._rows = [(0,)]  # 原生档不可用 → 降级链语义
        elif s.startswith("SELECT @@secure_file_priv"):
            self._rows = [(None,)]  # SERVER INFILE 不可用 → 降级链语义
        elif s.startswith("SELECT MAX("):
            self._rows = [(conn.maxes.get(_table_of(s)),)]
        elif s.startswith("SELECT MIN("):
            self._rows = [conn.domains.get(_table_of(s), (None, None))]

    def executemany(self, sql, batch) -> None:
        s = sql.strip()
        self.rowcount = len(batch)
        if s.startswith("INSERT INTO"):
            table = s.split()[2]
            self._conn.rows.setdefault(table, []).extend(batch)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self) -> None:
        pass


def _table_of(sql: str) -> str:
    after = sql.split(" FROM ", 1)[1]
    return after.split(" WHERE ", 1)[0].strip()


class FakeConn:
    """MySQL paramstyle 假连接（协议级；行记账供断言）。"""

    def __init__(self) -> None:
        self.columns: dict[str, list[tuple]] = {}
        self.maxes: dict[str, int | None] = {}
        self.domains: dict[str, tuple] = {}
        self.rows: dict[str, list[tuple]] = {}
        self.executed: list[str] = []
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass


def _repo() -> PtdeDatasetRepository:
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"),
                           expire_on_commit=False)
    import server.ptde_repo  # noqa: F401  注册模型

    Base.metadata.create_all(factory.kw["bind"])
    return PtdeDatasetRepository(factory)


def _engine(holder, tmp_path, repo=None) -> PtdeDatagenEngine:
    return PtdeDatagenEngine(
        repo=repo or _repo(), conn_factory=lambda: holder["conn"],
        staging_base=tmp_path / "staging")


def _capture_pipeline(monkeypatch) -> dict:
    """捕获 build_realdb_pipeline 装配 kwargs（relations/existing_max）。"""
    captured: dict = {}
    real = server.realdb_pipeline.build_realdb_pipeline

    def spy(repo, dsn, fields, **kw):
        captured["relations"] = kw.get("relations")
        captured["existing_max"] = kw.get("existing_max")
        return real(repo, dsn, fields, **kw)

    monkeypatch.setattr(server.realdb_pipeline, "build_realdb_pipeline", spy)
    return captured


# ---------------------------------------------------------------- 方言路由/校验

def test_execute_rejects_non_mysql_jdbc_url(tmp_path):
    engine = PtdeDatagenEngine(conn_factory=lambda: FakeConn())
    spec = DataGenSpec(jdbc_url="jdbc:oracle://h/db", db_user="u", db_password="p",
                       dry_run=True, tables=[TableSpec("t", 1)])
    result = engine.execute(spec)
    assert result.success is False
    assert "未注册匹配方言" in result.error_msg


def test_execute_rejects_postgres_url(tmp_path):
    engine = PtdeDatagenEngine(conn_factory=lambda: FakeConn())
    spec = DataGenSpec(jdbc_url="jdbc:postgresql://h/db", db_user="u", db_password="p",
                       dry_run=True, tables=[TableSpec("t", 1)])
    result = engine.execute(spec)
    assert result.success is False
    assert "仅支持 jdbc:mysql://" in result.error_msg


def test_execute_rejects_invalid_table_name(tmp_path):
    engine = PtdeDatagenEngine(conn_factory=lambda: FakeConn())
    spec = DataGenSpec(jdbc_url=JDBC, dry_run=True,
                       tables=[TableSpec("bad-name", 1)])
    result = engine.execute(spec)
    assert result.success is False
    assert result.error_msg == "表名仅允许字母数字下划线: bad-name"


def test_execute_rejects_empty_tables(tmp_path):
    engine = PtdeDatagenEngine(conn_factory=lambda: FakeConn())
    result = engine.execute(DataGenSpec(jdbc_url=JDBC, dry_run=True, tables=[]))
    assert result.success is False and result.error_msg == "表组为空"


def test_missing_table_fails_explicitly(tmp_path):
    conn = FakeConn()  # 未预置任何表 → 内省空
    engine = PtdeDatagenEngine(conn_factory=lambda: conn)
    spec = DataGenSpec(jdbc_url=JDBC, dry_run=True,
                       tables=[TableSpec("ghost", 10)])
    result = engine.execute(spec)
    assert result.success is False
    assert result.error_msg == "表不存在或无可见列: ghost"


# ---------------------------------------------------------------- dry-run

def test_dry_run_estimates_without_writes(tmp_path):
    conn = FakeConn()
    conn.columns["t_user"] = [
        _col("id", "bigint", key="PRI"), _col("name", "varchar")]
    engine = PtdeDatagenEngine(conn_factory=lambda: conn)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", dry_run=True,
                       seed=7, tables=[TableSpec("t_user", 5000,
                                                 fields={"name": FieldRule(
                                                     type=FieldRule.TYPE_RANGE,
                                                     min=1, max=9)})])
    result = engine.execute(spec)
    assert result.success is True and result.dry_run is True
    assert result.total_rows == 5000 and result.inserted_rows == 0
    assert result.estimated_max_txn_ms == (5000 // 500) * 5
    assert [(s.table, s.rows) for s in result.table_stats] == [("t_user", 5000)]
    assert not any(s.startswith("INSERT INTO") for s in conn.executed), \
        "dry-run 不得写库（legacy Dry-Run 优先同口径）"


# ---------------------------------------------------------------- 正式执行

def test_run_appends_after_existing_max(tmp_path):
    conn = FakeConn()
    conn.columns["t_user"] = [
        _col("id", "bigint", key="PRI"), _col("name", "varchar")]
    conn.maxes["t_user"] = 100
    holder = {"conn": conn}
    engine = _engine(holder, tmp_path)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=7,
                       tables=[TableSpec("t_user", 10, fields={
                           "name": FieldRule(type=FieldRule.TYPE_RANGE,
                                             min=1, max=9)})])
    result = engine.execute(spec)
    assert result.success is True, result.error_msg
    assert result.inserted_rows == 10 and result.total_rows == 10
    rows = conn.rows["t_user"]
    assert len(rows) == 10
    # staging 文件格式往返数值为字符串（MySQL/LOAD DATA 文本装载同口径强制转换）
    assert [int(r[0]) for r in rows] == list(range(101, 111)), \
        "id 主键自存量最大值 +1 续区间（planned_range 追加语义）"
    assert any("已有存量主键" in w for w in result.warnings)


def test_auto_increment_pk_excluded_from_insert(tmp_path):
    conn = FakeConn()
    conn.columns["t_order"] = [
        _col("id", "bigint", key="PRI",
             extra="auto_increment"), _col("amount", "decimal")]
    conn.maxes["t_order"] = 40
    holder = {"conn": conn}
    engine = _engine(holder, tmp_path)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=7,
                       tables=[TableSpec("t_order", 10, fields={
                           "amount": FieldRule(type=FieldRule.TYPE_RANGE,
                                               min=1, max=100)})])
    result = engine.execute(spec)
    assert result.success is True, result.error_msg
    rows = conn.rows["t_order"]
    assert len(rows) == 10 and all(len(r) == 1 for r in rows), \
        "自增列不进字段清单（DB 自动分配，legacy insert_columns 同口径）"


def test_ref_parent_in_task_declares_relation_with_zero_orphan_span(
        tmp_path, monkeypatch):
    conn = FakeConn()
    conn.columns["t_user"] = [_col("id", "bigint", key="PRI")]
    conn.columns["t_order"] = [
        _col("id", "bigint", key="PRI"), _col("user_id", "bigint")]
    conn.maxes["t_user"] = 0
    conn.maxes["t_order"] = 0
    holder = {"conn": conn}
    captured = _capture_pipeline(monkeypatch)
    engine = _engine(holder, tmp_path)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=7,
                       tables=[
                           TableSpec("t_user", 5),
                           TableSpec("t_order", 10, fields={
                               "user_id": FieldRule(type=FieldRule.TYPE_REF,
                                                    table="t_user",
                                                    column="id")})])
    result = engine.execute(spec)
    assert result.success is True, result.error_msg
    assert result.inserted_rows == 15
    assert captured["relations"] == [{
        "child_table": "t_order", "child_column": "user_id",
        "parent_table": "t_user", "parent_column": "id",
        "relation_kind": "db_fk"}]
    fk = [int(r[1]) for r in conn.rows["t_order"]]
    assert all(1 <= v <= 5 for v in fk), \
        f"任务内父表 → 零孤儿 planned span 采样，得到 {fk}"


def test_ref_parent_outside_task_uses_existing_domain(tmp_path, monkeypatch):
    conn = FakeConn()
    conn.columns["t_order"] = [
        _col("id", "bigint", key="PRI"), _col("user_id", "bigint")]
    conn.maxes["t_order"] = 0
    conn.domains["t_user"] = (3, 9)  # 存量父表 ID 域
    holder = {"conn": conn}
    captured = _capture_pipeline(monkeypatch)
    engine = _engine(holder, tmp_path)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=7,
                       tables=[TableSpec("t_order", 10, fields={
                           "user_id": FieldRule(type=FieldRule.TYPE_REF,
                                                table="t_user",
                                                column="id")})])
    result = engine.execute(spec)
    assert result.success is True, result.error_msg
    assert not captured["relations"], "存量父表不声明关系（父表不重建）"
    fk = [int(r[1]) for r in conn.rows["t_order"]]
    assert all(3 <= v <= 9 for v in fk), \
        f"存量父表 → 存量 ID 域采样，得到 {fk}"
    assert any("存量父表" in w for w in result.warnings)


def test_ref_parent_outside_task_missing_domain_fails(tmp_path):
    conn = FakeConn()
    conn.columns["t_order"] = [
        _col("id", "bigint", key="PRI"), _col("user_id", "bigint")]
    conn.maxes["t_order"] = 0
    # 未预置 t_user 域 → MIN/MAX 无返回（空表）
    engine = PtdeDatagenEngine(conn_factory=lambda: conn)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=7,
                       tables=[TableSpec("t_order", 5, fields={
                           "user_id": FieldRule(type=FieldRule.TYPE_REF,
                                                table="t_user",
                                                column="id")})])
    result = engine.execute(spec)
    assert result.success is False
    assert "父表 t_user 不存在或无存量数据" in result.error_msg


def test_orphan_rate_chain_downgrades_relation_to_logical(tmp_path, monkeypatch):
    conn = FakeConn()
    conn.columns["t_user"] = [_col("id", "bigint", key="PRI")]
    conn.columns["t_order"] = [
        _col("id", "bigint", key="PRI"),
        _col("user_id", "bigint", nullable="YES")]
    conn.maxes["t_user"] = 0
    conn.maxes["t_order"] = 0
    holder = {"conn": conn}
    captured = _capture_pipeline(monkeypatch)
    engine = _engine(holder, tmp_path)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p",
                       orphan_rate=0.5, seed=7,
                       tables=[
                           TableSpec("t_user", 5),
                           TableSpec("t_order", 10, fields={
                               "user_id": FieldRule(type=FieldRule.TYPE_REF,
                                                    table="t_user",
                                                    column="id")})])
    result = engine.execute(spec)
    assert result.success is True, result.error_msg
    assert captured["relations"][0]["relation_kind"] == "logical", \
        "orphan_rate>0（故意孤儿）→ 关系降级 logical（verify WARN 口径）"
    fk = [None if r[1] is None else int(r[1]) for r in conn.rows["t_order"]]
    assert any(v is None or v > 5 for v in fk), \
        f"故意孤儿语义落地（NULL/无效 ID），得到 {fk}"
    assert any("orphan_rate>0" in w for w in result.warnings)


def test_seq_rule_non_id_column_advances_per_row(tmp_path):
    conn = FakeConn()
    conn.columns["t_user"] = [
        _col("id", "bigint", key="PRI"), _col("code", "varchar")]
    conn.maxes["t_user"] = 0
    holder = {"conn": conn}
    engine = _engine(holder, tmp_path)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=1,
                       tables=[TableSpec("t_user", 5, fields={
                           "code": FieldRule(type=FieldRule.TYPE_SEQ,
                                             seq_prefix="U-", seq_start=1)})])
    result = engine.execute(spec)
    assert result.success is True, result.error_msg
    codes = [r[1] for r in conn.rows["t_user"]]
    assert codes == ["U-1", "U-2", "U-3", "U-4", "U-5"], \
        f"非 id 列 seq 语义逐值一致（row_index 上下文接线），得到 {codes}"


def test_seed_random_assignment_persisted(tmp_path):
    conn = FakeConn()
    conn.columns["t_user"] = [_col("id", "bigint", key="PRI")]
    conn.maxes["t_user"] = 0
    repo = _repo()
    engine = PtdeDatagenEngine(repo=repo, conn_factory=lambda: conn,
                               staging_base=None)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=None,
                       tables=[TableSpec("t_user", 3)])
    result = engine.execute(spec)
    assert result.success is True, result.error_msg
    with repo._sessions() as s:
        rows = s.query(PtdeDatasetDefinitionModel).all()
    assert len(rows) == 1 and rows[0].seed is not None, \
        "seed 缺省 → 随机分配并落 definition.seed（可追溯复现）"
    payload = json.loads(rows[0].spec_json)
    assert payload["reproducibility"]["seed"] == rows[0].seed


def test_zero_row_table_skipped(tmp_path, monkeypatch):
    conn = FakeConn()
    conn.columns["t_user"] = [_col("id", "bigint", key="PRI")]
    conn.columns["t_order"] = [
        _col("id", "bigint", key="PRI"), _col("amount", "decimal")]
    conn.maxes["t_user"] = 0
    conn.maxes["t_order"] = 0
    holder = {"conn": conn}
    captured = _capture_pipeline(monkeypatch)
    engine = _engine(holder, tmp_path)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=7,
                       tables=[
                           TableSpec("t_user", 0),
                           TableSpec("t_order", 5, fields={
                               "amount": FieldRule(type=FieldRule.TYPE_RANGE,
                                                   min=1, max=9)})])
    result = engine.execute(spec)
    assert result.success is True, result.error_msg
    assert result.inserted_rows == 5
    assert "t_user" not in conn.rows, "0 行表跳过（legacy no-op 同口径）"
    assert captured["existing_max"] is None or "t_user" not in \
        (captured["existing_max"] or {})
