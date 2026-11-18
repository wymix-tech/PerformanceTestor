"""泛型键池与预分配 ID 区间（R4 T4.5，实施方案 §7.2；总方案 §7.6）。

演进 `ptde_common/id_pool.py`（legacy 本体保留不动，A1 分轨，R7 统一退役）：
- **去除 `MAX_POOL_SIZE=500_000` 上限与整数限制**：键支持 int / str / UUID /
  复合键 tuple（同构 tuple），池不设上限——百万级不全量驻内存由 R4.3 分片
  （T4.8）与区间预分配承载；
- **预分配 ID 区间接口**（总方案 §7.6）：区间分配必须经目标 Schema（是否允许
  手工预分配自增）、既有数据（existing_max）与多 Worker 分片（reserved 区间）
  检查，任何冲突显式失败、不静默改道——对齐 A3 分级显式；
- rng 必须注入（确定性）：新链路一律走 `seed.py` 派生随机源，不提供 legacy 的
  `rng=None` 模块级 random 兼容路径（该兼容语义仅 legacy 本体保留）。

数值定标口径沿用 `primitive.scale_to_column_type`（B4.1 已提供）。
"""

from __future__ import annotations

import bisect
import math
import random
import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

INVALID_STR_PREFIX = "__invalid__"


@dataclass(frozen=True)
class IdRange:
    """预分配 ID 区间（含端点；对齐 ShardPlanDef 的 planned_range 语义）。"""

    entity: str
    shard_id: str
    start: int
    end: int
    strategy: str = "planned_range"

    @property
    def size(self) -> int:
        return self.end - self.start + 1

    def overlaps(self, other: IdRange) -> bool:
        return self.entity == other.entity and self.start <= other.end and other.start <= self.end


class IdRangeAllocator:
    """预分配 ID 区间分配器（总方案 §7.6：区间经三重检查后顺序打包）。"""

    def __init__(
        self,
        existing_max: Mapping[str, int] | None = None,
        reserved: Iterable[IdRange] = (),
        allow_manual: bool = True,
    ) -> None:
        """existing_max：既有数据每实体最大 ID（含数据库 Sequence/自增当前值）；
        reserved：其他 Worker/其他数据集已占用区间；allow_manual：目标 Schema
        是否允许手工预分配自增值（§7.6：不能假设所有数据库都适合）。"""
        self._existing_max = dict(existing_max or {})
        self._reserved = list(reserved)
        self._allow_manual = allow_manual

    def allocate(self, entity: str, requests: Mapping[str, int]) -> list[IdRange]:
        """按分片顺序打包互不重叠的 ID 区间（确定性：分片按名称排序）。

        显式失败（不静默改道）：
        - 目标 Schema 不允许手工预分配；
        - 请求为空或区间长度非正；
        - 与既有数据/已保留区间重叠。
        """
        if not self._allow_manual:
            raise ValueError(
                f"实体 {entity} 目标 Schema 不允许手工预分配自增 ID"
                "（总方案 §7.6；改用数据库 Sequence 预取或UUID 策略）"
            )
        if not requests:
            raise ValueError(f"实体 {entity} 无分片区间请求（显式失败，不做空分配）")
        bad = {s: n for s, n in requests.items() if n <= 0}
        if bad:
            raise ValueError(f"实体 {entity} 区间长度必须为正，非法请求：{bad}")

        allocated: list[IdRange] = []
        cursor = self._existing_max.get(entity, 0)
        for shard_id in sorted(requests):
            start = cursor + 1
            end = start + requests[shard_id] - 1
            rng = IdRange(entity=entity, shard_id=shard_id, start=start, end=end)
            for r in self._reserved:
                if rng.overlaps(r):
                    raise ValueError(
                        f"实体 {entity} 分片 {shard_id} 预分配区间 [{start}, {end}] "
                        f"与已保留区间 {r}（分片 {r.shard_id}）重叠——多 Worker 分片"
                        "规则检查失败，显式失败不静默改道"
                    )
            allocated.append(rng)
            cursor = end
        return allocated


class KeyPool:
    """泛型键池：无池上限，键支持 int / str / UUID / 复合键（同构 tuple）。"""

    def __init__(self) -> None:
        self._ids: list[Any] = []
        self._cumulative_weights: list[float] | None = None
        self._weighted = False

    def add(self, key) -> None:
        self._ids.append(key)
        self._weighted = False

    def add_all(self, keys: Iterable) -> None:
        self._ids.extend(keys)
        self._weighted = False

    def size(self) -> int:
        return len(self._ids)

    def is_empty(self) -> bool:
        return not self._ids

    def keys(self) -> list:
        """键快照（副本；大批量场景请走区间预分配而非全量驻留）。"""
        return list(self._ids)

    def _check_rng(self, rng: random.Random | None) -> random.Random:
        if rng is None:
            raise ValueError(
                "rng 必须注入（random.Random 实例，seed.py 派生）——"
                "新链路不提供模块级 random 退化路径"
            )
        return rng

    def sample_uniform(self, rng: random.Random):
        """均匀采样一个键（确定性：rng 注入）。"""
        self._check_rng(rng)
        if not self._ids:
            raise RuntimeError("键池为空，无法采样 ref 值")
        return self._ids[rng.randrange(len(self._ids))]

    def build_skew_weights(self, hot_percent: int, hot_share: int) -> None:
        """热点倾斜权重：前 hot_percent% 的键合计权重 hot_share%
        （算法与 legacy IdPool 一致，仅去池上限）。"""
        if not self._ids or hot_percent <= 0 or hot_percent >= 100:
            self._cumulative_weights = None
            self._weighted = False
            return
        hot_count = max(1, math.floor(len(self._ids) * hot_percent / 100.0 + 0.5))
        hot_each = hot_share / hot_count
        cold_each = (100 - hot_share) / max(1, len(self._ids) - hot_count)
        acc = 0.0
        w: list[float] = []
        for i in range(len(self._ids)):
            acc += hot_each if i < hot_count else cold_each
            w.append(acc)
        self._cumulative_weights = w
        self._weighted = True

    def sample_skewed(self, rng: random.Random):
        """热点倾斜采样：累计权重 + 二分查找 O(log n)/行（与 legacy 一致）。"""
        self._check_rng(rng)
        if not self._ids:
            raise RuntimeError("键池为空，无法采样 refSkew 值")
        if not self._weighted or self._cumulative_weights is None:
            return self.sample_uniform(rng)
        cum = self._cumulative_weights
        pick = rng.random() * cum[-1]
        return self._ids[bisect.bisect_left(cum, pick)]

    def sample_invalid(self, rng: random.Random):
        """无效键（orphanRate 替换用）：按键类型生成必然不命中真实行的值。"""
        self._check_rng(rng)
        if not self._ids:
            raise RuntimeError("键池为空，无法采样无效键")
        return self._invalid_one(self._ids[0], rng)

    def _invalid_one(self, sample_key, rng: random.Random):
        if isinstance(sample_key, bool):
            raise ValueError(f"不支持的键类型 {type(sample_key).__name__}（显式失败）")
        if isinstance(sample_key, int):
            return max(self._ids) + 1 + rng.randrange(1000)
        if isinstance(sample_key, str):
            return f"{INVALID_STR_PREFIX}{rng.randrange(10**6):06d}"
        if isinstance(sample_key, uuid.UUID):
            return uuid_from_rng(rng)
        if isinstance(sample_key, tuple):
            if len({type(k) for k in self._ids}) > 1 \
                    or any(not isinstance(k, tuple) or len(k) != len(sample_key) for k in self._ids):
                raise ValueError("复合键池必须为同构 tuple（同列数），显式失败")
            return tuple(self._invalid_element(i, rng) for i in range(len(sample_key)))
        raise ValueError(
            f"不支持的键类型 {type(sample_key).__name__}"
            "（支持 int/str/UUID/复合键 tuple，显式失败）"
        )

    def _invalid_element(self, col_idx: int, rng: random.Random):
        """复合键单列的无效值（按该列键类型逐列生成）。"""
        values = [k[col_idx] for k in self._ids]
        head = values[0]
        if isinstance(head, bool):
            raise ValueError(f"不支持的复合键列类型 {type(head).__name__}（显式失败）")
        if isinstance(head, int):
            return max(values) + 1 + rng.randrange(1000)
        if isinstance(head, str):
            return f"{INVALID_STR_PREFIX}{rng.randrange(10**6):06d}"
        if isinstance(head, uuid.UUID):
            return uuid_from_rng(rng)
        raise ValueError(
            f"不支持的复合键列类型 {type(head).__name__}（显式失败）"
        )


def uuid_from_rng(rng: random.Random) -> uuid.UUID:
    """由注入 rng 派生确定性 UUIDv4（同 rng 流同 UUID；跨进程可复现）。"""
    if rng is None:
        raise ValueError("rng 必须注入（random.Random 实例，seed.py 派生）")
    raw = bytearray(rng.getrandbits(128).to_bytes(16, "big"))
    raw[6] = (raw[6] & 0x0F) | 0x40  # version 4
    raw[8] = (raw[8] & 0x3F) | 0x80  # RFC 4122 variant
    return uuid.UUID(bytes=bytes(raw))


def composite_keys(column_ranges: Mapping[str, IdRange]) -> Iterator[tuple]:
    """复合主键生成：各列预分配区间按行序 zip（列数不定，区间长度必须一致）。

    复合键语义 = 各列独立区间预分配 + 逐行组合（§7.6 复合主键生成器）；
    各列长度不一致属计划矛盾，显式失败。
    """
    if not column_ranges:
        raise ValueError("复合键至少需要一个列区间（显式失败）")
    sizes = {name: r.size for name, r in column_ranges.items()}
    if len(set(sizes.values())) > 1:
        raise ValueError(f"复合键各列区间长度不一致：{sizes}（计划矛盾，显式失败）")
    columns = {}
    for name, r in column_ranges.items():
        columns[name] = range(r.start, r.end + 1)
    # 列序沿用调用方给定顺序（dict 插入序确定；复合主键列序有语义）
    yield from zip(*(columns[n] for n in column_ranges), strict=True)
