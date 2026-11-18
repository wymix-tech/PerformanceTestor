"""QW1 Seed 可复现单测：random.Random(seed) 实例注入 + 哈希对照。

验收口径（落地方案 §6.1 / 实施方案 §11 QW1）：
- 相同 spec + seed 两次执行，灌入数据逐行一致（sha256 哈希对照）；
- 不同 seed 产出不同数据（证明 seed 真实生效）；
- seed=None 时与现行为完全一致（模块级 random 调用序列不变，固定
  random.seed 后两次执行哈希一致）；
- spec.json 仅新增可选字段 seed，缺省不出现（契约不破坏）。

测试方言复用 test_datagen_engine 的 sqlite 内省思路（不连真库）。
"""

import hashlib
import json
import random
import sqlite3

import pytest
from typer.testing import CliRunner

from datagen.default_engine import DefaultDataGenEngine
from datagen.dialects.mysql import MysqlDialect
from datagen.executor import DbApiBatchExecutor
from datagen.model import ColumnMeta, DataGenSpec, FieldRule, TableSpec
from datagen.rule_engine import apply_rule, generate_default, random_text, weighted_index
from perf_cli.config import EXIT_OK
from perf_cli.datagen_runner import spec_from_request
from perf_cli.main import app
from ptde_common.id_pool import IdPool

runner = CliRunner()

SEED_A = 42
SEED_B = 7


class SqliteDialect(MysqlDialect):
    """测试方言：元数据经 PRAGMA 内省（同 test_datagen_engine）。"""

    param = "?"

    def supports(self, jdbc_url):
        return jdbc_url is not None and jdbc_url.startswith("jdbc:sqlite:")

    def enrich_jdbc_url(self, jdbc_url):
        return jdbc_url

    def connect(self, jdbc_url, user, password):
        return sqlite3.connect(jdbc_url[len("jdbc:sqlite:"):])

    def fetch_metadata(self, conn, table_name):
        from datagen.model import FkMeta, SchemaMetadata

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
        cur = conn.cursor()
        try:
            cur.execute(f"PRAGMA table_info({table_name})")
            for _cid, name, _ctype, _notnull, _dflt, pk in cur.fetchall():
                if pk:
                    meta.primary_keys.add(name.lower())
        finally:
            cur.close()
        for _id, seq, pk_table, fk_col, pk_col, *_ in fk_rows:
            if seq == 0:
                meta.foreign_keys.append(FkMeta(fk_col, pk_table, pk_col))
        meta.row_count = self.table_row_count(conn, table_name)
        return meta


def _prepare_db(path):
    """建库建表（无 DATETIME 列——now() 注入的时间字段不在可复现口径内）。"""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE perf_user (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "name VARCHAR(32), age INT, score DECIMAL(10,2), vip BIT)")
    conn.execute("CREATE TABLE perf_order (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "user_id BIGINT NOT NULL, amount DECIMAL(12,2), status VARCHAR(16), "
                 "remark VARCHAR(64), seq_code VARCHAR(32), "
                 "CONSTRAINT fk_o_u FOREIGN KEY (user_id) REFERENCES perf_user(id))")
    conn.commit()
    conn.close()


def _make_spec(db_file: str) -> DataGenSpec:
    s = DataGenSpec()
    s.jdbc_url = f"jdbc:sqlite:{db_file}"
    s.tables = [TableSpec(table_name="perf_user", row_count=120),
                TableSpec(table_name="perf_order", row_count=300)]
    s.tables[1].fields["user_id"] = FieldRule(type=FieldRule.TYPE_REF,
                                              table="perf_user", column="id")
    s.tables[1].fields["status"] = FieldRule.fixed("PAID")
    s.tables[1].fields["amount"] = FieldRule(
        type=FieldRule.TYPE_RANGE, min=1, max=999,
        over_threshold=None)
    s.tables[1].fields["seq_code"] = FieldRule(type=FieldRule.TYPE_SEQ,
                                               seq_prefix="S", seq_start=1)
    s.orphan_rate = 0.1  # 覆盖无效 ID 采样路径（池 rng 驱动）
    return s


def _run_gen(db_file: str, seed) -> str:
    """执行一次灌数，返回全表行集的 sha256（逐行按 rowid 排序拼接）。"""
    _prepare_db(db_file)
    engine = DefaultDataGenEngine(SqliteDialect(), DbApiBatchExecutor(), None)
    spec = _make_spec(db_file)
    spec.seed = seed
    r = engine.execute(spec, None)
    assert r.success, r.error_msg
    conn = sqlite3.connect(str(db_file))
    try:
        digest = hashlib.sha256()
        for table in ("perf_user", "perf_order"):
            cur = conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
            for row in cur.fetchall():
                digest.update(repr(row).encode())
    finally:
        conn.close()
    return digest.hexdigest()


def test_same_seed_reproduces_identical_rows(tmp_path):
    """相同 spec + seed 两次执行：灌入数据逐行一致（哈希对照）。"""
    h1 = _run_gen(str(tmp_path / "a.db"), SEED_A)
    h2 = _run_gen(str(tmp_path / "b.db"), SEED_A)
    assert h1 == h2


def test_different_seed_produces_different_rows(tmp_path):
    h1 = _run_gen(str(tmp_path / "a.db"), SEED_A)
    h2 = _run_gen(str(tmp_path / "b.db"), SEED_B)
    assert h1 != h2


def test_seed_none_uses_module_random_unchanged(tmp_path):
    """seed=None：固定 random.seed 后两次执行哈希一致——证明 seed=None
    路径仍走模块级 random 且调用序列与现行为完全一致（未被注入路径扰动）。"""
    random.seed(2026)
    h1 = _run_gen(str(tmp_path / "a.db"), None)
    random.seed(2026)
    h2 = _run_gen(str(tmp_path / "b.db"), None)
    assert h1 == h2


def test_seed_none_is_default_and_spec_shape_unchanged():
    """spec.json 新增可选字段 seed：缺省不出现 → seed=None（契约不破坏）。"""
    spec = spec_from_request({"dbUrl": "jdbc:mysql://h/d",
                              "tables": [{"tableName": "t", "rowCount": 1}]})
    assert spec.seed is None
    spec2 = spec_from_request({"dbUrl": "jdbc:mysql://h/d", "seed": 42,
                               "tables": [{"tableName": "t", "rowCount": 1}]})
    assert spec2.seed == 42
    with pytest.raises(ValueError, match="seed 必须是整数"):
        spec_from_request({"dbUrl": "jdbc:mysql://h/d", "seed": "abc",
                           "tables": [{"tableName": "t", "rowCount": 1}]})


def test_cli_seed_override(tmp_path, monkeypatch):
    """CLI --seed 透传：覆盖 spec 内 seed 字段。"""
    import perf_cli.datagen_runner as dr

    captured = {}

    class FakeEngine:
        def execute(self, spec, listener=None):
            captured["seed"] = spec.seed
            r = dr.EngineResult()
            r.success = True
            r.dry_run = spec.dry_run
            r.total_rows = 1
            r.inserted_rows = 0
            return r

    monkeypatch.setattr(dr, "build_default_engine", lambda: FakeEngine())
    p = tmp_path / "spec.json"
    p.write_text(json.dumps({"dbUrl": "jdbc:mysql://127.0.0.1:1/db", "dryRun": True,
                             "tables": [{"tableName": "perf_user", "rowCount": 1}]}),
                 encoding="utf-8")
    result = runner.invoke(app, ["datagen", "run", str(p), "--seed", str(SEED_A)])
    assert result.exit_code == EXIT_OK
    assert captured["seed"] == SEED_A


# ---------------------------------------------------------------- 单元层


def test_apply_rule_rng_injection_deterministic():
    col = ColumnMeta(name="age", type_name="INT")
    rule = FieldRule(type=FieldRule.TYPE_RANGE, min=0, max=100)
    a = [apply_rule(rule, col, 0, random.Random(7)) for _ in range(50)]
    b = [apply_rule(rule, col, 0, random.Random(7)) for _ in range(50)]
    assert a == b


def test_apply_rule_none_rng_uses_module_random():
    """rng 缺省 → 模块级 random（现行为）：固定种子下可复现。"""
    col = ColumnMeta(name="age", type_name="INT")
    rule = FieldRule(type=FieldRule.TYPE_RANGE, min=0, max=100)
    random.seed(11)
    a = [apply_rule(rule, col, 0) for _ in range(50)]
    random.seed(11)
    b = [apply_rule(rule, col, 0) for _ in range(50)]
    assert a == b


def test_enum_weighted_and_text_rng():
    rule = FieldRule(type=FieldRule.TYPE_ENUM, values=["A", "B"], weights=[1, 9])
    ra, rb = random.Random(3), random.Random(3)
    a = [apply_rule(rule, ColumnMeta(name="s", type_name="VARCHAR"), 0, ra)
         for _ in range(100)]
    b = [apply_rule(rule, ColumnMeta(name="s", type_name="VARCHAR"), 0, rb)
         for _ in range(100)]
    assert a == b and set(a) == {"A", "B"}
    assert random_text(16, random.Random(5)) == random_text(16, random.Random(5))
    assert weighted_index([1, 1], random.Random(9)) == weighted_index([1, 1], random.Random(9))


def test_generate_default_rng_none_fallback():
    col = ColumnMeta(name="n", type_name="VARCHAR", size=8)
    random.seed(1)
    a = [generate_default(col) for _ in range(10)]
    random.seed(1)
    b = [generate_default(col) for _ in range(10)]
    assert a == b


def test_id_pool_rng_injection_deterministic():
    a = IdPool(random.Random(1))
    b = IdPool(random.Random(1))
    for i in range(100):
        a.add(i)
        b.add(i)
    assert [a.sample_uniform() for _ in range(50)] == [b.sample_uniform() for _ in range(50)]
    a.build_skew_weights(20, 80)
    b.build_skew_weights(20, 80)
    assert [a.sample_skewed() for _ in range(50)] == [b.sample_skewed() for _ in range(50)]
    assert [a.sample_invalid_id() for _ in range(10)] == [b.sample_invalid_id() for _ in range(10)]


def test_id_pool_default_none_rng_module_random():
    """rng=None 的池：模块级 random（现行为），固定种子可复现。"""
    random.seed(3)
    p = IdPool()
    p.add_all(list(range(100)))
    a = [p.sample_uniform() for _ in range(50)]
    random.seed(3)
    p2 = IdPool()
    p2.add_all(list(range(100)))
    b = [p2.sample_uniform() for _ in range(50)]
    assert a == b
