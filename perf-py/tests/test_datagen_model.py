"""模型层等价单测：小写归一化 / 列查找 / RelationHint 优先级与自动生效 / 显式规则提取。

Java 侧 model.* 被 PIT 排除（纯数据载体），本文件覆盖其中的**行为性**归一化逻辑。
"""

from datagen.explicit_rule_hints import extract
from datagen.model import (
    ColumnMeta,
    DataGenSpec,
    FieldRule,
    FkMeta,
    RelationHint,
    SchemaMetadata,
    TableSpec,
)


def _meta(table: str, *columns: ColumnMeta) -> SchemaMetadata:
    m = SchemaMetadata(table_name=table)
    m.columns.extend(columns)
    return m


def test_table_and_column_names_lowercased():
    m = SchemaMetadata(table_name="Perf_Order")
    assert m.table_name == "perf_order"
    c = ColumnMeta(name="UserID")
    assert c.name == "userid"  # 整体小写，不转 snake_case（对齐 Java toLowerCase）
    fk = FkMeta("User_ID", "T_User", "ID")
    assert (fk.fk_column, fk.pk_table, fk.pk_column) == ("user_id", "t_user", "id")


def test_find_column_case_insensitive():
    m = _meta("t", ColumnMeta(name="user_id", type_name="BIGINT"))
    assert m.find_column("USER_ID") is not None
    assert m.find_column("User_Id").type_name == "BIGINT"
    assert m.find_column("missing") is None
    assert m.find_column(None) is None


def test_null_or_empty_table():
    m = _meta("t")
    assert m.is_null_or_empty_table()  # rowCount=-1
    m.row_count = 0
    assert m.is_null_or_empty_table()
    m.row_count = 5
    assert not m.is_null_or_empty_table()


def test_column_index():
    m = _meta("t", ColumnMeta(name="a"), ColumnMeta(name="b"))
    idx = m.column_index()
    assert set(idx) == {"a", "b"}
    assert idx["a"] is m.columns[0]


def test_relation_hint_priority_and_auto_applicable():
    explicit = RelationHint("o", "uid", "u", "id", RelationHint.SRC_EXPLICIT, 1.0)
    db_fk = RelationHint("o", "uid", "u", "id", RelationHint.SRC_DB_FK, 1.0)
    orm = RelationHint("o", "uid", "u", "id", RelationHint.SRC_ORM_JOIN, 0.9)
    naming = RelationHint("o", "uid", "u", "id", RelationHint.SRC_NAMING, 0.5)
    fallback = RelationHint("o", "uid", "u", "id", RelationHint.SRC_NAMING_EMPTY, 0.3)
    unknown = RelationHint("o", "uid", "u", "id", "mystery", 0.5)

    assert explicit.priority() > db_fk.priority() > orm.priority() > naming.priority()
    assert naming.priority() == fallback.priority() == 10
    assert unknown.priority() == 0
    assert explicit.auto_applicable() and db_fk.auto_applicable() and orm.auto_applicable()
    assert not naming.auto_applicable()
    assert not fallback.auto_applicable()


def test_relation_hint_naming_fallback_factory():
    h = RelationHint.naming_fallback("t_order", "user_id", "user", "id")
    assert h.source == RelationHint.SRC_NAMING_EMPTY
    assert h.confidence == 0.3
    assert h.source_refs == ["naming-convention(empty-table-fallback)"]
    assert (h.child_table, h.parent_column) == ("t_order", "id")


def test_field_rule_fixed_factory():
    r = FieldRule.fixed("PAID")
    assert r.type == FieldRule.TYPE_FIXED
    assert r.value == "PAID"


def test_explicit_rule_hints_extract():
    meta_map = {
        "order": _meta("order", ColumnMeta(name="id", type_name="BIGINT")),
        "user": _meta("user", ColumnMeta(name="id", type_name="BIGINT")),
        "item": _meta("item", ColumnMeta(name="id", type_name="BIGINT")),
    }
    spec = DataGenSpec(tables=[
        TableSpec(table_name="ORDER", row_count=10, fields={
            "user_id": FieldRule(type=FieldRule.TYPE_REF, table="USER", column="ID"),
            "user_id_skew": FieldRule(type=FieldRule.TYPE_REF_SKEW, table="user",
                                      column="id", hot_percent=20, hot_share=80),
            "status": FieldRule(type=FieldRule.TYPE_ENUM, values=["A"]),
            # 指向组外表 → 忽略
            "ext_id": FieldRule(type=FieldRule.TYPE_REF, table="external", column="id"),
            # 缺 parent column → 忽略
            "bad": FieldRule(type=FieldRule.TYPE_REF, table="user"),
        }),
        # 组外表整表跳过
        TableSpec(table_name="external", row_count=10, fields={
            "id": FieldRule(type=FieldRule.TYPE_REF, table="user", column="id"),
        }),
    ])
    hints = extract(spec, meta_map)
    assert len(hints) == 2
    assert [(h.child_table, h.child_column, h.parent_table, h.parent_column)
            for h in hints] == [("order", "user_id", "user", "id"),
                                ("order", "user_id_skew", "user", "id")]
    for h in hints:
        assert h.source == RelationHint.SRC_EXPLICIT
        assert h.confidence == 1.0
        assert h.source_refs == ["request"]


def test_explicit_rule_hints_null_spec():
    assert extract(None, {}) == []
