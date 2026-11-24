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
import re

from sqlalchemy.orm import sessionmaker

import server.realdb_pipeline  # noqa: F401  monkeypatch 目标
from perf_cli.ptde_datagen_engine import PtdeDatagenEngine
from ptde.models.wire import DataGenSpec, FieldRule, TableSpec
from server.db import Base, make_engine
from server.ptde_repo import PtdeDatasetDefinitionModel, PtdeDatasetRepository

JDBC = "jdbc:mysql://127.0.0.1:3306/demo"


def _col(name, data_type, nullable="YES", key="", extra="", char_len=0):
    return (name, data_type, nullable, key, extra, char_len)


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
        elif "information_schema.TABLES" in s:
            self._rows = [(t,) for t in conn.tables]
        elif "information_schema.KEY_COLUMN_USAGE" in s:
            self._rows = list(conn.fks.get(params[0], []))
        elif s.startswith("SELECT @@local_infile"):
            self._rows = [(0,)]  # 原生档不可用 → 降级链语义
        elif s.startswith("SELECT @@secure_file_priv"):
            self._rows = [(None,)]  # SERVER INFILE 不可用 → 降级链语义
        elif s.startswith("SELECT MAX("):
            self._rows = [(conn.maxes.get(_table_of(s)),)]
        elif s.startswith("SELECT COUNT("):
            self._rows = [(conn.counts.get(_table_of(s)),)]
        elif s.startswith("SELECT MIN("):
            lo, hi = conn.domains.get(_table_of(s), (None, None))
            cnt = conn.counts.get(_table_of(s))
            if cnt is None and lo is not None:
                cnt = hi - lo + 1  # 未显式给行数 → 按稠密域默认
            self._rows = [(lo, hi, cnt)]
        elif s.startswith("SELECT DISTINCT"):
            table = _table_of(s)
            self._rows = [(v,) for v in conn.id_values.get(table, [])]
        elif s.startswith("SELECT ") and ">= %s" in s:
            # 探点邻近回填：WHERE col >= 随机点 ORDER BY col LIMIT n
            table = _table_of(s)
            limit = int(re.search(r"LIMIT (\d+)", s).group(1))
            point = params[0] if params else 0
            vals = sorted(v for v in conn.id_values.get(table, []) if v >= point)
            self._rows = [(v,) for v in vals[:limit]]
        elif s.startswith("SELECT ") and "ORDER BY" in s and "LIMIT" in s:
            # 真实值池全量/截断抽取：ORDER BY col LIMIT n（整型或字符串）
            table = _table_of(s)
            limit = int(re.search(r"LIMIT (\d+)", s).group(1))
            self._rows = [(v,) for v in conn.id_values.get(table, [])[:limit]]

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
        self.counts: dict[str, int] = {}  # 域行数（缺省按 domain 跨度=稠密）
        self.id_values: dict[str, list[int]] = {}  # DISTINCT 真实 ID（稀疏域）
        self.tables: list[str] = []  # 目标库基表清单（命名推断候选域）
        self.fks: dict[str, list[tuple]] = {}
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
        captured["pk_columns"] = kw.get("pk_columns")
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


def test_auto_fk_parent_in_task_declares_relation(tmp_path, monkeypatch):
    conn = FakeConn()
    conn.columns["t_user"] = [_col("id", "bigint", key="PRI")]
    conn.columns["t_order"] = [
        _col("id", "bigint", key="PRI"), _col("user_id", "bigint")]
    conn.maxes["t_user"] = 0
    conn.maxes["t_order"] = 0
    conn.fks["t_order"] = [("user_id", "t_user", "id")]
    holder = {"conn": conn}
    captured = _capture_pipeline(monkeypatch)
    engine = _engine(holder, tmp_path)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=7,
                       tables=[TableSpec("t_user", 5),
                               TableSpec("t_order", 10)])
    result = engine.execute(spec)
    assert result.success is True, result.error_msg
    assert captured["relations"] == [{
        "child_table": "t_order", "child_column": "user_id",
        "parent_table": "t_user", "parent_column": "id",
        "relation_kind": "db_fk"}]
    fk = [int(r[1]) for r in conn.rows["t_order"]]
    assert all(1 <= v <= 5 for v in fk), \
        f"自动生成 + 任务内父表 → planned span 采样，得到 {fk}"


def test_auto_fk_parent_auto_increment_pk_passes_relation_gate(tmp_path, monkeypatch):
    """父表在任务内且主键自增（字段清单按 legacy 排除该列）→ 关系校验
    不得误判「父列不在映射字段中」；dry-run 与正式执行同口径。"""
    conn = FakeConn()
    conn.columns["t_user"] = [
        _col("id", "bigint", key="PRI", extra="auto_increment"),
        _col("name", "varchar")]
    conn.columns["t_order"] = [
        _col("id", "bigint", key="PRI"), _col("user_id", "bigint")]
    conn.maxes["t_user"] = 0
    conn.maxes["t_order"] = 0
    conn.fks["t_order"] = [("user_id", "t_user", "id")]
    holder = {"conn": conn}
    captured = _capture_pipeline(monkeypatch)
    engine = _engine(holder, tmp_path)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=7,
                       tables=[TableSpec("t_user", 5), TableSpec("t_order", 10)])
    import dataclasses
    dry = engine.execute(dataclasses.replace(spec, dry_run=True))
    assert dry.success is True, dry.error_msg
    result = engine.execute(spec)
    assert result.success is True, result.error_msg
    assert captured["relations"][0]["parent_column"] == "id"


def test_auto_fk_parent_outside_task_uses_existing_domain(tmp_path, monkeypatch):
    conn = FakeConn()
    conn.columns["t_order"] = [
        _col("id", "bigint", key="PRI"), _col("user_id", "bigint")]
    conn.maxes["t_order"] = 0
    conn.domains["t_user"] = (3, 9)
    conn.fks["t_order"] = [("user_id", "t_user", "id")]
    holder = {"conn": conn}
    captured = _capture_pipeline(monkeypatch)
    engine = _engine(holder, tmp_path)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=7,
                       tables=[TableSpec("t_order", 10)])
    result = engine.execute(spec)
    assert result.success is True, result.error_msg
    assert not captured["relations"], "存量父表不声明关系（父表不重建）"
    fk = [int(r[1]) for r in conn.rows["t_order"]]
    assert all(3 <= v <= 9 for v in fk), \
        f"自动生成 + 存量父表 → 存量 ID 域采样，得到 {fk}"
    assert any("数据库外键" in w for w in result.warnings)


def test_auto_fk_sparse_parent_domain_samples_real_ids(tmp_path, monkeypatch):
    """存量父表 ID 域稀疏（失败批次回滚烧号/删数造成空洞）→ 按真实 ID
    全量抽样（空洞永不命中，uniform 必中空洞触发外键 1452 的回归防线）。"""
    conn = FakeConn()
    conn.columns["t_order"] = [
        _col("id", "bigint", key="PRI"), _col("user_id", "bigint")]
    conn.maxes["t_order"] = 0
    conn.domains["t_user"] = (3, 9)
    conn.counts["t_user"] = 3  # 跨度 7 只有 3 行 → 稀疏
    conn.id_values["t_user"] = [3, 5, 9]
    conn.fks["t_order"] = [("user_id", "t_user", "id")]
    holder = {"conn": conn}
    captured = _capture_pipeline(monkeypatch)
    engine = _engine(holder, tmp_path)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=7,
                       tables=[TableSpec("t_order", 10)])
    result = engine.execute(spec)
    assert result.success is True, result.error_msg
    assert not captured["relations"]
    fk = [int(r[1]) for r in conn.rows["t_order"]]
    assert set(fk) <= {3, 5, 9}, \
        f"稀疏域须按真实 ID 全量抽样（不命中空洞），得到 {fk}"
    assert any("稀疏" in w and "真实 ID" in w for w in result.warnings)


def test_auto_fk_snowflake_parent_domain_probe_sampling(tmp_path, monkeypatch):
    """大跨度稀疏域（雪花/随机 ID，跨度 > 全量上限）→ 均匀探点统计抽样
    真实 ID（FK 值全部真实；uniform 必中空洞，此为 1452 回归防线）。"""
    conn = FakeConn()
    conn.columns["t_order"] = [
        _col("id", "bigint", key="PRI"), _col("user_id", "bigint")]
    conn.maxes["t_order"] = 0
    base = 683_952_738_000_000_000
    snow_ids = [base + i * 500_000 for i in range(500)]  # 跨度 2.5e8 > 全量上限
    conn.domains["t_user"] = (snow_ids[0], snow_ids[-1])
    conn.counts["t_user"] = 500
    conn.id_values["t_user"] = snow_ids
    conn.fks["t_order"] = [("user_id", "t_user", "id")]
    holder = {"conn": conn}
    captured = _capture_pipeline(monkeypatch)
    engine = _engine(holder, tmp_path)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=7,
                       tables=[TableSpec("t_order", 10)])
    result = engine.execute(spec)
    assert result.success is True, result.error_msg
    assert not captured["relations"]
    fk = [int(r[1]) for r in conn.rows["t_order"]]
    assert set(fk) <= set(snow_ids), \
        f"雪花域须按真实 ID 统计抽样（不命中空洞），得到 {fk[:5]}…"
    assert any("统计抽样" in w for w in result.warnings)


def test_auto_fk_probe_no_hit_fails_explicitly(tmp_path):
    """探点抽样未命中任何存量 ID → 显式失败（不伪装可用）。"""
    conn = FakeConn()
    conn.columns["t_order"] = [
        _col("id", "bigint", key="PRI"), _col("user_id", "bigint")]
    conn.maxes["t_order"] = 0
    conn.domains["t_user"] = (0, 10 ** 18)
    conn.counts["t_user"] = 5  # id_values 未预置 → 探点无命中（防御路径）
    conn.fks["t_order"] = [("user_id", "t_user", "id")]
    engine = PtdeDatagenEngine(conn_factory=lambda: conn)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=7,
                       tables=[TableSpec("t_order", 5)])
    result = engine.execute(spec)
    assert result.success is False
    assert "探点抽样未命中" in result.error_msg


def test_ref_parent_sparse_domain_samples_real_ids(tmp_path, monkeypatch):
    """显式 ref 规则 + 存量父表稀疏域（无热行/孤儿参数）→ 真实 ID 全量抽样。"""
    conn = FakeConn()
    conn.columns["t_order"] = [
        _col("id", "bigint", key="PRI"), _col("user_id", "bigint")]
    conn.maxes["t_order"] = 0
    conn.domains["t_user"] = (3, 9)
    conn.counts["t_user"] = 3
    conn.id_values["t_user"] = [3, 5, 9]
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
    fk = [int(r[1]) for r in conn.rows["t_order"]]
    assert set(fk) <= {3, 5, 9}, \
        f"ref 稀疏域须按真实 ID 全量抽样，得到 {fk}"
    assert any("真实 ID" in w for w in result.warnings)


def test_auto_fk_varchar_guid_parent_samples_real_values(tmp_path, monkeypatch):
    """非整型父列（varchar GUID 主键）→ 按存量真实值池抽样（形态一致，
    值全部真实——MIN/MAX 数值域语义不适用于非整型）。"""
    conn = FakeConn()
    conn.columns["t_order"] = [
        _col("id", "bigint", key="PRI"), _col("user_id", "varchar")]
    conn.columns["t_user"] = [
        _col("id", "varchar", key="PRI"), _col("name", "varchar")]
    conn.maxes["t_order"] = 0
    guid_values = ["u-0c9f3a", "u-7b21de", "u-ff8001"]
    conn.counts["t_user"] = 3
    conn.id_values["t_user"] = guid_values
    conn.fks["t_order"] = [("user_id", "t_user", "id")]
    holder = {"conn": conn}
    captured = _capture_pipeline(monkeypatch)
    engine = _engine(holder, tmp_path)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=7,
                       tables=[TableSpec("t_order", 10)])
    result = engine.execute(spec)
    assert result.success is True, result.error_msg
    assert not captured["relations"]
    fk = [str(r[1]) for r in conn.rows["t_order"]]
    assert set(fk) <= set(guid_values), \
        f"GUID 父列须按真实值池抽样，得到 {fk}"
    assert any("非整型父列" in w for w in result.warnings)


def test_task_entity_non_int_pk_fails_explicitly(tmp_path):
    """任务内非整型单列主键 → 显式失败（hash 整数伪装不可取，指引存量
    父表通道：确保父表已有存量数据后只灌子表）。"""
    conn = FakeConn()
    conn.columns["t_user"] = [
        _col("id", "varchar", key="PRI"), _col("name", "varchar")]
    engine = PtdeDatagenEngine(conn_factory=lambda: conn)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=7,
                       tables=[TableSpec("t_user", 5)])
    result = engine.execute(spec)
    assert result.success is False
    assert "主键列 id 为非整型" in result.error_msg
    assert "存量" in result.error_msg


def test_naming_inference_without_db_fk_samples_parent(tmp_path, monkeypatch):
    """数据库未声明外键 + 自动生成 → 命名约定推断逻辑外键（user_id →
    t_user.uid，父主键任意命名），子列按父表存量稠密域抽样。"""
    conn = FakeConn()
    conn.columns["t_order"] = [
        _col("id", "bigint", key="PRI"), _col("user_id", "bigint")]
    conn.columns["t_user"] = [
        _col("uid", "bigint", key="PRI"), _col("name", "varchar")]
    conn.tables = ["t_order", "t_user"]
    conn.maxes["t_order"] = 0
    conn.domains["t_user"] = (3, 9)  # 稠密（行数缺省=跨度）
    conn.fks = {}  # 未声明数据库外键
    holder = {"conn": conn}
    captured = _capture_pipeline(monkeypatch)
    engine = _engine(holder, tmp_path)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=7,
                       tables=[TableSpec("t_order", 10)])
    result = engine.execute(spec)
    assert result.success is True, result.error_msg
    fk = [int(r[1]) for r in conn.rows["t_order"]]
    assert all(3 <= v <= 9 for v in fk), \
        f"命名推断逻辑外键须按父表存量域抽样，得到 {fk}"
    assert any("命名约定推断" in w and "t_user.uid" in w
               for w in result.warnings)


def test_naming_inference_ambiguous_candidates_skips(tmp_path, monkeypatch):
    """命名推断候选父表不唯一（t_user / user 同库并存）→ 不猜测，
    普通字段生成并留痕。"""
    conn = FakeConn()
    conn.columns["t_order"] = [
        _col("id", "bigint", key="PRI"), _col("user_id", "bigint")]
    conn.tables = ["t_order", "t_user", "user"]
    conn.maxes["t_order"] = 0
    holder = {"conn": conn}
    engine = _engine(holder, tmp_path)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=7,
                       tables=[TableSpec("t_order", 5)])
    result = engine.execute(spec)
    assert result.success is True, result.error_msg
    assert any("候选父表不唯一" in w for w in result.warnings)


def test_naming_inference_empty_parent_falls_back(tmp_path, monkeypatch):
    """命名推断命中父表但无存量数据 → 留痕降级普通字段，不阻断任务。"""
    conn = FakeConn()
    conn.columns["t_order"] = [
        _col("id", "bigint", key="PRI"), _col("user_id", "bigint")]
    conn.tables = ["t_order", "t_user"]
    conn.columns["t_user"] = [_col("id", "bigint", key="PRI")]
    conn.maxes["t_order"] = 0
    conn.fks = {}
    engine = PtdeDatagenEngine(conn_factory=lambda: conn)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=7,
                       tables=[TableSpec("t_order", 5)])
    result = engine.execute(spec)
    assert result.success is True, result.error_msg
    assert any("父表无存量数据" in w for w in result.warnings)


def test_auto_fk_parent_missing_domain_fails(tmp_path):
    conn = FakeConn()
    conn.columns["t_order"] = [
        _col("id", "bigint", key="PRI"), _col("user_id", "bigint")]
    conn.maxes["t_order"] = 0
    conn.fks["t_order"] = [("user_id", "t_user", "id")]
    engine = PtdeDatagenEngine(conn_factory=lambda: conn)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=7,
                       tables=[TableSpec("t_order", 5)])
    result = engine.execute(spec)
    assert result.success is False
    assert "父表 t_user 不存在或无存量数据" in result.error_msg


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


def test_non_auto_non_id_pk_planned_range_takeover(tmp_path, monkeypatch):
    """技术债清理：非 id 名非自增主键由 planned_range 接管（区间自存量 +1）。"""
    conn = FakeConn()
    conn.columns["t_user"] = [
        _col("code", "bigint", key="PRI"), _col("name", "varchar")]
    conn.maxes["t_user"] = 40
    holder = {"conn": conn}
    captured = _capture_pipeline(monkeypatch)
    engine = _engine(holder, tmp_path)
    spec = DataGenSpec(jdbc_url=JDBC, db_user="u", db_password="p", seed=7,
                       tables=[TableSpec("t_user", 5)])
    result = engine.execute(spec)
    assert result.success is True, result.error_msg
    rows = conn.rows["t_user"]
    assert [int(r[0]) for r in rows] == [41, 42, 43, 44, 45], \
        "主键列 code 自存量最大值 +1 续区间（planned_range 接管）"
    assert (captured["pk_columns"] or {}).get("t_user") == "code", \
        "主键列名按内省显式下传（非 id 名泛化）"
    assert any("planned_range 接管" in w for w in result.warnings), \
        "接管决策留痕告警"
