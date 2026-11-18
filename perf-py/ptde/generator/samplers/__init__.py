"""分布采样器层（R4 T4.2，实施方案 §7.2；总方案 §7.1 全部 12 类分布）。

每类分布只认 B3 产出形态 DistributionFit(type+params)（不读生产明文样本——
脱敏边界延续 R2），注入 random.Random 实例（确定性采样）。

估计分布（fitted=False / source=estimated）照常可采样，但来源标注不因可采样
而改变——不得伪装统计口径（A3 分级显式）。

期望/采样口径与 planner/derivation.py 对齐（勿另起一套）：
- uniform：整数域 randint / 连续域 uniform；
- normal：max(0, gauss(mean, sigma))（基数语义非负）；
- lognormal：exp(gauss(mu, sigma))；
- weighted_categorical：累计分布 pick <= acc（values/weights 键名与
  distribution_planner 产出严格一致）；
- 期望公式对齐 derivation.distribution_expectation。
"""

from ptde.generator.samplers.registry import (
    SUPPORTED_TYPES,
    sample,
    sample_params,
)

__all__ = [
    "SUPPORTED_TYPES",
    "sample",
    "sample_params",
]
