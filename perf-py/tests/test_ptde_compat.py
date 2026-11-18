"""R0 验收：旧→新模型适配（compat.py 往返无丢失，实施方案 §3.2 T0.3）。"""

from __future__ import annotations

from datagen.model import (
    ColumnMeta,
    DataGenSpec,
    FieldRule,
    FkMeta,
    OverThreshold,
    RelationHint,
    SchemaMetadata,
    TableSpec,
)
from ptde.models.compat import (
    DataGenSpecV2,
    data_gen_spec_from_v2,
    data_gen_spec_to_v2,
    field_rule_from_v2,
    field_rule_to_v2,
    relation_hint_from_v2,
    relation_hint_to_v2,
    schema_metadata_from_v2,
    schema_metadata_to_v2,
    table_spec_from_v2,
    table_spec_to_v2,
)


def _rich_field_rule() -> FieldRule:
    return FieldRule(
        type=FieldRule.TYPE_REF_SKEW,
        value=None,
        min=1,
        max=99.5,
        values=[1, 2, 3],
        weights=[0.5, 0.3, 0.2],
        table="users",
        column="id",
        hot_percent=20,
        hot_share=80,
        over_threshold=OverThreshold(op=">=", value=1000, share=30),
        seq_prefix="ORD-",
        seq_start=10001,
    )


def test_field_rule_roundtrip_no_loss():
    r = _rich_field_rule()
    back = field_rule_from_v2(field_rule_to_v2(r))
    for f in dc_field_names(r):
        assert getattr(back, f) == getattr(r, f), f


def dc_field_names(obj) -> list[str]:
    return [f.name for f in type(obj).__dataclass_fields__.values()]


def test_field_rule_roundtrip_minimal():
    r = FieldRule(type=FieldRule.TYPE_FIXED, value="x")
    back = field_rule_from_v2(field_rule_to_v2(r))
    assert (back.type, back.value) == (r.type, r.value)
    assert back.over_threshold is None and back.seq_start is None


def test_relation_hint_roundtrip_no_loss():
    h = RelationHint(
        child_table="orders", child_column="user_id",
        parent_table="users", parent_column="id",
        source=RelationHint.SRC_ORM_JOIN, confidence=0.85,
    )
    h.source_refs.extend(["Order.java:12", "UserMapper.xml:40"])
    back = relation_hint_from_v2(relation_hint_to_v2(h))
    for f in dc_field_names(h):
        assert getattr(back, f) == getattr(h, f), f
    assert back.priority() == h.priority() and back.auto_applicable() == h.auto_applicable()


def test_schema_metadata_roundtrip_no_loss():
    meta = SchemaMetadata(
        table_name="T_Order",
        columns=[
            ColumnMeta(name="ID", type_name="bigint", primary_key=True, auto_increment=True),
            ColumnMeta(name="status", type_name="varchar", size=20, nullable=False,
                       comment="状态 1=待支付 2=已支付"),
        ],
        foreign_keys=[FkMeta(fk_column="user_id", pk_table="users", pk_column="id")],
        row_count=12345,
    )
    meta.primary_keys = {"id"}
    back = schema_metadata_from_v2(schema_metadata_to_v2(meta))
    assert back.table_name == "t_order"
    assert back.row_count == 12345
    assert back.primary_keys == {"id"}
    for c_orig, c_back in zip(meta.columns, back.columns, strict=True):
        for f in dc_field_names(c_orig):
            assert getattr(c_back, f) == getattr(c_orig, f), f
    for f_orig, f_back in zip(meta.foreign_keys, back.foreign_keys, strict=True):
        for f in dc_field_names(f_orig):
            assert getattr(f_back, f) == getattr(f_orig, f), f


def test_table_spec_roundtrip_no_loss():
    spec = TableSpec(
        table_name="t_order", row_count=5000,
        fields={"amount": _rich_field_rule(), "status": FieldRule.fixed("PAID")},
        orphan_rate=0.01,
    )
    back = table_spec_from_v2(table_spec_to_v2(spec))
    for f in dc_field_names(spec):
        assert getattr(back, f) == getattr(spec, f), f


def test_data_gen_spec_roundtrip_no_loss_but_password():
    spec = DataGenSpec(
        jdbc_url="jdbc:mysql://h:3306/db", db_user="u", db_password="SECRET",
        tables=[TableSpec(table_name="t", row_count=10)],
        orphan_rate=0.02, max_rows=500_000, dry_run=True,
    )
    v2 = data_gen_spec_to_v2(spec)
    # 安全：凭据不进入 V2 模型
    assert "db_password" not in DataGenSpecV2.model_fields
    assert "SECRET" not in v2.to_json()
    # 往返（其余字段无丢失）
    back = data_gen_spec_from_v2(v2, db_password="SECRET")
    for f in dc_field_names(spec):
        if f == "db_password":
            assert back.db_password == "SECRET"
        else:
            assert getattr(back, f) == getattr(spec, f), f


def test_v2_models_are_versioned_and_dual_readable():

    v2 = data_gen_spec_to_v2(DataGenSpec(tables=[TableSpec(table_name="t", row_count=5)]))
    assert v2.version == "1"
    fp = v2.compute_fingerprint()
    v3 = DataGenSpecV2.from_yaml(v2.to_yaml())
    assert v3 == v2 and v3.compute_fingerprint() == fp
    v4 = DataGenSpecV2.from_json(v2.to_json())
    assert v4 == v2


def test_v2_max_rows_is_legacy_only():
    """A1 分轨：V2 镜像承载 legacy 钳制口径；新 DatasetSpec 无 max_rows（models 测试覆盖）。"""
    v2 = data_gen_spec_to_v2(DataGenSpec(max_rows=123))
    assert v2.max_rows == 123
