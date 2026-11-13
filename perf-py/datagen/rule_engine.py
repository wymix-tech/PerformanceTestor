"""规则引擎：移植自 engine/RuleEngine.java。

字段值生成（fixed/range/enum+weights/seq/overThreshold + 类型默认映射）。
纯函数式（ref/refSkew 的采样池由引擎层注入），可独立单测。

数值语义对齐 Java：Math.round 为 floor(x+0.5)（非 Python 银行家舍入）；
nextInt(a,b) 为 [a,b)（Python 用 randint(a, b-1) 等价表达）。
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from datagen.model import ColumnMeta, FieldRule

_TEXT_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _java_round(x: float) -> int:
    """Java Math.round 语义：floor(x + 0.5)。"""
    return math.floor(x + 0.5)


def generate_default(col: ColumnMeta):
    """列类型默认生成器（承接原 DataGenService.generateDefault 行为）。"""
    type_name = col.type_name if col.type_name is not None else "VARCHAR"
    base = base_type(type_name)
    if base == "INT":
        return random.randint(1, 99999)
    if base == "BIGINT":
        return random.randint(0, 2**63 - 1)
    if base in ("FLOAT", "DOUBLE"):
        return _java_round(random.random() * 10000) / 100.0
    if base == "DECIMAL":
        return _java_round(random.random() * 100000) / 100.0
    if base in ("DATE", "DATETIME"):
        return (datetime.now() - timedelta(days=random.randint(0, 364))).replace(microsecond=0)
    if base == "BIT":
        return random.randint(0, 1)
    if base == "TEXT":
        return random_text(min(max(col.size, 1), 200))
    return random_text(max(1, min(col.size, 64)))


def base_type(type_name: str) -> str:
    """ENUM('A','B') 等复合类型名的基础类型提取（结果大写）。"""
    t = type_name.upper()
    paren = t.find("(")
    head = t[:paren].strip() if paren > 0 else t.strip()
    if head == "ENUM" or head == "SET":
        return "TEXT"
    if head in ("INT", "INTEGER", "SMALLINT", "TINYINT", "MEDIUMINT", "YEAR"):
        return "INT"
    if head in ("FLOAT", "REAL"):
        return "FLOAT"
    if head in ("DOUBLE", "DOUBLE PRECISION"):
        return "DOUBLE"
    if head in ("DECIMAL", "NUMERIC"):
        return "DECIMAL"
    if head == "DATE":
        return "DATE"
    if head in ("DATETIME", "TIMESTAMP"):
        return "DATETIME"
    if head in ("BIT", "BOOLEAN", "BOOL"):
        return "BIT"
    if head in ("TEXT", "MEDIUMTEXT", "LONGTEXT", "TINYTEXT", "CLOB"):
        return "TEXT"
    return head  # VARCHAR/CHAR 等


def is_integer_family(type_name: str) -> bool:
    """是否整数族列（关联列类型兼容校验用）。"""
    base = base_type(type_name)
    return base == "INT" or base == "BIGINT"


def apply_rule(rule: FieldRule, col: ColumnMeta, row_index: int = 0):
    """按规则生成一行该列的值（ref/refSkew 由引擎从 ID 池采样后直接覆盖）。

    row_index 从 0 起，仅供 seq 规则生成单调递增序号使用。
    """
    type_ = rule.type if rule.type is not None else ""
    if type_ == FieldRule.TYPE_FIXED:
        return normalize_scalar(rule.value, col)
    if type_ == FieldRule.TYPE_RANGE:
        mn = rule.min if rule.min is not None else 0.0
        mx = rule.max if rule.max is not None else 100.0
        if mx < mn:
            mn, mx = mx, mn
        ot = rule.over_threshold
        if ot is not None and random.random() * 100 < ot.share:
            # 越过阈值区间：(value, max]（op=">"）或 [value, max]（op=">="）
            low = ot.value if ot.op == ">=" else math.nextafter(ot.value, math.inf)
            picked = low + random.random() * max(0.0, mx - low)
        else:
            # 未越阈区间：[min, threshold]（阈值低于 min 时回退 [min, max]）
            high = ot.value if (ot is not None and ot.value > mn) else mx
            picked = mn + random.random() * (high - mn)
        return scale_to_column_type(picked, col)
    if type_ == FieldRule.TYPE_ENUM:
        values = rule.values
        if not values:
            return generate_default(col)
        weights = rule.weights
        if (weights is not None and len(weights) == len(values)
                and sum(weights) > 0):
            idx = weighted_index(weights)
        else:
            idx = random.randrange(len(values))
        return normalize_scalar(values[idx], col)
    if type_ == FieldRule.TYPE_SEQ:
        return normalize_scalar(seq_value(rule, row_index), col)
    return generate_default(col)


def seq_value(rule: FieldRule, row_index: int) -> str:
    """seq 值：seq_prefix + (seq_start + row_index)。

    单调递增且带执行时刷新的起始值，保证同一方案重复执行也不撞唯一键。
    """
    start = rule.seq_start if rule.seq_start is not None else 1
    prefix = rule.seq_prefix if rule.seq_prefix is not None else ""
    return prefix + str(start + row_index)


def weighted_index(weights: list[float]) -> int:
    """权重采样：累计分布 + 线性扫描（候选值通常 ≤ 32 个，无需二分）。"""
    total = sum(weights)
    pick = random.random() * total
    acc = 0.0
    for i, w in enumerate(weights):
        acc += w
        if pick < acc:
            return i
    return len(weights) - 1


def normalize_scalar(v, col: ColumnMeta):
    """标量归一化：数值列收 Number，布尔列收 0/1，其余转 String。"""
    if v is None:
        return None
    base = base_type(col.type_name if col.type_name is not None else "VARCHAR")
    is_number = isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)
    if base == "INT":
        if is_number:
            return int(v)  # Java intValue(): 向零截断
        try:
            return int(str(v).strip())
        except ValueError:
            return v
    if base == "BIGINT":
        if is_number:
            return int(v)  # Java longValue()
        try:
            return int(str(v).strip())
        except ValueError:
            return v
    if base in ("FLOAT", "DOUBLE", "DECIMAL"):
        if is_number:
            return scale_to_column_type(float(v), col)
        return v
    if base == "BIT":
        if is_number:
            return int(v) != 0
        # Java Boolean.parseBoolean：仅精确忽略大小写匹配 "true"
        return str(v).lower() == "true"
    # Java String.valueOf 语义：Boolean → "true"/"false"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def scale_to_column_type(picked: float, col: ColumnMeta):
    """数值按列类型定标：整数族取整，DECIMAL 保留 2 位，FLOAT/DOUBLE 保留 2 位。"""
    base = base_type(col.type_name if col.type_name is not None else "DECIMAL")
    if base == "INT":
        return int(_java_round(picked))
    if base == "BIGINT":
        return _java_round(picked)
    if base in ("FLOAT", "DOUBLE"):
        return _java_round(picked * 100) / 100.0
    # Java BigDecimal.valueOf(picked).setScale(2, HALF_UP) 等价
    return Decimal(str(picked)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def random_text(length: int) -> str:
    return "".join(random.choice(_TEXT_CHARS) for _ in range(length))
