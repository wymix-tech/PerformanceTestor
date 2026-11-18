"""确定性数据生成内核（R4，实施方案 §7）：Primitive → Distribution Samplers → Relations。

B4.1 交付（R4.1 分布采样器，实施方案 §7.2 T4.1~T4.4）：
- primitive：`datagen/rule_engine.py` 函数级收编副本（T4.1；legacy 本体保留不动，
  A4a 终态才退役，两套并存期间以「同输入同输出」对照单测保证一致）；
- samplers：总方案 §7.1 全部 12 类分布采样器（T4.2；只认 DistributionFit
  type+params 形态，不读生产明文样本——脱敏边界延续 R2）；
- field_deps：条件分布字段依赖图（T4.3；消费 B3 条件对，拓扑决定字段生成
  顺序，环依赖显式失败不剔边——对齐 A1/A3）；
- seed：Seed 派生机制（T4.4；root_seed + entity + shard_id + field → 每字段
  独立 random.Random；跨进程一致；seed=None 退化路径显式）。

B4.2 交付（R4.2 关系生成器泛型化，实施方案 §7.2 T4.5~T4.7）：
- keys：泛型键池 + 预分配 ID 区间（T4.5；演进 ptde_common/id_pool.py——
  去池上限与整数限制、int/str/UUID/复合键、§7.6 三重区间检查）；
- relations：外键关联按分布采样父子绑定（T4.6；1:1/1:N/M:N/复合键/逻辑
  关联；M:N 按规模选型 §7.7；parent_binding 复核锚点）；
- deps：实体级循环依赖分析（T4.7；Tarjan SCC，升级 topo_sorter.py 思路）；
- cycle_policy：环依赖安全策略逐条判定（T4.7；废除 break_weakest_cycle_edge
  静默剔边语义，无安全方式显式阻止并输出原因）。

B4.3 交付（R4.3 分片并行 + Pilot Run，实施方案 §7.2 T4.8~T4.12）：
- partitioner：分片规划器（T4.8；planned_range 经 §7.6 三重检查 /
  hash_bucket 确定性 sha256；分片大小按总方案 §8.4 指标动态调节）；
- file_format + staging：文件中转模式（T4.9；格式要素全集 §7.4、每分片
  sha256+行数校验和、manifest 检查点原子落盘可恢复）；
- stream：流式直传模式（T4.10；有界缓冲 + 背压不丢数据，与文件模式共用
  分片元数据/格式定义/检查点协议）；
- worker：多进程 Worker（T4.11；只产数据不持 DB 连接、分片间无共享可变
  状态、协议 §7.5 JSON 原生、跨 Worker 复现一致）；
- cycle_policy.project_fk_edges：B4.2 遗留问题 2（Schema 快照 → 环内
  CycleFkEdge 投影）。
A2 生成 Pilot（T4.12）落 ptde/planner/pilot_run.run_generation_pilot。
"""

from ptde.generator import (
    cycle_policy,
    deps,
    field_deps,
    file_format,
    keys,
    partitioner,
    primitive,
    relations,
    samplers,
    seed,
    staging,
    stream,
    worker,
)
from ptde.generator.field_deps import (
    FieldDependencyError,
    build_field_deps,
    generation_orders,
    topological_order,
)
from ptde.generator.samplers import (
    SUPPORTED_TYPES,
    sample,
    sample_params,
)
from ptde.generator.seed import derive_seed, field_rng, is_reproducible

__all__ = [
    "SUPPORTED_TYPES",
    "FieldDependencyError",
    "build_field_deps",
    "cycle_policy",
    "deps",
    "derive_seed",
    "field_deps",
    "field_rng",
    "file_format",
    "generation_orders",
    "is_reproducible",
    "keys",
    "partitioner",
    "primitive",
    "relations",
    "sample",
    "sample_params",
    "samplers",
    "seed",
    "staging",
    "stream",
    "topological_order",
    "worker",
]
