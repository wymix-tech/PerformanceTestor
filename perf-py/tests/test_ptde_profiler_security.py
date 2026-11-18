"""T2.7 明文防护审计测试（R2 批次 B2，验收门禁 1）。

构造含"姓名/手机号/身份证"形数据的生产模拟库（fake DBAPI 连接，不连真库），
对只读统计采集做两项审计：
1. 画像输出不含明文业务值：仅分布参数/计数/分桶/哈希类别；
2. 执行过的全部语句均为语句白名单内的只读 SELECT，且不含敏感列原值投影。
"""

from __future__ import annotations

import re

import pytest

from ptde.adapters.mysql.statistics import StatisticsCollectionError, validate_identifier
from ptde.config import PtdeConfig
from ptde.profiler.collector import (
    CollectionManifest,
    StatisticsCollector,
    assert_readonly_sql,
)


class FakeCursor:
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


# 生产模拟库明文（仅存在于"库"侧语义中；采集输出不得含这些值）
PLANTED_PLAINTEXT = [
    "张三", "李四", "王五",
    "13800138000", "13912345678",
    "110101199001011234", "310101198805054321",
]

H1, H2, H3 = "a1" * 16, "b2" * 16, "c3" * 16  # 模拟 SQL 侧 MD5 桶（32 hex）

PROD_ROUTES = [
    # 表清单 / 行数（近似）
    ("TABLE_TYPE = 'BASE TABLE'", [("prod_users",)]),
    ("SELECT TABLE_ROWS FROM information_schema.TABLES", [(10000,)]),
    # 列清单：姓名 / 手机号 / 身份证形敏感列 + 非敏感数值列
    ("COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS", [
        ("id", "bigint"),
        ("full_name", "varchar"),
        ("phone", "varchar"),
        ("id_card", "varchar"),
        ("amount", "decimal"),
    ]),
    # 无外键
    ("REFERENCED_TABLE_NAME IS NOT NULL", []),
    # 非敏感列原值统计（数值列允许：仅产出分布参数）
    ("COUNT(DISTINCT id)", [(10000,)]),
    ("COUNT(DISTINCT amount)", [(8000,)]),
    ("AVG(id IS NULL)", [(0,)]),
    ("AVG(amount IS NULL)", [(0,)]),
    ("SELECT id, COUNT(*)", [(1, 6000), (2, 3000), (3, 1000)]),
    ("SELECT amount, COUNT(*)", [(100, 5000), (200, 4000), (50, 1000)]),
    ("SELECT id FROM prod_users ORDER BY RAND()", [(1,), (2,), (3,), (4,)]),
    ("SELECT amount FROM prod_users ORDER BY RAND()", [(100,), (200,), (50,)]),
    # 敏感列 1：full_name（SQL 侧 MD5，明文不出库）
    ("COUNT(DISTINCT v) FROM (SELECT MD5(full_name)", [(9800,)]),
    ("AVG(v IS NULL) FROM (SELECT MD5(full_name)", [(0.01,)]),
    ("MD5(full_name) AS v", [(H1, 6000), (H2, 3500), (H3, 400)]),
    ("CHAR_LENGTH(full_name)", [(2, 20, 6.7)]),
    # 敏感列 2：phone
    ("COUNT(DISTINCT v) FROM (SELECT MD5(phone)", [(9700,)]),
    ("AVG(v IS NULL) FROM (SELECT MD5(phone)", [(0.02,)]),
    ("MD5(phone) AS v", [(H1, 8000), (H2, 1800), (H3, 100)]),
    ("CHAR_LENGTH(phone)", [(11, 11, 11.0)]),
    # 敏感列 3：id_card
    ("COUNT(DISTINCT v) FROM (SELECT MD5(id_card)", [(9900,)]),
    ("AVG(v IS NULL) FROM (SELECT MD5(id_card)", [(0.005,)]),
    ("MD5(id_card) AS v", [(H1, 7000), (H2, 2900), (H3, 50)]),
    ("CHAR_LENGTH(id_card)", [(18, 18, 18.0)]),
]

_MD5_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


def _collect():
    conn = FakeConn(PROD_ROUTES)
    config = PtdeConfig(
        profile_dsn="mysql://readonly_user:ro_secret@prod-db:3306/prod_biz",
        target_dsn="mysql://writer_user:rw_secret@target-db:3306/test_biz",
    )
    collector = StatisticsCollector(config, connect=lambda dsn: conn)
    return collector.collect(tables=["prod_users"], profile_id="audit-1")


def test_profile_output_contains_no_plaintext():
    """审计 1：画像输出仅分布参数/计数/分桶/哈希类别，不含任何明文业务值。"""
    profile, _manifest = _collect()
    dumped = profile.model_dump_json()
    for plaintext in PLANTED_PLAINTEXT:
        assert plaintext not in dumped, f"画像泄露明文: {plaintext}"
    # 敏感列只允许哈希桶（32 hex）或披露保护合并桶 OTHER
    tp = profile.tables[0]
    cols = {c.column: c for c in tp.columns}
    for sensitive in ("full_name", "phone", "id_card"):
        entries = cols[sensitive].top_k
        assert entries, f"{sensitive} 缺少脱敏 Top-K"
        for entry in entries:
            assert _MD5_HEX_RE.match(entry.bucket) or entry.bucket == "OTHER", (
                f"{sensitive} 出现非哈希桶: {entry.bucket!r}"
            )
    # 分布参数仍然齐备（基数/NULL/长度统计均为统计特征）
    assert cols["full_name"].distinct_count == 9800
    assert cols["phone"].null_ratio == pytest.approx(0.02)
    assert cols["id_card"].length_stats.max == 18


def test_all_executed_statements_are_whitelisted_readonly():
    """审计 2：执行过的全部语句均为白名单内只读 SELECT（逐条复检）。"""
    _profile, manifest = _collect()
    assert manifest.statements, "语句审计记录为空"
    for sql in manifest.statements:
        assert assert_readonly_sql(sql)  # 白名单外会直接抛错


def test_no_raw_projection_of_sensitive_columns():
    """审计 2 补充：敏感列不存在原值投影语句（采样子查询内层即哈希）。"""
    _profile, manifest = _collect()
    joined = "\n".join(manifest.statements)
    for forbidden in (
        "SELECT full_name FROM", "SELECT full_name,",
        "SELECT phone FROM", "SELECT phone,",
        "SELECT id_card FROM", "SELECT id_card,",
        "GROUP BY full_name", "GROUP BY phone", "GROUP BY id_card",
    ):
        assert forbidden not in joined, f"出现敏感列原值投影: {forbidden}"
    # 哈希采集确实发生（MD5 桶语句存在）
    assert "MD5(full_name) AS v" in joined
    assert "MD5(phone) AS v" in joined
    assert "MD5(id_card) AS v" in joined


def test_statements_recorded_in_manifest_are_select_only():
    """审计 3：执行清单仅含 SELECT 语句（凭据身份脱敏随清单记录）。"""
    profile, manifest = _collect()
    assert all(s.lstrip().lower().startswith("select") for s in manifest.statements)
    # A5：清单凭据身份脱敏，不含密码
    assert manifest.credential_label == "mysql://readonly_user@prod-db:3306/prod_biz"
    assert "ro_secret" not in manifest.credential_label
    assert profile.source.credential_label == manifest.credential_label
    # 画像元数据随指纹入库
    assert profile.source.collected_at is not None
    assert profile.source.sampling_rate is not None
    assert profile.source.accuracy
    assert profile.fingerprint


def test_identifier_injection_rejected():
    """白名单外标识符直接拒绝（防注入，不做转义兜底）。"""
    for bad in ("prod_users; DROP TABLE prod_users", "prod`", "prod users"):
        with pytest.raises(StatisticsCollectionError):
            validate_identifier(bad)


def test_non_readonly_statement_never_sent():
    """白名单外语句在发送前即拒绝，不进入执行清单（无执行痕迹）。"""
    conn = FakeConn(PROD_ROUTES)
    # 通过内部守卫直接验证：非 SELECT 不被发送
    from ptde.profiler.collector import _GuardedQuery

    manifest = CollectionManifest()
    guard = _GuardedQuery(conn, manifest, "mysql")
    with pytest.raises(StatisticsCollectionError):
        guard("DELETE FROM prod_users", (), "prod_users")
    with pytest.raises(StatisticsCollectionError):
        guard("SELECT 1; DELETE FROM prod_users", (), "multi")
    assert manifest.statements == []  # 白名单外语句零执行痕迹
