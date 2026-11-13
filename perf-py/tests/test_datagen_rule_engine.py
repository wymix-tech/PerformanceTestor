"""规则引擎等价单测：移植 RuleEngineCoverageTest + SeqRuleTest（纯逻辑部分）
+ SemanticParserTest 的分布统计用例。

对齐说明：
- Java Math.round 语义在 Python 侧以 floor(x+0.5) 复现；
- random 播种仅控制可重复性，分布断言阈值与 Java 侧一致。
- SeqRuleTest.batchInsertOfSeqColumnNeverViolatesUniqueConstraint 依赖 DB 引擎
  （H2），随 4e（DefaultDataGenEngine 编排）一并移植。
"""

import random
from datetime import datetime
from decimal import Decimal

import pytest

from datagen.model import ColumnMeta, FieldRule, OverThreshold
from datagen.rule_engine import (
    apply_rule,
    base_type,
    generate_default,
    is_integer_family,
    normalize_scalar,
    scale_to_column_type,
    seq_value,
    weighted_index,
)


def col(name: str, type_name: str) -> ColumnMeta:
    return ColumnMeta(name=name, type_name=type_name, size=64, nullable=True)


# ---------- RuleEngineCoverageTest 移植 ----------

def test_generate_default_type_matrix():
    assert isinstance(generate_default(col("a", "INT")), int)
    assert isinstance(generate_default(col("b", "BIGINT")), int)
    assert isinstance(generate_default(col("c", "DOUBLE")), float)
    assert isinstance(generate_default(col("d", "DECIMAL(12,2)")), float)
    assert isinstance(generate_default(col("e", "DATETIME")), datetime)
    assert isinstance(generate_default(col("f", "BIT")), int)
    assert isinstance(generate_default(col("g", "TEXT")), str)
    assert isinstance(generate_default(col("h", "VARCHAR(10)")), str)
    # SET/REAL/TINYTEXT/CHAR 分支
    assert isinstance(generate_default(col("i", "SET('A','B')")), str)
    assert isinstance(generate_default(col("j", "REAL")), float)
    assert isinstance(generate_default(col("k", "TINYTEXT")), str)


def test_generate_default_value_ranges():
    random.seed(42)
    # INT: nextInt(1,100000) → [1, 99999]；BIT: 0/1；日期回看 0~364 天
    for _ in range(500):
        v = generate_default(col("a", "INT"))
        assert 1 <= v <= 99999
        assert generate_default(col("f", "BIT")) in (0, 1)
        d = generate_default(col("e", "DATETIME"))
        assert (datetime.now() - d).days <= 364
    # 文本长度由 size 决定（Java 用 col.getSize()，不解析类型名括号里的长度）
    assert len(generate_default(ColumnMeta(name="h", type_name="VARCHAR(10)", size=10))) == 10
    assert len(generate_default(ColumnMeta(name="h", type_name="VARCHAR(200)", size=200))) == 64
    assert len(generate_default(ColumnMeta(name="h", type_name="TEXT", size=0))) == 1


def test_base_type_matrix():
    assert base_type("INT") == "INT"
    assert base_type("TINYINT(1)") == "INT"
    assert base_type("ENUM('A','B')") == "TEXT"
    assert base_type("SET('A','B')") == "TEXT"
    assert base_type("DOUBLE PRECISION") == "DOUBLE"
    assert base_type("NUMERIC(10)") == "DECIMAL"
    assert base_type("TIMESTAMP") == "DATETIME"
    assert base_type("CLOB") == "TEXT"
    assert base_type("VARCHAR(32)") == "VARCHAR"


def test_normalize_scalar_matrix():
    assert normalize_scalar("42", col("a", "INT")) == 42
    assert normalize_scalar("42", col("b", "BIGINT")) == 42
    assert normalize_scalar(1, col("c", "BIT")) is True
    assert normalize_scalar(0, col("d", "BOOLEAN")) is False
    d = normalize_scalar(3.14159, col("e", "DECIMAL(12,2)"))
    assert d == Decimal("3.14")
    assert normalize_scalar("txt", col("f", "VARCHAR(8)")) == "txt"
    # 解析失败原样返回（Java NumberFormatException 分支）
    assert normalize_scalar("abc", col("a", "INT")) == "abc"
    assert normalize_scalar(None, col("a", "INT")) is None


def test_scale_to_column_type_matrix():
    assert scale_to_column_type(5.4, col("a", "INT")) == 5
    assert scale_to_column_type(5.4, col("b", "BIGINT")) == 5
    assert scale_to_column_type(5.4321, col("c", "DOUBLE")) == pytest.approx(5.43)
    # Java Math.round(2.5)=3（floor+0.5），非银行家舍入
    assert scale_to_column_type(2.5, col("a", "INT")) == 3
    assert scale_to_column_type(-2.5, col("a", "INT")) == -2


def test_is_integer_family():
    assert is_integer_family("INT")
    assert is_integer_family("BIGINT")
    assert is_integer_family("TINYINT(1)")
    assert not is_integer_family("VARCHAR(10)")
    assert not is_integer_family("DECIMAL(12,2)")


def test_enum_null_values_falls_back_to_default():
    r = FieldRule()
    r.type = FieldRule.TYPE_ENUM
    # values 缺省 → generateDefault（VARCHAR → String）
    assert isinstance(apply_rule(r, col("s", "VARCHAR(8)")), str)


def test_range_respects_min_and_max():
    r = FieldRule(type=FieldRule.TYPE_RANGE, min=100.0, max=5000.0)
    for _ in range(5000):
        v = float(apply_rule(r, col("amt", "DECIMAL(12,2)")))
        assert 100.0 <= v <= 5000.0


def test_range_max_less_than_min_swaps():
    r = FieldRule(type=FieldRule.TYPE_RANGE, min=5000.0, max=100.0)
    v = float(apply_rule(r, col("amt", "DECIMAL(12,2)")))
    assert 100.0 <= v <= 5000.0


def test_over_threshold_op_gte_inclusive():
    """op=">=" 时越阈区间含阈值本身（Java Math.nextUp 仅用于 ">"）。"""
    random.seed(7)
    r = FieldRule(
        type=FieldRule.TYPE_RANGE,
        min=0.0, max=10000.0,
        over_threshold=OverThreshold(op=">=", value=1000, share=100),
    )
    for _ in range(500):
        v = float(apply_rule(r, col("amt", "DECIMAL(12,2)")))
        assert 1000.0 <= v <= 10000.0


def test_weighted_index_zero_weight_never_selected():
    weights = [10.0, 0.0, 90.0]
    random.seed(1)
    # 零权重值不得被选中
    for _ in range(20000):
        idx = weighted_index(weights)
        assert idx in (0, 2), f"零权重索引被选中: {idx}"


def test_unknown_rule_type_falls_back_to_default():
    r = FieldRule(type="mystery")
    assert isinstance(apply_rule(r, col("s", "VARCHAR(8)")), str)


def test_fixed_type_normalization():
    int_fixed = FieldRule.fixed("7")
    assert apply_rule(int_fixed, col("n", "INT")) == 7
    str_fixed = FieldRule.fixed("PAID")
    assert apply_rule(str_fixed, col("s", "VARCHAR(8)")) == "PAID"


# ---------- SeqRuleTest（纯逻辑部分）移植 ----------

def _seq(prefix, start) -> FieldRule:
    return FieldRule(type=FieldRule.TYPE_SEQ, seq_prefix=prefix, seq_start=start)


def test_value_is_prefix_plus_start_plus_row_index():
    assert seq_value(_seq("ORD_", 1), 0) == "ORD_1"
    assert seq_value(_seq("ORD_", 1), 4) == "ORD_5"
    assert seq_value(_seq("ORD_", 100), 0) == "ORD_100"


def test_null_prefix_and_start_fall_back_safely():
    r = FieldRule(type=FieldRule.TYPE_SEQ)
    assert seq_value(r, 0) == "1"
    assert seq_value(r, 2) == "3"


def test_start_shift_moves_whole_range_which_enables_repeat_execution():
    first = {seq_value(_seq("C", 1), i) for i in range(50)}
    second = {seq_value(_seq("C", 51), i) for i in range(50)}
    assert first.isdisjoint(second), "seqStart 刷新到 MAX+1 后，新一批取值必须与历史批次完全不重叠"


def test_apply_rule_returns_string_for_varchar_column():
    v = apply_rule(_seq("ORD_", 7), col("coupon_no", "VARCHAR"), 3)
    assert v == "ORD_10"


def test_apply_rule_on_numeric_column_yields_number():
    v = apply_rule(_seq("", 1), col("seq_no", "BIGINT"), 2)
    assert v == 3


# ---------- SemanticParserTest 分布统计用例移植 ----------

def test_range_over_threshold_share():
    random.seed(11)
    rule = FieldRule(type=FieldRule.TYPE_RANGE, min=100.0, max=5000.0,
                     over_threshold=OverThreshold(value=1000, share=30))
    total = 20_000
    over = sum(1 for _ in range(total)
               if float(apply_rule(rule, col("amount", "DECIMAL(12,2)"))) > 1000)
    share = over * 100.0 / total
    assert 26 < share < 34, f"overThreshold actual {share}"


def test_enum_weights_distribution():
    random.seed(13)
    rule = FieldRule(type=FieldRule.TYPE_ENUM, values=["PENDING", "PAID", "DONE"],
                     weights=[10.0, 80.0, 10.0])
    total = 20_000
    paid = sum(1 for _ in range(total)
               if apply_rule(rule, col("status", "VARCHAR(16)")) == "PAID")
    share = paid * 100.0 / total
    assert 77 < share < 83, f"weights actual {share}"


def test_range_threshold_acts_as_upper_bound_of_normal_band():
    """未越阈分支上界为阈值（ot.value > min 时），分布上不应大量越过阈值。"""
    random.seed(17)
    rule = FieldRule(type=FieldRule.TYPE_RANGE, min=0.0, max=10000.0,
                     over_threshold=OverThreshold(value=100, share=0))
    # share=0 → 全部走未越阈区间 [0, 100]
    for _ in range(2000):
        assert float(apply_rule(rule, col("amt", "DECIMAL(12,2)"))) <= 100.0
