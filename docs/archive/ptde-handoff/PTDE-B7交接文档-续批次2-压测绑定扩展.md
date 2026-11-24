# PTDE B7 续批次 2 交接文档 — 压测绑定扩展（scenario 透传 + perf-web 绑定表单）

> 状态：**已完成并通过门禁**（2026-11-19）
> 依据：`PTDE-B7交接文档-R7平台集成前段.md` §7「B7 续可立即推进项」第 3 项
> （压测绑定扩展）；批次 1 交付口径（offline 执行体/manifest 落库，未入本文档
> 的既有批次，全绿可信）；代码位置：`perf-py/tests/test_stress_dataset_binding.py`
> （scenario 透传补测）、`perf-web/src/components/stress/StressForm.vue` +
> `perf-web/src/api/index.ts`（绑定表单）；前序交接：B7 前段 + B7 续批次 1
> 全部有效。

---

## 1. 交接基线与结果

- 入口基线：B7 续批次 1 交接口径 **1671 passed, 2 skipped**（skip 为 locust
  导入闸门既有口径）；
- 交付后门禁：
  - 全库 `uv run --extra server --extra locust pytest tests/` →
    **1674 passed, 2 skipped, 0 failed**（新增 3 例计入口径；skip 既有口径）；
  - `uv run ruff check ptde/ perf_cli/ server/ tests/` → **All checks passed**；
  - 前端 `perf-web`：`npm run build`（vue-tsc 类型检查 + vite build）→
    **构建通过**；
- 本批为 **后端零生产代码改动（纯补测）+ 前端最小新增**；**未触碰**
  legacy `datagen/**`、`/api/datagen` 契约、stress wire 契约（dataset_* 不进
  `to_camel`，既有 golden/契约测试全绿确认）。

## 2. 交付内容（按任务）

### 2.1 scenario 模式 dataset 透传对齐（核对结论：代码已一致，缺口在测试）

逐项核对结果（`stress_request.py` / `stress_service.py` / `stress_api.py`）：

- **绑定解析与模式无关**：`StressTaskService._resolve_dataset_binding(payload)`
  直读原始请求 dict 的 `payload["dataset"]["instanceId"]`，不经过
  `parse_stress_request`（后者按 Jackson 语义忽略未知字段，`dataset` 键
  原样保留在 raw payload 中）——单接口与场景模式走同一条绑定路径；
- **`create_and_run` 两模式共用**：`parse_stress_request` + `_validate` +
  `_resolve_dataset_binding(payload)` + `insert_task(dataset_*)` 全链路对
  `is_scenario_mode` 无分支（场景模式仅影响 `target_url` 兜底取场景摘要）；
- **gate 路径同样覆盖**：`run_gate → create_and_run(payload)`，CI 门禁携带
  dataset 绑定同样生效；
- **READY 门禁不放宽**：非 READY/不存在/查询缝未注入的显式拒绝文案与
  模式无关（§9.5 门禁 5 语义一致）；
- **缺口补齐 = 测试**：既有 7 例绑定测试全部为单接口 payload，scenario
  模式零覆盖——新增 3 例（见 §3），确认场景路径绑定快照落库、反查链路、
  READY 门禁、未绑定 data=null 全部与非场景一致。**生产代码零改动**。

### 2.2 perf-web 绑定表单（StressForm.vue + api/index.ts）

- **api 层**（`perf-web/src/api/index.ts`）：新增 `datasetApi.list(params)`
  → `GET /api/datasets`（透传 `status`/`limit`），纳入默认导出；
- **StressForm.vue 共用参数区新增「数据集绑定」控件**（单接口与多接口
  混合两模式一致生效，置于引擎/并发参数之前）：
  - 拉取 `GET /api/datasets?status=READY`（onMounted 自动加载 + 「刷新」
    按钮）；下拉仅列 READY 实例（前端先行过滤，与后端 §9.5 门禁 5 对齐，
    避免选中后提交才报错）；选项展示 `definitionId（行数，环境）`；
  - 选中后 `buildPayload()` 携带 `dataset: { instanceId }`——在共用段组装，
    **单接口与场景两模式自动一致携带**；「按接口准备数据」（prepare-data）
    复用同一 buildPayload，`dataset` 键随请求透出，后端 datagen analyze
    按 dict 取键、未知键忽略（Jackson 语义对齐），已确认无误伤；
  - **绑定快照摘要展示**：选中实例显示定义版本 / Schema 指纹（>16 位截断，
    未封存显示 —）/ 实际行数；
  - 容错：列表加载失败内联提示不阻断压测（可留空不绑定）；选中项刷新后
    不在 READY 列表（被清理/降级）时自动复位为不绑定；
  - `fillFrom`（再来一次回填）复位 `datasetId`：wire 契约不含 dataset_*，
    历史任务绑定快照不随回填还原。

## 3. 文件清单

```text
新建：无

修改（后端测试，+3 例）
perf-py/tests/test_stress_dataset_binding.py
  # test_bind_ready_dataset_scenario_mode_snapshot_recorded（场景绑定快照 + 反查链路 + wire 零改动）
  # test_bind_non_ready_dataset_rejected_scenario_mode（场景模式 READY 门禁不放宽）
  # test_scenario_unbound_task_no_binding（场景不带 dataset → data=null）

修改（前端）
perf-web/src/api/index.ts                  # datasetApi.list（GET /api/datasets）
perf-web/src/components/stress/StressForm.vue
  # 模板：共用参数区数据集绑定下拉 + 快照摘要 + 加载失败提示 + 刷新按钮
  # 脚本：datasets/datasetId/selectedDataset/shortFingerprint + loadDatasets(onMounted)
  #       buildPayload 携带 dataset.instanceId（两模式共用段）+ fillFrom 复位
```

## 4. 关键设计决策与踩坑记录（承接 B7 §4 / 批次 1）

1. **scenario 透传 = 纯补测而非补代码**：绑定解析在 raw payload 层完成
   （先于/独立于 `parse_stress_request` 的字段抽取），结构上不可能出现
   「单接口覆盖、场景遗漏」的分叉；本轮逐项核对（create/gate/insert/
   拒绝文案/未知字段忽略）确认一致后，把工作量落在场景路径的测试证据上——
   避免为「对齐」而改对齐的代码。
2. **绑定控件放共用参数区而非两模式各自的模板分支**：绑定语义与压测模式
   正交（数据集描述「压什么数据」，模式描述「怎么发压」），共用一处天然
   保证两模式 payload 一致，且与后端「绑定与模式无关」的实现互为镜像。
3. **前端先行过滤 READY，但不做第二道门禁**：下拉只列 READY 是交互优化；
   非 READY 的最终拒绝仍由后端 `_resolve_dataset_binding` 显式给出（文案
   原样透出），前端不做静默兜底——延续「非 READY 显式拒绝不静默」口径。
4. **列表加载失败不阻断压测**：dataset 域未接线/后端降级时绑定属可选功能，
   内联提示 + 可留空；不因可选增强项拖垮压测主链路可用性。
5. **READY 列表选中项失效自动复位**：READY 是时点状态（清理/降级后失效），
   下拉刷新后保留失效选中项会让用户在提交时才撞上后端拒绝——复位并提示
   重新选择比报错回滚体验一致。
6. **fillFrom 不还原绑定**：压测 wire 契约不含 dataset_*（B7 前段决策 6），
   历史任务回填拿不到绑定信息；如需还原可走 `GET /tasks/{id}/dataset`
   反查端点——本期从简复位为不绑定，避免半还原状态误导。
7. **prepare-data 载荷携带 dataset 已核无害**：`/api/datagen/plans/analyze`
   对 payload 按 dict 取键，未知键忽略（Jackson 语义既有口径）；契约测试
   全绿确认零影响。

## 5. 验收对照（本批任务书 3 项）

| 验收项 | 结果 | 落点 |
|---|---|---|
| 1. scenario 模式 dataset.instanceId 解析/透传与非场景一致，缺口补齐 + 测试 | ✅ | 逐项核对代码一致（§2.1）；3 例场景路径测试：绑定快照/反查全链、READY 门禁、未绑定 data=null |
| 2. perf-web 绑定表单：READY 列表 → payload 携带 dataset.instanceId（两模式）→ 快照摘要展示 | ✅ | StressForm.vue 共用参数区（下拉/摘要/刷新/容错）+ buildPayload 共用段 + datasetApi |
| 3. 后端 pytest 全量 + ruff 全绿；stress wire 契约零改动；前端构建通过 | ✅ | 1674 passed, 2 skipped；ruff All checks passed；`test_unbound_task_wire_contract_unchanged` 等契约测试全绿（dataset_* 不进 to_camel）；npm run build 通过 |

## 6. 回归保障与回滚（§10.4）

- **契约零改动**：stress wire 形态不含 dataset_* 新列；`/api/datagen` 与
  `perf datagen` 契约/golden 全绿；后端生产代码本批**零改动**（纯补测）；
- 前端仅 StressForm.vue + api/index.ts 两文件最小新增；绑定不选（缺省
  `datasetId=''`）时 payload 与改动前完全一致——QuickStartView/
  StressCenterView/SchedulePanel 等既有调用方零改动；
- 回滚：还原 3 个文件（测试文件删 3 例 / index.ts 删 datasetApi /
  StressForm.vue 删绑定块）即回到批次 1 基线；无 DDL、无后端行为变更。

## 7. 后续批次衔接（B7 续 / 后续，供下一 session 使用）

- **本批如实记录的未闭环项**：
  - **绑定还原**：`fillFrom`/定时压测模板（SchedulePanel）不带绑定快照；
    若需「再来一次」还原绑定，走 `GET /api/stress/tasks/{id}/dataset`
    反查端点回填 `datasetId`（端点已就绪，纯前端工作）；
  - **数据集管理入口**：perf-web 尚无 dataset 生命周期管理页（建实例/
    构建/进度/清理走 API/CLI）；绑定下拉依赖外部手段先造出 READY 实例，
    管理页随 R7 平台集成后段或独立批次落；
  - **场景步骤级数据引用**：绑定目前是任务级快照（压什么数据），场景步骤
    内引用数据集内容（变量注入/参数化取数）属引擎侧语义，未在本批范围；
  - **`GET /api/datasets` 列表分页**：前端 limit=100 上限取 READY 列表，
    实例量超限时需游标分页（后端当前 limit 直通，无游标）；
- **B7 续可立即推进项**（承接 B7 前段 §7，批次 1 已完成前两项）：
  - ~~dataset 构建执行体的离线组装~~（批次 1 ✅）；
  - ~~`ptde_execution_manifest` 表 repo~~（批次 1 ✅）；
  - ~~压测绑定扩展~~（本批 ✅）；
  - 剩余项沿 B7 前段 §7 原口径：运行中任务取消端点（状态机出边待评审）、
    API 请求超时控制、外部观测体系接线、selfCheck 端点抽检回调接线、
    R1/R2 QW2 清理收编子项；
- **T7.6~T7.9（legacy 接管/迁移/退役）**：仍被 §13 门禁 2（真库双跑对照）
  前置，随真库环境批次推进（B7 前段 §7 口径不变）。

---

*B7 续批次 2 交接完成：压测绑定扩展落地——scenario 透传核对一致（补 3 例
场景测试），perf-web 绑定表单两模式统一携带，真库执行体缝之外的平台绑定
闭环就位。*
