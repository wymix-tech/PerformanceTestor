"""R2 批次 B2 统计画像功能测试（fake DBAPI 连接，不连真库）。

覆盖：
- T2.1 A5 双 DSN：collector 实际使用 profile_dsn；仅配 target_dsn 时回退 +
  warning 进执行清单（可断言）；采集日志记录脱敏凭据身份。
- T2.2 采集器 + MySQL/PG 统计子适配器：采集项覆盖 §5.4 表；SELECT 白名单校验。
- T2.3 采集安全：采样率/行数上限生效；敏感列 SQL 侧哈希分桶；小样本披露保护。
- T2.4 文件来源画像导入：source_type 区分标记、指纹稳定。
- T2.5 测试库自画像 source_type=test_db_self。
- T2.6 Profile Merger：硬约束不覆盖、DatasetSpec 覆盖记账、缺失特征 estimated。
"""

from __future__ import annotations

import json

import pytest

from ptde.adapters.mysql.statistics import StatisticsCollectionError, validate_identifier
from ptde.config import PtdeConfig
from ptde.models.snapshot import (
    ColumnSchema,
    DatabaseSchemaSnapshot,
    ForeignKeySchema,
    TableSchema,
)
from ptde.profiler.collector import (
    SOURCE_TYPE_DB_READONLY,
    SOURCE_TYPE_FILE_UPLOAD,
    SOURCE_TYPE_TEST_DB_SELF,
    StatisticsCollector,
    assert_readonly_sql,
    credential_label,
    fit_zipf,
    is_sensitive_column,
)
from ptde.profiler.file_source import load_statistics_profile
from ptde.profiler.merger import ProfileMerger


class FakeCursor:
    """按 SQL 关键字路由返回行的假游标（鸭子类型，不依赖驱动）。"""

    def __init__(self, routes: list[tuple[str, list[tuple]]]) -> None:
        self._routes = routes

    def execute(self, sql: str, params: tuple = ()) -> None:  # noqa: ARG002
        self._result: list[tuple] = []
        for marker, rows in self._routes:
            if marker in sql:
                self._result = rows
                return
        self._result = []

    def fetchall(self) -> list[tuple]:
        return self._result

    def fetchone(self) -> tuple | None:
        return self._result[0] if self._result else None

    def close(self) -> None:
        pass


class FakeConn:
    def __init__(self, routes: list[tuple[str, list[tuple]]]) -> None:
        self._routes = routes

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._routes)


# ---------- 语句白名单（T2.2 硬约束） ----------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select TABLE_ROWS from information_schema.TABLES",
        "  SELECT COUNT(*) FROM t  ;",
    ],
)
def test_whitelist_accepts_readonly_select(sql):
    assert assert_readonly_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM t",
        "UPDATE t SET a = 1",
        "INSERT INTO t VALUES (1)",
        "DROP TABLE t",
        "TRUNCATE TABLE t",
        "GRANT SELECT ON t TO u",
        "SELECT * FROM t FOR UPDATE",
        "SELECT * FROM t FOR SHARE",
        "SELECT * INTO OUTFILE '/tmp/x' FROM t",
        "SELECT 1; DROP TABLE t",
        "/* stealth */ DELETE FROM t",
        "",
        "  ",
    ],
)
def test_whitelist_rejects_non_readonly(sql):
    with pytest.raises(StatisticsCollectionError):
        assert_readonly_sql(sql)


def test_validate_identifier_rejects_injection():
    assert validate_identifier("orders") == "orders"
    for bad in ["orders; DROP TABLE x", "o`rders", "or der", "orders-", ""]:
        with pytest.raises(StatisticsCollectionError):
            validate_identifier(bad)


# ---------- 凭据脱敏（A5） ----------


def test_credential_label_masks_password():
    dsn = "mysql://ro_user:secret123@prod-host:3306/proddb"
    label = credential_label(dsn)
    assert label == "mysql://ro_user@prod-host:3306/proddb"
    assert "secret123" not in label
    assert credential_label(None) is None
    assert credential_label("mysql://plain-dsn") == "mysql://plain-dsn"


# ---------- T2.1 + T2.2 + T2.3：MySQL 采集主流程 ----------

# 路由顺序敏感：带列名的精确标记必须先于泛化采样标记（内层子查询含同样文本）。
MYSQL_ROUTES = [
    # 表清单 / 行数（approximate）
    ("TABLE_TYPE = 'BASE TABLE'", [("orders",), ("users",)]),
    ("SELECT TABLE_ROWS FROM information_schema.TABLES", [(1000,)]),
    # 列清单
    ("COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS", [
        ("id", "bigint"),
        ("status", "varchar"),
        ("amount", "decimal"),
        ("full_name", "varchar"),
    ]),
    # 外键（关联基数）
    ("REFERENCED_TABLE_NAME IS NOT NULL", [("user_id", "users", "id")]),
    # distinct（精确列标记优先于内层采样子查询）
    ("COUNT(DISTINCT id)", [(1000,)]),
    ("COUNT(DISTINCT status)", [(4,)]),
    ("COUNT(DISTINCT amount)", [(500,)]),
    ("COUNT(DISTINCT full_name)", [(900,)]),
    # NULL 比例
    ("AVG(id IS NULL)", [(0,)]),
    ("AVG(status IS NULL)", [(0.1,)]),
    ("AVG(amount IS NULL)", [(0,)]),
    ("AVG(full_name IS NULL)", [(0.2,)]),
    # Top-K（原值，低基数类别列）
    ("SELECT status, COUNT(*)", [("PAID", 500), ("NEW", 300), ("TINY", 2)]),
    # Top-K（数值列）
    ("SELECT id, COUNT(*)", [(1, 600), (2, 300), (3, 100)]),
    ("SELECT amount, COUNT(*)", [(100, 400), (200, 300), (50, 300)]),
    # 敏感列（SQL 侧 MD5 哈希：distinct/NULL/Top-K 全哈希变体，无原值采样）
    ("COUNT(DISTINCT v) FROM (SELECT MD5(full_name)", [(900,)]),
    ("AVG(v IS NULL) FROM (SELECT MD5(full_name)", [(0.2,)]),
    ("SELECT v AS bucket, COUNT(*)", [
        ("a" * 32, 400), ("b" * 32, 300), ("c" * 32, 200), ("d" * 32, 90), ("e" * 32, 10),
    ]),
    # 原值采样（仅非敏感数值列会走到这里）
    ("SELECT id FROM orders ORDER BY RAND()", [(10,), (20,), (30,), (40,)]),
    ("SELECT amount FROM orders ORDER BY RAND()", [(50,), (100,), (100,), (200,)]),
    # 字符长度分布（敏感列安全项）
    ("CHAR_LENGTH(full_name)", [(3, 20, 7.5)]),
    # 关联基数（父键哈希分桶）+ 父键去重
    ("SELECT MD5(user_id) AS bucket", [("f" * 32, 700), ("0" * 32, 300)]),
    ("COUNT(DISTINCT user_id)", [(800,)]),
]


def _mysql_collector(connect_calls: list[str], **config_kwargs):
    conn = FakeConn(MYSQL_ROUTES)

    def fake_connect(dsn: str):
        connect_calls.append(dsn)
        return conn

    config = PtdeConfig(**config_kwargs)
    return StatisticsCollector(config, connect=fake_connect)


def test_collect_uses_profile_dsn_and_metadata():
    """T2.1/T2.2：双 DSN 分别配置时连接身份正确 + 画像元数据齐全。"""
    calls: list[str] = []
    collector = _mysql_collector(
        calls,
        profile_dsn="mysql://ro_user:secret@prod:3306/prod",
        target_dsn="mysql://rw_user:pw@target:3306/target",
    )
    profile, manifest = collector.collect(tables=["orders"], profile_id="p1")
    # A5：采集连接实际使用 profile_dsn（可断言）
    assert calls == ["mysql://ro_user:secret@prod:3306/prod"]
    assert manifest.fallback_same_value is False
    assert manifest.warnings == []
    # 采集日志只记录脱敏凭据身份
    assert manifest.credential_label == "mysql://ro_user@prod:3306/prod"
    assert "secret" not in manifest.credential_label
    # 画像元数据：collected_at / sampling_rate / accuracy / source_type 非空且随指纹
    assert profile.source.type == SOURCE_TYPE_DB_READONLY
    assert profile.source.database_type == "mysql"
    assert profile.source.collected_at is not None
    assert profile.source.sampling_rate == 1.0
    assert profile.source.accuracy in {"exact", "approximate", "estimated"}
    assert profile.source.credential_label == manifest.credential_label
    assert profile.fingerprint  # 已 seal，随指纹入库
    assert manifest.statements  # 语句审计非空


def test_collect_fallback_warning_in_manifest():
    """T2.1：仅配 target_dsn 时采集可用，A5 回退 warning 进执行清单（可断言）。"""
    calls: list[str] = []
    collector = _mysql_collector(calls, target_dsn="mysql://rw_user:pw@target:3306/db")
    profile, manifest = collector.collect(tables=["orders"])
    assert calls == ["mysql://rw_user:pw@target:3306/db"]  # 回退同值
    assert manifest.fallback_same_value is True
    assert len(manifest.warnings) == 1
    assert "profile_dsn" in manifest.warnings[0]
    assert profile.source.collected_at is not None


def test_mysql_profile_covers_section_54_items():
    """T2.2：采集项覆盖 §5.4 表（行数/基数/NULL/Top-K/直方图/长度/关联基数/倾斜）。"""
    calls: list[str] = []
    collector = _mysql_collector(
        calls, profile_dsn="mysql://ro@prod/prod", target_dsn="mysql://rw@tgt/tgt"
    )
    profile, manifest = collector.collect(tables=["orders"])
    tp = profile.tables[0]
    assert tp.table == "orders"
    assert tp.approximate_rows == 1000 and tp.row_count_accuracy == "approximate"
    cols = {c.column: c for c in tp.columns}
    assert set(cols) == {"id", "status", "amount", "full_name"}
    # 字段基数 + NULL 比例
    assert cols["status"].distinct_count == 4
    assert cols["status"].null_ratio == pytest.approx(0.1)
    # Top-K + 小样本披露保护（TINY=2 低于阈值并入 OTHER）
    topk = {e.bucket: e for e in cols["status"].top_k}
    assert topk["PAID"].count == 500
    assert topk["PAID"].ratio == pytest.approx(500 / 802)
    assert topk["OTHER"].count == 2
    assert "TINY" not in topk
    # 数值直方图/分位数（数值列采样）
    assert cols["id"].quantiles["p50"] is not None
    assert cols["id"].distribution is not None
    assert cols["id"].distribution.type == "numeric_histogram"
    assert cols["id"].distribution.params["max"] == 40.0
    assert cols["id"].accuracy == "estimated"
    # 敏感列：仅哈希分桶（无原值桶）
    fn_topk = [e.bucket for e in cols["full_name"].top_k]
    assert all(len(b) == 32 or b == "OTHER" for b in fn_topk)
    assert cols["full_name"].length_stats is not None
    assert cols["full_name"].length_stats.avg == pytest.approx(7.5)
    # 关联基数：父键哈希分桶分布 + 零子实体父实体占比
    rel = tp.relations[0]
    assert rel.child_table == "orders" and rel.parent_table == "users"
    assert rel.child_per_parent_distribution is not None
    assert rel.zero_child_ratio == pytest.approx(0.2)
    # 热点倾斜：敏感列 Top-K 占比 + Zipf 拟合（0.4/0.3/0.2/0.09/0.01）
    assert cols["full_name"].distribution.type == "skewed"
    assert cols["full_name"].distribution.params["fitted_model"] == "zipf"
    assert cols["full_name"].accuracy == "fitted"
    # 全部语句只读（审计）
    assert all(s.lstrip().lower().startswith("select") for s in manifest.statements)


def test_sensitive_column_never_sampled_raw():
    """T2.3：敏感列不发原值采样语句（明文不出库），Top-K 走 SQL 侧哈希。"""
    calls: list[str] = []
    collector = _mysql_collector(
        calls, profile_dsn="mysql://ro@prod/prod", target_dsn="mysql://rw@tgt/tgt"
    )
    profile, manifest = collector.collect(tables=["orders"])
    joined = "\n".join(manifest.statements)
    assert "SELECT MD5(full_name)" in joined
    for forbidden in ("SELECT full_name FROM", "SELECT full_name,"):
        assert forbidden not in joined
    cols = {c.column: c for c in profile.tables[0].columns}
    assert cols["full_name"].top_k  # 哈希分桶 Top-K 仍然产出


def test_sampling_settings_take_effect():
    """T2.3：采样率/行数上限/采集超时进入执行清单并体现在 LIMIT。"""
    conn = FakeConn(MYSQL_ROUTES)
    config = PtdeConfig(
        profile_dsn="mysql://ro@prod/prod",
        target_dsn="mysql://rw@tgt/tgt",
        sampling_rate=0.1,
        collect_timeout_seconds=7,
        collect_max_rows=100,
    )
    collector = StatisticsCollector(config, connect=lambda dsn: conn)
    profile, manifest = collector.collect(tables=["orders"])
    assert manifest.sampling_rate == 0.1
    assert manifest.timeout_seconds == 7
    assert manifest.max_rows == 100
    assert "LIMIT 100" in "\n".join(manifest.statements)
    assert profile.source.sampling_rate == 0.1


def test_query_failure_raises_explicitly():
    """A3 对齐：查询失败带对象名与原因显式上抛，不静默降级。"""

    class BoomConn:
        class _Cur:
            def execute(self, sql, params=()):
                raise RuntimeError("connection refused")

            def close(self):
                pass

        def cursor(self):
            return BoomConn._Cur()

    config = PtdeConfig(
        profile_dsn="mysql://ro@prod/prod", target_dsn="mysql://rw@tgt/tgt"
    )
    collector = StatisticsCollector(config, connect=lambda dsn: BoomConn())
    with pytest.raises(
        StatisticsCollectionError, match="mysql:orders.*connection refused"
    ):
        collector.collect(tables=["orders"])


# ---------- T2.2：PG 统计子适配器（pg_stats 优先） ----------

PG_ROUTES = [
    ("SELECT c.reltuples::bigint", [(1000,)]),
    ("pg_class c JOIN pg_namespace", [("orders",)]),
    ("COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS", [
        ("status", "varchar"),
    ]),
    ("FROM pg_stats WHERE", [(4, 0.1, "{PAID,NEW,REFUND}", "{0.6,0.3,0.08}", None)]),
    ("MIN(LENGTH(status))", [(3, 10, 6.5)]),
    ("COUNT(DISTINCT status)", [(4,)]),
    ("AVG(CASE WHEN status IS NULL", [(0.1,)]),
    ("SELECT status, COUNT(*)", [("PAID", 600), ("NEW", 300)]),
]


def test_postgres_uses_pg_stats_first():
    """PG：n_distinct/null_frac/MCV 优先 pg_stats，回退采样可用。"""
    conn = FakeConn(PG_ROUTES)
    config = PtdeConfig(
        profile_dsn="postgres://ro@prod/prod", target_dsn="postgres://rw@tgt/tgt"
    )
    collector = StatisticsCollector(config, connect=lambda dsn: conn)
    profile, manifest = collector.collect(tables=["orders"])
    assert profile.source.database_type == "postgres"
    cols = {c.column: c for c in profile.tables[0].columns}
    status = cols["status"]
    # n_distinct / null_frac 来自 pg_stats
    assert status.distinct_count == 4
    assert status.null_ratio == pytest.approx(0.1)
    # MCV → Top-K（频次×样本量换算）
    topk = {e.bucket: e.count for e in status.top_k}
    assert topk["PAID"] == 600 and topk["NEW"] == 300
    assert status.distribution is not None  # Zipf 拟合（0.6/0.3/0.08）
    assert status.distribution.type == "skewed"
    assert status.length_stats.max == 10
    assert all(s.lstrip().lower().startswith("select") for s in manifest.statements)


# ---------- T2.5：测试库自画像 ----------


def test_test_db_self_profile():
    calls: list[str] = []
    collector = _mysql_collector(calls, target_dsn="mysql://test@localhost/testdb")
    profile, manifest = collector.collect_test_db_self(tables=["orders"])
    assert profile.source.type == SOURCE_TYPE_TEST_DB_SELF
    assert profile.source.collected_at is not None
    assert profile.fingerprint
    assert manifest.tables == ["orders"]


# ---------- T2.4：文件来源画像 ----------


def _sample_profile_dict() -> dict:
    return {
        "id": "file-profile-001",
        "source": {
            "type": "whatever_will_be_overridden",
            "database_type": "mysql",
            "sampling_rate": 0.1,
            "accuracy": "approximate",
        },
        "tables": [
            {
                "table": "orders",
                "approximate_rows": 125000000,
                "row_count_accuracy": "approximate",
                "columns": [
                    {
                        "column": "status",
                        "distinct_count": 4,
                        "null_ratio": 0,
                        "distribution": {
                            "type": "weighted_categorical",
                            "params": {"PAID": 0.64, "COMPLETED": 0.26},
                            "fitted": True,
                        },
                    }
                ],
            }
        ],
    }


def test_file_source_yaml_json_import_and_stable_fingerprint(tmp_path):
    """T2.4：YAML/JSON 导入；source_type 区分标记；两次导入指纹一致。"""
    data = _sample_profile_dict()
    yaml_path = tmp_path / "profile.yaml"
    yaml_text = (
        "id: file-profile-001\n"
        "source:\n  type: whatever_will_be_overridden\n"
        "  database_type: mysql\n  sampling_rate: 0.1\n  accuracy: approximate\n"
        "tables:\n- table: orders\n  approximate_rows: 125000000\n"
        "  row_count_accuracy: approximate\n  columns:\n  - column: status\n"
        "    distinct_count: 4\n    null_ratio: 0\n    distribution:\n"
        "      type: weighted_categorical\n      params: {PAID: 0.64, COMPLETED: 0.26}\n"
        "      fitted: true\n"
    )
    yaml_path.write_text(yaml_text, encoding="utf-8")
    json_path = tmp_path / "profile.json"
    json_path.write_text(json.dumps(data), encoding="utf-8")

    from_yaml = load_statistics_profile(yaml_path)
    from_json = load_statistics_profile(json_path)
    # 来源可区分标记
    assert from_yaml.source.type == SOURCE_TYPE_FILE_UPLOAD
    assert from_json.source.type == SOURCE_TYPE_FILE_UPLOAD
    # 指纹稳定（同一内容两次导入一致）
    assert from_yaml.fingerprint == load_statistics_profile(yaml_path).fingerprint
    assert from_json.fingerprint == load_statistics_profile(json_path).fingerprint
    # 同内容 YAML/JSON 解析为同一模型 → 指纹一致
    assert from_yaml.fingerprint == from_json.fingerprint
    # 结构同 StatisticsProfile
    assert from_yaml.tables[0].table == "orders"
    assert from_yaml.tables[0].columns[0].distribution.params["PAID"] == 0.64


# ---------- T2.6：Profile Merger ----------


def _schema_snapshot() -> DatabaseSchemaSnapshot:
    orders = TableSchema(
        name="orders",
        columns=[
            ColumnSchema(name="id", type_name="bigint", ordinal=1),
            ColumnSchema(name="status", type_name="varchar", ordinal=2),
            ColumnSchema(name="remark", type_name="varchar", ordinal=3),
        ],
        primary_keys=["id"],
        foreign_keys=[
            ForeignKeySchema(name="fk_orders_user", parent_table="users",
                             child_columns=["user_id"], parent_columns=["id"])
        ],
    )
    users = TableSchema(
        name="users",
        columns=[ColumnSchema(name="id", type_name="bigint", ordinal=1)],
        primary_keys=["id"],
    )
    snap = DatabaseSchemaSnapshot(id="snap-1", tables=[orders, users])
    return snap.seal()


def _stats_profile(pk_columns=None) -> object:
    from ptde.models.profile import (
        ColumnProfile,
        DistributionFit,
        ProfileSource,
        RelationCardinalityProfile,
        StatisticsProfile,
        TableProfile,
    )

    return StatisticsProfile(
        id="stats-1",
        source=ProfileSource(type="db_readonly", database_type="mysql",
                             sampling_rate=0.1, collected_at=None),
        tables=[
            TableProfile(
                table="orders",
                approximate_rows=1000,
                pk_columns=pk_columns or [],
                columns=[
                    ColumnProfile(
                        column="id",
                        distribution=DistributionFit(type="numeric_histogram",
                                                     params={"min": 1, "max": 1000},
                                                     fitted=True),
                    ),
                    ColumnProfile(
                        column="status",
                        distribution=DistributionFit(
                            type="weighted_categorical",
                            params={"PAID": 0.6, "NEW": 0.3},
                            fitted=True,
                        ),
                    ),
                ],
                relations=[
                    RelationCardinalityProfile(
                        child_table="orders",
                        parent_table="users",
                        child_per_parent_distribution=DistributionFit(
                            type="skewed", params={"fitted_parameter": 1.2}, fitted=True
                        ),
                        zero_child_ratio=0.2,
                    )
                ],
            )
        ],
    ).seal()


def test_merger_schema_hard_constraints_not_overridden():
    """门禁 3：Schema 主键 vs 统计冲突 → 以 Schema 为准并记冲突。"""
    schema = _schema_snapshot()
    stats = _stats_profile(pk_columns=["code"])  # 统计侧声称主键 code
    unified, report = ProfileMerger().merge(stats, schema=schema)
    pk_conflicts = [c for c in report.conflicts if c.subject == "orders.primary_key"]
    assert len(pk_conflicts) == 1
    assert pk_conflicts[0].winner == "schema"
    assert pk_conflicts[0].sources == ["schema", "stats"]
    # 主键裁决记录以 Schema 为准
    pk_decisions = [d for d in report.decisions if d.subject == "orders.primary_key"]
    assert any(d.chosen_from == "schema" for d in pk_decisions)


def test_merger_value_override_recorded():
    """字段值分布可被 DatasetSpec 覆盖，覆盖动作记入 MergeReport。"""
    schema = _schema_snapshot()
    stats = _stats_profile()
    unified, report = ProfileMerger().merge(
        stats,
        schema=schema,
        value_overrides={
            "orders.status": {
                "type": "weighted_categorical",
                "params": {"PAID": 1.0},
                "fitted": False,
            }
        },
    )
    dist = unified.entities["orders"].columns["status"].distribution
    assert dist.params == {"PAID": 1.0} and dist.fitted is False
    conflicts = [
        c for c in report.conflicts if c.subject == "orders.status.distribution"
    ]
    assert conflicts and conflicts[0].winner == "dataset_spec"
    assert conflicts[0].sources == ["stats", "dataset_spec"]


def test_merger_missing_feature_marked_estimated():
    """缺失特征标 estimated，不得伪装为生产统计。"""
    schema = _schema_snapshot()
    stats = _stats_profile()
    unified, report = ProfileMerger().merge(stats, schema=schema)
    # remark 列无统计 → estimated
    remark = unified.entities["orders"].columns["remark"]
    assert remark.distribution is None
    assert remark.accuracy == "estimated"
    decisions = [
        d for d in report.decisions if d.subject == "orders.remark.distribution"
    ]
    assert decisions and "estimated" in decisions[0].chosen_from
    # 统计热点分布裁决带新鲜度/置信度注记
    dist_decisions = [
        d for d in report.decisions if d.subject == "orders.status.distribution"
    ]
    assert dist_decisions and "sampling_rate" in dist_decisions[0].note
    # 外键边来自 Schema；分布来自统计
    assert any(
        r.child_table == "orders" and r.parent_table == "users"
        for r in unified.entities["orders"].relation_cardinalities
    )
    rel_decisions = [
        d for d in report.decisions if d.subject == "orders->users:cardinality"
    ]
    assert rel_decisions and rel_decisions[0].chosen_from == "stats"


def test_merger_without_schema_falls_back_to_stats():
    """无 Schema 快照：纯统计画像可融合，统计侧主键仅按 estimated 登记。"""
    stats = _stats_profile(pk_columns=["code"])
    unified, report = ProfileMerger().merge(stats, schema=None)
    assert "orders" in unified.entities
    decisions = [d for d in report.decisions if d.subject == "orders.primary_key"]
    assert decisions and "estimated" in decisions[0].note
    unified2, _ = ProfileMerger().merge(stats, schema=None)
    assert unified.fingerprint == unified2.fingerprint  # 同输入融合指纹稳定


# ---------- Zipf 拟合单元 ----------


def test_fit_zipf():
    exponent, quality = fit_zipf([0.6, 0.3, 0.1])
    assert exponent is not None and exponent > 0
    assert quality is not None and 0 < quality <= 1
    assert fit_zipf([0.5]) == (None, None)
    assert fit_zipf([]) == (None, None)


def test_is_sensitive_column():
    assert is_sensitive_column("full_name")
    assert is_sensitive_column("phone")
    assert is_sensitive_column("id_card_no")
    assert is_sensitive_column("mobile")
    assert is_sensitive_column("email")
    assert not is_sensitive_column("status")
    assert not is_sensitive_column("amount")
    assert not is_sensitive_column("id")
