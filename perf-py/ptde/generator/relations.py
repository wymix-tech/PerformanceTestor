"""关系生成器（R4 T4.6，实施方案 §7.2；总方案 §7.6/§7.7）。

外键关联按分布采样父子绑定（消费 B2/B3 产出，口径对齐勿另起一套）：
- 每父实体子行数分布：`RelationCardinalityProfile.child_per_parent_distribution`
  → 经 `samplers.sample` 采样（与 derivation.sample_one 同 fit 同 rng 逐值一致）；
  `zero_child_ratio`（B2 采集口径：零子实体父实体占比）按父实体逐个判定；
- 复核锚点：`EntityPlan.parent_binding`（expected_total + tolerance 比例容差，
  derivation.py 产出）→ `review_binding` 生成后复核；
- M:N 按规模选型（§7.7）：可计算映射 / 分片映射 / 外部 KV / 关系配额；
- 复合键：键为 tuple，父子绑定逻辑与单列键完全一致（keys.composite_keys）；
- 逻辑关联（RelationKind.LOGICAL）：无外键，父子两侧以相同参数重算确定性
  join token 关联（`logical_join_key`）。

rng 必须注入（确定性；seed.py 派生随机源）。
"""

from __future__ import annotations

import random
from collections.abc import Iterator, Mapping, Sequence
from enum import StrEnum

from ptde.generator.samplers import sample as sample_distribution
from ptde.generator.seed import derive_seed
from ptde.models.profile import DistributionFit

# M:N 可计算映射的均匀散列乘子（Knuth multiplicative，纯函数 of i，不耗 rng）
_KNUTH_MULT = 2654435761


class MnStrategy(StrEnum):
    """M:N 映射选型（总方案 §7.7 优先序：computed → sharded → external → quota）。"""

    COMPUTED = "computed"
    SHARDED = "sharded"
    EXTERNAL = "external_kv"
    QUOTA = "quota"


def select_mn_strategy(
    total_pairs: int,
    memory_budget_pairs: int | None = None,
    shards: int = 1,
    external_store: bool = False,
) -> MnStrategy:
    """按规模选 M:N 映射策略（数亿级映射不全量驻内存，§7.7）。

    - total_pairs 可全部驻内存（或未设预算）→ 可计算映射；
    - 超预算但多分片 → 分片映射（每分片只保存自身需要的映射）；
    - 有外部存储 → 外部 KV；
    - 兜底 → 关系配额（全局计数器保证目标关联数量，恒可用）。
    """
    if total_pairs <= 0:
        raise ValueError(f"M:N 关联对数必须为正，收到 {total_pairs}（显式失败）")
    if memory_budget_pairs is not None and memory_budget_pairs <= 0:
        raise ValueError(f"内存预算必须为正，收到 {memory_budget_pairs}（显式失败）")
    if memory_budget_pairs is None or total_pairs <= memory_budget_pairs:
        return MnStrategy.COMPUTED
    if shards > 1:
        return MnStrategy.SHARDED
    if external_store:
        return MnStrategy.EXTERNAL
    return MnStrategy.QUOTA


def assign_children(
    parent_ids: Sequence,
    n_children: int,
    rng: random.Random,
    dist: DistributionFit | None = None,
    zero_child_ratio: float | None = None,
) -> list:
    """1:N / N:1 关联绑定：返回长度 n_children 的父键列表（按子行序）。

    确定性算法（rng 注入，与 samplers.sample 同 fit 同 rng 逐值一致）：
    - dist 为 None → 轮转均匀绑定（每子行依次取父键，不消耗随机流）；
    - 有 dist → 逐父实体采样子行数（`zero_child_ratio` 先判定零子占比，
      非零父 max(1, round(采样值))），总量与 n_children 的差额按
      「少者先补 / 多者先减」确定性收敛到精确目标。
    显式失败：父池空、zero_child_ratio 越界、分布采样值非数值。
    """
    if n_children < 0:
        raise ValueError(f"子行数必须非负，收到 {n_children}（显式失败）")
    if not parent_ids and n_children > 0:
        raise ValueError("父键池为空，无法绑定子行（显式失败，不静默置 NULL）")
    if dist is None:
        if zero_child_ratio is not None:
            raise ValueError(
                "zero_child_ratio 依赖子行数分布（B2 口径），无分布时显式失败"
                "（不得伪装统计语义——A3）"
            )
        return [parent_ids[i % len(parent_ids)] for i in range(n_children)]
    if zero_child_ratio is not None and not 0.0 <= zero_child_ratio < 1.0:
        raise ValueError(
            f"zero_child_ratio 必须在 [0, 1)，收到 {zero_child_ratio}（显式失败）"
        )

    counts = [0] * len(parent_ids)
    for i in range(len(parent_ids)):
        if zero_child_ratio is not None and rng.random() < zero_child_ratio:
            counts[i] = 0
            continue
        value = sample_distribution(dist, rng)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"子行数分布采样值非数值（{value!r}），无法作为每父子行数——显式失败"
            )
        counts[i] = max(1, round(value))
    total = sum(counts)
    if total < n_children:
        # 少者先补：按 (当前子行数, 父下标) 升序轮转补齐（确定性）
        order = sorted(range(len(parent_ids)), key=lambda i: (counts[i], i))
        for k in range(n_children - total):
            counts[order[k % len(order)]] += 1
    elif total > n_children:
        # 多者先减：按 (当前子行数降序, 父下标) 逐个削减到 0 为止（确定性）
        surplus = total - n_children
        order = sorted(range(len(parent_ids)), key=lambda i: (-counts[i], i))
        for i in order:
            if surplus <= 0:
                break
            take = min(counts[i], surplus)
            counts[i] -= take
            surplus -= take
    bindings: list = []
    for i, c in enumerate(counts):
        bindings.extend([parent_ids[i]] * c)
    return bindings


def bind_one_to_one(parent_ids: Sequence, n_children: int, rng: random.Random) -> list:
    """1:1 关联绑定：子行绑定互不重复的父键（子行数超父行数显式失败）。"""
    if n_children < 0:
        raise ValueError(f"子行数必须非负，收到 {n_children}（显式失败）")
    if n_children > len(parent_ids):
        raise ValueError(
            f"1:1 关联子行数 {n_children} 超过父行数 {len(parent_ids)}，"
            "无法一一绑定（显式失败，不静默降级为 1:N）"
        )
    if n_children == 0:
        return []
    return rng.sample(list(parent_ids), n_children)


def computed_pairs(
    left_count: int,
    right_count: int,
    n_pairs: int,
    rng: random.Random | None = None,
    skew_exponent: float | None = None,
    max_rank: int = 1000,
) -> Iterator[tuple[int, int]]:
    """M:N 可计算映射（§7.7：实体 ID 范围 + 确定性函数推导，生成器逐对产出
    不驻内存）。

    - uniform（默认）：第 i 对 = (i % left_count, (i·Knuth) % right_count)，
      纯函数 of i，不消耗 rng——任意重算一致；
    - skew（skew_exponent 给出）：左键按 Zipf 逆变换取热点（rng 必须注入，
      重放 rng 流确定性）。
    """
    if left_count <= 0 or right_count <= 0:
        raise ValueError(
            f"M:N 两侧实体数必须为正，收到 left={left_count}, right={right_count}"
            "（显式失败）"
        )
    if n_pairs < 0:
        raise ValueError(f"M:N 关联对数必须非负，收到 {n_pairs}（显式失败）")
    if skew_exponent is not None:
        if rng is None:
            raise ValueError("skew 模式 rng 必须注入（random.Random 实例）")
        from ptde.generator.samplers.zipf import MAX_RANK_LIMIT, sample_rank

        for _ in range(n_pairs):
            rank = sample_rank(rng, float(skew_exponent), max(1, min(max_rank, MAX_RANK_LIMIT)))
            left = min(rank - 1, left_count - 1)
            yield (left, rng.randrange(right_count))
        return
    for i in range(n_pairs):
        yield (i % left_count, (i * _KNUTH_MULT) % right_count)


class PairQuota:
    """关系配额（§7.7）：全局计数器保证目标关联数量，超发显式失败。"""

    def __init__(self, total: int) -> None:
        if total < 0:
            raise ValueError(f"配额必须非负，收到 {total}（显式失败）")
        self.total = total
        self._used = 0

    @property
    def remaining(self) -> int:
        return self.total - self._used

    def consume(self, n: int = 1) -> None:
        if n < 0 or self.remaining - n < 0:
            raise ValueError(
                f"关系配额超发：剩余 {self.remaining}，请求 {n}"
                "（目标关联数量必须精确，显式失败不静默截断）"
            )
        self._used += n


def logical_join_key(
    entity: str, shard_id: str, field: str, row_index: int, root_seed: int
) -> str:
    """逻辑关联 join token（RelationKind.LOGICAL，无外键）。

    父子两侧以相同参数重算即得相同 token（确定性，seed.py 派生链路）：
    token = entity:shard:field:sha256 派生值低 63 位十六进制。
    """
    if root_seed is None:
        raise ValueError(
            "root_seed 未配置：逻辑关联 token 无法确定性重算"
            "（可复现需显式 seed，DatasetSpec.reproducibility.seed 为配置入口）"
        )
    digest = derive_seed(root_seed, entity, shard_id, f"{field}#{row_index}")
    return f"{entity}:{shard_id}:{field}:{digest & ((1 << 63) - 1):016x}"


def review_binding(binding: Mapping, actual_total: int) -> bool:
    """生成后复核（验收门禁 4）：关联总量落在 parent_binding 容差内。

    binding 形态 = derivation.py parent_binding：expected_total + tolerance
    （比例容差，DEFAULT_TOLERANCE=0.05）。
    """
    expected = binding.get("expected_total")
    if expected is None:
        raise ValueError("parent_binding 缺少 expected_total 锚点（显式失败）")
    expected = int(expected)
    if expected == 0:
        return actual_total == 0
    tolerance = float(binding.get("tolerance", 0.05))
    return abs(actual_total - expected) <= tolerance * expected
