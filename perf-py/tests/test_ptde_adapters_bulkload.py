"""T5.3/T5.4 原生批量装载适配器测试（实施方案 §8.2；总方案 §9.3/§9.4）。

覆盖：
- T5.3 MySQL：LOCAL INFILE 可用性判定（服务端/客户端开关）、行数核对
  （短装显式失败不静默丢弃——QW4）、警告样例、触发器/分区预检、csv_v1
  文件头核对、标识符校验；
- T5.4 PG：COPY FROM STDIN 流式装载（跳声明+列头）、RLS 显式拒绝、
  INSERT 权限、触发器说明、失败 VACUUM 空间清理提示。

全部走鸭子类型假连接（不连真库）；分片文件用 file_format.write_shard_file
真实落盘（csv_v1 契约收编，勿另起一套）。
"""

from __future__ import annotations

import pytest

from ptde.adapters.base import BulkLoadError, BulkLoadUnsupported
from ptde.adapters.mysql.bulkload import MysqlBulkLoadAdapter
from ptde.adapters.postgres.bulkload import PostgresBulkLoadAdapter, ShardTextStream
from ptde.generator.file_format import write_shard_file


class MysqlFakeCursor:
    """LOAD DATA 路由假游标：rowcount 由 marker 驱动。"""

    def __init__(self, routes: list[tuple[str, list[tuple]]], load_rowcount: int,
                 executed_log: list[str] | None = None) -> None:
        self._routes = routes
        self._load_rowcount = load_rowcount
        self._log = executed_log if executed_log is not None else []
        self._result: list[tuple] = []
        self.rowcount = 0

    def execute(self, sql: str, params: tuple = ()) -> None:  # noqa: ARG002
        self._log.append(sql)
        self._result = []
        if sql.startswith("LOAD DATA LOCAL INFILE"):
            self.rowcount = self._load_rowcount
            return
        for marker, rows in self._routes:
            if marker in sql:
                self._result = rows
                return

    def fetchall(self) -> list[tuple]:
        return self._result

    def fetchone(self) -> tuple | None:
        return self._result[0] if self._result else None

    def close(self) -> None:
        pass


class MysqlFakeConn:
    def __init__(self, routes: list[tuple[str, list[tuple]]], load_rowcount: int,
                 local_infile: bool | None = True) -> None:
        self._routes = routes
        self._load_rowcount = load_rowcount
        self.local_infile = local_infile
        self.committed = 0
        self.executed_sql: list[str] = []

    def cursor(self) -> MysqlFakeCursor:
        return MysqlFakeCursor(self._routes, self._load_rowcount,
                               executed_log=self.executed_sql)

    def commit(self) -> None:
        self.committed += 1


MYSQL_OK_ROUTES = [
    ("EVENT_OBJECT_TABLE", [("trg_audit", "AFTER", "INSERT")]),
    ("information_schema.PARTITIONS", [("p2026",), ("p2027",)]),
    ("SELECT @@local_infile", [("1",)]),
    ("SELECT @@secure_file_priv", [("/var/lib/mysql-files/",)]),
    ("SHOW COUNT(*) WARNINGS", [(2,)]),
    ("SHOW WARNINGS", [("Warning", 1265, "Data truncated for column 'status'")]),
]


def _make_shard(tmp_path, rows=None):
    path = tmp_path / "shard_0.csv"
    meta = write_shard_file(
        path,
        ["id", "name", "note"],
        rows if rows is not None else [
            ["1", "alice", "plain"],
            ["2", "bob", "with,comma"],
            ["3", "carol", None],  # \N → NULL
        ],
    )
    return str(path), meta.rows


# ---------- T5.3 MySQL：可用性判定 ----------


def test_mysql_local_infile_server_off_unsupported(tmp_path) -> None:
    conn = MysqlFakeConn([("SELECT @@local_infile", [("0",)])], 0)
    with pytest.raises(BulkLoadUnsupported, match="服务端 @@local_infile=OFF"):
        MysqlBulkLoadAdapter().ensure_capable(conn)


def test_mysql_local_infile_unprobeable_fails(tmp_path) -> None:
    conn = MysqlFakeConn([], 0)
    with pytest.raises(BulkLoadUnsupported, match="无法读取 @@local_infile"):
        MysqlBulkLoadAdapter().ensure_capable(conn)


def test_mysql_client_local_infile_off_unsupported(tmp_path) -> None:
    conn = MysqlFakeConn([("SELECT @@local_infile", [("1",)])], 0, local_infile=False)
    with pytest.raises(BulkLoadUnsupported, match="客户端未启用"):
        MysqlBulkLoadAdapter().ensure_capable(conn)


# ---------- T5.3 MySQL：装载 ----------


def test_mysql_load_shard_success_with_notes(tmp_path) -> None:
    path, rows = _make_shard(tmp_path)
    conn = MysqlFakeConn(MYSQL_OK_ROUTES, load_rowcount=rows)
    result = MysqlBulkLoadAdapter().load_shard(
        conn, path, "orders", ["id", "name", "note"], rows,
        shard_id="s0", entity="orders",
    )
    assert result.rows_loaded == rows and result.rows_verified
    assert result.warnings_count == 2  # SHOW COUNT(*) WARNINGS 可见不吞
    assert result.warning_samples == ["[Warning 1265] Data truncated for column 'status'"]
    notes = "\n".join(result.precheck_notes)
    assert "trg_audit(AFTER INSERT)" in notes  # 触发器影响说明
    assert "2 个分区" in notes  # 分区影响说明
    assert "secure_file_priv='/var/lib/mysql-files/'" in notes
    assert "不经服务端文件白名单" in notes  # QW4 口径
    assert conn.committed == 1


def test_mysql_load_sql_shape(tmp_path) -> None:
    path, rows = _make_shard(tmp_path)
    conn = MysqlFakeConn(MYSQL_OK_ROUTES, load_rowcount=rows)
    MysqlBulkLoadAdapter().load_shard(conn, path, "orders", ["id", "name", "note"], rows)
    load_sql = next(s for s in conn.executed_sql if s.startswith("LOAD DATA"))
    assert "IGNORE 2 LINES (id, name, note)" in load_sql  # 跳声明+列头
    assert "OPTIONALLY ENCLOSED BY '\"'" in load_sql  # RFC4180 引号字段


def test_mysql_row_mismatch_raises_with_samples(tmp_path) -> None:
    path, rows = _make_shard(tmp_path)  # 3 行
    conn = MysqlFakeConn(MYSQL_OK_ROUTES, load_rowcount=rows - 1)  # 短装 1 行
    with pytest.raises(BulkLoadError) as exc:
        MysqlBulkLoadAdapter().load_shard(
            conn, path, "orders", ["id", "name", "note"], rows, shard_id="s0"
        )
    assert "期望 3 行，实际装载 2 行" in str(exc.value)
    assert "不静默丢弃" in str(exc.value)  # QW4：错误记录可见
    assert exc.value.error_samples  # 错误样例回传
    assert exc.value.rows_expected == 3 and exc.value.rows_loaded == 2


def test_mysql_invalid_identifier_fails(tmp_path) -> None:
    path, rows = _make_shard(tmp_path)
    conn = MysqlFakeConn(MYSQL_OK_ROUTES, load_rowcount=rows)
    with pytest.raises(Exception, match="非法标识符"):
        MysqlBulkLoadAdapter().load_shard(
            conn, path, "orders; DROP TABLE x", ["id"], rows
        )


def test_mysql_bad_shard_header_fails(tmp_path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("id,name\n1,a\n", encoding="utf-8")
    conn = MysqlFakeConn(MYSQL_OK_ROUTES, 0)
    with pytest.raises(BulkLoadError, match="csv_v1"):
        MysqlBulkLoadAdapter().load_shard(conn, str(path), "orders", ["id"], 1)


def test_mysql_load_statement_failure_wrapped(tmp_path) -> None:
    path, rows = _make_shard(tmp_path)
    conn = MysqlFakeConn(MYSQL_OK_ROUTES, load_rowcount=rows)
    orig_execute = MysqlFakeCursor.execute

    def _boom(self, sql, params=()):
        if sql.startswith("LOAD DATA"):
            raise RuntimeError("server has gone away")
        return orig_execute(self, sql, params)

    from unittest.mock import patch

    with patch.object(MysqlFakeCursor, "execute", _boom):
        with pytest.raises(BulkLoadError, match="LOAD DATA 执行失败"):
            MysqlBulkLoadAdapter().load_shard(
                conn, path, "orders", ["id", "name", "note"], rows
            )


# ---------- T5.4 PG：预检 ----------


class PgFakeCursor:
    def __init__(self, routes: list[tuple[str, list[tuple]]], copy_rows: int) -> None:
        self._routes = routes
        self._copy_rows = copy_rows
        self._result: list[tuple] = []
        self.rowcount = 0
        self.copied_text: str | None = None
        self.copied_sql: str | None = None

    def execute(self, sql: str, params: tuple = ()) -> None:  # noqa: ARG002
        self._result = []
        for marker, rows in self._routes:
            if marker in sql:
                self._result = rows
                return

    def copy_expert(self, sql: str, stream) -> None:
        self.copied_sql = sql
        self.copied_text = stream.read()
        self.rowcount = self._copy_rows

    def fetchall(self) -> list[tuple]:
        return self._result

    def fetchone(self) -> tuple | None:
        return self._result[0] if self._result else None

    def close(self) -> None:
        pass


class PgFakeConn:
    def __init__(self, routes: list[tuple[str, list[tuple]]], copy_rows: int) -> None:
        self._routes = routes
        self._copy_rows = copy_rows
        self.committed = 0
        self.last_cursor: PgFakeCursor | None = None

    def cursor(self) -> PgFakeCursor:
        cur = PgFakeCursor(self._routes, self._copy_rows)
        self.last_cursor = cur
        return cur

    def commit(self) -> None:
        self.committed += 1


PG_OK_ROUTES = [
    ("current_schema()", [(True,)]),
    ("has_table_privilege", [(True,)]),
    ("relrowsecurity", [(False, False)]),
    ("tgisinternal", [("trg_audit",)]),
]


# ---------- T5.4 PG：流式装载 ----------


def test_pg_load_shard_success_streaming(tmp_path) -> None:
    path, rows = _make_shard(tmp_path)
    conn = PgFakeConn(PG_OK_ROUTES, copy_rows=rows)
    result = PostgresBulkLoadAdapter().load_shard(
        conn, path, "orders", ["id", "name", "note"], rows,
        shard_id="s0", entity="orders",
    )
    assert result.rows_loaded == rows and result.rows_verified
    assert result.strategy == "copy_from_stdin"
    cur = conn.last_cursor
    assert cur.copied_sql == (
        "COPY orders (id, name, note) FROM STDIN WITH (FORMAT csv, NULL '\\N')"
    )
    # 流式剥离声明+列头：直送数据体（NULL token 与空串区分保留）
    assert cur.copied_text.splitlines() == [
        "1,alice,plain",
        "2,bob,\"with,comma\"",
        "3,carol,\\N",
    ]
    notes = "\n".join(result.precheck_notes)
    assert "trg_audit" in notes  # 触发器说明回传


def test_pg_rls_enabled_refused(tmp_path) -> None:
    path, rows = _make_shard(tmp_path)
    conn = PgFakeConn([
        ("has_table_privilege", [(True,)]),
        ("relrowsecurity", [(True, True)]),
    ], 0)
    with pytest.raises(BulkLoadError) as exc:
        PostgresBulkLoadAdapter().load_shard(
            conn, path, "orders", ["id"], rows, shard_id="s0"
        )
    assert "行级安全" in str(exc.value)
    assert "RLS 策略豁免" in str(exc.value)  # 处置建议可见
    assert exc.value.cleanup_hint


def test_pg_insert_privilege_denied(tmp_path) -> None:
    path, rows = _make_shard(tmp_path)
    conn = PgFakeConn([("has_table_privilege", [(False,)])], 0)
    with pytest.raises(BulkLoadUnsupported, match="无 INSERT 权限"):
        PostgresBulkLoadAdapter().load_shard(conn, path, "orders", ["id"], rows)


def test_pg_table_not_found(tmp_path) -> None:
    path, rows = _make_shard(tmp_path)
    conn = PgFakeConn([("has_table_privilege", [("t",)])], 0)  # pg_class 无行
    with pytest.raises(BulkLoadError, match="不存在"):
        PostgresBulkLoadAdapter().load_shard(conn, path, "ghost", ["id"], rows)


def test_pg_copy_failure_carries_vacuum_hint(tmp_path) -> None:
    path, rows = _make_shard(tmp_path)
    conn = PgFakeConn(PG_OK_ROUTES, 0)

    def _boom(self, sql, stream):
        raise RuntimeError("check constraint violated")

    from unittest.mock import patch

    with patch.object(PgFakeCursor, "copy_expert", _boom):
        with pytest.raises(BulkLoadError) as exc:
            PostgresBulkLoadAdapter().load_shard(
                conn, path, "orders", ["id", "name", "note"], rows, shard_id="s0"
            )
    assert "COPY 装载失败" in str(exc.value)
    assert "VACUUM" in str(exc.value)  # 失败空间清理提示（§8.2）
    assert "check constraint violated" in str(exc.value)


def test_pg_rowcount_mismatch_carries_vacuum_hint(tmp_path) -> None:
    path, rows = _make_shard(tmp_path)  # 3 行
    conn = PgFakeConn(PG_OK_ROUTES, copy_rows=rows - 1)
    with pytest.raises(BulkLoadError) as exc:
        PostgresBulkLoadAdapter().load_shard(
            conn, path, "orders", ["id", "name", "note"], rows, shard_id="s0"
        )
    assert "期望 3 行，实际装载 2 行" in str(exc.value)
    assert "VACUUM" in str(exc.value)


def test_pg_ensure_capable_schema_unreachable() -> None:
    conn = PgFakeConn([("current_schema()", [(False,)])], 0)
    with pytest.raises(BulkLoadUnsupported, match="current_schema"):
        PostgresBulkLoadAdapter().ensure_capable(conn)


# ---------- ShardTextStream（流式包装器） ----------


def test_shard_text_stream_skips_header_and_streams(tmp_path) -> None:
    path, _rows = _make_shard(tmp_path)
    stream = ShardTextStream(path, skip_lines=2)
    assert "format_version=csv_v1" not in stream.read()  # 声明行已剥离
    assert "id,name,note" not in stream.read()  # 列头已剥离（流已消费）
    stream.close()
    assert stream.closed is True


def test_shard_text_stream_chunked_reads(tmp_path) -> None:
    path, _rows = _make_shard(tmp_path)
    stream = ShardTextStream(path, skip_lines=2)
    chunks: list[str] = []
    while (chunk := stream.read(4)):
        chunks.append(chunk)
    assert "".join(chunks).splitlines() == ["1,alice,plain", "2,bob,\"with,comma\"", "3,carol,\\N"]
    stream.close()
