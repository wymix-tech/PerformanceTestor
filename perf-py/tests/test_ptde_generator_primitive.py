"""T4.1 验收：Primitive 收编对照（同输入同输出；rule_engine.py 本体保留不动，A4a 终态退役）。

对照基准：datagen/rule_engine.py 与其单测 tests/test_datagen_rule_engine.py
（RuleEngineCoverageTest + SeqRuleTest 纯逻辑部分 + 分布统计用例）。
fixed / range（含 overThreshold）/ enum+weights / seq 语义逐一对照。
"""

from __future__ import annotations

import random
from datetime import datetime
from decimal import Decimal

import pytest

from datagen import rule_engine as legacy
from datagen.model import ColumnMeta, FieldRule, OverThreshold
from ptde.generator import primitive as prim


def col(name: str, type_name: str) -> ColumnMeta:
    return ColumnMeta(name=name, type_name=type_name, size=64, nullable=True)


# ---------------------------------------------------------------- 模块形状


def test_module_shape_parity():
    """收编副本含全部 7 个收编函数与配套辅助；legacy 本体未改动。"""
    for name in ("apply_rule", "weighted_index", "normalize_scalar",
                 "scale_to_column_type", "base_type", "is_integer_family",
                 "seq_value", "generate_default", "random_text"):
        assert hasattr(prim, name), name


# ---------------------------------------------------------------- 同输入同输出对照


@pytest.mark.parametrize("type_name", [
    "INT", "TINYINT(1)", "BIGINT", "ENUM('A','B')", "SET('A','B')",
    "DOUBLE PRECISION", "NUMERIC(10)", "TIMESTAMP", "CLOB", "VARCHAR(32)",
    "FLOAT", "REAL", "DATE", "BOOLEAN", "MEDIUMTEXT",
])
def test_base_type_parity(type_name):
    assert prim.base_type(type_name) == legacy.base_type(type_name)
    assert prim.is_integer_family(type_name) == legacy.is_integer_family(type_name)


@pytest.mark.parametrize("v,tname", [
    ("42", "INT"), ("42", "BIGINT"), (1, "BIT"), (0, "BOOLEAN"),
    (3.14159, "DECIMAL(12,2)"), ("txt", "VARCHAR(8)"), (True, "VARCHAR(8)"),
    (None, "INT"), ("abc", "INT"), (True, "INT"),
])
def test_normalize_scalar_parity(v, tname):
    assert prim.normalize_scalar(v, col("c", tname)) \
        == legacy.normalize_scalar(v, col("c", tname))


@pytest.mark.parametrize("v,tname", [
    (5.4, "INT"), (5.4, "BIGINT"), (5.4321, "DOUBLE"),
    (2.5, "INT"), (-2.5, "INT"), (7.777, "DECIMAL(12,2)"),
])
def test_scale_to_column_type_parity(v, tname):
    assert prim.scale_to_column_type(v, col("c", tname)) \
        == legacy.scale_to_column_type(v, col("c", tname))


@pytest.mark.parametrize("prefix,start,idx", [("ORD_", 1, 0), ("ORD_", 1, 4),
                                              ("ORD_", 100, 0), (None, None, 2)])
def test_seq_value_parity(prefix, start, idx):
    rl = FieldRule(type=FieldRule.TYPE_SEQ, seq_prefix=prefix, seq_start=start)
    assert prim.seq_value(rl, idx) == legacy.seq_value(rl, idx)


def _rule_cases():
    return [
        FieldRule.fixed("7"),
        FieldRule.fixed("PAID"),
        FieldRule(type=FieldRule.TYPE_RANGE, min=100.0, max=5000.0),
        FieldRule(type=FieldRule.TYPE_RANGE, min=5000.0, max=100.0),
        FieldRule(type=FieldRule.TYPE_RANGE, min=0.0, max=10000.0,
                  over_threshold=OverThreshold(op=">=", value=1000, share=100)),
        FieldRule(type=FieldRule.TYPE_RANGE, min=0.0, max=10000.0,
                  over_threshold=OverThreshold(value=1000, share=30)),
        FieldRule(type=FieldRule.TYPE_RANGE, min=0.0, max=10000.0,
                  over_threshold=OverThreshold(value=100, share=0)),
        FieldRule(type=FieldRule.TYPE_ENUM, values=["PENDING", "PAID", "DONE"],
                  weights=[10.0, 80.0, 10.0]),
        FieldRule(type=FieldRule.TYPE_ENUM, values=["A", "B"]),
        FieldRule(type=FieldRule.TYPE_ENUM),
        FieldRule(type=FieldRule.TYPE_SEQ, seq_prefix="ORD_", seq_start=1),
        FieldRule(type="mystery"),
    ]


@pytest.mark.parametrize("tname", ["INT", "BIGINT", "DECIMAL(12,2)",
                                   "VARCHAR(16)", "BIT", "DOUBLE"])
def test_apply_rule_parity_seeded_rng(tname):
    """同规则 + 同注入 rng：逐值一致（fixed/range/overThreshold/enum+weights/seq/兜底）。"""
    for rule in _rule_cases():
        ra, rb = random.Random(7), random.Random(7)
        a = [legacy.apply_rule(rule, col("c", tname), i, ra) for i in range(200)]
        b = [prim.apply_rule(rule, col("c", tname), i, rb) for i in range(200)]
        assert a == b, f"规则 {rule.type} 在 {tname} 上与 legacy 不一致"


def test_apply_rule_parity_module_random():
    """rng=None（模块级 random）路径：同种子下两套副本调用序列一致。"""
    for rule in _rule_cases():
        random.seed(2026)
        a = [legacy.apply_rule(rule, col("c", "DECIMAL(12,2)"), i) for i in range(100)]
        random.seed(2026)
        b = [prim.apply_rule(rule, col("c", "DECIMAL(12,2)"), i) for i in range(100)]
        assert a == b


def test_generate_default_parity_seeded():
    for tname in ("INT", "BIGINT", "DOUBLE", "DECIMAL(12,2)", "BIT"):
        ra, rb = random.Random(9), random.Random(9)
        a = [legacy.generate_default(col("c", tname), ra) for _ in range(100)]
        b = [prim.generate_default(col("c", tname), rb) for _ in range(100)]
        assert a == b


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


def test_random_text_and_weighted_index_parity():
    assert prim.random_text(16, random.Random(5)) == legacy.random_text(16, random.Random(5))
    for weights in ([10.0, 0.0, 90.0], [1, 1], [50.0], [0.0, 0.0, 1.0]):
        ra, rb = random.Random(3), random.Random(3)
        a = [legacy.weighted_index(weights, ra) for _ in range(500)]
        b = [prim.weighted_index(weights, rb) for _ in range(500)]
        assert a == b


# ---------------------------------------------------------------- 语义保留（对照 legacy 单测口径）


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
