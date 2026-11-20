"""B7 续批次 9：legacy FieldRule → PTDE 字段规格翻译路径单测（T7.6/T7.7 规则语义对齐）。

覆盖：seq/range(+over_threshold)/enum/fixed/ref/refSkew 六类规则映射、
表级 orphan_rate 故意孤儿语义（无效 ID + 可空列 50% NULL 拆分）、
关系四元组产出（db_fk / 故意孤儿降级 logical）、HOT_ROW_SHARE_WARN 告警、
camelCase JSON dict 输入（perf_cli rule_from_json 键名口径）。
"""

from __future__ import annotations

import pytest

from ptde.models.wire import FieldRule, OverThreshold
from server.rule_translator import (
    field_rule_to_distribution,
    field_rule_to_relation,
    translate_fields,
)

# ---------------------------------------------------------------- 逐规则映射


def test_seq_rule_maps_to_sequential_with_prefix_and_start():
    rule = FieldRule(type=FieldRule.TYPE_SEQ, seq_prefix="ORD_", seq_start=100)
    assert field_rule_to_distribution(rule) == {
        "type": "sequential",
        "params": {"seq_prefix": "ORD_", "seq_start": 100},
    }


def test_seq_rule_defaults_align_legacy_seq_value():
    """seq_prefix/seq_start 缺省 ""/1（对齐 rule_engine.seq_value）。"""
    assert field_rule_to_distribution(
        FieldRule(type=FieldRule.TYPE_SEQ)) == {
        "type": "sequential",
        "params": {"seq_prefix": "", "seq_start": 1},
    }


def test_range_rule_maps_to_uniform():
    rule = FieldRule(type=FieldRule.TYPE_RANGE, min=1, max=10000)
    assert field_rule_to_distribution(rule) == {
        "type": "uniform", "params": {"min": 1, "max": 10000}}


def test_range_rule_defaults_align_legacy():
    assert field_rule_to_distribution(
        FieldRule(type=FieldRule.TYPE_RANGE)) == {
        "type": "uniform", "params": {"min": 0.0, "max": 100.0}}


def test_range_with_over_threshold_maps_to_over_threshold_distribution():
    """over_threshold 语义 = share% 超阈值区间分配（非截断），参数逐项透传。"""
    rule = FieldRule(type=FieldRule.TYPE_RANGE, min=0, max=100,
                     over_threshold=OverThreshold(op=">=", value=80, share=40))
    assert field_rule_to_distribution(rule) == {
        "type": "over_threshold",
        "params": {"min": 0, "max": 100, "threshold_value": 80,
                   "threshold_op": ">=", "share": 40.0},
    }


def test_enum_rule_with_weights_maps_to_weighted_categorical():
    rule = FieldRule(type=FieldRule.TYPE_ENUM,
                     values=["A", "B"], weights=[0.3, 0.7])
    assert field_rule_to_distribution(rule) == {
        "type": "weighted_categorical",
        "params": {"values": ["A", "B"], "weights": [0.3, 0.7]},
    }


def test_enum_rule_without_weights_maps_to_random():
    """无有效权重 → 均匀取一（对齐 rule_engine 加权分支回退）。"""
    rule = FieldRule(type=FieldRule.TYPE_ENUM, values=["A", "B"])
    assert field_rule_to_distribution(rule) == {
        "type": "random", "params": {"values": ["A", "B"]}}
    assert field_rule_to_distribution(
        FieldRule(type=FieldRule.TYPE_ENUM, values=["A"], weights=[])) == {
        "type": "random", "params": {"values": ["A"]}}


def test_enum_empty_values_falls_back_to_default_generation():
    """values 空 → None（生成侧 generate_default，对齐 apply_rule 回落）。"""
    assert field_rule_to_distribution(
        FieldRule(type=FieldRule.TYPE_ENUM)) is None


def test_fixed_rule_maps_to_single_value_categorical():
    rule = FieldRule(type=FieldRule.TYPE_FIXED, value="ACTIVE")
    assert field_rule_to_distribution(rule) == {
        "type": "weighted_categorical",
        "params": {"values": ["ACTIVE"], "weights": [1.0]},
    }


def test_unknown_type_falls_back_to_default_generation():
    assert field_rule_to_distribution(FieldRule(type="mystery")) is None
    assert field_rule_to_distribution(FieldRule()) is None


# ---------------------------------------------------------------- ref/refSkew


def test_ref_rule_yields_no_distribution_relation_carries_semantics():
    """纯 ref → 子列不带分布（零孤儿 uniform 由管线 _apply_fk_fields 注入）。"""
    rule = FieldRule(type=FieldRule.TYPE_REF, table="users", column="id")
    assert field_rule_to_distribution(rule) is None


def test_ref_skew_rule_maps_to_ref_distribution_with_default_hot_params():
    """refSkew → ref 分布热行参数（缺省 20/80，对齐 legacy 引擎侧缺省）。"""
    rule = FieldRule(type=FieldRule.TYPE_REF_SKEW,
                     table="users", column="id")
    assert field_rule_to_distribution(rule) == {
        "type": "ref",
        "params": {"hot_percent": 20.0, "hot_share": 80.0},
    }


def test_ref_skew_explicit_hot_params_preserved():
    rule = FieldRule(type=FieldRule.TYPE_REF_SKEW,
                     table="users", column="id",
                     hot_percent=10, hot_share=90)
    assert field_rule_to_distribution(rule) == {
        "type": "ref",
        "params": {"hot_percent": 10.0, "hot_share": 90.0},
    }


# ---------------------------------------------------------------- orphan_rate


def test_ref_with_orphan_rate_maps_to_ref_distribution():
    """表级 orphan_rate → ref 分布携带故意孤儿语义（min/max 由管线补全）。"""
    rule = FieldRule(type=FieldRule.TYPE_REF, table="users", column="id")
    dist = field_rule_to_distribution(rule, orphan_rate=0.05)
    assert dist == {"type": "ref", "params": {"orphan_rate": 0.05}}


def test_ref_with_orphan_rate_nullable_splits_null_path():
    """可空列 → orphan_null_ratio=0.5（legacy 可空列 50% 置 NULL 同口径）。"""
    rule = FieldRule(type=FieldRule.TYPE_REF, table="users", column="id")
    dist = field_rule_to_distribution(rule, orphan_rate=0.05, nullable=True)
    assert dist == {"type": "ref",
                    "params": {"orphan_rate": 0.05, "orphan_null_ratio": 0.5}}


def test_ref_skew_with_orphan_rate_combines_hot_and_orphan():
    rule = FieldRule(type=FieldRule.TYPE_REF_SKEW,
                     table="users", column="id")
    dist = field_rule_to_distribution(rule, orphan_rate=0.1, nullable=True)
    assert dist == {"type": "ref",
                    "params": {"hot_percent": 20.0, "hot_share": 80.0,
                               "orphan_rate": 0.1, "orphan_null_ratio": 0.5}}


# ---------------------------------------------------------------- 关系四元组


def test_ref_rule_maps_to_db_fk_relation():
    rule = FieldRule(type=FieldRule.TYPE_REF, table="users", column="id")
    assert field_rule_to_relation(rule, "orders", "user_id") == {
        "child_table": "orders", "child_column": "user_id",
        "parent_table": "users", "parent_column": "id",
        "relation_kind": "db_fk",
    }


def test_ref_rule_without_explicit_table_column_yields_no_relation():
    """名称推断边随 plan_pipeline 编排，翻译层只接显式四元组。"""
    rule = FieldRule(type=FieldRule.TYPE_REF)
    assert field_rule_to_relation(rule, "orders", "user_id") is None


def test_ref_with_orphan_rate_degrades_relation_to_logical():
    """故意孤儿 → logical（verify WARN 口径；db_fk 零孤儿硬核对不适用）。"""
    rule = FieldRule(type=FieldRule.TYPE_REF, table="users", column="id")
    rel = field_rule_to_relation(rule, "orders", "user_id", orphan_rate=0.05)
    assert rel is not None and rel["relation_kind"] == "logical"


def test_non_ref_rule_yields_no_relation():
    assert field_rule_to_relation(
        FieldRule(type=FieldRule.TYPE_SEQ, seq_start=1),
        "orders", "id") is None


# ---------------------------------------------------------------- camelCase dict 输入


def test_camel_case_json_dict_input():
    """perf_cli rule_from_json 键名口径（camelCase）。"""
    dist = field_rule_to_distribution({
        "type": "seq", "seqPrefix": "U_", "seqStart": 5})
    assert dist == {"type": "sequential",
                    "params": {"seq_prefix": "U_", "seq_start": 5}}
    dist2 = field_rule_to_distribution({
        "type": "range", "min": 0, "max": 100,
        "overThreshold": {"op": ">", "value": 80, "share": 30}})
    assert dist2 == {"type": "over_threshold",
                     "params": {"min": 0, "max": 100, "threshold_value": 80,
                                "threshold_op": ">", "share": 30.0}}
    dist3 = field_rule_to_distribution({
        "type": "refSkew", "table": "users", "column": "id",
        "hotPercent": 15, "hotShare": 85})
    assert dist3 == {"type": "ref",
                     "params": {"hot_percent": 15.0, "hot_share": 85.0}}


# ---------------------------------------------------------------- translate_fields 聚合


def test_translate_fields_aggregates_distributions_relations_warnings():
    fields = {
        "id": FieldRule(type=FieldRule.TYPE_SEQ, seq_prefix="", seq_start=1),
        "user_id": FieldRule(type=FieldRule.TYPE_REF, table="users", column="id"),
        "amount": FieldRule(type=FieldRule.TYPE_RANGE, min=1, max=10000),
        "status": FieldRule(type=FieldRule.TYPE_ENUM,
                            values=["P", "C"], weights=[0.9, 0.1]),
    }
    out = translate_fields(fields, table="orders")
    assert out.distributions["id"]["type"] == "sequential"
    assert out.distributions["user_id"] is None  # 零孤儿 uniform 由管线注入
    assert out.distributions["amount"]["type"] == "uniform"
    assert out.distributions["status"]["type"] == "weighted_categorical"
    assert out.relations == [{
        "child_table": "orders", "child_column": "user_id",
        "parent_table": "users", "parent_column": "id",
        "relation_kind": "db_fk",
    }]
    assert out.warnings == []


def test_translate_fields_hot_skew_emits_hot_row_share_warning():
    fields = {"user_id": FieldRule(type=FieldRule.TYPE_REF_SKEW,
                                   table="users", column="id")}
    out = translate_fields(fields, table="orders")
    assert out.distributions["user_id"]["type"] == "ref"
    assert len(out.warnings) == 1
    assert "user_id" in out.warnings[0] and "热点倾斜极高" in out.warnings[0]


def test_translate_fields_orphan_rate_emits_logical_downgrade_warning():
    fields = {"user_id": FieldRule(type=FieldRule.TYPE_REF,
                                   table="users", column="id")}
    out = translate_fields(fields, table="orders", orphan_rate=0.05,
                           nullable_columns=["user_id"])
    assert out.relations[0]["relation_kind"] == "logical"
    assert out.distributions["user_id"]["params"]["orphan_null_ratio"] == 0.5
    assert any("logical" in w for w in out.warnings)


def test_translate_fields_orphan_rate_applies_to_table_level():
    """表级 orphan_rate 同时作用于 ref 与 refSkew 列（legacy 表级口径）。"""
    fields = {
        "a": FieldRule(type=FieldRule.TYPE_REF, table="p", column="id"),
        "b": FieldRule(type=FieldRule.TYPE_REF_SKEW, table="p", column="id"),
    }
    out = translate_fields(fields, table="orders", orphan_rate=0.02)
    assert out.distributions["a"]["params"]["orphan_rate"] == 0.02
    assert out.distributions["b"]["params"]["orphan_rate"] == 0.02
    assert all(r["relation_kind"] == "logical" for r in out.relations)


def test_translate_fields_rejects_bad_over_threshold_op():
    with pytest.raises(ValueError, match="op"):
        field_rule_to_distribution({
            "type": "range", "overThreshold": {"op": "<", "value": 1,
                                               "share": 30}})
