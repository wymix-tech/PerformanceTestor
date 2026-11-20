"""Ref 采样器（B7 续批次 9：legacy FieldRule ref/refSkew/orphan_rate 语义对齐）。

对齐 legacy 口径（datagen/ptde_common/id_pool.py + datagen/default_engine.py）：
- 无 hot/orphan 参数 → 父主键区间 [min, max] 均匀采样（对齐 IdPool.sample_uniform
  与管线 _apply_fk_fields 注入的 uniform 语义）；
- hot_percent/hot_share → 两段线性热行权重：前 round(n×hot_percent/100) 个
  父 ID（区间头部）合计承载 hot_share%，其余均分 (100−hot_share)%
  （对齐 IdPool.build_skew_weights/sample_skewed：累计权重 + bisect_left；
  hot_percent<=0 或 >=100 退化为均匀）；
- orphan_rate → 故意孤儿：无效 ID = max + 1 + randrange(invalid_span)
  （对齐 IdPool.sample_invalid_id 池最大值 + 1 + randrange(1000)，必然不命中
  真实行）；orphan_null_ratio → orphan 行置 None 概率（legacy 可空列 50% 置
  NULL 语义；缺省 0 = 只造无效 ID，None 语义可叠加 null_ratio 组合）；
- HOT_ROW_SHARE_WARN：单热行承载占比估算（100×hot_share/max(1,hot_percent)）
  > 50% 告警（对齐 legacy default_engine.HOT_ROW_SHARE_WARN 同口径）。

故意孤儿 × verify 关系层口径：DB_FK/ORM 声明关系零孤儿硬核对会 FAIL，
故意孤儿须配 LOGICAL 关系（verify 降级 WARN 如实暴露，不伪装通过）——
管线构造期门禁见 server/offline_pipeline.py。
"""

from __future__ import annotations

import math
import random
import warnings
from bisect import bisect_left

from ptde.models.profile import DistributionFit

HOT_ROW_SHARE_WARN = 50.0  # 单热行承载占比告警阈值（legacy default_engine 同口径）
DEFAULT_INVALID_SPAN = 1000  # 无效 ID 偏移跨度（legacy sample_invalid_id 同口径）


def _int_param(params: dict, key: str) -> int:
    v = params.get(key)
    if v is None:
        raise ValueError(f"ref 分布需要 {key} 参数（父主键区间，显式失败），得到 {sorted(params)}")
    fv = float(v)
    if fv != int(fv):
        raise ValueError(
            f"ref 分布 {key} 参数须为整数值（父主键区间语义，显式失败），得到 {v!r}")
    return int(fv)


def _float_param(params: dict, key: str, default: float | None = None) -> float | None:
    v = params.get(key)
    return float(v) if v is not None else default


def sample(fit: DistributionFit, rng: random.Random, context: dict | None = None):
    """父主键区间采样：orphan 掷骰 → 两段线性热行权重 → 均匀（legacy 依赖序）。"""
    p = fit.params or {}
    lo, hi = _int_param(p, "min"), _int_param(p, "max")
    if lo > hi:
        lo, hi = hi, lo
    n = hi - lo + 1
    orphan_rate = _float_param(p, "orphan_rate", 0.0) or 0.0
    orphan_null_ratio = _float_param(p, "orphan_null_ratio", 0.0) or 0.0
    invalid_span = int(_float_param(p, "invalid_span", DEFAULT_INVALID_SPAN)
                       or DEFAULT_INVALID_SPAN)
    if orphan_rate > 0 and rng.random() < orphan_rate:
        if orphan_null_ratio > 0 and rng.random() < orphan_null_ratio:
            return None  # legacy 可空列 50% 置 NULL 语义（orphan_null_ratio 承载）
        return hi + 1 + rng.randrange(invalid_span)  # 无效 ID，必然不命中真实行
    hot_percent = _float_param(p, "hot_percent", 0.0) or 0.0
    hot_share = _float_param(p, "hot_share", 0.0) or 0.0
    if not (0 < hot_percent < 100):
        return rng.randint(lo, hi)  # 越界/缺省退化为均匀（legacy build_skew_weights 同口径）
    hot_count = max(1, math.floor(n * hot_percent / 100.0 + 0.5))
    hot_each = hot_share / hot_count
    cold_each = (100.0 - hot_share) / max(1, n - hot_count)
    acc = 0.0
    cumulative: list[float] = []
    for i in range(n):
        acc += hot_each if i < hot_count else cold_each
        cumulative.append(acc)
    pick = rng.random() * cumulative[-1]
    return lo + bisect_left(cumulative, pick)  # 累计权重 + 二分（legacy sample_skewed 同口径）


def hot_row_share_warning_text(params: dict | None) -> str | None:
    """单热行承载占比估算 > HOT_ROW_SHARE_WARN → 告警文案（否则 None）。

    估算口径对齐 legacy default_engine：假设热区权重全部落单行，
    per_hot = 100 × hot_share / max(1, hot_percent)。
    """
    p = params or {}
    hot_percent = _float_param(p, "hot_percent", 0.0) or 0.0
    if hot_percent <= 0:
        return None
    hot_share = _float_param(p, "hot_share", 0.0) or 0.0
    per_hot = 100.0 * hot_share / max(1.0, hot_percent)
    if per_hot > HOT_ROW_SHARE_WARN:
        return (f"热点倾斜极高（单热行约承载 {per_hot:.1f}% 子行），"
                "建议降低 BATCH_SIZE 或为子表分区")
    return None


def warn_hot_row_share(params: dict | None) -> None:
    """热行告警（warnings 通道；同文案去重，不阻断生成）。"""
    text = hot_row_share_warning_text(params)
    if text:
        warnings.warn(text, stacklevel=2)
