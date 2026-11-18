"""存储估算（R3 T3.4；总方案 §6.5）。

硬约束：`B_i`（行均存储成本）**必须由试装载实测校准**（Pilot Run 产出，
见 pilot_run.py），**禁止字段定义长度相加**。未经校准的表使用兜底行成本时
显式标记 calibrated=False 并产出 warning（不得伪装为实测值）。

S_total = Σ_i N_i × B_i（含索引成本，Pilot Run 分别实测表/索引大小）；
误差区间来自 Pilot Run 两轮扩样的非线性偏差。
"""

from __future__ import annotations

from ptde.models.spec import StorageEstimate

__all__ = [
    "STORAGE_ESTIMATOR_VERSION",
    "CalibratedRowCost",
    "estimate_storage",
]

STORAGE_ESTIMATOR_VERSION = "1"

# 未经校准表的兜底行成本（仅占位：显式 calibrated=False + warning，禁止伪装实测）
DEFAULT_UNCALIBRATED_ROW_COST_BYTES = 256
DEFAULT_UNCALIBRATED_INDEX_RATIO = 0.4
UNCALIBRATED_ERROR_BAND = "±30%（未经试装载校准，兜底行成本，禁止作为容量承诺）"


class CalibratedRowCost:
    """Pilot Run 校准产物：单表行均存储成本（实测，非字段长度相加）。

    pydantic 版：见 pilot_run.PilotRunReport.to_calibration()；
    本 dataclass 为求解期轻量视图。
    """

    def __init__(
        self,
        table: str,
        bytes_per_row: float,
        index_bytes_per_row: float = 0.0,
        sample_rows: int = 0,
        error_band: str | None = None,
        calibrated: bool = True,
    ) -> None:
        if bytes_per_row <= 0:
            raise ValueError(f"表 {table} 行均存储成本必须为正，得到 {bytes_per_row}")
        self.table = table
        self.bytes_per_row = bytes_per_row
        self.index_bytes_per_row = max(0.0, index_bytes_per_row)
        self.sample_rows = sample_rows
        self.error_band = error_band
        self.calibrated = calibrated


def estimate_storage(
    rows_by_table: dict[str, int],
    calibrated_costs: dict[str, CalibratedRowCost] | None = None,
    include_indexes: bool = True,
) -> tuple[StorageEstimate, list[str]]:
    """存储估算入口：返回 (StorageEstimate, warnings)。

    - 有校准的表：B_i 取实测 bytes_per_row（索引成本单独实测）；
    - 无校准的表：兜底行成本 + 显式 warning + calibrated=False；
    - 误差区间：全部校准取校准 band；部分/全部未校准取兜底 band。
    """
    warnings: list[str] = []
    costs = calibrated_costs or {}
    table_bytes = 0
    index_bytes = 0
    calibrated_count = 0

    for table, rows in rows_by_table.items():
        if rows <= 0:
            continue
        cost = costs.get(table)
        if cost is not None and cost.calibrated:
            table_bytes += int(rows * cost.bytes_per_row)
            if include_indexes:
                index_bytes += int(rows * cost.index_bytes_per_row)
            calibrated_count += 1
        else:
            table_bytes += rows * DEFAULT_UNCALIBRATED_ROW_COST_BYTES
            if include_indexes:
                index_bytes += int(rows * DEFAULT_UNCALIBRATED_ROW_COST_BYTES
                                   * DEFAULT_UNCALIBRATED_INDEX_RATIO)
            if cost is not None and not cost.calibrated:
                warnings.append(f"表 {table} 校准记录标记为未校准，使用兜底行成本")
            else:
                warnings.append(
                    f"表 {table} 无 Pilot Run 校准记录，使用兜底行成本 "
                    f"{DEFAULT_UNCALIBRATED_ROW_COST_BYTES} 字节/行"
                    "（未经试装载实测，禁止作为容量承诺）"
                )

    total = table_bytes + index_bytes
    all_calibrated = bool(rows_by_table) and calibrated_count == len(
        [t for t, n in rows_by_table.items() if n > 0]
    )
    bands = sorted({c.error_band for c in costs.values()
                    if c.calibrated and c.error_band})
    if all_calibrated and bands:
        error_band = bands[0]
    else:
        error_band = UNCALIBRATED_ERROR_BAND
        warnings.append("存在未校准表：误差区间取兜底 ±30%（校准后重估）")

    confidence = 0.9 if all_calibrated else (0.5 if calibrated_count else 0.3)
    estimate = StorageEstimate(
        estimated_table_size_bytes=table_bytes,
        estimated_index_size_bytes=index_bytes if include_indexes else None,
        estimated_total_size_bytes=total,
        confidence=confidence,
        error_band=error_band,
        calibrated=all_calibrated,
    )
    return estimate, warnings
