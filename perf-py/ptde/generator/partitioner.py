"""分片规划器（R4 T4.8，实施方案 §7.2；总方案 §8.4/§8.3/协议 §7.5）。

按表 / ID 范围 / 分区键 / 确定性哈希切分：
- 整型键实体走 ``planned_range``：区间经 ``keys.IdRangeAllocator`` 三重检查
  （目标 Schema / 既有数据 / 多 Worker 分片规则）后顺序打包，冲突显式失败；
- 非整型键实体走 ``hash_bucket``：``hash_bucket`` 用 sha256（禁用内置
  ``hash()``——受 PYTHONHASHSEED 影响不可跨进程复现，延续 seed.py 口径）；
- 分片大小由总方案 §8.4 指标动态调节：导入吞吐 / Worker 资源 / 暂存空间 /
  倾斜度（``compute_rows_per_shard``，纯确定性公式，输入相同输出相同）。

产出自 ptde.models.spec 的 ``ShardingPlan``（协议 §7.5 计划侧：
partition_algorithm_version 记录算法版本，变更需 bump 并写入
ExecutionManifest）。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from typing import Any

from ptde.generator.keys import IdRangeAllocator
from ptde.models.spec import GenerationPlan, ShardingPlan, ShardPlanDef

PARTITION_ALGORITHM_VERSION = "1"  # 协议 §7.5：变更需 bump 并记录 ExecutionManifest
DEFAULT_ROWS_PER_SHARD = 250_000
DEFAULT_SHARDS_PER_WORKER = 4  # 均衡分片数（Worker 数 × k，防长尾）
MIN_ROWS_PER_SHARD = 1_000
MAX_ROWS_PER_SHARD = 2_000_000

KEY_KIND_RANGE = "planned_range"
KEY_KIND_HASH = "hash_bucket"


def compute_rows_per_shard(
    total_rows: int,
    workers: int = 1,
    shards_per_worker: int = DEFAULT_SHARDS_PER_WORKER,
    staging_bytes: int | None = None,
    avg_row_bytes: int | None = None,
    import_rows_per_sec: float | None = None,
    target_load_seconds: float | None = None,
    skew_factor: float | None = None,
    min_rows: int = MIN_ROWS_PER_SHARD,
    max_rows: int = MAX_ROWS_PER_SHARD,
) -> int:
    """按总方案 §8.4 指标计算分片行数（确定性：同输入同输出）。

    约束取交集后钳制到 [min_rows, max_rows]：
    - 均衡：total_rows / (workers × shards_per_worker)（防 Worker 长尾）；
    - 暂存空间：staging_bytes / avg_row_bytes（两者齐备才生效）；
    - 导入吞吐：import_rows_per_sec × target_load_seconds（单分片装载时长）；
    - 倾斜度 skew_factor ∈ (0, 1]：热点占比越高分片越小（并行度补偿倾斜）。
    显式失败：total_rows 非正、workers/shards_per_worker 非正、区间倒挂、
    skew_factor 越界、staging/avg_row_bytes 只给其一。
    """
    if total_rows <= 0:
        raise ValueError(f"总行数必须为正，收到 {total_rows}（显式失败）")
    if workers <= 0 or shards_per_worker <= 0:
        raise ValueError(
            f"workers 与 shards_per_worker 必须为正，收到 workers={workers}, "
            f"shards_per_worker={shards_per_worker}（显式失败）"
        )
    if (staging_bytes is None) != (avg_row_bytes is None):
        raise ValueError(
            "staging_bytes 与 avg_row_bytes 必须同时给出（暂存空间约束需要行均字节）"
        )
    if staging_bytes is not None and (staging_bytes <= 0 or avg_row_bytes <= 0):
        raise ValueError("staging_bytes 与 avg_row_bytes 必须为正（显式失败）")
    if (import_rows_per_sec is None) != (target_load_seconds is None):
        raise ValueError(
            "import_rows_per_sec 与 target_load_seconds 必须同时给出"
            "（吞吐约束需要目标装载时长）"
        )
    if import_rows_per_sec is not None and (import_rows_per_sec <= 0 or target_load_seconds <= 0):
        raise ValueError("import_rows_per_sec 与 target_load_seconds 必须为正（显式失败）")
    if min_rows <= 0 or max_rows < min_rows:
        raise ValueError(f"分片行数区间非法：[{min_rows}, {max_rows}]（显式失败）")
    if skew_factor is not None and not 0.0 < skew_factor <= 1.0:
        raise ValueError(f"skew_factor 必须在 (0, 1]，收到 {skew_factor}（显式失败）")

    rows = math.ceil(total_rows / (workers * shards_per_worker))
    if staging_bytes is not None:
        rows = min(rows, math.floor(staging_bytes / avg_row_bytes))
    if import_rows_per_sec is not None:
        rows = min(rows, math.ceil(import_rows_per_sec * target_load_seconds))
    if skew_factor is not None:
        rows = math.floor(rows * (1.0 - skew_factor / 2.0))
    return max(min_rows, min(rows, max_rows))


def hash_bucket(key: Any, buckets: int) -> int:
    """确定性哈希分桶：sha256(str(key)) 前 8 字节 mod buckets。

    不用内置 hash()（PYTHONHASHSEED 不可跨进程复现）；key 须可 str()。
    """
    if buckets <= 0:
        raise ValueError(f"桶数必须为正，收到 {buckets}（显式失败）")
    digest = hashlib.sha256(str(key).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % buckets


def plan_shards(
    entities: Mapping[str, int],
    rows_per_shard: int = DEFAULT_ROWS_PER_SHARD,
    key_kinds: Mapping[str, str] | None = None,
    id_allocator: IdRangeAllocator | None = None,
) -> ShardingPlan:
    """按实体切分并产出 ShardingPlan（确定性：实体与分片均按名称排序）。

    - 整型键实体（默认）→ ``planned_range``：必须提供 id_allocator，区间经
      三重检查后打包（keys.py §7.6 口径）；
    - ``hash_bucket`` 实体：键为分区键，桶数 = ceil(rows / rows_per_shard)，
      行落桶用 ``hash_bucket``（id_start/id_end 留空）；
    - 0 行实体不产分片（显式跳过，不静默造空分片）。
    显式失败：rows_per_shard 非正、实体行数非负、planned_range 缺分配器、
    未知 key kind、分配器区间冲突（IdRangeAllocator 抛出）。
    """
    if rows_per_shard <= 0:
        raise ValueError(f"rows_per_shard 必须为正，收到 {rows_per_shard}（显式失败）")
    kinds = dict(key_kinds or {})
    unknown = set(kinds) - set(entities)
    if unknown:
        raise ValueError(f"key_kinds 含未规划实体：{sorted(unknown)}（显式失败）")
    bad = {e: n for e, n in entities.items() if n < 0}
    if bad:
        raise ValueError(f"实体行数必须非负，非法：{bad}（显式失败）")

    shards: list[ShardPlanDef] = []
    for entity in sorted(entities):
        rows = entities[entity]
        if rows == 0:
            continue
        kind = kinds.get(entity, KEY_KIND_RANGE)
        if kind == KEY_KIND_RANGE:
            if id_allocator is None:
                raise ValueError(
                    f"实体 {entity} 为 planned_range 键型，必须提供 IdRangeAllocator"
                    "（区间须经 §7.6 三重检查，显式失败不静默跳过）"
                )
            n_shards = math.ceil(rows / rows_per_shard)
            names = {f"{entity}-{i:04d}": rows_per_shard for i in range(n_shards)}
            names[sorted(names)[-1]] = rows - rows_per_shard * (n_shards - 1)
            for rng in id_allocator.allocate(entity, names):
                shards.append(
                    ShardPlanDef(
                        entity=entity,
                        shard_id=rng.shard_id,
                        id_start=rng.start,
                        id_end=rng.end,
                        strategy=KEY_KIND_RANGE,
                        rows=rng.size,
                    )
                )
        elif kind == KEY_KIND_HASH:
            n_buckets = math.ceil(rows / rows_per_shard)
            bucket_rows = rows_per_shard
            for i in range(n_buckets):
                if i == n_buckets - 1:
                    bucket_rows = rows - rows_per_shard * (n_buckets - 1)
                shards.append(
                    ShardPlanDef(
                        entity=entity,
                        shard_id=f"{entity}-hb-{i:04d}",
                        id_start=None,
                        id_end=None,
                        strategy=KEY_KIND_HASH,
                        rows=bucket_rows,
                    )
                )
        else:
            raise ValueError(
                f"实体 {entity} 未知键型 {kind!r}（支持 planned_range/hash_bucket，显式失败）"
            )
    return ShardingPlan(partition_algorithm_version=PARTITION_ALGORITHM_VERSION, shards=shards)


def build_sharding_plan(
    plan: GenerationPlan,
    rows_per_shard: int = DEFAULT_ROWS_PER_SHARD,
    key_kinds: Mapping[str, str] | None = None,
    existing_max: Mapping[str, int] | None = None,
    reserved: Any = (),
    allow_manual: bool = True,
) -> ShardingPlan:
    """GenerationPlan → ShardingPlan（消费 plan.entities 行数，便捷入口）。"""
    entities = {name: ep.rows for name, ep in plan.entities.items()}
    allocator = IdRangeAllocator(existing_max=existing_max, reserved=reserved,
                                 allow_manual=allow_manual)
    return plan_shards(entities, rows_per_shard=rows_per_shard, key_kinds=key_kinds,
                       id_allocator=allocator)
