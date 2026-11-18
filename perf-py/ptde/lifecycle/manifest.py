"""T6.7 Manifest 汇总（实施方案 §9.2 T6.7；总方案 §12.4 可复现要素全集）。

ExecutionManifest 全要素核对与回填：
- ``missing_elements``：§12.4 八项要素逐项核对，缺项清单显式返回
  （READY 门禁条件 7「Manifest/执行清单/验证报告完整」的判定输入）；
- ``require_complete``：缺项即 ManifestIncomplete（不伪装完整）；
- ``summaries_from_staging`` / ``backfill_from_staging``：R6 承接项——
  ExecutionManifest.shard_summaries 回填（staging.totals() 形态已对齐，
  R5 §13 口径）。
"""

from __future__ import annotations

from collections.abc import Iterable

from ptde.generator.staging import ShardRecord
from ptde.models.spec import ExecutionManifest, ShardSummary

__all__ = [
    "ManifestIncomplete",
    "backfill_from_staging",
    "missing_elements",
    "require_complete",
    "summaries_from_staging",
]

# 生成器版本哨兵（ExecutionManifest 默认值，未回填即视为缺失）
_GENERATOR_VERSION_DEFAULT = "0.0.0"


class ManifestIncomplete(Exception):
    """Manifest 要素缺失（缺项清单随异常返回，显式失败不伪装完整）。"""


def summaries_from_staging(records: Iterable[ShardRecord]) -> list[ShardSummary]:
    """staging 分片记录 → ShardSummary（status 语义对齐：pending/generated/loaded）。"""
    return [
        ShardSummary(
            shard_id=r.shard_id,
            entity=r.entity,
            generated_rows=r.rows,
            loaded_rows=r.loaded_rows,
            checksum=r.checksum,
            status=r.status,
        )
        for r in records
    ]


def backfill_from_staging(
    manifest: ExecutionManifest,
    records: Iterable[ShardRecord],
) -> ExecutionManifest:
    """回填 shard_summaries（R6 承接项：staging.totals() 形态已对齐）。"""
    manifest.shard_summaries = summaries_from_staging(records)
    return manifest


def missing_elements(
    manifest: ExecutionManifest,
    *,
    require_time_inputs: bool = False,
) -> list[str]:
    """§12.4 全要素逐项核对，返回缺项清单（空 = 完整）。"""
    missing: list[str] = []
    fps = manifest.model_fingerprints
    if not fps.spec:
        missing.append("DatasetSpec 指纹")
    if not fps.plan:
        missing.append("GenerationPlan 指纹")
    if not fps.schema_fp:
        missing.append("Schema 指纹")
    if not fps.profile:
        missing.append("画像快照指纹")
    if manifest.seed is None:
        missing.append("Seed")
    if not manifest.generator_version or manifest.generator_version == _GENERATOR_VERSION_DEFAULT:
        missing.append("生成器版本")
    if not manifest.partition_algorithm_version:
        missing.append("分片算法版本")
    if not manifest.adapter_capability_snapshot:
        missing.append("Adapter 能力快照")
    if not manifest.adapter_version:
        missing.append("Adapter 版本")
    if not manifest.shard_summaries:
        missing.append("分片摘要")
    if not manifest.actual_id_ranges:
        missing.append("实际 ID 范围")
    if require_time_inputs and not manifest.time_inputs:
        missing.append("时间输入（固定值）")
    return missing


def require_complete(
    manifest: ExecutionManifest,
    *,
    require_time_inputs: bool = False,
) -> ExecutionManifest:
    """Manifest 完整性强制：缺项抛 ManifestIncomplete（带缺项清单）。"""
    missing = missing_elements(manifest, require_time_inputs=require_time_inputs)
    if missing:
        raise ManifestIncomplete(
            f"ExecutionManifest 要素缺失（{len(missing)} 项）：{missing}"
            "——READY 门禁条件 7 不满足，显式失败不伪装完整"
        )
    return manifest
