# PTDE B7 续批次 8 交接文档 — 关系映射接线 + 门禁 2 关系对照补齐（单表版）

> 状态：**已完成并通过门禁**（2026-11-19）
> 依据：续批次 7 交接 §7「T7.6/T7.7 接管切换的剩余前置」第 1/2 项；
> 实施方案 §10.2（A2 真库批次）/ §13.1（门禁 2）/ §9.2（T6.2 结构关系验证）；
> 代码位置：`server/offline_pipeline.py`、`server/realdb_pipeline.py`、
> `tests/test_server_realdb_pipeline.py`、`tests/test_ptde_legacy_ptde_crosscheck.py`、
> `perf-py/benchmarks/gate2_crosscheck_report.json`（更新归档）；
> 前序交接：B7 前段 + 续批次 1~7 全部有效。

---

## 1. 交接基线与结果

- 入口基线：续批次 7 交付后口径（perf-py 1723 passed, 5 skipped；
  perf-web vitest 44 passed, build 通过）；
- **verify 关系层从显式空集到实映射**（续批次 7 §7 剩余前置第 1 项落地）：
  `OfflinePipeline.verify` 中 `validate_relationships(conn, [])` 空集占位
  改为消费注入的关系规格（`validate_relationships(conn, self._relations)`）；
- 交付后门禁：
  - perf-py 全库 `uv run --extra server --extra locust pytest tests/` →
    **1728 passed, 5 skipped, 0 failed**（基线 1723 + 本批 5 通过；
    5 skipped = 既有 2 + 3 个真库门控测试在未设 `PTDE_REALDB_URL` 时
    跳过——设置后全绿，见 §5）；
  - `uv run ruff check ptde/ perf_cli/ server/ tests/ datagen/` →
    All checks passed；
  - perf-web `npm run build`（vue-tsc + vite）→ 通过（零改动基线确认）；
  - perf-web `npx vitest run` → 5 文件 44 passed（零改动基线确认）；
- **门禁 2 关系对照补齐**（续批次 7 §7 剩余前置第 2 项落地，真库实测）：
  `relationship_check` 从 `"N/A（关系映射随 Schema 批次接入）"` 占位改为
  实测对照结果——两侧外键有效率均 **1.0**（孤儿均为 0，外键域均
  [1, 1000]），PTDE verify RELATIONSHIP 层 **PASS**（relations_checked=1,
  orphan_free_relations=1）；报告已重新归档。

## 2. 交付内容

### 2.1 关系映射接线（`server/offline_pipeline.py`）

- **`normalize_relations`（新模块级函数，`__all__` 导出）**：关系规格归一化
  （JSON 原生四元组 dict → `RelationModel.model_validate`）+ 显式校验——
  四元组完整性、标识符合法性、子/父表与列均在实体映射内逐项核对
  （未映射实体/列缺失/清单不一致显式失败，不静默降级为空集）；子列不得为
  `id`（主键区间语义列，不承载外键）；
- **`OfflinePipeline` 新注入缝 `relations`**（缺省 None = 空集，向后兼容，
  既有全部测试零改动通过）：构造期经 `normalize_relations` 归一化校验
  （fail-fast，不等到 verify 才炸）；
- **生成侧关系语义落地 `_apply_fk_fields`**：子表外键列**无显式分布**时按
  父实体 planned_range 预分配区间合并（[id_start, id_end]）注入 uniform
  整数域采样——零孤儿语义在生成侧成立，verify 全量 LEFT JOIN 核对可
  PASS；父实体无预分配区间（非 planned_range）显式失败；**子列已有显式
  分布不静默覆盖**（调用方口径优先，如倾斜/配额语义；孤儿越界由 verify
  关系层如实暴露）；
- verify 段：`validate_relationships(conn, list(self._relations))` 实映射
  （DB_FK/ORM 孤儿违规 → FAIL；LOGICAL → WARN；既有 validator 零重造）。

### 2.2 真库通道透传（`server/realdb_pipeline.py`）

- `build_realdb_pipeline` 新参 `relations`（JSON 原生四元组序列）透传
  `OfflinePipeline`；归一化与映射一致性校验收口在 `normalize_relations`
  （单一出处，不双份实现）。

### 2.3 门禁 2 关系对照补齐（`tests/test_ptde_legacy_ptde_crosscheck.py`）

- 双跑规格升级为**双表**：父表 `ptde_xcheck_users`（默认 1000 行，env
  `PTDE_XCHECK_PARENT_ROWS`）+ 子表 `ptde_xcheck_orders`（默认 1 万行）；
  - legacy 侧：`FieldRule.TYPE_REF`（table+column 显式四元组 → explicit
    边自动生效，父 ID 池采样；topo 依赖序父表先行）；orphan_rate 缺省 0
    （与 PTDE 零孤儿口径对齐）；
  - PTDE 侧：`relations` 四元组（db_fk）注入真库通道；`user_id` 无显式
    分布 → 生成侧自动推导父实体主键区间 uniform 采样；
- 外键有效率对照（全量非抽样口径：valid = (非空外键 − 孤儿) / 非空外键）：
  **两侧均 1.0、孤儿均 0、外键域均 [1, 1000]**；
- PTDE verify RELATIONSHIP 层实映射断言（PASS + metrics 在册）入报告；
- 报告 schema 更新：新增 `parent_table` / `parent_rows` / legacy+ptde
  `fk` 统计 / `verdicts.relationship_check` 对象化（原 N/A 字符串占位移除）。

### 2.4 协议级测试（不连真库，FakeConn，+5 例）

- `tests/test_server_realdb_pipeline.py`：
  - 关系接线端到端：生成侧外键列区间注入断言（uniform [1, 40]）+
    verify RELATIONSHIP 层 PASS + metrics/检查名留痕；
  - 孤儿违规 → RELATIONSHIP 层 FAIL（零孤儿要求，不降级放行；
    FakeConn 预置孤儿数 3）；
  - 缺省 relations=None → 关系层空集退回既有口径（零改动兼容确认）；
  - 关系规格显式失败四态（未映射实体/列缺失/子列为 id/四元组不完整）；
  - 子列已有显式分布不被静默覆盖（调用方口径优先）。
  - FakeCursor 协议增强：零孤儿核对 SQL（`SELECT COUNT(*) FROM (SELECT ...`
    子查询形态）按子表返回预置孤儿数（`fk_orphans`，缺省 0）。

## 3. 文件清单

```text
修改（生产代码）
perf-py/server/offline_pipeline.py         # normalize_relations + relations 注入缝
                                           #  + 生成侧 _apply_fk_fields + verify 实映射
perf-py/server/realdb_pipeline.py          # build_realdb_pipeline 透传 relations

修改（测试，perf-py +5）
perf-py/tests/test_server_realdb_pipeline.py       # 12→17（关系接线协议测试 5 例）
perf-py/tests/test_ptde_legacy_ptde_crosscheck.py  # 门禁 2 升级双表 + 关系对照

归档（更新）
perf-py/benchmarks/gate2_crosscheck_report.json    # relationship_check 实测化

未改动：ptde/** 全部、datagen/**、/api/datagen 契约与 golden、perf-web/**、
sql/init.sql、CLI（datagen_runner/ptde_runner）、test_ptde_realdb_benchmark.py
```

## 4. 关键设计决策与踩坑记录

1. **关系注入缝落在 server 层而非 DatasetSpec**：`DatasetSpec`（ptde/**）
   当前无 relations 字段，续批次口径 `ptde/**` 零修改——关系规格经
   `OfflinePipeline(relations=...)` 注入缝由装配方/调用方提供（JSON 原生
   四元组），DatasetSpec/Definition 级关系语义细化随 A2 Schema 批次扩展
   （届时映射产出器从 Schema 快照自动产出 relations）。
2. **外键值生成 = 父实体主键区间 uniform 采样（最小语义）**：planned_range
   主键自 [id_start, id_end] 区间顺序生成，子表外键 uniform 采样同域即
   零孤儿成立——不需要生成侧持有连接或跨实体协调（worker 分片间无共享
   状态口径保持）；refSkew/倾斜/配额等高级语义走子列显式分布覆盖缝。
3. **显式分布不静默覆盖**：子列已有分布说明调用方自有口径（如倾斜），
   管线不做魔法替换；若该分布产生孤儿，verify 关系层 FAIL 如实暴露
   （不伪装通过）——与「探测-降级留痕」「快照如实记录」同一家族口径。
4. **校验 fail-fast 在构造期**：`normalize_relations` 在
   `OfflinePipeline.__init__` 执行（未映射实体/列缺失/子列为 id 即刻显式
   失败），不等到 generate/verify 才炸——装配错误与运行错误分离。
5. **踩坑：门禁 2 复跑残留数据**——legacy 侧原用
   `CREATE TABLE IF NOT EXISTS`，真库留存上批数据触发主键重复
   （Duplicate entry '1'）；补 `DROP TABLE IF EXISTS`（与 PTDE 侧既有
   复跑清理口径对齐）。
6. **踩坑（测试侧）**：`OfflinePipeline` 构造第一参映射需
   `EntityMapping`（或其 kwargs dict）——字段规格 dict 须经映射产出器
   `entity_mappings_from_fields` 转换后再注入，不能直接传
   `{entity: [FieldSpec...]}`。

## 5. 验收对照

| 验收项 | 结果 | 落点 |
|---|---|---|
| verify 关系层实映射（DB_FK 零孤儿全量核对） | ✅ | `test_realdb_channel_relations_wire_fk_and_verify_layer`（PASS + metrics 在册） |
| 孤儿违规 FAIL（不降级放行） | ✅ | `test_realdb_channel_relations_orphan_fail_marks_layer` |
| 生成侧外键区间自动推导 + 显式分布保留 | ✅ | 同上端到端例 + `test_relations_explicit_distribution_not_overridden` |
| 缺省行为不变（relations=None 空集） | ✅ | `test_relations_without_wiring_keeps_empty_layer` + 既有 offline 全部测试零改动通过 |
| 关系规格显式失败 | ✅ | `test_relations_explicit_failure_cases`（四态） |
| 门禁 2 关系对照（ref 外键有效率） | ✅ | `test_gate2_crosscheck_legacy_vs_ptde` 真库实测：两侧 valid_rate=1.0 / orphans=0 / 域 [1,1000]；报告归档更新 |
| PTDE verify RELATIONSHIP 层真库 PASS | ✅ | 同上（`relationship_layer: PASS, relations_checked=1` 入报告） |
| 门禁 | ✅ | perf-py 1728 passed, 5 skipped；ruff 全绿；perf-web build + vitest 44 passed |

## 6. 回归保障与回滚（§10.4）

- **契约零改动**：`/api/datagen` 13 端点 + golden、stress 绑定、CLI 面
  全部未触碰；`ptde/**` 零修改（RelationModel/validate_relationships 仅
  消费）；`OfflinePipeline` 扩展缺省路径行为不变（既有 offline/dataset_api
  测试零改动通过；真库门控冒烟/基准测试零改动通过）；
- 回滚：还原 offline_pipeline 三段（normalize_relations/relations 缝/
  _apply_fk_fields/verify 实映射）+ realdb_pipeline 透传参 + 两个测试
  文件 + 报告归档即可；真库侧仅目标表数据（DROP 可重建），无破坏性变更。

## 7. 后续批次衔接（供下一 session 使用）

- **本批已闭环**：T7.6/T7.7 剩余前置第 1 项（verify 关系层实映射——
  生成侧区间推导 + verify 全量核对）+ 第 2 项（门禁 2 关系对照补齐）；
- **剩余前置**（第 3 项）：**PTDE 规则语义对齐 legacy FieldRule**——seq
  前缀串/orphan_rate 故意造孤儿/refSkew 热行/over_threshold 等接管语义
  （A1 legacy 钳制随接管归一）；注意 orphan_rate 接管后 PTDE 侧需支持
  「故意孤儿」语义（当前零孤儿硬口径 + 外键区间推导，故意孤儿走子列
  显式分布覆盖缝或新分布类型，落点待该批次决策）；
- **完成后按 §13.2 步骤 3/4 切换**（`perf_cli/datagen_runner.py` 路由 +
  `server/datagen_api.py` 实现替换，端点/形状不变，契约全绿）；
- **A2 映射产出器扩展方向**（Schema 批次）：完整 Schema 快照 → 映射 +
  relations 自动产出（db_fk 声明外键自动生效，对齐 legacy 四级合并
  口径）；索引/分区/环组同批接入；
- **selfCheck probe 注入缝已就位**：`build_realdb_pipeline(endpoint_probe=)`
  ← 装配方传 `datagen_service.self_check_probe(repo_id)`（部署入口接线时
  闭合，OfflinePipeline verify 已透传）；
- **API 超时控制**：随切换批次编排层一并落（口径不变）；
- **T7.9 legacy 整包退役**：切换 + 观察期后（迁移脚本续批次 4 已就位）；
- 真库环境复现口径：docker `mysql:8.0`（`--local-infile=1`）+
  `sql/init.sql` + `GRANT ALL ON performance_testor.* TO 'perf'@'%'` +
  `PTDE_REALDB_URL=mysql://perf:perf_pw@127.0.0.1:3306/performance_testor`。

---

*B7 续批次 8 交接完成：关系映射接线（verify 关系层实映射 + 生成侧外键
区间推导 + 关系规格显式校验）、门禁 2 关系对照补齐（双表双跑，两侧外键
有效率 1.0 / 孤儿 0，PTDE RELATIONSHIP 层真库 PASS）；T7.6/T7.7 剩余前置
收敛为规则语义对齐（seq 前缀/orphan_rate/refSkew/over_threshold），随后切换。*
