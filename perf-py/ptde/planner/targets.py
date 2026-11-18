"""目标类型组合校验与单位换算（R3 T3.1，实施方案 §6.1/§6.2）。

职责划分：
- 模型层校验（pydantic 校验器，互斥/冲突组合、正值、权重合计）落在
  `ptde/models/spec.py` 的 `SpecTargets`（R0 建模补齐路径，勿在此重复建模）；
- 本模块承载规划器侧的组合检查（需要外部上下文：生产基线是否存在等）
  与存储容量字面量换算（"200GB" → 字节数）。

5 类目标（总方案 §6.1，可组合）：
1. RowTarget          rows            指定核心表精确行数
2. ScaleFactorTarget  scale_factor    生产基线 × 倍率（逐表独立）
3. StorageTarget      storage         存储容量预算反推规模
4. WorkloadTarget     workload        工作负载驱动（SQL 访问模式/选择性/热点）
5. AutoTarget         auto            自动规划（画像 + 约束给多档；与其他目标互斥）
"""

from __future__ import annotations

import re

from ptde.models.spec import DatasetSpec

__all__ = [
    "check_target_combination",
    "parse_storage_bytes",
    "validate_planning_targets",
]

# 1024 进制（数据库容量口径）；与 spec.py 的 _STORAGE_TARGET_RE 单位集一致
_STORAGE_UNITS: dict[str, int] = {
    "B": 1,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
    "PB": 1024**5,
}
_SIZE_RE = re.compile(r"(?i)^\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|PB)\s*$")


def parse_storage_bytes(text: str) -> int:
    """解析 "200GB" 形态的容量字面量为字节数（1024 进制；非法形态抛 ValueError）。"""
    m = _SIZE_RE.fullmatch(text)
    if m is None:
        raise ValueError(
            f"存储容量字面量形态非法：{text!r}（应为 '数值 + 单位'，如 '200GB'，"
            "单位 B/KB/MB/GB/TB/PB）"
        )
    value = float(m.group(1))
    unit = m.group(2).upper()
    return int(value * _STORAGE_UNITS[unit])


def validate_planning_targets(spec: DatasetSpec, has_production_baseline: bool = False
                              ) -> list[str]:
    """规划器侧目标组合检查（模型层校验之外、需要外部上下文的冲突项）。

    返回逐条冲突说明（空列表 = 可规划）；由 planner/solver 汇入 PlanningConflict。
    """
    conflicts: list[str] = []
    targets = spec.targets

    if targets.scale_factor is not None and targets.scale_factor.factors \
            and not has_production_baseline:
        conflicts.append(
            "生产规模倍率目标缺少生产基线行数："
            f"倍率表 {sorted(targets.scale_factor.factors)} 需要目标库 Schema 快照 "
            "row_estimate 或生产画像提供基线行数"
        )

    if targets.storage is not None and targets.storage.max_total is not None \
            and not (targets.rows or targets.scale_factor is not None
                     or targets.workload is not None):
        conflicts.append(
            "存储容量目标缺少规模锚点：仅给 storage.max_total 时无法反推各表规模，"
            "需要 rows / scale_factor / workload 至少其一"
        )

    if not (targets.rows or targets.scale_factor is not None
            or targets.storage is not None or targets.workload is not None
            or targets.auto is not None):
        conflicts.append("未提供任何规划目标：5 类目标（rows/scale_factor/storage/"
                         "workload/auto）至少需其一")

    return conflicts


def check_target_combination(spec: DatasetSpec, has_production_baseline: bool = False
                             ) -> list[str]:
    """validate_planning_targets 的别名入口（对齐 T3.1 任务命名）。"""
    return validate_planning_targets(spec, has_production_baseline)
