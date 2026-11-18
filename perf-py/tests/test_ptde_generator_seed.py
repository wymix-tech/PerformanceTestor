"""T4.4 验收：Seed 可复现机制（每字段独立派生 random.Random；跨进程一致）。

- 同 root_seed 两次生成逐字段一致（sha256 哈希对照，验收门禁 1 字段级）；
- 不同 entity/shard/field 派生独立随机源（互不串流）；
- root_seed=None 退化路径显式（warning，不伪装可复现）；
- 跨进程一致（subprocess 哈希对照——sha256 派生不受 PYTHONHASHSEED 影响）。
"""

from __future__ import annotations

import hashlib
import random
import subprocess
import sys
from pathlib import Path

import pytest

from ptde.generator.seed import (
    SEED_DERIVATION_ALGORITHM,
    derive_seed,
    field_rng,
    is_reproducible,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIELDS = ("id", "status", "amount", "paid_at")


def _values(rng, n: int = 200) -> list:
    return [rng.random() for _ in range(n)]


def _digest(values) -> str:
    return hashlib.sha256(repr(values).encode()).hexdigest()


def _generate_entity_rows(root_seed, n_rows: int = 50) -> str:
    """按 Seed 链路生成逻辑数据并取哈希（每字段独立派生 rng，对照验收门禁 1）。"""
    digest = hashlib.sha256()
    for row_index in range(n_rows):
        for field in FIELDS:
            rng = field_rng(root_seed, "orders", "orders-0007", field)
            digest.update(f"{row_index}|{field}|{rng.random():.17g};".encode())
    return digest.hexdigest()


# ---------------------------------------------------------------- Seed 派生


def test_derive_seed_deterministic_and_distinct():
    a = derive_seed(42, "orders", "orders-0007", "paid_at")
    assert a == derive_seed(42, "orders", "orders-0007", "paid_at")
    derived = {
        derive_seed(42, entity, shard, field)
        for entity in ("orders", "users")
        for shard in ("orders-0007", "orders-0008")
        for field in FIELDS
    }
    assert len(derived) == 2 * 2 * len(FIELDS), "不同 entity/shard/field 必须派生不同 Seed"


def test_derive_seed_rejects_none():
    with pytest.raises(ValueError, match="root_seed 未配置"):
        derive_seed(None, "orders", "s", "paid_at")


def test_field_rng_independent_streams():
    """同字段同输入复现；不同字段不同流（独立实例，互不扰动）。"""
    ra = field_rng(42, "orders", "s1", "paid_at")
    rb = field_rng(42, "orders", "s1", "paid_at")
    assert _values(ra) == _values(rb)
    stream_a = _values(field_rng(42, "orders", "s1", "paid_at"))
    stream_b = _values(field_rng(42, "orders", "s1", "amount"))
    assert stream_a != stream_b


def test_field_rng_cross_shard_differs():
    assert _values(field_rng(42, "orders", "s1", "paid_at")) \
        != _values(field_rng(42, "orders", "s2", "paid_at"))


# ---------------------------------------------------------------- 哈希对照


def test_same_seed_reproduces_identical_logical_data():
    """同 root_seed 两次生成：逐字段哈希一致（R4.1 字段级口径）。"""
    h1 = _generate_entity_rows(42)
    h2 = _generate_entity_rows(42)
    assert h1 == h2


def test_different_seed_produces_different_data():
    assert _generate_entity_rows(42) != _generate_entity_rows(7)


def test_cross_process_reproduction():
    """跨进程一致：子进程（不同 PYTHONHASHSEED）派生 Seed 与主进程一致。"""
    code = (
        "from ptde.generator.seed import derive_seed;"
        "print(derive_seed(42, 'orders', 'orders-0007', 'paid_at'))"
    )
    r1 = subprocess.run(
        [sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True,
        text=True, check=True, env={"PYTHONHASHSEED": "1", "PATH": "/usr/bin:/bin"},
    )
    r2 = subprocess.run(
        [sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True,
        text=True, check=True, env={"PYTHONHASHSEED": "random", "PATH": "/usr/bin:/bin"},
    )
    assert r1.stdout.strip() == r2.stdout.strip()
    assert r1.stdout.strip() == str(derive_seed(42, "orders", "orders-0007", "paid_at"))


def test_field_rng_cross_process_values():
    """派生 random.Random 的取值序列跨进程一致（PYTHONHASHSEED 不同仍复现）。"""
    code = (
        "import random;"
        "from ptde.generator.seed import field_rng;"
        "rng = field_rng(42, 'orders', 's1', 'paid_at');"
        "print(repr([rng.random() for _ in range(20)]))"
    )
    envs = ({"PYTHONHASHSEED": "1", "PATH": "/usr/bin:/bin"},
            {"PYTHONHASHSEED": "9", "PATH": "/usr/bin:/bin"})
    outs = [subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT,
                           capture_output=True, text=True, check=True,
                           env=e).stdout.strip() for e in envs]
    assert outs[0] == outs[1]


# ---------------------------------------------------------------- seed=None 显式退化


def test_seed_none_warns_and_does_not_pretend_reproducible():
    """seed=None：UserWarning + 未播种实例（is_reproducible 显式 False）。"""
    with pytest.warns(UserWarning, match="不可复现"):
        rng = field_rng(None, "orders", "s1", "paid_at")
    assert isinstance(rng, random.Random)
    assert is_reproducible(None) is False
    assert is_reproducible(42) is True
    assert SEED_DERIVATION_ALGORITHM == "sha256-v1"


def test_seed_none_two_runs_differ():
    """退化路径两次采样不同（证明未伪装成固定种子）。"""
    with pytest.warns(UserWarning):
        a = _values(field_rng(None, "orders", "s1", "paid_at"), 20)
    with pytest.warns(UserWarning):
        b = _values(field_rng(None, "orders", "s1", "paid_at"), 20)
    assert a != b
