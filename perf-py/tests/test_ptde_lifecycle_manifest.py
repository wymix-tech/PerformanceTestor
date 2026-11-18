"""T6.7 Manifest 汇总测试（实施方案 §9.2 T6.7；总方案 §12.4 八项要素）。"""

from __future__ import annotations

import pytest

from ptde.generator.staging import ShardRecord, ShardStatus
from ptde.lifecycle.manifest import (
    ManifestIncomplete,
    backfill_from_staging,
    missing_elements,
    require_complete,
    summaries_from_staging,
)
from ptde.models.spec import ExecutionManifest, ShardSummary


def _complete_manifest() -> ExecutionManifest:
    m = ExecutionManifest(
        id="m1",
        instance_id="inst-1",
        seed=42,
        generator_version="0.5.0",
        partition_algorithm_version="1",
        adapter_version="mysql-bulkload-1",
        adapter_capability_snapshot={"product": "mysql"},
        model_fingerprints={
            "spec": "fp-spec", "plan": "fp-plan",
            "schema": "fp-schema", "profile": "fp-profile",
        },
        shard_summaries=[ShardSummary(
            shard_id="s1", entity="users", generated_rows=10,
            loaded_rows=10, status="loaded")],
        actual_id_ranges={"users": {"start": 1, "end": 10}},
    )
    return m


def test_complete_manifest_has_no_missing():
    assert missing_elements(_complete_manifest()) == []


def test_empty_manifest_lists_all_missing():
    missing = missing_elements(ExecutionManifest(id="m0"))
    for label in ("DatasetSpec 指纹", "GenerationPlan 指纹", "Schema 指纹",
                  "画像快照指纹", "Seed", "生成器版本", "Adapter 能力快照",
                  "Adapter 版本", "分片摘要", "实际 ID 范围"):
        assert label in missing


def test_generator_version_sentinel_counts_as_missing():
    m = _complete_manifest()
    m.generator_version = "0.0.0"
    assert "生成器版本" in missing_elements(m)


def test_time_inputs_optional_by_default():
    """时间输入缺省不判缺（无时间窗数据集合法）；显式要求时缺项可见。"""
    m = _complete_manifest()
    assert missing_elements(m) == []
    missing = missing_elements(m, require_time_inputs=True)
    assert "时间输入（固定值）" in missing


def test_require_complete_raises_with_itemized_list():
    with pytest.raises(ManifestIncomplete) as excinfo:
        require_complete(ExecutionManifest(id="m0"))
    assert "Seed" in str(excinfo.value)
    require_complete(_complete_manifest())  # 完整则原样返回


def test_summaries_from_staging_mapping():
    records = [
        ShardRecord(shard_id="s1", entity="users", status=ShardStatus.LOADED.value,
                    rows=10, loaded_rows=10, checksum="abc", path="p.csv"),
        ShardRecord(shard_id="s2", entity="users", status=ShardStatus.GENERATED.value,
                    rows=5, checksum="def"),
    ]
    summaries = summaries_from_staging(records)
    assert [s.shard_id for s in summaries] == ["s1", "s2"]
    assert summaries[0].loaded_rows == 10
    assert summaries[0].status == "loaded"
    assert summaries[1].loaded_rows is None
    assert summaries[1].status == "generated"


def test_backfill_from_staging_fills_shard_summaries():
    """R6 承接项：ExecutionManifest.shard_summaries 回填（staging 形态对齐）。"""
    m = _complete_manifest()
    m.shard_summaries = []
    records = [
        ShardRecord(shard_id="s1", entity="users",
                    status=ShardStatus.LOADED.value, rows=7, loaded_rows=7),
    ]
    backfill_from_staging(m, records)
    assert len(m.shard_summaries) == 1
    assert m.shard_summaries[0].generated_rows == 7
    assert missing_elements(m) == []
