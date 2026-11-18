"""Seed 可复现机制（R4 T4.4，实施方案 §7.2；总方案 §12.4 可复现要素）。

每字段采样器持有派生自 root_seed + entity + shard_id + field 的独立
random.Random 实例：
- 派生用 sha256（跨进程/跨解释器一致；禁用内置 hash()——其受 PYTHONHASHSEED
  影响不可跨进程复现），派生算法版本 SEED_DERIVATION_ALGORITHM 记录可追溯；
- 同 root_seed 两次生成逐字段一致（对照哈希，验收门禁 1）；
- root_seed=None 时显式退化：发 warning 并返回未播种实例——不伪装可复现
  （A3 分级显式；QW1 legacy 的 rng=None 模块级 random 路径保留不动）。
"""

from __future__ import annotations

import hashlib
import random
import warnings

SEED_DERIVATION_ALGORITHM = "sha256-v1"
SEED_NAMESPACE = "ptde.generator.seed.v1"


def derive_seed(root_seed: int, entity: str, shard_id: str, field: str) -> int:
    """派生字段级 Seed：sha256(namespace|root_seed|entity|shard_id|field) 前 8 字节。"""
    if root_seed is None:
        raise ValueError(
            "root_seed 未配置：无法派生确定性 Seed（可复现需显式 seed，"
            "DatasetSpec.reproducibility.seed 为配置入口）"
        )
    payload = f"{SEED_NAMESPACE}|{root_seed}|{entity}|{shard_id}|{field}"
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def field_rng(root_seed: int | None, entity: str, shard_id: str,
              field: str) -> random.Random:
    """字段级独立随机源（确定性：派生 Seed 落地 random.Random）。

    root_seed=None → 显式退化路径：UserWarning + 未播种实例（每次运行不同，
    不伪装可复现）。
    """
    if root_seed is None:
        warnings.warn(
            f"seed 未配置：{entity}.{field} 采样退化为不可复现路径"
            "（不伪装可复现；如需复现请配置 DatasetSpec.reproducibility.seed）",
            UserWarning,
            stacklevel=2,
        )
        return random.Random()
    return random.Random(derive_seed(root_seed, entity, shard_id, field))


def is_reproducible(root_seed: int | None) -> bool:
    """Seed 链路是否可复现（None = 退化路径，显式 False）。"""
    return root_seed is not None
