"""legacy FieldRule → PTDE 字段规格翻译路径（B7 续批次 9：T7.6/T7.7 最后硬前置）。

打通「legacy 规格（规则 JSON）→ PTDE 规格（FieldSpec.distribution + 关系
四元组）」的翻译路径，为 datagen_runner/datagen_api 切换提供单一出处。
语义逐项对齐 legacy（datagen/model.py + rule_engine.py + default_engine.py）：

- ``seq`` → ``sequential`` 分布（seq_prefix/seq_start 缺省 ""/1，对齐
  rule_engine.seq_value）；
- ``range`` → ``uniform``（min/max 缺省 0/100）；带 ``over_threshold`` →
  ``over_threshold`` 分布（share% 超阈值区间分配，非截断）；
- ``enum`` → ``weighted_categorical``（权重有效时）或 ``random``（无权重
  均匀取一，对齐 rule_engine 加权分支）；values 空 → None（生成侧缺省
  生成，对齐 apply_rule 回落）；
- ``fixed`` → ``weighted_categorical`` 单值常量；
- ``ref``/``refSkew`` → 关系四元组（table+column 显式才产出；名称推断边
  随 plan_pipeline 编排，不在翻译层）：
  - 纯 ``ref``（orphan_rate=0）→ 子列不带分布（管线 _apply_fk_fields 注入
    父主键区间 uniform，零孤儿语义）；
  - ``refSkew`` → 子列 ``ref`` 分布（hot_percent/hot_share 缺省 20/80，
    对齐 legacy 引擎侧缺省；min/max 由管线补全父区间；热区两段线性权重
    语义在 ref 采样器，对齐 IdPool.build_skew_weights）；
  - orphan_rate>0（表级，legacy TableSpec/DataGenSpec 口径）→ 子列 ``ref``
    分布携带 orphan_rate（无效 ID = 池 max+1+randrange(1000)，对齐
    IdPool.sample_invalid_id）；可空列 → orphan_null_ratio=0.5（legacy
    可空列 50% 置 NULL 同口径），关系 kind 降级 ``logical``（verify WARN
    如实暴露——故意孤儿不得配 db_fk 零孤儿硬核对）；
- 未知类型/缺省 → None（生成侧 generate_default 缺省生成，对齐
  apply_rule 末行回落）。

输入 duck-typing：legacy ``FieldRule``/V2 镜像（属性访问）或 dict
（snake_case / camelCase JSON 均收，对齐 perf_cli rule_from_json 键名）。
行级语义差异显式记录：legacy orphan 掷骰同行多个 ref 列共享（行级一次），
PTDE 逐列独立掷骰（边际分布一致，行内相关性不同）——双跑对照按统计
容差口径核对。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ptde.generator.samplers.ref import hot_row_share_warning_text

__all__ = [
    "TranslatedFields",
    "field_rule_to_distribution",
    "field_rule_to_relation",
    "translate_fields",
]

# legacy 引擎侧缺省（default_engine.py：hot_percent/hot_share 缺省 20/80）
LEGACY_DEFAULT_HOT_PERCENT = 20.0
LEGACY_DEFAULT_HOT_SHARE = 80.0

_TYPE_FIXED = "fixed"
_TYPE_RANGE = "range"
_TYPE_ENUM = "enum"
_TYPE_REF = "ref"
_TYPE_REF_SKEW = "refskew"  # 小写归一（legacy "refSkew"）
_TYPE_SEQ = "seq"

_CAMEL_KEYS = {
    "hot_percent": "hotPercent",
    "hot_share": "hotShare",
    "over_threshold": "overThreshold",
    "seq_prefix": "seqPrefix",
    "seq_start": "seqStart",
    "orphan_rate": "orphanRate",
}


def _get(rule: object, name: str, default=None):
    """duck-typing 取值：属性（legacy FieldRule/V2）优先，dict（snake/camel）兜底。"""
    if isinstance(rule, Mapping):
        if name in rule and rule[name] is not None:
            return rule[name]
        camel = _CAMEL_KEYS.get(name, name)
        return rule.get(camel, default)
    v = getattr(rule, name, None)
    return default if v is None else v


def _type_of(rule: object) -> str:
    t = _get(rule, "type")
    return (t or "").strip().lower()


def _over_threshold_params(rule: object) -> dict | None:
    ot = _get(rule, "over_threshold")
    if ot is None:
        return None
    # legacy OverThreshold 缺省：op=">"、value=0.0、share=30.0
    op = _get(ot, "op", ">") or ">"  # 空串回退（legacy rule_from_json 同口径）
    value = _get(ot, "value", 0.0)
    share = _get(ot, "share", 30.0)
    if op not in (">", ">="):
        raise ValueError(f"over_threshold op 仅支持 >/>=（显式失败），得到 {op!r}")
    return {
        "min": _get(rule, "min", 0.0),
        "max": _get(rule, "max", 100.0),
        "threshold_value": value,
        "threshold_op": op,
        "share": float(share),
    }


def _ref_params(
    rule: object, *, orphan_rate: float, nullable: bool,
) -> dict | None:
    """ref/refSkew → ref 分布参数（min/max 由管线补全父实体主键区间）。"""
    t = _type_of(rule)
    params: dict = {}
    if t == _TYPE_REF_SKEW:
        params["hot_percent"] = float(_get(
            rule, "hot_percent", LEGACY_DEFAULT_HOT_PERCENT))
        params["hot_share"] = float(_get(
            rule, "hot_share", LEGACY_DEFAULT_HOT_SHARE))
    if orphan_rate > 0:
        params["orphan_rate"] = orphan_rate
        if nullable:
            params["orphan_null_ratio"] = 0.5  # legacy 可空列 50% 置 NULL 同口径
    return params or None


def field_rule_to_distribution(
    rule: object,
    *,
    orphan_rate: float = 0.0,
    nullable: bool = False,
) -> dict | None:
    """legacy FieldRule → PTDE 分布 dict（JSON 原生形态；None = 生成侧缺省）。

    ``ref``/``refSkew`` 的关系四元组走 ``field_rule_to_relation``，本函数
    只负责子列分布（纯 ref 无分布——零孤儿 uniform 由管线注入）。
    """
    t = _type_of(rule)
    if t == _TYPE_SEQ:
        return {
            "type": "sequential",
            "params": {
                "seq_prefix": _get(rule, "seq_prefix", ""),
                "seq_start": int(_get(rule, "seq_start", 1)),
            },
        }
    if t == _TYPE_RANGE:
        ot = _over_threshold_params(rule)
        if ot is not None:
            return {"type": "over_threshold", "params": ot}
        return {"type": "uniform", "params": {
            "min": _get(rule, "min", 0.0), "max": _get(rule, "max", 100.0)}}
    if t == _TYPE_ENUM:
        values = _get(rule, "values")
        if not values:
            return None  # legacy：values 空 → generate_default 回落
        weights = _get(rule, "weights")
        if (weights is not None and len(weights) == len(values)
                and sum(weights) > 0):
            return {"type": "weighted_categorical",
                    "params": {"values": list(values), "weights": list(weights)}}
        return {"type": "random", "params": {"values": list(values)}}
    if t == _TYPE_FIXED:
        return {"type": "weighted_categorical",
                "params": {"values": [_get(rule, "value")], "weights": [1.0]}}
    if t in (_TYPE_REF, _TYPE_REF_SKEW):
        params = _ref_params(rule, orphan_rate=orphan_rate, nullable=nullable)
        if params is None:
            return None  # 纯 ref：零孤儿 uniform 由管线 _apply_fk_fields 注入
        return {"type": "ref", "params": params}
    return None  # 未知类型/缺省 → 生成侧缺省生成（legacy apply_rule 回落同口径）


def field_rule_to_relation(
    rule: object,
    child_table: str,
    child_column: str,
    *,
    orphan_rate: float = 0.0,
) -> dict | None:
    """legacy ref/refSkew 规则 → 关系四元组（JSON 原生 dict；非 ref 语义 → None）。

    - table+column 显式才产出（名称推断边随 plan_pipeline 编排，不在翻译层）；
    - orphan_rate>0（故意孤儿）→ relation_kind 降级 ``logical``
      （verify WARN 如实暴露）；否则 ``db_fk``（零孤儿硬核对）。
    """
    if _type_of(rule) not in (_TYPE_REF, _TYPE_REF_SKEW):
        return None
    parent_table = _get(rule, "table")
    parent_column = _get(rule, "column")
    if not parent_table or not parent_column:
        return None
    return {
        "child_table": child_table,
        "child_column": child_column,
        "parent_table": parent_table,
        "parent_column": parent_column,
        "relation_kind": "logical" if orphan_rate > 0 else "db_fk",
    }


@dataclass
class TranslatedFields:
    """translate_fields 产出：字段分布 + 关系四元组 + 告警留痕。"""

    distributions: dict[str, dict | None] = field(default_factory=dict)
    relations: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def translate_fields(
    fields: Mapping[str, object],
    *,
    table: str = "",
    orphan_rate: float = 0.0,
    nullable_columns: Sequence[str] | None = None,
) -> TranslatedFields:
    """整表翻译：legacy 字段规则映射 → PTDE 字段分布/关系四元组/告警。

    - ``table``：表名（关系四元组 child_table 回填；缺省空串由调用方补）；
    - ``orphan_rate``：表级故意孤儿率（调用方先按 legacy 缺省链解析：
      表级 → 全局级 → 0）；
    - ``nullable_columns``：可空列名集（orphan NULL 拆分语义需要——legacy
      可空列 50% 置 NULL；不可空列只造无效 ID）；
    - 告警：HOT_ROW_SHARE_WARN（单热行 >50%）+ 故意孤儿降级 logical 留痕。
    """
    nullable_set = {c.lower() for c in (nullable_columns or [])}
    out = TranslatedFields()
    for col, rule in dict(fields).items():
        nullable = col.lower() in nullable_set
        dist = field_rule_to_distribution(rule, orphan_rate=orphan_rate,
                                          nullable=nullable)
        out.distributions[col] = dist
        rel = field_rule_to_relation(rule, table, col, orphan_rate=orphan_rate)
        if rel is not None:
            out.relations.append(rel)
        if dist is not None and dist["type"] == "ref":
            text = hot_row_share_warning_text(dist["params"])
            if text:
                out.warnings.append(f"{col}: {text}")
            if orphan_rate > 0:
                out.warnings.append(
                    f"{col}: orphan_rate>0（故意孤儿）→ 关系降级 logical"
                    "（verify WARN 口径，零孤儿硬核对不适用）")
    return out
