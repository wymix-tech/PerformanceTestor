"""关联解析等价单测：移植 RelationResolverMergeTest + RelationResolverStatelessTest
+ SemanticParserTest.namingInferenceAndEmptyFallback。

四级合并优先级 / 组外表忽略 / naming 待确认 / 无状态契约（并发不互踩）。
"""

import threading

from datagen.model import (
    ColumnMeta,
    DataGenSpec,
    FieldRule,
    FkMeta,
    RelationHint,
    SchemaMetadata,
    TableSpec,
)
from datagen.naming_relation_resolver import match_parent_table
from datagen.relation_resolver import RelationResolver


def table(name: str) -> SchemaMetadata:
    return SchemaMetadata(table_name=name)


def spec_with_ref(child: str, col: str, parent: str) -> DataGenSpec:
    spec = DataGenSpec()
    ts = TableSpec(table_name=child, row_count=10)
    r = FieldRule(type=FieldRule.TYPE_REF, table=parent, column="id")
    ts.fields[col] = r
    spec.tables.append(ts)
    return spec


# ---------- RelationResolverMergeTest 移植 ----------

def test_explicit_overrides_db_fk():
    order = table("t_order")
    order.foreign_keys.append(FkMeta("user_id", "other", "id"))
    user = table("t_user")
    r = RelationResolver().resolve([order, user],
                                   spec_with_ref("t_order", "user_id", "t_user"))
    assert len(r.auto_edges) == 1
    assert r.auto_edges[0].source == RelationHint.SRC_EXPLICIT
    assert r.auto_edges[0].parent_table == "t_user"


def test_db_fk_auto_applied():
    order = table("t_order")
    order.foreign_keys.append(FkMeta("user_id", "t_user", "id"))
    r = RelationResolver().resolve([order, table("t_user")], DataGenSpec())
    assert len(r.auto_edges) == 1
    assert r.auto_edges[0].source == RelationHint.SRC_DB_FK


def test_orm_join_auto_applied_and_overridden_by_explicit():
    resolver = RelationResolver()
    resolver.register_source([RelationHint("t_order", "user_id", "t_user", "id",
                                           RelationHint.SRC_ORM_JOIN, 0.9)])
    r = resolver.resolve([table("t_order"), table("t_user")],
                         spec_with_ref("t_order", "user_id", "t_user"))
    assert len(r.auto_edges) == 1
    assert r.auto_edges[0].source == RelationHint.SRC_EXPLICIT


def test_naming_falls_back_to_pending():
    order = table("t_order")
    order.columns.append(ColumnMeta(name="other_id", type_name="BIGINT"))
    other = table("other")
    pk = ColumnMeta(name="id", type_name="BIGINT", primary_key=True)
    other.columns.append(pk)
    other.primary_keys.add("id")
    r = RelationResolver().resolve([order, other], DataGenSpec())
    assert not r.auto_edges
    assert len(r.pending_suggestions) == 1
    assert r.pending_suggestions[0].source == RelationHint.SRC_NAMING


def test_explicit_ref_to_unknown_table_ignored():
    r = RelationResolver().resolve([table("t_order"), table("t_user")],
                                   spec_with_ref("t_order", "user_id", "ghost_table"))
    assert not r.auto_edges
    assert not r.pending_suggestions


def test_naming_suffix_matching():
    tables = {"t_user", "users"}
    assert match_parent_table("t_user_id", "id", tables) == "t_user"
    assert match_parent_table("users_id", "id", tables) == "users"
    assert match_parent_table("amount", "id", tables) is None
    assert match_parent_table("_id", "id", tables) is None
    assert match_parent_table("t_category_id", "id", {"t_category"}) == "t_category"


# ---------- RelationResolverStatelessTest 移植 ----------

def metas() -> list[SchemaMetadata]:
    return [table("t_order"), table("t_user"), table("t_shop")]


def test_alternate_resolutions_are_isolated():
    resolver = RelationResolver()  # 单实例复用（模拟引擎并发复用）
    order_to_user = [RelationHint("t_order", "user_id", "t_user", "id",
                                  RelationHint.SRC_ORM_JOIN, 0.9)]
    order_to_shop = [RelationHint("t_order", "shop_id", "t_shop", "id",
                                  RelationHint.SRC_ORM_JOIN, 0.9)]

    # 第一次解析（任务 A：order → user）
    r_a = resolver.resolve(metas(), DataGenSpec(), orm_hints=order_to_user)
    assert len(r_a.auto_edges) == 1
    assert r_a.auto_edges[0].parent_table == "t_user"

    # 第二次解析（任务 B：order → shop）——旧实现 registerSource 后 A 的 hints 仍残留
    r_b = resolver.resolve(metas(), DataGenSpec(), orm_hints=order_to_shop)
    assert len(r_b.auto_edges) == 1
    assert r_b.auto_edges[0].parent_table == "t_shop"

    # 第三次：无 orm 语义（任务 C）——不得读到前序任何残留
    r_c = resolver.resolve(metas(), DataGenSpec(), orm_hints=None)
    assert not r_c.auto_edges


def test_concurrent_resolutions_do_not_cross_contaminate():
    resolver = RelationResolver()
    to_user = [RelationHint("t_order", "user_id", "t_user", "id",
                            RelationHint.SRC_ORM_JOIN, 0.9)]
    to_shop = [RelationHint("t_order", "shop_id", "t_shop", "id",
                            RelationHint.SRC_ORM_JOIN, 0.9)]

    iterations = 200
    contaminated: list[str] = []

    def run_loop(hints, expected_parent):
        try:
            for _ in range(iterations):
                r = resolver.resolve(metas(), DataGenSpec(), orm_hints=hints)
                if len(r.auto_edges) != 1 or r.auto_edges[0].parent_table != expected_parent:
                    contaminated.append(expected_parent)
                    return
        except Exception as e:  # noqa: BLE001
            contaminated.append(f"exception: {e}")

    threads = [threading.Thread(target=run_loop, args=(to_user, "t_user")),
               threading.Thread(target=run_loop, args=(to_shop, "t_shop"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not contaminated, f"并发解析出现任务间 ormHints 交叉污染: {contaminated}"


# ---------- SemanticParserTest.namingInferenceAndEmptyFallback 移植 ----------

def test_naming_inference_and_empty_fallback():
    user = table("user")
    pk = ColumnMeta(name="id", type_name="BIGINT",
                    primary_key=True, auto_increment=True)
    user.columns.append(pk)
    user.primary_keys.add("id")
    user.row_count = 0  # 空表

    order = table("t_order")
    fk = ColumnMeta(name="user_id", type_name="BIGINT", auto_increment=False)
    order.columns.append(fk)

    hints = NamingRelationResolverProxy().resolve(order, {"user": user}, None, None)
    # conn=None → 纯命名 0.5；空表兜底路径需要 dialect（此处覆盖匹配逻辑）
    assert len(hints) == 1
    assert hints[0].parent_table == "user"
    assert hints[0].source == RelationHint.SRC_NAMING


class NamingRelationResolverProxy:
    """从 datagen 包导入 NamingRelationResolver 的薄包装（保持测试名对齐 Java）。"""

    def __init__(self) -> None:
        from datagen.naming_relation_resolver import NamingRelationResolver
        self._impl = NamingRelationResolver()

    def resolve(self, child, parents, conn, dialect):
        return self._impl.resolve(child, parents, conn, dialect)
