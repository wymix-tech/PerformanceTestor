"""IdPool 覆盖测试。

Java 侧 IdPool 无独立单测（经 EngineH2Test/SeqRuleTest 间接触达），
本文件补齐语义覆盖：池上限截断、倾斜分布、权重失效重建、异常路径。
分布阈值凭确定性种子控制在窄带内。
"""

import random

import pytest

from datagen.id_pool import MAX_POOL_SIZE, IdPool


def test_add_and_bounds():
    pool = IdPool()
    pool.add_all([1, 2, 3])
    assert pool.size() == 3
    assert pool.min() == 1
    assert pool.max() == 3
    assert not pool.is_empty()
    pool.add(4)
    assert pool.max() == 4


def test_max_pool_size_truncation():
    pool = IdPool()
    pool.add_all(list(range(1, MAX_POOL_SIZE + 10)))  # 超限 10 个
    assert pool.size() == MAX_POOL_SIZE
    pool.add(0)  # 满池后 add 被忽略
    assert pool.size() == MAX_POOL_SIZE
    pool.add_all([0])  # 满池后 addAll 全量截断为空
    assert pool.size() == MAX_POOL_SIZE
    pool.add_all(list(range(MAX_POOL_SIZE + 1, MAX_POOL_SIZE + 3)))
    assert pool.size() == MAX_POOL_SIZE


def test_sample_uniform():
    random.seed(3)
    pool = IdPool()
    pool.add_all(list(range(1, 101)))
    for _ in range(1000):
        assert 1 <= pool.sample_uniform() <= 100


def test_sample_uniform_empty_raises():
    pool = IdPool()
    with pytest.raises(RuntimeError, match="父 ID 池为空，无法采样 ref 值"):
        pool.sample_uniform()


def test_sample_skewed_empty_raises():
    pool = IdPool()
    with pytest.raises(RuntimeError, match="父 ID 池为空，无法采样 refSkew 值"):
        pool.sample_skewed()


def test_skew_distribution():
    """hotPercent=20/hotShare=80：前 20% 父行承接约 80% 采样。"""
    random.seed(5)
    pool = IdPool()
    pool.add_all(list(range(1, 101)))  # 100 行，热点为前 20 行
    pool.build_skew_weights(20, 80)
    hot_count = 20
    total = 20_000
    hot = sum(1 for _ in range(total) if pool.sample_skewed() <= hot_count)
    share = hot * 100.0 / total
    assert 77 < share < 83, f"hotShare actual {share}"


def test_skew_uniform_fallback_when_params_invalid():
    """hotPercent 越界 → 权重不生效，退化为均匀采样。"""
    random.seed(7)
    pool = IdPool()
    pool.add_all(list(range(1, 101)))
    pool.build_skew_weights(0, 80)
    assert pool.size() == 100
    pool.build_skew_weights(100, 80)
    hot = sum(1 for _ in range(2000) if pool.sample_skewed() <= 20)
    assert 15 < hot / 2000 * 100 < 25  # 均匀分布下前 20 行约 20%


def test_pool_mutation_invalidates_weights():
    """池变更后未重建权重 → 采样退化为均匀（对齐 Java weighted 标志语义）。"""
    random.seed(9)
    pool = IdPool()
    pool.add_all(list(range(1, 101)))
    pool.build_skew_weights(20, 80)
    pool.add(101)  # 池变更，权重失效
    hot = sum(1 for _ in range(4000) if pool.sample_skewed() <= 20)
    assert 15 < hot / 4000 * 100 < 25  # 不再是 80% 热区


def test_sample_invalid_id():
    random.seed(11)
    pool = IdPool()
    pool.add_all(list(range(1, 101)))
    for _ in range(500):
        v = pool.sample_invalid_id()
        assert 101 <= v <= 101 + 999  # max+1 ~ max+1+999


def test_sample_invalid_id_empty_pool():
    pool = IdPool()
    v = pool.sample_invalid_id()
    assert 2 <= v <= 2 + 999  # 空池 base=1
