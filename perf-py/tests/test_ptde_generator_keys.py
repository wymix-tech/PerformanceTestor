"""T4.5 验收：泛型键池与预分配 ID 区间（演进 ptde_common/id_pool.py）。

- 去除 MAX_POOL_SIZE=500_000 上限与整数限制（int/str/UUID/复合键 tuple）；
- 预分配 ID 区间接口（总方案 §7.6）：既有数据 / 已保留区间 / 目标 Schema
  三重检查，冲突显式失败不静默改道；
- 复合键各列区间按行序 zip（长度不一致显式失败）；
- rng 必须注入（新链路不提供模块级 random 退化路径——与 legacy 区分）。
"""

from __future__ import annotations

import random
import uuid

import pytest

from ptde.generator.keys import (
    INVALID_STR_PREFIX,
    IdRange,
    IdRangeAllocator,
    KeyPool,
    composite_keys,
    uuid_from_rng,
)

# ---------------------------------------------------------------- 预分配 ID 区间


def test_allocate_deterministic_and_disjoint():
    a = IdRangeAllocator(existing_max={"orders": 1000})
    ranges = a.allocate("orders", {"orders-0002": 500, "orders-0001": 300})
    # 分片按名称排序顺序打包，起点接既有数据最大 ID
    assert [r.shard_id for r in ranges] == ["orders-0001", "orders-0002"]
    assert (ranges[0].start, ranges[0].end) == (1001, 1300)
    assert (ranges[1].start, ranges[1].end) == (1301, 1800)
    assert ranges[0].strategy == "planned_range"
    assert all(ranges[i].end < ranges[i + 1].start for i in range(len(ranges) - 1))


def test_allocate_repeats_identically():
    a = IdRangeAllocator(existing_max={"t": 9})
    r1 = a.allocate("t", {"s1": 10, "s2": 20})
    r2 = IdRangeAllocator(existing_max={"t": 9}).allocate("t", {"s1": 10, "s2": 20})
    assert r1 == r2


def test_allocate_conflict_with_reserved_fails_explicitly():
    reserved = [IdRange("orders", "worker-9", 1, 2000)]
    a = IdRangeAllocator(reserved=reserved)
    with pytest.raises(ValueError, match="重叠"):
        a.allocate("orders", {"orders-0001": 100})


def test_allocate_allow_manual_false_fails():
    a = IdRangeAllocator(allow_manual=False)
    with pytest.raises(ValueError, match="不允许手工预分配"):
        a.allocate("orders", {"orders-0001": 100})


def test_allocate_rejects_empty_and_nonpositive():
    a = IdRangeAllocator()
    with pytest.raises(ValueError, match="无分片区间请求"):
        a.allocate("orders", {})
    with pytest.raises(ValueError, match="必须为正"):
        a.allocate("orders", {"s1": 0})


# ---------------------------------------------------------------- 泛型键池


def test_pool_no_size_cap_and_bigint_keys():
    pool = KeyPool()
    pool.add_all(range(600_001))  # 远超 legacy MAX_POOL_SIZE=500_000
    assert pool.size() == 600_001
    pool.add(2**62)  # bigint 无整数限制
    rng = random.Random(7)
    assert pool.sample_uniform(rng) == 2**62 or pool.sample_uniform(rng) < 600_001
    assert isinstance(pool.sample_uniform(rng), int)


def test_pool_uniform_deterministic():
    def draw(seed):
        pool = KeyPool()
        pool.add_all(range(100, 200))
        rng = random.Random(seed)
        return [pool.sample_uniform(rng) for _ in range(50)]

    assert draw(42) == draw(42)
    assert draw(42) != draw(43)


def test_pool_skew_hot_share():
    pool = KeyPool()
    pool.add_all(range(1000))
    pool.build_skew_weights(hot_percent=20, hot_share=80)
    rng = random.Random(11)
    picks = [pool.sample_skewed(rng) for _ in range(20_000)]
    hot = sum(1 for k in picks if k < 200) / len(picks)
    assert 0.77 <= hot <= 0.83, f"热点承接占比应约 80%，实测 {hot:.1%}"


def test_pool_skew_fallback_to_uniform_without_weights():
    pool = KeyPool()
    pool.add_all(range(10))
    rng = random.Random(3)
    assert pool.sample_skewed(rng) in range(10)


def test_pool_requires_rng():
    pool = KeyPool()
    pool.add_all(range(10))
    with pytest.raises(ValueError, match="rng 必须注入"):
        pool.sample_uniform(None)
    with pytest.raises(ValueError, match="rng 必须注入"):
        pool.sample_skewed(None)
    with pytest.raises(ValueError, match="rng 必须注入"):
        pool.sample_invalid(None)


def test_pool_empty_fails_explicitly():
    pool = KeyPool()
    with pytest.raises(RuntimeError, match="键池为空"):
        pool.sample_uniform(random.Random(1))


def test_pool_invalid_by_key_type():
    rng = random.Random(5)
    ints = KeyPool()
    ints.add_all([10, 20, 30])
    invalid_int = ints.sample_invalid(rng)
    assert invalid_int > 30

    strs = KeyPool()
    strs.add_all(["u-1", "u-2"])
    assert strs.sample_invalid(rng).startswith(INVALID_STR_PREFIX)

    uuids = KeyPool()
    uuids.add_all([uuid_from_rng(rng), uuid_from_rng(rng)])
    invalid_uuid = uuids.sample_invalid(rng)
    assert isinstance(invalid_uuid, uuid.UUID)
    assert invalid_uuid not in uuids.keys()


def test_pool_invalid_composite_key():
    pool = KeyPool()
    pool.add_all([(1, "a"), (2, "b")])
    invalid = pool.sample_invalid(random.Random(6))
    assert isinstance(invalid, tuple) and len(invalid) == 2
    assert invalid not in pool.keys()


def test_pool_rejects_unknown_key_type():
    pool = KeyPool()
    pool.add_all([1.5])
    with pytest.raises(ValueError, match="不支持的键类型"):
        pool.sample_invalid(random.Random(1))


# ---------------------------------------------------------------- UUID / 复合键


def test_uuid_from_rng_deterministic_and_valid():
    r1 = uuid_from_rng(random.Random(42))
    r2 = uuid_from_rng(random.Random(42))
    assert r1 == r2
    assert r1.variant == uuid.RFC_4122
    assert r1.version == 4
    assert uuid_from_rng(random.Random(43)) != r1


def test_composite_keys_zip_row_wise():
    ranges = {
        "order_id": IdRange("orders", "s1", 1, 3),
        "item_no": IdRange("orders", "s1", 100, 102),
    }
    rows = list(composite_keys(ranges))
    assert rows == [(1, 100), (2, 101), (3, 102)]


def test_composite_keys_length_mismatch_fails():
    with pytest.raises(ValueError, match="长度不一致"):
        list(composite_keys({
            "a": IdRange("t", "s1", 1, 3),
            "b": IdRange("t", "s1", 1, 4),
        }))
    with pytest.raises(ValueError, match="至少需要"):
        list(composite_keys({}))
