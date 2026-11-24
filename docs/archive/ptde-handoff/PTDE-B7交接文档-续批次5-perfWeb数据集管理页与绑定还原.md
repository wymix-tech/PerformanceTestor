# PTDE B7 续批次 5 交接文档 — perf-web 数据集管理页 + 绑定还原锁定（纯前端）

> 状态：**已完成并通过门禁**（2026-11-19）
> 依据：`PTDE-B7交接文档-续批次3-运行中取消与selfCheck接线.md` §7 前端待办
> （数据集管理页含取消按钮、绑定还原）；`PTDE-B7交接文档-续批次2-压测绑定
> 扩展.md` §7（绑定还原反查端点已就绪、数据集管理页缺失）；
> `PTDE-实施方案-R0-R7分批执行计划.md` T7.2/T7.3 前端验收面；
> 代码位置：`perf-web/src/views/DatasetManageView.vue`（新建页面）、
> `perf-web/src/api/index.ts`（datasetApi 扩展）、`perf-web/src/utils/
> format.ts`（datasetStatusClass）、路由/导航接线；
> 前序交接：B7 前段 + 续批次 1/2/3/4 全部有效。

---

## 1. 交接基线与结果

- 入口基线：B7 续批次 4 交付后全库口径（perf-py 1707 passed, 2 skipped；
  perf-web vitest 23 例）；本批 **perf-py 零改动**（纯前端批次）；
- 交付后门禁：
  - `npm run build`（vue-tsc -b && vite build）→ **通过**（类型门禁 +
    产物构建，5.19s）；
  - `npx vitest run` → **5 个测试文件 41 passed**（既有 23 + 本批新增 18：
    DatasetManageView 13 + StressForm.binding 5）；
- 契约面：`/api/datasets` 端点族与 `/api/stress/tasks/{id}/dataset` 反查端点
  形状零改动（前端按既有形状消费）；后端 perf-py/** 全部未触碰。

## 2. 交付内容

### 2.1 数据集管理页（新建 `views/DatasetManageView.vue`，路由 `/datasets`）

- **实例列表**：`GET /api/datasets`（status 过滤下拉 = ptde 状态机十态 +
  全部；limit=100——后端列表为 offset/limit 型非游标，游标分页留待后端
  端点扩展）；行展示 definitionId/状态徽章/目标环境/实际行数/创建时间/
  errorMsg 摘要；5s 轮询刷新 + 失败静默保旧数据（StressTaskList 口径）；
  选中项被清理后自动复位详情（不展示陈旧快照）；
- **详情面板**：`GET /api/datasets/{id}`（definitionId/environmentId/
  状态/actualRows/actualStorageBytes/schemaFingerprint/验证摘要 JSON 预览/
  transitionLog 逐条 from→to+operator+reason+时间）；
- **构建进度**：运行中实例（PLANNED/GENERATING/LOADING/VERIFYING）轮询
  `GET /api/datasets/{id}/progress`（running/taskId/instanceStatus/
  errorMsg）；终态不请求（`progress-panel` 不渲染）；
- **取消按钮**（B7 续批次 3 端点的前端暴露）：仅对 CANCELABLE 集
  （PLANNED/GENERATING/LOADING）渲染——VERIFYING/PARTIAL/终态无取消出边；
  交互链 = `prompt` 取消理由（审计必填，空/取消即中止）→ `confirm` 终态
  风险告知（「取消为终态不可恢复，已产出分片保留供排查」）→
  `POST /api/datasets/{id}/cancel` body `{operator, reason}`（operator 取
  登录用户名，缺省 'web'）；`code!=0` → alert 后端文案（如「VERIFYING
  状态无取消出边」）；**`accepted=false` 幂等返回按成功处理**（提示
  「幂等返回，未重复流转」，口径对齐 dataset_service）；成功后刷新列表。

### 2.2 绑定还原：锁定测试 + 模板链路验证（代码零改动）

- **StressForm.restoreBinding 三态锁定**（续批次 2 已实现的回填逻辑，本批
  补 vitest 防回归）：
  - 反查命中且实例在 READY 列表 → 回填（`snapshot().dataset` 携带
    instanceId）；
  - 未绑定（`data=null`）→ 保持不绑定；
  - 绑定失效（实例已清理/降级离开 READY 列表）→ 复位不绑定；
  - 反查失败 → 静默保持不绑定、不阻断回填主链路（其余参数已回填）；
- **SchedulePanel 链路验证**：调度模板经 `StressForm.snapshot()` 已携带
  `dataset:{instanceId}`（B7 续批次 2 口径：调度触发走同一 create_and_run
  绑定路径）——新增测试锁定「fillFrom 还原绑定后 snapshot 进入模板 JSON」
  全链，调度侧零改动。

### 2.3 支撑改动

- `api/index.ts` datasetApi 扩展：`detail(id, withReports?)` /
  `progress(id)` / `cancel(id, {operator, reason})`（注释标注幂等口径；
  list 既有）；
- `utils/format.ts` 新增 `datasetStatusClass`（ptde 十态徽章配色，取消/
  清理终态用中性灰——「取消是主动处置非失败」口径的视觉表达）；
- `router/index.ts` 注册 `/datasets`（meta.title='数据管理'）；
- `layouts/MainLayout.vue` 侧边导航新增「数据管理」入口（Database 图标，
  置于压测中心之后）。

## 3. 文件清单

```text
修改（前端）
perf-web/src/api/index.ts                    # datasetApi 扩展 detail/progress/cancel
perf-web/src/utils/format.ts                 # + datasetStatusClass（ptde 十态徽章）
perf-web/src/router/index.ts                 # + /datasets 路由
perf-web/src/layouts/MainLayout.vue          # + 「数据管理」导航项（Database 图标）

新建（前端）
perf-web/src/views/DatasetManageView.vue     # 数据集管理页（列表/详情/进度/取消）
perf-web/src/views/__tests__/DatasetManageView.spec.ts          # 新建 13 例
perf-web/src/components/stress/__tests__/StressForm.binding.spec.ts  # 新建 5 例

未改动：StressForm.vue（restoreBinding/fillFrom/snapshot 既有逻辑零修改——
测试锁定而非重写）、SchedulePanel.vue、perf-py/** 全部（纯前端批次）
```

## 4. 关键设计决策与踩坑记录

1. **取消按钮 = CANCELABLE 集前端复刻**：可取消状态集在视图内显式常量
   （对齐后端 `CANCELABLE_STATES`），而非按后端报错动态发现——按钮不渲染
   即语义（VERIFYING/PARTIAL 无出边），后端报错仅作纵深兜底；两层文案一致。
2. **审计必填前置到交互层**：后端 operator/reason 空值 code=1 拒绝；前端
   prompt 空理由即中止并明示「审计必填」——不让用户走到后端报错才知道
   （显式拒绝不静默口径的交互化）。
3. **accepted=false 是成功不是错误**：幂等返回在交互层按成功处理并解释
   语义（「未重复流转」）——对齐续批次 3 §2.2 幂等口径，避免用户把
   「重复点取消」当成故障。
4. **绑定还原是锁定不是重写**：续批次 2 已在 StressForm 落地 restoreBinding；
   本批不动生产代码，以 5 例 vitest 锁定三态 + 模板链路——「先查后写/
   先实现后锁形」与后端批次 3 §4 决策 6（接线=共享实现+锁定形状）同构。
5. **踩坑：vi.spyOn(window, 'prompt') 的交叉签名类型**——vue-tsc 推导出
   不可赋值的 MockInstance 交叉类型（`as ReturnType<typeof vi.spyOn>` 亦
   不通过）；以窄化结构类型声明承接（运行时无此约束），build 门禁恢复绿。
6. **列表游标分页未做**：后端 `/api/datasets` 为 offset/limit 型（§12.5
   遗留项原口径）——前端 limit=100 消费现状，游标分页随后端端点扩展
   一并接（避免前端造双轨分页逻辑）。

## 5. 验收对照

| 验收项 | 结果 | 落点 |
|---|---|---|
| 数据集管理页（列表/过滤/详情/进度） | ✅ | DatasetManageView.spec：渲染/status 透传/详情+进度轮询/终态跳过进度/失败保旧数据/卸载停轮询 |
| 取消按钮暴露（续批次 3 端点） | ✅ | 仅 CANCELABLE 渲染；`sends cancel with operator and audit reason`；幂等 accepted=false；code!=0 文案透出；空理由/拒绝确认/关闭 prompt 三路中止 |
| 绑定还原（fillFrom 反查回填） | ✅ | StressForm.binding.spec 五例（命中回填/未绑定/失效复位/失败静默/模板快照携带） |
| 门禁 | ✅ | npm run build（vue-tsc + vite）通过；vitest 41 passed（5 文件）；perf-py 零改动 |

## 6. 回归保障与回滚（§10.4）

- **perf-py 契约零改动**：本批纯前端消费既有端点形状；后端全库测试不涉及；
- 回滚：删 DatasetManageView.vue + 两 spec 文件 + 还原 api/index.ts、
  format.ts、router/index.ts、MainLayout.vue 四处追加段即可；无后端联动。

## 7. 后续批次衔接（供下一 session 使用）

- **本批已闭环**（续批次 3 §7 前端待办两项）：
  - ~~数据集管理页（含取消按钮）~~（本批 ✅）；
  - ~~绑定还原~~（续批次 2 实现 + 本批锁定测试 ✅）；
- **仍未闭环**（原口径转记）：
  - 场景步骤级数据引用（`{{alias.result.x}}` 之外引用数据集字段——依赖
    真库执行体与 Definition 数据契约细化）；
  - `GET /api/datasets` 游标分页（后端端点扩展 + 前端 CursorPage 接入，
    既有 `normalizePage` 基建可复用）；
  - T7.6/T7.7 接管切换 + §13 门禁 2 双跑（真库批次）；
  - T7.9 legacy 整包退役（观察期后）；
  - API 超时控制 / 外部观测接线 / R1R2 QW2 收编（随真库与部署形态批次）；
- **可立即推进项**：`GET /api/datasets` 游标分页后端端点扩展（server 侧，
  纯后端可落地，前端基建已就位）。

---

*B7 续批次 5 交接完成：perf-web 验收面首次打开即闭环——数据集管理页落地
（取消按钮 = 续批次 3 协作式取消协议的前端暴露，审计必填前置到交互层，
幂等返回按成功处理），绑定还原三态以锁定测试收编；纯前端批次，perf-py
零改动、契约零变更。*
