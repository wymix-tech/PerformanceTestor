# PTDE B4.1 批次交接文档 — R4.1 分布采样器（T4.1~T4.4）

> 状态：**已完成并通过门禁**（2026-11-17）
> 依据：`PTDE-实施方案-R0-R7分批执行计划.md` §7.2（R4.1 任务表）、§7.6（验收门禁 1/2/6 属 R4.1 部分）、§7.7（回归保障）；
> `性能测试平台-智能测试数据引擎-完整技术方案.md` §7.1/§7.2/§7.3；
> `PTDE-落地方案-现状差距分析与改造路线.md` A1（分轨：legacy 剔边/钳制保留不动）、A3（分级显式：估计值不得伪装统计）
> 代码位置：`perf-py/ptde/generator/`（纯新增包）

---

## 1. 交接基线与结果

- 入口基线：B0~B3 + QW1~QW4 已交付，测试数 **778**（`uv run pytest tests/contract tests/test_datagen_* tests/test_ptde_* -q` 全绿）。
- 交付后门禁：
  - `uv run pytest tests/contract tests/test_datagen_* tests/test_ptde_* -q` → **907 passed**（新增 129，零回归）；
  - `uv run ruff check ptde/ tests/test_ptde_*` → **All checks passed**。
- 本批次为纯新增：`ptde/generator/` 包 + 4 个新测试文件；**未触碰** legacy `datagen/`、`sql/init.sql`、`ptde/adapters|profiler|planner` 既有行为（git 状态中的 M 文件均为前序批次遗留）。

## 2. 交付内容（按任务）

### T4.1 Primitive 收编 — `ptde/generator/primitive.py`

- `datagen/rule_engine.py` **函数级副本迁移**（本体保留不动，A4a 终态才退役）：
  `apply_rule / weighted_index / normalize_scalar / scale_to_column_type / base_type / is_integer_family / seq_value`，
  含依赖辅助 `generate_default / random_text / _java_round / _pick`。
- QW1 的 `random.Random(seed)` 注入语义与 `rng=None` 模块级 random 路径一并迁入（兼容保证）。
- 数值语义对齐 Java：`Math.round = floor(x+0.5)`（非银行家舍入）；`nextInt(a,b) = [a,b)`。
- 对照测试（`tests/test_ptde_generator_primitive.py`）：
  - 12 类规则 × 6 类列型 seeded-rng **逐值对照** legacy；模块级 random 同种子调用序列对照；
  - fixed / range（含 overThreshold：`op=">="` 含阈值、`share` 统计 26%~34% 阈带、`share=0` 未越阈上界）/
    enum+weights（77%~83%）/ seq（prefix+start+row_index、起始值刷新不撞唯一键）语义逐一保留。

### T4.2 12 类分布采样器 — `ptde/generator/samplers/`

- 覆盖总方案 §7.1 全部 12 类分布（模块：`uniform / normal / lognormal / zipf（含 skewed 别名）/ temporal /
  weighted_categorical / sequential / random_discrete（type="random"）/ conditional / correlated /
  null_ratio / length`）+ `registry.py` 分发（`SUPPORTED_TYPES`、`sample`、`sample_params`）。
- 契约：只认 B3 产出形态 `DistributionFit(type+params)`，注入 `random.Random`；**不读生产明文样本**（脱敏边界延续 R2）。
- 参数键名与 B3 产出严格一致：
  - `weighted_categorical`：`values/weights`（`distribution_planner._stats_fit` 形态，主形态）；
    兼容 B2 collector 的 `{bucket: ratio}` 字典形态；
  - `conditional`：`depends_on`（`planner.py` 折叠形态）+ `branches`（采样期分支表）；
  - `temporal`：`buckets`（B2 collector 四桶占比）/ `start/end` 两种形态；
  - `zipf/skewed`：`exponent | s | fitted_parameter`（B2 collector 拟合形态）、`max_rank`、`values` 或 `min/max` 映射；
  - `length`：`min_length/max_length`（B3 字符列默认形态）；`uniform + min_length/max_length` 经分发器路由到 length 语义；
  - `null_ratio`：`null_ratio` + `base`（子分布 dict，递归分发）。
- **口径对齐（勿另起一套）**：uniform（整数域 `randint`/连续域 `uniform`）、normal（`max(0, gauss(mean, sigma))`
  非负语义 + 可选 min/max 钳制）、lognormal（`exp(gauss(mu, sigma))`）、weighted_categorical（累计分布 `pick <= acc`）
  与 `derivation.sample_one` 同 fit 同 rng **逐值一致**（对照单测锁死）；zipf 无值域时取 `params.mean`
  对齐 `distribution_expectation`；`sequential` step=1 时与 `primitive.seq_value` 逐值一致。
- 统计特性单测（`tests/test_ptde_generator_samplers.py`）：每类分布有均值/方差/分位数/频率容差断言
  （如 zipf rank 均值 ≈ H(N,s-1)/H(N,s) 闭式、correlated Pearson ≈ rho、temporal 桶频率 ≈ 占比）。
- A3 对齐：估计分布（`fitted=False`/`source=estimated`）照常可采样，但来源标注不变——不得伪装统计口径；
  缺参/未知分支/未知类型**显式失败**，不静默退化。

### T4.3 条件分布字段依赖图 — `ptde/generator/field_deps.py`

- 双形态消费 B3 条件对：
  ① `DistributionPlanEntry`（`distribution.type=="conditional"` + `params["depends_on"]`，planner.py 折叠形态）；
  ② `ConditionalPair`（`distribution_planner` 原始形态）；鸭子类型识别，非条件型不产生依赖边。
- `topological_order`：依赖字段先于依赖它的字段生成（P(paid_at|status) 型）；依赖字段可不在分布计划内
  （如 status 由规则生成）一并排序；同层按名称排序（确定性）。
- 环依赖（含自引用）→ `FieldDependencyError` 显式失败并输出环成员；**禁止静默剔边**
  （A1 分轨：legacy `break_weakest_cycle_edge` 保留不动仅对照）。
- 入口 `generation_orders(entries) -> {entity: [字段生成顺序]}`。

### T4.4 Seed 可复现机制 — `ptde/generator/seed.py`

- `derive_seed(root_seed, entity, shard_id, field)`：sha256(`namespace|root_seed|entity|shard_id|field`) 前 8 字节 →
  每字段独立 `random.Random`（`SEED_DERIVATION_ALGORITHM="sha256-v1"` 可追溯）。
- **不用内置 `hash()`**（受 PYTHONHASHSEED 影响不可跨进程复现）；跨进程一致性有子进程对照单测
  （不同 PYTHONHASHSEED 下派生 Seed 与取值序列均一致）。
- 哈希对照：同 root_seed 两次逐字段一致；不同 seed 不同（验收门禁 1 的字段级口径，R4.2/R4.3 才做端到端逐行）。
- `seed=None` 显式退化路径：`UserWarning` + 未播种实例 + `is_reproducible(None) is False`——不伪装可复现。

## 3. 文件清单

```text
新建（生产代码）
perf-py/ptde/generator/__init__.py
perf-py/ptde/generator/primitive.py            # T4.1 收编副本
perf-py/ptde/generator/field_deps.py           # T4.3
perf-py/ptde/generator/seed.py                 # T4.4
perf-py/ptde/generator/samplers/__init__.py    # T4.2 re-export
perf-py/ptde/generator/samplers/registry.py    # 分发注册表（12 类 + 别名）
perf-py/ptde/generator/samplers/{uniform,normal,lognormal,zipf,temporal,weighted_categorical,
    sequential,random_discrete,conditional,correlated,null_ratio,length}.py

新建（测试）
perf-py/tests/test_ptde_generator_primitive.py   # T4.1 对照（legacy 双跑）
perf-py/tests/test_ptde_generator_samplers.py    # T4.2 统计特性 + 口径对照
perf-py/tests/test_ptde_generator_field_deps.py  # T4.3 拓扑 + 环显式失败
perf-py/tests/test_ptde_generator_seed.py        # T4.4 哈希对照 + 跨进程

未改动：datagen/**（rule_engine.py 本体保留）、ptde/planner/**、ptde/models/**、sql/init.sql
```

## 4. 关键设计决策与踩坑记录

1. **conditional 分支的 None 哨兵**：分支查找用 `key in branches` 判定而非 `dict.get`，
   否则「分支值为 None 字面量」（如 PENDING → 空值）会被误判为「无匹配键」；无匹配且无 `*` 兜底仍显式失败。
2. **uniform 整数域判定**：`min/max` 均为整值时走 `randint`——这是与 `derivation.sample_one` 的对齐口径，
   连续分布测试须用非整值上界（如 `max=1.5`），勿"修正"采样器。
3. **null_ratio 消耗一次 random**：null 检查先于 base 采样消耗随机流；与 uniform 直接采样的流对照需同步消耗。
4. **length 均值模式**：截断正态在均值靠近边界时会产生钳制偏置；单测用例应让均值居中（如 [10,50] 均值 30）。
5. **样本量与容差**：统计断言普遍 5000~30000 样本；容差按标准误放大 3~5 倍设定，CI 上稳定不抖动。
6. 前序批次已知坑延续有效：鸭子类型假连接、pydantic `extra="forbid"`、指纹排除时间戳、`zip(strict=)`、
   `datetime.UTC`、ruff line-length=100（E501 ignore）、总方案 §6.3 分群表算术以 B3 测试 docstring 口径为准勿"修正"文档。

## 5. 验收对照（实施方案 §7.6 中属 R4.1 的条目）

| 条目 | 结果 | 落点 |
|---|---|---|
| 1. 同 DatasetSpec + Seed 两次构建逻辑数据一致 | ✅ 字段级哈希对照（端到端逐行留 R4.2/R4.3） | `test_ptde_generator_seed.py` |
| 2. 12 类分布各有统计特性单测 | ✅ 均值/方差/分位数容差 | `test_ptde_generator_samplers.py` |
| 3. 条件分布依赖图：拓扑正确、环显式失败不剔边 | ✅ 消费 B3 条件对 | `test_ptde_generator_field_deps.py` |
| 4. 收编对照：rule_engine 语义迁入全绿 | ✅ 同输入同输出 + 边界语义 | `test_ptde_generator_primitive.py` |
| 5. seed=None 退化路径显式 | ✅ UserWarning + is_reproducible=False | `test_ptde_generator_seed.py` |

门禁 6（legacy 对照）：legacy 管线全程未动，双跑对照由 T4.1 对照单测承载；R4.2 起做同 DatasetSpec 双跑行数/关联/分布对照。

## 6. 回归保障与回滚（§7.7）

- R4.1 不触碰 legacy 生成路径；回滚仅删除 `ptde/generator/` 与 4 个 `tests/test_ptde_generator_*` 文件即可，
  legacy 与其余批次完全不受影响。
- 采样器不读生产明文样本，仅消费 `DistributionFit` 统计参数——脱敏边界延续 R2。

## 7. 后续批次衔接（R4.2，供下一 session 使用）

- **T4.5 keys.py**：演进 `ptde_common/id_pool.py`（去 `MAX_POOL_SIZE=500_000` 与整数限制、预分配 ID 区间接口）；
  本批次 `primitive.scale_to_column_type` 已为其提供数值定标口径。
- **T4.6 relations.py**：外键关联按分布采样父子绑定；消费 `EntityPlan.parent_binding` 复核锚点与
  `derivation.distribution_expectation/sample_one` 口径（采样器层已对齐，直接复用 `samplers.sample`）。
- **T4.7 deps.py + cycle_policy.py**：Tarjan SCC + 安全策略；实体级环判定应复用 `field_deps` 的显式失败语义
  （本批次为字段级依赖图，`FieldDependencyError` 文案已注明 legacy `break_weakest_cycle_edge` 仅对照）。
- 端到端可复现验收（门禁 1 逐行、门禁 3 内存、门禁 4 Pilot Run）随 R4.2/R4.3 推进。
