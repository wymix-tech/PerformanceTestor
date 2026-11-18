"""T4.8 验收：分片规划器（实施方案 §7.2；总方案 §8.4/协议 §7.5）。

- 分片大小由 §8.4 指标动态调节（均衡/暂存空间/导入吞吐/倾斜度）；
- planned_range 经 IdRangeAllocator 三重检查（目标 Schema/既有数据/多 Worker）；
- hash_bucket 确定性哈希（sha256，非内置 hash——跨进程可复现）；
- 产出 ShardingPlan（协议计划侧，partition_algorithm_version 记录）。
"""

from __future__ import annotations

import math

import pytest

from ptde.generator.keys import IdRange, IdRangeAllocator
from ptde.generator.partitioner import (
    MAX_ROWS_PER_SHARD,
    MIN_ROWS_PER_SHARD,
    PARTITION_ALGORITHM_VERSION,
    build_sharding_plan,
    compute_rows_per_shard,
    hash_bucket,
    plan_shards,
)
from ptde.models.spec import EntityPlan, GenerationPlan

# ---------------------------------------------------------------- 分片大小（§8.4）


def test_rows_per_shard_balanced_by_workers():
    # 100 万行 / 4 Worker × 4 分片 → 均衡分片 ≈ 62,500
    assert compute_rows_per_shard(1_000_000, workers=4) == 62_500


def test_rows_per_shard_capped_by_staging_space():
    # 暂存空间 50,000,000B、行均 500B → 单分片 ≤ 100,000 行
    assert compute_rows_per_shard(
        1_000_000, workers=1, shards_per_worker=1,
        staging_bytes=50_000_000, avg_row_bytes=500,
    ) == 100_000


def test_rows_per_shard_capped_by_import_throughput():
    # 吞吐 10k 行/秒、目标单分片装载 20s → ≤ 200,000 行
    assert compute_rows_per_shard(
        10_000_000, workers=1, shards_per_worker=1,
        import_rows_per_sec=10_000, target_load_seconds=20,
    ) == 200_000


def test_rows_per_shard_reduced_by_skew_factor():
    base = compute_rows_per_shard(1_000_000, workers=4)
    skewed = compute_rows_per_shard(1_000_000, workers=4, skew_factor=0.5)
    assert skewed == math.floor(base * (1.0 - 0.5 / 2.0))  # 倾斜越大分片越小
    assert skewed < base


def test_rows_per_shard_clamped_and_half_constraints_fail():
    assert compute_rows_per_shard(10) == MIN_ROWS_PER_SHARD  # 下限钳制
    assert compute_rows_per_shard(10**12) == MAX_ROWS_PER_SHARD  # 上限钳制
    with pytest.raises(ValueError, match="必须为正"):
        compute_rows_per_shard(0)
    with pytest.raises(ValueError, match="同时给出"):
        compute_rows_per_shard(1_000_000, staging_bytes=1024)  # 缺 avg_row_bytes
    with pytest.raises(ValueError, match="显式失败"):
        compute_rows_per_shard(1_000_000, skew_factor=1.5)


# ---------------------------------------------------------------- 确定性哈希分桶


def test_hash_bucket_deterministic_and_bounded():
    assert hash_bucket("orders-0001", 8) == hash_bucket("orders-0001", 8)
    assert all(0 <= hash_bucket(k, 8) < 8 for k in (1, "abc", None, 3.14))
    with pytest.raises(ValueError, match="必须为正"):
        hash_bucket("k", 0)


def test_hash_bucket_distributes_uniformly():
    buckets = [hash_bucket(i, 16) for i in range(16_000)]
    counts = [buckets.count(b) for b in range(16)]
    assert all(c > 700 for c in counts)  # 均匀（无空桶、无独大桶）


# ---------------------------------------------------------------- 分片计划


def _allocator(**kw) -> IdRangeAllocator:
    return IdRangeAllocator(**kw)


def test_plan_shards_range_allocates_contiguous_id_ranges():
    plan = plan_shards({"orders": 2500, "customers": 300},
                       rows_per_shard=1000, id_allocator=_allocator())
    assert plan.partition_algorithm_version == PARTITION_ALGORITHM_VERSION == "1"
    orders = [s for s in plan.shards if s.entity == "orders"]
    customers = [s for s in plan.shards if s.entity == "customers"]
    assert [s.shard_id for s in orders] == ["orders-0000", "orders-0001", "orders-0002"]
    assert (orders[0].id_start, orders[0].id_end) == (1, 1000)
    assert (orders[2].id_start, orders[2].id_end) == (2001, 2500)  # 末片精确
    # ID 区间按实体独立分配（IdRangeAllocator 以 entity 为界）
    assert (customers[0].id_start, customers[0].id_end) == (1, 300)
    assert all(s.strategy == "planned_range" for s in plan.shards)


def test_plan_shards_range_requires_allocator_and_zero_rows_skipped():
    with pytest.raises(ValueError, match="IdRangeAllocator"):
        plan_shards({"orders": 100})
    plan = plan_shards({"empty": 0, "orders": 100},
                       rows_per_shard=100, id_allocator=_allocator())
    assert [s.entity for s in plan.shards] == ["orders"]


def test_plan_shards_reserved_conflict_explicit_failure():
    # 多 Worker 分片规则检查：与已保留区间重叠 → 显式失败（keys.py §7.6 口径）
    with pytest.raises(ValueError, match="重叠"):
        plan_shards(
            {"orders": 1000}, rows_per_shard=500,
            id_allocator=_allocator(reserved=[IdRange(entity="orders", shard_id="w1",
                                                      start=1, end=800)]),
        )


def test_plan_shards_hash_buckets_deterministic():
    p1 = plan_shards({"users": 2500}, rows_per_shard=1000,
                     key_kinds={"users": "hash_bucket"})
    p2 = plan_shards({"users": 2500}, rows_per_shard=1000,
                     key_kinds={"users": "hash_bucket"})
    assert p1 == p2
    assert len(p1.shards) == 3
    assert all(s.strategy == "hash_bucket" for s in p1.shards)
    assert all(s.id_start is None and s.id_end is None for s in p1.shards)
    assert [s.shard_id for s in p1.shards] == ["users-hb-0000", "users-hb-0001", "users-hb-0002"]


def test_plan_shards_explicit_failures():
    with pytest.raises(ValueError, match="必须为正"):
        plan_shards({"orders": 100}, rows_per_shard=0)
    with pytest.raises(ValueError, match="必须非负"):
        plan_shards({"orders": -1})
    with pytest.raises(ValueError, match="未规划实体"):
        plan_shards({"orders": 100}, key_kinds={"ghost": "hash_bucket"})
    with pytest.raises(ValueError, match="未知键型"):
        plan_shards({"orders": 100}, key_kinds={"orders": "by_date"})


def test_plan_shards_mixed_kinds_and_determinism():
    plan = plan_shards(
        {"users": 1500, "orders": 1500}, rows_per_shard=1000,
        key_kinds={"users": "hash_bucket"}, id_allocator=_allocator(),
    )
    assert [s.entity for s in plan.shards] == ["orders", "orders",
                                               "users", "users"]
    # 同输入两次一致（分片规划可复现）
    assert plan == plan_shards(
        {"users": 1500, "orders": 1500}, rows_per_shard=1000,
        key_kinds={"users": "hash_bucket"}, id_allocator=_allocator(),
    )


def test_build_sharding_plan_from_generation_plan():
    plan = GenerationPlan(
        id="p1", dataset_spec_fingerprint="fp",
        entities={"orders": EntityPlan(entity="orders", rows=2000)},
    )
    sp = build_sharding_plan(plan, rows_per_shard=1000)
    assert len(sp.shards) == 2
    assert (sp.shards[0].id_start, sp.shards[1].id_end) == (1, 2000)
