"""Primitive 语义单测（原 T4.1 收编对照收编终态平移）。

B7 续批次 11（T7.9 退役）：legacy rule_engine 对照段随包退役，
absolute 断言保留（fixed / range（含 overThreshold）/ enum+weights / seq）。
"""

from __future__ import annotations

import random
from datetime import datetime
from decimal import Decimal

import pytest

from ptde.generator import primitive as prim
from ptde.models.wire import ColumnMeta, FieldRule, OverThreshold


def col(name: str, type_name: str) -> ColumnMeta:
    return ColumnMeta(name=name, type_name=type_name, size=64, nullable=True)


# ---------------------------------------------------------------- 模块形状


def test_module_shape():
    """收编终态含全部 9 个公开函数与配套辅助。"""
    for name in ("apply_rule", "weighted_index", "normalize_scalar",
                 "scale_to_column_type", "base_type", "is_integer_family",
                 "seq_value", "generate_default", "random_text"):
        assert hasattr(prim, name), name


def test_base_type_matrix():
    """类型名 → 基础类型族（Java BaseType 对齐）。"""
    assert prim.base_type("INT") == "INT"
    assert prim.base_type("TINYINT(1)") == "INT"
    assert prim.base_type("BIGINT") == "BIGINT"
    assert prim.base_type("DECIMAL(12,2)") == "DECIMAL"
    assert prim.base_type("NUMERIC(10)") == "DECIMAL"
    assert prim.base_type("DOUBLE PRECISION") == "DOUBLE"
    assert prim.base_type("VARCHAR(32)") == "VARCHAR"
    assert prim.base_type("TIMESTAMP") == "DATETIME"
    assert prim.base_type("DATE") == "DATE"
    assert prim.base_type("BOOLEAN") == "BIT"
    assert prim.base_type("CLOB") == "TEXT"
    assert prim.base_type("ENUM('A','B')") == "TEXT"
    assert prim.base_type("FLOAT") == "FLOAT"
    assert prim.is_integer_family("INT") and prim.is_integer_family("BIGINT")
    assert not prim.is_integer_family("VARCHAR(32)")


@pytest.mark.parametrize("v,tname", [
    ("42", "INT"), ("42", "BIGINT"), (1, "BIT"), (0, "BOOLEAN"),
    (3.14159, "DECIMAL(12,2)"), ("txt", "VARCHAR(8)"), (True, "VARCHAR(8)"),
    (None, "INT"), ("abc", "INT"), (True, "INT"),
])
def test_normalize_scalar_wire_types(v, tname):
    out = prim.normalize_scalar(v, col("c", tname))
    assert out is not None or v is None


def test_generate_default_type_and_length_matrix():
    assert isinstance(prim.generate_default(col("a", "INT")), int)
    assert isinstance(prim.generate_default(col("c", "DOUBLE")), float)
    assert isinstance(prim.generate_default(col("d", "DECIMAL(12,2)")), float)
    assert isinstance(prim.generate_default(col("e", "DATETIME")), datetime)
    assert isinstance(prim.generate_default(col("g", "TEXT")), str)
    # 文本长度由 size 决定（Java 用 col.getSize()，不解析类型名括号里的长度）
    assert len(prim.generate_default(ColumnMeta(name="h", type_name="VARCHAR(10)", size=10))) == 10
    assert len(prim.generate_default(ColumnMeta(name="h", type_name="VARCHAR(200)", size=200))) == 64
    assert len(prim.generate_default(ColumnMeta(name="h", type_name="TEXT", size=0))) == 1


# ---------------------------------------------------------------- 语义保留（原对照 legacy 单测口径）


def test_scale_java_round_semantics():
    """Java Math.round(2.5)=3（floor+0.5），非银行家舍入。"""
    assert prim.scale_to_column_type(2.5, col("a", "INT")) == 3
    assert prim.scale_to_column_type(-2.5, col("a", "INT")) == -2
    assert prim.scale_to_column_type(5.4, col("b", "BIGINT")) == 5
    assert prim.scale_to_column_type(5.4321, col("c", "DOUBLE")) == pytest.approx(5.43)


def test_normalize_scalar_matrix():
    assert prim.normalize_scalar("42", col("a", "INT")) == 42
    assert prim.normalize_scalar(1, col("c", "BIT")) is True
    assert prim.normalize_scalar(0, col("d", "BOOLEAN")) is False
    assert prim.normalize_scalar(3.14159, col("e", "DECIMAL(12,2)")) == Decimal("3.14")
    # 解析失败原样返回（Java NumberFormatException 分支）
    assert prim.normalize_scalar("abc", col("a", "INT")) == "abc"
    assert prim.normalize_scalar(None, col("a", "INT")) is None


def test_fixed_type_normalization():
    assert prim.apply_rule(FieldRule.fixed("7"), col("n", "INT")) == 7
    assert prim.apply_rule(FieldRule.fixed("PAID"), col("s", "VARCHAR(8)")) == "PAID"


def test_range_respects_min_and_max():
    r = FieldRule(type=FieldRule.TYPE_RANGE, min=100.0, max=5000.0)
    for _ in range(5000):
        v = float(prim.apply_rule(r, col("amt", "DECIMAL(12,2)")))
        assert 100.0 <= v <= 5000.0


def test_range_max_less_than_min_swaps():
    r = FieldRule(type=FieldRule.TYPE_RANGE, min=5000.0, max=100.0)
    v = float(prim.apply_rule(r, col("amt", "DECIMAL(12,2)")))
    assert 100.0 <= v <= 5000.0


def test_over_threshold_op_gte_inclusive():
    """op=">=" 时越阈区间含阈值本身（Java Math.nextUp 仅用于 ">"）。"""
    random.seed(7)
    r = FieldRule(type=FieldRule.TYPE_RANGE, min=0.0, max=10000.0,
                  over_threshold=OverThreshold(op=">=", value=1000, share=100))
    for _ in range(500):
        v = float(prim.apply_rule(r, col("amt", "DECIMAL(12,2)")))
        assert 1000.0 <= v <= 10000.0


def test_range_over_threshold_share():
    random.seed(11)
    r = FieldRule(type=FieldRule.TYPE_RANGE, min=100.0, max=5000.0,
                  over_threshold=OverThreshold(value=1000, share=30))
    total = 20_000
    over = sum(1 for _ in range(total)
               if float(prim.apply_rule(r, col("amount", "DECIMAL(12,2)"))) > 1000)
    share = over * 100.0 / total
    assert 26 < share < 34, f"overThreshold actual {share}"


def test_range_threshold_acts_as_upper_bound_of_normal_band():
    """share=0 → 全部走未越阈区间 [0, threshold]。"""
    random.seed(17)
    r = FieldRule(type=FieldRule.TYPE_RANGE, min=0.0, max=10000.0,
                  over_threshold=OverThreshold(value=100, share=0))
    for _ in range(2000):
        assert float(prim.apply_rule(r, col("amt", "DECIMAL(12,2)"))) <= 100.0


def test_enum_weights_distribution():
    random.seed(13)
    r = FieldRule(type=FieldRule.TYPE_ENUM, values=["PENDING", "PAID", "DONE"],
                  weights=[10.0, 80.0, 10.0])
    total = 20_000
    paid = sum(1 for _ in range(total)
               if prim.apply_rule(r, col("status", "VARCHAR(16)")) == "PAID")
    share = paid * 100.0 / total
    assert 77 < share < 83, f"weights actual {share}"


def test_weighted_index_zero_weight_never_selected():
    weights = [10.0, 0.0, 90.0]
    random.seed(1)
    for _ in range(20000):
        idx = prim.weighted_index(weights)
        assert idx in (0, 2), f"零权重索引被选中: {idx}"


def test_enum_null_values_falls_back_to_default():
    r = FieldRule(type=FieldRule.TYPE_ENUM)
    assert isinstance(prim.apply_rule(r, col("s", "VARCHAR(8)")), str)


def test_unknown_rule_type_falls_back_to_default():
    r = FieldRule(type="mystery")
    assert isinstance(prim.apply_rule(r, col("s", "VARCHAR(8)")), str)


# ---------------------------------------------------------------- SeqRuleTest（纯逻辑部分）


def _seq(prefix, start) -> FieldRule:
    return FieldRule(type=FieldRule.TYPE_SEQ, seq_prefix=prefix, seq_start=start)


def test_value_is_prefix_plus_start_plus_row_index():
    assert prim.seq_value(_seq("ORD_", 1), 0) == "ORD_1"
    assert prim.seq_value(_seq("ORD_", 1), 4) == "ORD_5"
    assert prim.seq_value(_seq("ORD_", 100), 0) == "ORD_100"


def test_null_prefix_and_start_fall_back_safely():
    r = FieldRule(type=FieldRule.TYPE_SEQ)
    assert prim.seq_value(r, 0) == "1"
    assert prim.seq_value(r, 2) == "3"


def test_start_shift_moves_whole_range_which_enables_repeat_execution():
    first = {prim.seq_value(_seq("C", 1), i) for i in range(50)}
    second = {prim.seq_value(_seq("C", 51), i) for i in range(50)}
    assert first.isdisjoint(second), \
        "seqStart 刷新到 MAX+1 后，新一批取值必须与历史批次完全不重叠"


def test_apply_rule_returns_string_for_varchar_column():
    assert prim.apply_rule(_seq("ORD_", 7), col("coupon_no", "VARCHAR"), 3) == "ORD_10"


def test_apply_rule_on_numeric_column_yields_number():
    assert prim.apply_rule(_seq("", 1), col("seq_no", "BIGINT"), 2) == 3


# ---------------------------------------------------------------- 技术债清理


def test_generate_default_for_parity_with_generate_default():
    """generate_default_for 与 generate_default 语义逐值一致（类型矩阵）。"""
    types = ["BIGINT", "INT", "SMALLINT", "TINYINT", "DOUBLE", "FLOAT",
             "DECIMAL(18,2)", "DATETIME", "DATE", "TIMESTAMP", "BIT",
             "TEXT", "VARCHAR(255)", "CHAR(32)", "ENUM('A','B')", None]
    for t in types:
        for size in (0, 64, 200):
            c1 = prim.generate_default(ColumnMeta(name="c", type_name=t, size=size),
                                       rng=random.Random(7))
            c2 = prim.generate_default_for(t, size=size, rng=random.Random(7))
            assert c1 == c2, f"type={t!r} size={size} 两口径不一致"
