# PTDE B4.2 批次交接文档 — R4.2 关系生成器泛型化（T4.5~T4.7）

> 状态：**已完成并通过门禁**（2026-11-18）
> 依据：`PTDE-实施方案-R0-R7分批执行计划.md` §7.2（R4.2 任务表 T4.5~T4.7）、§7.3（文件级清单）；`性能测试平台-智能测试数据引擎-完整技术方案.md` §7.6（主键外键生成/预分配区间）、§7.7（多对多与复杂映射）；
> `PTDE-落地方案-现状差距分析与改造路线.md` A1（分轨：legacy 保留不动）、A3（分级显式：冲突/缺参显式失败不静默）
> 代码位置：`perf-py/ptde/generator/`（纯新增 + `__init__.py` 导出面更新）

---

## 1. 交接基线与结果

- 入口基线：B0~B3 + QW1~QW4 + R4.1 已交付，测试数 **907**（`uv run pytest tests/contract tests/test_datagen_* tests/test_ptde_* -q` 全绿）。
- 交付后门禁：
  - `uv run pytest tests/contract tests/test_datagen_* tests/test_ptde_* -q` → **961 passed**（新增 54，零回归）；
  - `uv run ruff check ptde/ tests/test_ptde_generator_*.py` → **All checks passed**。
- 本批次为纯新增：4 个生产模块 + 4 个测试文件；**未触碰** legacy `datagen/`、`ptde_common/id_pool.py` 本体、`ptde/planner|models|adapters|profiler` 既有行为（git 状态中的 M 文件均为前序批次遗留）。
- 环境注意：本 session 曾因依赖缺失（`sqlalchemy` 等）测试收集失败，需先 `uv sync --all-extras`。

## 2. 交付内容（按任务）

### T4.5 IdPool 泛型化 — `ptde/generator/keys.py`

- **去除 `MAX_POOL_SIZE=500_000` 上限与整数限制**：键支持 int（含 bigint）/str/UUID/复合键（同构 tuple）；60 万键测试验证无上限。
- `IdRangeAllocator.allocate(entity, requests)`：预分配 ID 区间（§7.6 三重检查）——
  目标 Schema `allow_manual=False` 拒绝手工预分配自增；`existing_max` 接既有数据最大 ID；
  与 `reserved`（其他 Worker 已占区间）重叠 → **显式失败不静默改道**；分片按名称排序顺序打包（确定性），产出对齐 `ShardPlanDef` 的 `planned_range` 语义。
- `KeyPool`：`add/add_all/sample_uniform/sample_skewed/sample_invalid`；skew 算法与 legacy
  `IdPool` 一致（累计权重 + 二分 O(log n)），仅去上限；`sample_invalid` 按 int/str/UUID/复合键逐列生成必然不命中值。
- `uuid_from_rng(rng)`：rng 派生确定性 UUIDv4（version/variant 位合法）；`composite_keys(column_ranges)`：各列区间按行序 zip，**列序 = 调用方 dict 插入序**（复合主键列序有语义），长度不一致显式失败。
- rng **必须注入**（新链路一律 `seed.py` 派生；不提供 legacy `rng=None` 模块级 random 兼容路径）。数值定标口径沿用 B4.1 `primitive.scale_to_column_type`。

### T4.6 关系生成器 — `ptde/generator/relations.py`

- `assign_children(parent_ids, n_children, rng, dist=None, zero_child_ratio=None)`：
  每父子行数经 `samplers.sample` 采样（与 `derivation.sample_one` 同 fit 同 rng 口径）；
  `zero_child_ratio`（B2 零子占比口径）逐父判定（先于分布采样消耗随机流）；总量与
  n_children 差额按「少者先补 / 多者先减」**精确收敛**；dist=None → 确定性轮转。
- `bind_one_to_one`：互不重复绑定（子行数超父行数显式失败）；复合键 tuple 直接复用同一逻辑。
- M:N 按规模选型（§7.7）：`select_mn_strategy(total_pairs, memory_budget_pairs, shards, external_store)`
  优先序 computed → sharded → external → quota；`computed_pairs` 可计算映射——uniform 模式
  **纯函数 of i**（Knuth 乘法散列，不耗 rng，任意重算一致），skew 模式走 Zipf 逆变换（rng 必须注入）；`PairQuota` 关系配额（超发显式失败）。
- `logical_join_key`：逻辑关联（`RelationKind.LOGICAL`）无外键，父子两侧以相同参数重算
  确定性 join token（seed 派生链路）；`review_binding(binding, actual_total)`：消费
  `EntityPlan.parent_binding`（expected_total + tolerance 比例容差，DEFAULT 0.05）生成后复核（门禁 4 锚点）。
- 端到端逐行复现测试（门禁 1 管线级口径）：users→orders mini 管线（键 + 绑定 + 字段采样），
  同 root_seed 两次逐行一致、异 seed 不同。

### T4.7 循环依赖显式安全策略 — `ptde/generator/deps.py` + `cycle_policy.py`

- `deps.py`（升级 `topo_sorter.py` 思路）：迭代版 Tarjan SCC（深图不递归溢出；节点与邻接
  排序，输出确定性）；边方向 child → parent，`topo_groups` 产出**父实体组先于子实体组**；
  环 = SCC 长度 > 1 或自环；`require_acyclic` → `EntityCycleError` 显式失败输出环成员
  （复用 field_deps 显式失败语义）；**只分析不剔边**。
- `cycle_policy.py`（**废除 `break_weakest_cycle_edge` 静默剔边语义**，legacy 本体保留仅对照）：
  单环策略逐条判定（`resolve_cycle`）：
  1. 自引用环 → 带内自引用，`SELF_REF_MAX_DEPTH=5`（继承 `datagen/default_engine.py`）；
  2. 环内全部 FK 列可空 → `nullable_backfill`（两阶段：插 NULL → 回填）；
  3. 目标库支持延迟约束 → `deferred_constraint`；
  4. 显式批准关闭约束 → `staged_load`（总方案风险表：需显式批准并完整验证）；
  5. 无安全方式 → `CyclePolicyError` 逐边输出原因阻止执行；环成员缺 FK 可空性信息 → 显式失败（不得伪装可装载）。
  `resolve_cycles` 批量判定（任一环无安全方式即抛错）。

## 3. 文件清单

```text
新建（生产代码）
perf-py/ptde/generator/keys.py           # T4.5
perf-py/ptde/generator/relations.py      # T4.6
perf-py/ptde/generator/deps.py           # T4.7（Tarjan SCC）
perf-py/ptde/generator/cycle_policy.py   # T4.7（安全策略）

新建（测试）
perf-py/tests/test_ptde_generator_keys.py           # 区间三重检查 + 泛型键池
perf-py/tests/test_ptde_generator_relations.py      # 绑定统计 + M:N 选型 + 门禁1管线级
perf-py/tests/test_ptde_generator_deps.py           # SCC + 拓扑分组 + 显式失败
perf-py/tests/test_ptde_generator_cycle_policy.py   # 策略逐条判定 + 阻止执行

修改
perf-py/ptde/generator/__init__.py       # 导出面新增 keys/relations/deps/cycle_policy

未改动：datagen/**、ptde_common/id_pool.py（保留 MAX_POOL_SIZE 语义，R7 统一退役）、
ptde/planner/**、ptde/models/**、sql/init.sql
```

## 4. 关键设计决策与踩坑记录

1. **不导入 planner.derivation**（层向纯度，延续 R4.1 采样器先例）：期望/采样口径由
   `samplers.sample` 同 fit 同 rng 承载，`review_binding` 只消费 parent_binding 字典形态。
2. **assign_children 收敛调整的边界**：「少者先补/多者先减」在 n_children 与 P×E 期望量级
   偏差大时会轻微改变分布尾部（如压缩零子占比）——调用方应让 n_children ≈ P×E×(1-z)
   （planner 期望推导已保证）；测试按此口径设定样本量，勿按直觉改采样器。
3. **computed_pairs 两种模式随机流语义不同**：uniform 纯函数 of i 不耗 rng（重放零成本）；
   skew 模式耗 rng——重放必须按同序重放 rng 流（R4.3 worker 分片复现时注意）。
4. **composite_keys 列序勿改排序**：dict 插入序即复合主键列序（有语义）；测试曾按字母序
   断言导致错位。
5. **KeyPool 无上限是有意设计**：内存约束由 R4.3 分片 + 区间预分配承载（§7.7/§8），勿
   「补回」池上限或加缓存截断。
6. **Zipf 热点断言按闭式口径**：P(rank≤5)=H(5,1.2)/H(1000,1.2)≈47%（勿按直觉 50% 或
   均匀散列 25% 修正阈值）。
7. **Tarjan 迭代实现的两个坑**：构图先取键快照再补孤立父节点（迭代中改 dict →
   RuntimeError）；环带环外父实体时 SCC 产出顺序为 [父实体组, 环组]（父先于子，正确语义，
   勿按「环优先」改测试）。
8. **cycle_policy 自引用不查可空性**（带内自引用插入时动态追加 ID）；多实体环缺 FK 边
   信息按「信息不足」显式失败。
9. 前序批次坑延续有效：鸭子类型假连接、pydantic `extra="forbid"`、指纹排除时间戳、
   `zip(strict=)`（keys.py composite_keys 与测试均遵守）、`datetime.UTC`、ruff
   line-length=100（E501 ignore，E741 歧义变量名会报）。

## 5. 验收对照（实施方案 §7.6 中属 R4.2 的条目）

| 条目 | 结果 | 落点 |
|---|---|---|
| T4.5 去池上限与整数限制、bigint/UUID/复合键、预分配区间（§7.6 三重检查） | ✅ | `test_ptde_generator_keys.py` |
| T4.6 外键按分布采样父子绑定；M:N 四选型（§7.7） | ✅ computed/quota 全实现，sharded/external 选型已定（遗留 1） | `test_ptde_generator_relations.py` |
| T4.7 Tarjan SCC + 安全策略逐条判定 + 废除静默剔边；SELF_REF_MAX_DEPTH 继承 | ✅ | `test_ptde_generator_deps.py` + `test_ptde_generator_cycle_policy.py` |
| 门禁 1 同 DatasetSpec + Seed 两次构建逐行一致 | ✅ 管线级 mini 管线（完整端到端留 R4.3） | `test_ptde_generator_relations.py::test_end_to_end_row_level_reproducibility` |
| 门禁 4 复核锚点（parent_binding 容差复核） | ✅ review_binding 承载（Pilot Run 端到端留 R4.3） | `test_ptde_generator_relations.py` |

门禁 6（legacy 对照）：legacy 管线全程未动；`ptde_common/id_pool.py` 保留不动仅演进新模块。

## 6. 回归保障与回滚（§7.7）

- R4.2 不触碰 legacy 生成路径与共享层；回滚仅删除 4 个生产模块 + 4 个测试文件 +
  还原 `ptde/generator/__init__.py` 导出面即可，legacy 与其余批次完全不受影响。
- 不读生产明文样本，仅消费 DistributionFit/parent_binding 统计参数——脱敏边界延续 R2。

## 7. 遗留问题（按优先级）

1. **M:N sharded / external 两策略只有选型与接口**：执行载体待 R4.3（分片映射，配合
   `partitioner/staging`）与 R5（外部 KV/装载侧）实现；`select_mn_strategy` 返回值语义已锁定（勿改枚举值）。
2. **CycleFkEdge 需调用方投影**：`resolve_cycle` 输入的 FK 可空性需从 Schema 快照
   （`snapshot.py` ColumnSchema.nullable）投影——R4.3 worker 接线时补转换函数。
3. **验收门禁端到端口径留 R4.3**：门禁 1 逐行（完整多实体 DatasetSpec + 分片）、
   门禁 3 内存（百万级不全量驻内存）、门禁 4 Pilot Run（百万级 < 30min）。
4. **assign_children 无 zero_child_ratio 时 n_children 精确收敛靠调整**：超大规模 +
   重倾斜分布下调整量与分布保形的权衡未压测（R4.3 Pilot Run 观察点）。

## 8. 后续批次衔接（R4.3，供下一 session 使用）

- **T4.8 partitioner.py**：按表/ID 范围/分区键/确定性哈希切分；`IdRangeAllocator` 直接复用
  （分片区间已对齐 `ShardPlanDef.planned_range`）；分片大小由导入吞吐/Worker 资源/暂存空间/倾斜度调节（总方案 §8.4）。
- **T4.9 staging.py + file_format.py**：格式要素全集（总方案 §8.3/实施方案 §7.4：UTF-8、
  `\N` NULL、RFC4180、UTC+时区声明、Decimal 字符串化、sha256+行数、format_version）。
- **T4.10 stream.py**：与文件模式共用分片元数据/检查点协议；有界缓冲背压不丢数据。
- **T4.11 worker.py**：Worker 只产数据不持 DB 连接；分片间无共享可变状态；Seed 派生已含
  `shard_id`（`field_rng(root_seed, entity, shard_id, field)`），跨 Worker 复现的派生基础已就绪。
- **T4.12 Pilot Run**：借 legacy `executor.py` 降级通道（R5 才移交 Loader 侧）；A2 实测门禁
  百万级 < 30min；不达标触发 A2 决策反转评估。
- 语言无关分片协议（A2 预留）见实施方案 §7.5：`seed_derivation` 字段应填
  `seed.SEED_DERIVATION_ALGORITHM`（sha256-v1）。
