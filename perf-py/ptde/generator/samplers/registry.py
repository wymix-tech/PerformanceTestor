"""采样器分发注册表：总方案 §7.1 全部 12 类分布 → 模块 sample 函数。

zipf/skewed 同族别名；weightedcategorical 为 weighted_categorical 别名；
uniform 参数为 min_length/max_length（B3 字符列默认形态）时路由到 length
采样器（长度语义）。未知类型显式失败。

B7 续批次 9（legacy FieldRule 语义对齐）扩展 2 类：
- ref：父主键区间采样（ref/refSkew 热行两段线性权重 + orphan_rate 故意孤儿）；
- over_threshold：范围超阈值比例分配（legacy rule_engine 语义）。
"""

from __future__ import annotations

import random
from typing import Any

from ptde.generator.samplers import (
    conditional,
    correlated,
    length,
    lognormal,
    normal,
    null_ratio,
    over_threshold,
    random_discrete,
    ref,
    sequential,
    temporal,
    uniform,
    weighted_categorical,
    zipf,
)
from ptde.models.profile import DistributionFit

# 总方案 §7.1 全部 12 类分布（zipf/skewed 同族，weightedcategorical 为别名）
# + B7 续批次 9 legacy 语义对齐扩展（ref/over_threshold）
SUPPORTED_TYPES: tuple[str, ...] = (
    "uniform",
    "normal",
    "lognormal",
    "zipf",
    "skewed",
    "weighted_categorical",
    "temporal",
    "sequential",
    "random",
    "conditional",
    "correlated",
    "null_ratio",
    "length",
    "ref",
    "over_threshold",
)

_DISPATCH: dict[str, Any] = {
    "uniform": uniform.sample,
    "normal": normal.sample,
    "lognormal": lognormal.sample,
    "zipf": zipf.sample,
    "skewed": zipf.sample,
    "weighted_categorical": weighted_categorical.sample,
    "weightedcategorical": weighted_categorical.sample,
    "temporal": temporal.sample,
    "sequential": sequential.sample,
    "random": random_discrete.sample,
    "conditional": conditional.sample,
    "correlated": correlated.sample,
    "null_ratio": null_ratio.sample,
    "length": length.sample,
    "ref": ref.sample,
    "over_threshold": over_threshold.sample,
}


def sample(fit: DistributionFit, rng: random.Random,
           context: dict | None = None) -> Any:
    """按 DistributionFit.type 分发到对应采样器（rng 必须注入，确定性采样）。"""
    t = (fit.type or "").lower()
    if t == "uniform" and "min" not in (fit.params or {}) \
            and ("min_length" in (fit.params or {}) or "max_length" in (fit.params or {})):
        # B3 字符列默认形态：uniform + min_length/max_length → 长度分布语义
        return length.sample(fit, rng, context)
    fn = _DISPATCH.get(t)
    if fn is None:
        raise ValueError(
            f"不支持的分布类型 {t!r}（支持总方案 §7.1 共 12 类"
            f" + ref/over_threshold 扩展：{', '.join(SUPPORTED_TYPES)}）"
        )
    return fn(fit, rng, context)


def sample_params(dist_type: str, params: dict, rng: random.Random,
                  context: dict | None = None) -> Any:
    """便捷入口：以 (type, params) 形态分发（内部构造 DistributionFit）。"""
    return sample(DistributionFit(type=dist_type, params=params), rng, context)
