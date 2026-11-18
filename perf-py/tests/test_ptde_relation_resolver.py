"""T1.3 关联解析三态化测试：relation_resolver 四级合并 + naming_resolver 分级。

验收点（实施方案 T1.3）：naming 级、注解 SQL JOIN 正则结果一律 INFERRED；
禁止静默跳过（被覆盖边显式登记 superseded）。
"""

from __future__ import annotations

from ptde.analyzer.lang_java._legacy_model import RelationHint
from ptde.analyzer.naming_resolver import (
    NamingRelationResolver,
    match_parent_table,
    plural,
    singular,
)
from ptde.analyzer.relation_resolver import RelationResolver
from ptde.models.base import EvidenceStatus
from ptde.models.entity import RelationKind
from ptde.models.snapshot import ColumnSchema, ForeignKeySchema, TableSchema


def _col(name: str, type_name: str = "bigint", **kw) -> ColumnSchema:
    return ColumnSchema(name=name, type_name=type_name, **kw)


def _table(name: str, columns: list[ColumnSchema], pks: list[str],
           fks: list[ForeignKeySchema] | None = None,
           row_estimate: int = -1) -> TableSchema:
    pk_set = {p.lower() for p in pks}
    for c in columns:
        if c.name.lower() in pk_set:
            c.primary_key = True
    return TableSchema(
        name=name, columns=columns, primary_keys=pks,
        foreign_keys=fks or [], row_estimate=row_estimate,
    )


USERS = _table("users", [_col("id"), _col("name", "varchar(64)")], ["id"])
PRODUCTS = _table("products", [_col("id"), _col("title", "varchar(128)")], ["id"])
ORDERS = _table(
    "orders",
    [_col("id"), _col("user_id"), _col("product_id"), _col("amount", "decimal(18,2)")],
    ["id"],
    fks=[ForeignKeySchema(name="fk_orders_user",
                          child_columns=["user_id"],
                          parent_table="users",
                          parent_columns=["id"])],
)


def _resolve(tables, **kw):
    return RelationResolver().resolve(tables, **kw)


def _find(relations, child, column):
    return next((r for r in relations if r.child_table == child and r.child_column == column), None)


# ---------- db_fk / explicit / orm / naming 三态 ----------

def test_db_fk_confirmed() -> None:
    out = _resolve([USERS, ORDERS, PRODUCTS])
    rel = _find(out.relations, "orders", "user_id")
    assert rel is not None
    assert rel.status == EvidenceStatus.CONFIRMED
    assert rel.relation_kind == RelationKind.DB_FK
    assert rel.confidence == 1.0
    assert rel.evidence[0].type == "database_foreign_key"
    assert rel.evidence[0].object_name == "fk_orders_user" or "fk" in (rel.evidence[0].detail or "")


def test_naming_stays_inferred_and_pending() -> None:
    out = _resolve([USERS, ORDERS, PRODUCTS])
    # product_id 无声明外键 → naming 推断，INFERRED 且不自动生效（pending）
    rel = _find(out.pending, "orders", "product_id")
    assert rel is not None
    assert rel.status == EvidenceStatus.INFERRED
    assert rel.relation_kind == RelationKind.LOGICAL
    assert rel.confidence == 0.5
    assert rel.parent_table == "products"
    # 不伪装为事实：不在自动边集
    assert _find(out.relations, "orders", "product_id") is None


def test_orm_join_inferred_not_confirmed() -> None:
    # 注解 SQL JOIN 正则结果（orm_join）一律 INFERRED（A3）
    orm = RelationHint("orders", "product_id", "products", "id",
                       RelationHint.SRC_ORM_JOIN, 0.9)
    out = _resolve([USERS, ORDERS, PRODUCTS], orm_hints=[orm])
    rel = _find(out.relations, "orders", "product_id")
    assert rel.status == EvidenceStatus.INFERRED
    assert rel.relation_kind == RelationKind.ORM
    # 胜出边也登记进 findings（显式记录）
    f = next(f for f in out.findings
             if f.value.get("child_column") == "product_id" and not f.value.get("superseded"))
    assert f.status.value == "INFERRED"


def test_orm_join_superseded_by_db_fk_recorded_not_silent() -> None:
    orm = RelationHint("orders", "user_id", "users", "id", RelationHint.SRC_ORM_JOIN, 0.9)
    out = _resolve([USERS, ORDERS, PRODUCTS], orm_hints=[orm])
    # db_fk 优先级更高胜出；orm 边不静默丢弃 → superseded 显式登记
    assert _find(out.relations, "orders", "user_id").relation_kind == RelationKind.DB_FK
    sup = [f for f in out.findings
           if f.value.get("child_column") == "user_id" and f.value.get("superseded")]
    assert sup and sup[0].value["source"] == RelationHint.SRC_ORM_JOIN


def test_explicit_highest_priority_confirmed() -> None:
    explicit = RelationHint("orders", "product_id", "users", "id",
                            RelationHint.SRC_EXPLICIT, 1.0)
    out = _resolve([USERS, ORDERS, PRODUCTS], explicit_hints=[explicit])
    rel = _find(out.relations, "orders", "product_id")
    assert rel.status == EvidenceStatus.CONFIRMED
    assert rel.confidence == 1.0
    # explicit 40 > orm 20：同键被覆盖的 orm 需显式登记（若同时传入）
    orm = RelationHint("orders", "product_id", "users", "id", RelationHint.SRC_ORM_JOIN, 0.9)
    out2 = _resolve([USERS, ORDERS, PRODUCTS], orm_hints=[orm], explicit_hints=[explicit])
    assert any(f.value.get("superseded") for f in out2.findings)


def test_every_edge_has_finding_no_silent_skip() -> None:
    orm = RelationHint("orders", "user_id", "users", "id", RelationHint.SRC_ORM_JOIN, 0.9)
    out = _resolve([USERS, ORDERS, PRODUCTS], orm_hints=[orm])
    # findings 数 ≥ relations + pending + superseded 总数（每条边均有登记）
    assert len(out.findings) >= len(out.relations) + len(out.pending) + 1


def test_naming_superseded_by_db_fk_recorded() -> None:
    # user_id 已有 db_fk；naming 也会推断同一边（users 匹配 user_id）→ superseded
    out = _resolve([USERS, ORDERS, PRODUCTS])
    sup = [f for f in out.findings
           if f.value.get("child_column") == "user_id" and f.value.get("superseded")]
    assert sup and sup[0].value["source"] == RelationHint.SRC_NAMING


# ---------- naming_resolver 分级与抽样 ----------

class _FakeDialect:
    @staticmethod
    def quote_identifier(name: str) -> str:
        return f'"{name}"'


class _FakeSamplingConn:
    """抽样假连接：父表 id 全量返回；子表 fk 值按给定序列返回。"""

    def __init__(self, parent_ids: list[int], child_values: list[int | None]) -> None:
        self._parent_ids = parent_ids
        self._child_values = child_values

    def cursor(self):
        conn = self

        class Cur:
            def execute(self, sql, params=()):
                self._sql = sql.lower()

            def fetchmany(self, n):
                return [(v,) for v in conn._parent_ids[:n]]

            def fetchall(self):
                if "product_id" in self._sql or "_id from" in self._sql:
                    return [(v,) for v in conn._child_values]
                return [(v,) for v in conn._parent_ids]

            def close(self):
                pass

        return Cur()


def _orders_with(product_id_value: str = "bigint") -> TableSchema:
    return _table(
        "orders",
        [_col("id"), _col("product_id", product_id_value), _col("amount", "decimal(18,2)")],
        ["id"],
    )


def test_naming_empty_parent_fallback_low_confidence() -> None:
    empty_products = _table("products", [_col("id")], ["id"], row_estimate=0)
    orders = _orders_with()
    conn = _FakeSamplingConn(parent_ids=[], child_values=[])
    rels = NamingRelationResolver().resolve(
        orders, {"products": empty_products}, conn=conn, dialect=_FakeDialect()
    )
    assert len(rels) == 1
    assert rels[0].confidence == 0.3
    assert rels[0].status == EvidenceStatus.INFERRED
    assert any("empty-table-fallback" in (e.detail or "") for e in rels[0].evidence)


def test_naming_sampling_high_contain_rate_085() -> None:
    products = _table("products", [_col("id")], ["id"], row_estimate=50)
    orders = _orders_with()
    conn = _FakeSamplingConn(parent_ids=[1, 2, 3], child_values=[1, 2, 3, 1, 2])
    rels = NamingRelationResolver().resolve(
        orders, {"products": products}, conn=conn, dialect=_FakeDialect()
    )
    assert rels[0].confidence == 0.85
    assert any("containRate=1.00" in (e.detail or "") for e in rels[0].evidence)


def test_naming_sampling_low_contain_rate_04() -> None:
    products = _table("products", [_col("id")], ["id"], row_estimate=50)
    orders = _orders_with()
    conn = _FakeSamplingConn(parent_ids=[1, 2, 3], child_values=[1, 9, 9, 8])
    rels = NamingRelationResolver().resolve(
        orders, {"products": products}, conn=conn, dialect=_FakeDialect()
    )
    assert rels[0].confidence == 0.4


def test_naming_sampling_failure_degrades_to_05() -> None:
    products = _table("products", [_col("id")], ["id"], row_estimate=50)
    orders = _orders_with()

    class BrokenConn:
        def cursor(self):
            raise RuntimeError("boom")

    rels = NamingRelationResolver().resolve(
        orders, {"products": products}, conn=BrokenConn(), dialect=_FakeDialect()
    )
    assert rels[0].confidence == 0.5


def test_naming_type_incompatibility_rejected() -> None:
    products = _table("products", [_col("id", "varchar(32)")], ["id"])
    orders = _orders_with()
    rels = NamingRelationResolver().resolve(orders, {"products": products})
    assert rels == []


def test_naming_excludes_pk_auto_increment_and_non_integer() -> None:
    orders = _table(
        "orders",
        [_col("id"), _col("product_id", "varchar(16)"), _col("auto_inc", "bigint",
                                                          auto_increment=True)],
        ["id"],
    )
    products = _table("products", [_col("id")], ["id"])
    rels = NamingRelationResolver().resolve(orders, {"products": products})
    assert rels == []


# ---------- 单复数归一 ----------

def test_match_parent_table_variants() -> None:
    assert match_parent_table("user_id", "id", {"user"}) == "user"
    assert match_parent_table("user_id", "id", {"users"}) == "users"
    assert match_parent_table("category_id", "id", {"categories"}) == "categories"
    assert match_parent_table("box_id", "id", {"boxes"}) == "boxes"
    assert match_parent_table("remark", "id", {"users"}) is None


def test_singular_plural() -> None:
    assert singular("users") == "user"
    assert singular("categories") == "category"
    assert plural("user") == "users"
    assert plural("category") == "categories"
    assert plural("box") == "boxes"


def test_no_conn_and_no_parents_safe() -> None:
    assert NamingRelationResolver().resolve(None, {}) == []
    assert NamingRelationResolver().resolve(USERS, {}) == []
