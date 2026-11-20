# PTDE B7 续批次 9 交接文档 — 规则语义对齐 legacy FieldRule（T7.6/T7.7 最后硬前置）

> 状态：**已完成并通过门禁**（2026-11-19）
> 依据：续批次 8 交接 §7「剩余前置」第 3 项（PTDE 规则语义对齐 legacy
> FieldRule：seq 前缀串 / orphan_rate 故意造孤儿 / refSkew 热行 /
> over_threshold）；
> 代码位置：`ptde/generator/samplers/`、`server/offline_pipeline.py`、
> `server/rule_translator.py`（新）、
> `tests/test_ptde_generator_samplers.py`、`tests/test_server_rule_translator.py`（新）、
> `tests/test_server_realdb_pipeline.py`、`tests/test_ptde_legacy_ptde_crosscheck.py`；
> 前序交接：B7 前段 + 续批次 1~8 全部有效。

---

## 1. 交接基线与结果

- 入口基线：续批次 8 交付后口径（perf-py 1728 passed, 5 skipped；
  ruff 全绿）；
- 交付后门禁：
  - perf-py 全库 `uv run --extra server --extra locust pytest tests/` →
    **1771 passed, 5 skipped, 0 failed**（基线 1728 + 本批 43：采样器 13 +
    翻译器 26 + 管线协议 4；5 skipped = 既有 2 + 3 个真库门控测试在未设
    `PTDE_REALDB_URL` 时跳过——crosscheck 改造随真库复跑核对）；
  - `uv run ruff check ptde/ perf_cli/ server/ tests/ datagen/` →
    All checks passed。

## 2. 交付内容

### 2.1 ref 采样器（`ptde/generator/samplers/ref.py`，新）

- **父主键区间采样**（整数域，min/max 缺失或非整值显式失败），依赖序对齐
  legacy（行级 orphan 掷骰 → 热区两段线性权重 → 均匀）：
  - 无 hot/orphan 参数 → `[min, max]` 均匀 randint（对齐管线
    `_apply_fk_fields` 注入的 uniform 语义）；
  - `hot_percent/hot_share` → **两段线性热行权重**：前
    `max(1, floor(n×hot_percent/100 + 0.5))` 个父 ID（区间头部）合计承载
    `hot_share%`，其余均分 `(100−hot_share)%`；累计权重 + `bisect_left`
    二分——逐项对齐 `ptde_common/id_pool.py`
    `build_skew_weights/sample_skewed`；`hot_percent<=0 或 >=100` 退化为
    均匀（legacy 同口径）；**非 zipf**——zipf 语义与 legacy 两段线性
    不等价，未采用；
  - `orphan_rate` → 故意孤儿：无效 ID = `max + 1 + randrange(invalid_span)`
    （缺省 span=1000，对齐 `IdPool.sample_invalid_id` 池最大值 + 1 +
    randrange(1000)，必然不命中真实行）；
  - `orphan_null_ratio` → orphan 行按概率置 None（legacy 可空列 50% 置
    NULL 同口径；缺省 0 = 只造无效 ID）；
- **`HOT_ROW_SHARE_WARN = 50.0`**（legacy `default_engine` 同名常量同口径）：
  `hot_row_share_warning_text`（单热行承载估算
  `100×hot_share/max(1,hot_percent)` > 50 → 文案）+ `warn_hot_row_share`
  （warnings 通道，同文案去重不阻断）；
- 注册进 `registry.py`：`SUPPORTED_TYPES` 扩为 §7.1 全部 12 类 +
  `ref`/`over_threshold` 两个 legacy 对齐扩展（错误提示文案同步）。

### 2.2 over_threshold 采样器（`ptde/generator/samplers/over_threshold.py`，新）

- **查证 legacy 语义**（`datagen/rule_engine.py` TYPE_RANGE + OverThreshold）：
  **share% 行取超阈值区间、其余取 `[min, threshold]`——超阈值比例分配，
  非取值截断**；
- 逐项对齐：`(value, max]`（op=">"，`math.nextafter` 开区间）/
  `[value, max]`（op=">="）；op 缺省 ">"、value 缺省 0、share 缺省 30、
  min/max 缺省 0/100；`max < min` 交换；阈值 ≤ min 时未越阈分支回退
  `[min, max]`；
- 整数/连续域按「参数值是否整值」路由（与 uniform 采样器口径一致）：
  整数域 Java `Math.round` 取整（`floor(x+0.5)`，legacy
  `scale_to_column_type` INT/BIGINT 对齐），连续域保留浮点（DECIMAL
  2 位定标由列类型归一化层承接，采样层不做截断）。

### 2.3 管线缝扩展（`server/offline_pipeline.py`）

- **`_apply_fk_fields` ref 父区间补全缝**：子列显式 `ref` 分布缺父主键
  区间（min/max **同缺**）→ 注入父实体 planned_range 合并区间**补全而非
  覆盖**（热行/孤儿参数保留）——legacy refSkew/orphan_rate 语义走此缝；
  min/max 单边缺 → 显式失败；补全后触发热行告警
  （`warn_hot_row_share`）；
- **构造期故意孤儿门禁 `_validate_orphan_semantics`**：子列显式 `ref`
  分布携带 `orphan_rate>0` 且关系为声明类（db_fk/orm，非 `_INFERRED_
  RELATION_KINDS = {"logical"}`，与 `relationship_validator._INFERRED_
  KINDS` 同口径）→ `OfflinePipelineError` 显式失败（零孤儿硬核对必 FAIL，
  不伪装通过；提示故意孤儿须配 logical——verify 降级 WARN 如实暴露）；
  `OfflinePipeline.__init__` 内 `normalize_relations` 之后执行
  （装配错误与运行错误分离，续批次 8 同家族口径）。

### 2.4 legacy 规格 → PTDE 规格翻译路径（`server/rule_translator.py`，新）

- **`field_rule_to_distribution(rule, *, orphan_rate=0, nullable=False)`**：
  逐规则映射（duck-typing 收 legacy `FieldRule`/`FieldRuleV2` 属性访问或
  dict——snake_case / camelCase JSON 均收，对齐 `perf_cli/datagen_runner.py`
  `rule_from_json` 键名）：
  - `seq` → `sequential`（`seq_prefix` 缺省 ""、`seq_start` 缺省 1，对齐
    `rule_engine.seq_value`；PTDE 采样器既有参数语义逐值一致）；
  - `range` → `uniform`（min/max 缺省 0/100）；带 `over_threshold` →
    `over_threshold` 分布（op 空串回退 ">"，对齐 legacy runner）；
  - `enum` → `weighted_categorical`（权重等长且 sum>0 时）/ `random`
    （无有效权重均匀取一，对齐 rule_engine 加权分支回退）；values 空 →
    None（生成侧 `generate_default`，对齐 `apply_rule` 回落）；
  - `fixed` → `weighted_categorical` 单值常量；
  - `ref`/`refSkew` → ref 分布（仅 refSkew/orphan 语义时）：refSkew 热行
    参数缺省 **20/80**（对齐 legacy 引擎侧缺省，非 FieldRule 层缺省）；
    orphan_rate>0 → 携带 `orphan_rate`，可空列加 `orphan_null_ratio=0.5`；
    纯 ref → None（零孤儿 uniform 由管线注入）；
  - 未知类型/缺省 → None（生成侧缺省生成，legacy 回落同口径）；
- **`field_rule_to_relation(rule, child_table, child_column, *,
  orphan_rate=0)`**：ref/refSkew 且 table+column 显式 → 关系四元组
  （名称推断边随 plan_pipeline 编排，不在翻译层）；`orphan_rate>0` →
  `relation_kind="logical"`（故意孤儿降级 WARN 口径），否则 `"db_fk"`；
- **`translate_fields(fields, *, table, orphan_rate, nullable_columns)`**：
  整表聚合产出 `TranslatedFields(distributions/relations/warnings)`；
  告警：HOT_ROW_SHARE_WARN 文案（列前缀）+ 故意孤儿降级 logical 留痕；
  `nullable_columns` 供 orphan NULL 拆分（可空性信息调用方注入——
  FieldSpec 无 nullable 概念）。

### 2.5 门禁 2 双跑改走翻译路径（`tests/test_ptde_legacy_ptde_crosscheck.py`）

- `_ptde_run` 的 PTDE 侧字段规格不再手写，改为**从同一批 legacy
  FieldRule（seq/ref/range/enum）经 `translate_fields` 产出**（翻译路径
  真库端到端核对；id 列 distribution 由 worker planned_range 主键语义
  接管，行为不变；relations 用翻译产出的 db_fk 四元组）；
- 真库门控（`PTDE_REALDB_URL` 未设 → skip），语义断言不变（行数精确一致 /
  分布容差 / 主键区间 / 外键有效率 1.0）。

### 2.6 协议级测试（+43 例）

- `tests/test_ptde_generator_samplers.py`（+13）：
  - ref：缺省均匀统计特性 / 两段热行占比（40 热 ID 承载 80% + 单热行
    2%）/ 越界退化均匀 / 无效 ID 域与占比 / orphan_null_ratio NULL 拆分 /
    缺参-非整值显式失败 / HOT_ROW_SHARE_WARN 文案阈值；
  - over_threshold：share 占比分配 / op=">=" 含阈值（连续域）/ op=">"
    开区间（连续域）+ 整数域取整伪差（legacy INT 列同款，80.000…1 → 80）/
    阈值≤min 回退混合均值 / 整数域 int 输出 / 缺参-非法 op 显式失败；
  - `test_supported_types_cover_section_7_1` 更新（+ref/over_threshold）；
- `tests/test_server_rule_translator.py`（新，26）：六类规则映射（含
  缺省值逐项对齐）/ ref+orphan（含 nullable 拆分 / refSkew 组合）/
  关系四元组（db_fk / 故意孤儿降级 logical / 非显式四元组不产出）/
  camelCase dict 输入 / translate_fields 聚合（含两类告警）；
- `tests/test_server_realdb_pipeline.py`（+4）：
  - ref 分布父区间补全端到端（热行参数保留 + 告警触发）；
  - ref 区间单边缺 → 显式失败；
  - db_fk × 故意孤儿 → 构造期显式失败；
  - logical × 故意孤儿 → 构造通过 + 生成侧参数补全确认 + verify
    孤儿如实 WARN（FakeConn 预置 3 孤儿）。

## 3. 文件清单

```text
新增（生产代码）
perf-py/ptde/generator/samplers/ref.py             # ref 采样器（热行两段线性权重
                                                   #  + 故意孤儿 + HOT_ROW_SHARE_WARN）
perf-py/ptde/generator/samplers/over_threshold.py  # 超阈值比例分配采样器
perf-py/server/rule_translator.py                  # legacy FieldRule → PTDE
                                                   #  字段规格/关系四元组翻译路径

修改（生产代码）
perf-py/ptde/generator/samplers/registry.py        # 注册 ref/over_threshold（12+2）
perf-py/ptde/generator/samplers/__init__.py        # docstring 扩展说明
perf-py/server/offline_pipeline.py                 # ref 父区间补全缝
                                                   #  + 构造期故意孤儿门禁

修改（测试，perf-py +43）
perf-py/tests/test_ptde_generator_samplers.py      # +13（ref/over_threshold 语义）
perf-py/tests/test_server_rule_translator.py       # 新（翻译路径 26 例）
perf-py/tests/test_server_realdb_pipeline.py       # +4（补全缝 + 故意孤儿门禁）
perf-py/tests/test_ptde_legacy_ptde_crosscheck.py  # 双跑改走翻译路径（真库门控）

未改动：datagen/**（legacy 本体保留不动，A1 分轨）、perf_cli/**、
perf-web/**、/api/datagen 契约与 golden、CLI（datagen_runner/ptde_runner）
```

## 4. 关键设计决策与踩坑记录

1. **orphan_rate 落点 = 新分布类型 `ref`（非 null_ratio 组合）**：无效 ID
   语义（池 max+1+randrange(1000)）与 NULL 拆分（可空列 50%）无法用
   null_ratio 组合精确表达（null_ratio 两段独立掷骰的 P(invalid) 与
   legacy 不等）——`ref` 采样器内聚 orphan 掷骰 + `orphan_null_ratio`
   参数，边际分布与 legacy 逐项一致；独立 NULL 语义仍可叠加 null_ratio
   组合（ref 作 base）。
2. **故意孤儿 × verify 口径**：零孤儿硬核对不放松——db_fk/orm 声明关系
   × orphan_rate>0 在**构造期显式失败**（fail-fast，不等到 verify 才
   FAIL）；故意孤儿须配 logical 关系，verify 孤儿如实 WARN（"探测-降级
   留痕""不伪装通过"同家族口径）。
3. **refSkew 对齐实现为两段线性权重（非 zipf）**：legacy 热行语义是
   "前 round(n×hot%) 个父 ID 承载 hotShare%，冷区均分"（累计权重 +
   二分），zipf 的 rank-幂律不等价；语义对齐以 legacy 逐项一致为准。
4. **over_threshold 非截断**：查证 legacy 语义为 share% 超阈值区间分配
   （`(value,max]` / `[min,threshold]`），非取值阈值截断——采样器按
   原语义实现；整数域保留 Java Math.round 伪差（80.000…1 → 80，legacy
   INT 列同款），不"修复"以防口径漂移。
5. **已知语义差异显式记录**：legacy orphan 掷骰为行级一次（同行多个
   ref 列共享同一 orphan 结果），PTDE 逐列独立掷骰——边际分布一致，
   行内相关性不同；双跑对照按统计容差口径核对（模块 docstring 留痕）。
6. **可空性信息由调用方注入**：FieldSpec 无 nullable 概念，翻译层经
   `nullable_columns` 参数承接 legacy ColumnMeta.nullable（orphan NULL
   拆分需要）；切换批次接线时从 Schema 快照传入。
7. **踩坑（测试侧）**：over_threshold 整数/连续域按「参数值是否整值」
   路由（与 uniform 口径一致），`min=0.0/max=100.0` 属整数域——连续域
   语义测试须用非整值参数（如 0.5/100.5）；先两次断言失败均源于此。
8. **踩坑**：新增 import 按仓库 isort 分组口径归位（ptde 块内按字母序），
   ruff I001 一处自动修复（测试文件 import 后空行）。

## 5. 验收对照

| 验收项 | 结果 | 落点 |
|---|---|---|
| seq 前缀串翻译路径（legacy 规格 → PTDE 规格） | ✅ | `test_seq_rule_maps_to_sequential_with_prefix_and_start` 等（缺省 ""/1 对齐 seq_value） |
| ref 采样器热行两段线性权重（refSkew 语义） | ✅ | `test_ref_hot_two_segment_share`（40 热 ID 承载 80%）+ 越界退化 |
| ref 采样器故意孤儿（无效 ID + NULL 拆分） | ✅ | `test_ref_orphan_invalid_ids` + `test_ref_orphan_null_share` |
| HOT_ROW_SHARE_WARN（单热行 >50% 告警，同口径） | ✅ | `test_hot_row_share_warning_text_threshold` + 管线端到端告警 |
| over_threshold 语义对齐（比例分配非截断） | ✅ | `test_over_threshold_share_allocation` 等六例 |
| 翻译路径六类规则映射 + camelCase 输入 | ✅ | `tests/test_server_rule_translator.py`（26 例） |
| ref 父区间补全缝（不覆盖热行/孤儿参数） | ✅ | `test_ref_fk_distribution_gets_parent_domain_injected` |
| db_fk × 故意孤儿构造期显式失败 | ✅ | `test_declared_fk_with_deliberate_orphans_fails_at_construction` |
| logical × 故意孤儿 verify WARN 如实暴露 | ✅ | `test_logical_relation_allows_deliberate_orphans_and_verify_warns` |
| 门禁 2 双跑走翻译路径 | ✅ | `_ptde_run` 经 `translate_fields` 产出（真库门控，设 `PTDE_REALDB_URL` 复跑核对） |
| 门禁 | ✅ | perf-py 1771 passed, 5 skipped；ruff 全绿 |

## 6. 回归保障与回滚（§10.4）

- **契约零改动**：`/api/datagen` 13 端点 + golden、stress 绑定、CLI 面
  全部未触碰；`datagen/**` 零修改（legacy 本体 A1 分轨保留）；采样器
  扩展为**纯增量**（12 类既有语义零改动，`test_supported_types_cover_
  section_7_1` 更新为 12+2）；`OfflinePipeline` 扩展缺省路径行为不变
  （无 ref 分布 / 无 orphan 参数时既有 offline/dataset_api 测试零改动
  通过）；
- 回滚：删除 `ref.py`/`over_threshold.py`/`rule_translator.py` 三文件 +
  还原 registry 注册 + offline_pipeline 两段（补全缝/门禁）+ 四个测试
  文件即可；无数据/契约破坏性变更。

## 7. 后续批次衔接（供下一 session 使用）

- **本批已闭环**：T7.6/T7.7 接管切换三个硬前置全部完成（关系映射接线 +
  门禁 2 关系对照【续批次 8】+ 规则语义对齐【本批】）；
- **下一批 = 切换**：按实施方案 §13.2 步骤 3/4——`perf_cli/datagen_
  runner.py` 路由 + `server/datagen_api.py` 实现替换（端点/形状不变，
  契约全绿）；接线要点：
  - 规则 JSON → legacy FieldRule 解析（`rule_from_json`）后经
    `server.rule_translator.translate_fields` 翻译为 PTDE 字段规格 +
    relations 四元组（本批已打通，含 camelCase 输入）；
  - 可空列清单从 Schema 快照传入 `nullable_columns`（orphan NULL 拆分）；
    表级 orphan_rate 按 legacy 缺省链解析（表级 → 全局级 → 0）后传入；
  - `seqStart` 执行时刷新语义（legacy `refresh_seq_starts` 探测 DB
    MAX+1 回写）随切换批次编排层决策落点（PTDE 侧 sequential 起始值
    参数已就位）；
  - 已知差异（orphan 行级 vs 列级掷骰）按统计容差口径验收；
- **门禁 2 复跑**：设 `PTDE_REALDB_URL` 复跑 crosscheck（本批改走翻译
  路径，语义断言不变；真库环境口径见续批次 8 §7）；
- **A2 映射产出器扩展方向**（Schema 批次）：完整 Schema 快照 → 映射 +
  relations 自动产出；`translate_fields` 的 nullable/orphan_rate 解析
  随 Schema 快照内聚；
- **API 超时控制**：随切换批次编排层一并落（口径不变）；
- **T7.9 legacy 整包退役**：切换 + 观察期后（迁移脚本续批次 4 已就位）；
  届时 `ptde/models/compat.py`（V2 镜像）一并清理；
- 真库环境复现口径：docker `mysql:8.0`（`--local-infile=1`）+
  `sql/init.sql` + `GRANT ALL ON performance_testor.* TO 'perf'@'%'` +
  `PTDE_REALDB_URL=mysql://perf:perf_pw@127.0.0.1:3306/performance_testor`。

---

*B7 续批次 9 交接完成：规则语义对齐 legacy FieldRule（seq 前缀串翻译
路径 + ref 采样器〔refSkew 热行两段线性权重 / orphan_rate 故意孤儿 +
HOT_ROW_SHARE_WARN〕+ over_threshold 比例分配采样器 + legacy 规格 →
PTDE 规格翻译路径 + 构造期故意孤儿门禁）；T7.6/T7.7 三个硬前置全部
闭环，下一批按 §13.2 步骤 3/4 切换。*
