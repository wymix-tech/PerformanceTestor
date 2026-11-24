# PTDE B7 续批次 6 交接文档 — /api/datasets 游标分页（CursorPage 全链贯通）

> 状态：**已完成并通过门禁**（2026-11-19）
> 依据：续批次 5 交接 §7「可立即推进项」；续批次 3 交接 §7 前端待办；
> 前端 CursorPage 契约以 `perf-web/src/api/index.ts`
> （CursorPage/normalizePage/CursorParams）为既有口径来源；
> 代码位置：`server/dataset_service.py`、`server/ptde_repo.py`、
> `server/dataset_api.py`、`perf-web` 三处消费端；
> 前序交接：B7 前段 + 续批次 1~5 全部有效。

---

## 1. 交接基线与结果

- 入口基线：续批次 5 交付后口径（perf-py 1707 passed, 2 skipped；
  perf-web vitest 41 passed, build 通过）；
- 交付后门禁：
  - perf-py 全库 `uv run --extra server --extra locust pytest tests/` →
    **1711 passed, 2 skipped, 0 failed**（基线 1707 + 新增 4）；
  - `uv run ruff check ptde/ perf_cli/ server/ tests/` → All checks passed；
  - perf-web `npm run build`（vue-tsc + vite）→ 通过；
  - perf-web `npx vitest run` → **5 文件 44 passed**（基线 41 + 新增 3）；
- 本批为 **dataset 域列表端点形态切换 + 全链消费端适配**；`/api/datagen`
  13 端点契约与 golden 零触碰；`ptde/**` 零修改；stress 绑定契约零变更。

## 2. 交付内容

### 2.1 后端：`GET /api/datasets` 切换为 CursorPage 单形态

- **响应形态**：`data = {items, nextCursor: str|null, hasMore: bool}`
  ——对齐前端既有 `CursorPage<T>` 类型（本仓库首个后端 CursorPage 实现；
  trace/jvm 域的同名前端类型此前无后端对应，normalizePage 的「裸数组
  兜底」灰度口径退役为纯防御）；
- **游标设计（keyset，跨方言最稳）**：排序 id 降序（自增主键与 create_time
  单调一致，避免元组比较方言差异与 offset 深翻页漂移）；cursor =
  `base64url(json {"id": 上页末行行主键})` 不透明串；解码失败/非法 id →
  code=1 显式拒绝（不静默当首页）；hasMore 由多取一行（limit+1）判定，
  末页 nextCursor=null；
- **参数**：`status` 与游标可组合；`size` 每页条数（默认 50，上限 200）；
  `limit` 为 **deprecated 兼容回退**（未传 size 时等价 size——对齐前端
  CursorParams 注释既有口径）；
- **兼容面核对**：list 响应形状无契约测试锁定（全库仅一处断言，已同步
  更新）；CLI `perf ptde dataset list` 走 service.list_instances（裸列表）
  不经 HTTP——旧方法保留不删，CLI/内部调用面零变更。

### 2.2 前端：管理页「加载更多」翻页 + 绑定下拉适配

- `api/index.ts` `datasetApi.list` 切换为 `normalizePage` 归一化
  （traceApi 同款写法）；
- `DatasetManageView`：游标状态（nextCursor/hasMore/listLoading）+
  「加载更多」追加式翻页（hasMore=false 按钮不渲染 → 「已到最后一条」，
  TraceAnalyzerView 口径）；切过滤/手动刷新/取消成功 = 重置游标拉首页；
  翻页失败静默保旧数据（不闪空）；**轮询收敛**——列表不再 5s 自动轮询
  （避免翻页态被覆盖），改为选中实例详情/进度定时刷新；
- `StressForm.loadDatasets`：绑定下拉 `size:100` 单页拉满（READY 全量
  口径不变），消费 `page.items`。

## 3. 文件清单

```text
修改（生产代码）
perf-py/server/ptde_repo.py          # + list_instances_page（keyset id 降序，limit+1 探尾）
perf-py/server/dataset_service.py    # + list_instances_page（cursor 编解码/显式拒绝/CursorPage 组装）
perf-py/server/dataset_api.py        # GET /api/datasets 切换（size/cursor + limit 回退 + ValueError→code=1）
perf-web/src/api/index.ts            # datasetApi.list → normalizePage
perf-web/src/views/DatasetManageView.vue    # 游标翻页（加载更多）+ 轮询收敛
perf-web/src/components/stress/StressForm.vue  # 绑定下拉 page.items 消费

修改（测试，perf-py +4 / perf-web +3）
perf-py/tests/test_server_dataset_api.py
  # 既有 list 断言更新为 CursorPage 形态（1 处）；+4：翻页闭环/非法游标/
  # status 组合游标/limit 回退
perf-web/src/views/__tests__/DatasetManageView.spec.ts
  # mock 边界切换为归一化形态；+3：加载更多追加+末页提示/翻页失败保旧/轮询收敛
perf-web/src/components/stress/__tests__/StressForm.binding.spec.ts
  # 桩形态 + 参数断言同步（size:100）

未改动：ptde/** 全部、datagen/**、/api/datagen 契约与 golden、
stress 绑定端点、sql/init.sql、CLI ptde_runner（service 直连不变）
```

## 4. 关键设计决策与踩坑记录

1. **keyset 行主键游标而非 offset**：offset 深翻页随插入漂移且代价线性
   增长；(create_time, id) 元组比较 SQLite/MySQL 行为差异大——自增主键
   与创建序单调一致，id 降序 keyset 是跨方言最小公共解。
2. **cursor 不透明串 + 服务端签发**：客户端不解析游标内容，非法游标
   code=1 显式拒绝——解码失败不当首页静默吞掉（「未执行 ≠ 通过」在分页
   侧的表达）。
3. **单形态而非灰度双形态**：list 形状无契约锁定 + 消费端全在本仓库内
   （StressForm/管理页/CLI 不走 HTTP）——一次性切换单形态比双形态并存少
   一套分支维护；normalizePage 兜底保留为防御（不依赖）。
4. **列表轮询收敛**：翻页态被 5s 列表轮询覆盖会丢页；收敛为「选中实例
   详情/进度轮询 + 列表手动刷新」——实时性诉求集中在运行中实例进度。
5. **踩坑：测试桩边界**——datasetApi.list 已内嵌 normalizePage，组件消费
   归一化后形态；mock 整个 api 模块时桩须返回 `{items,...}` 而非
   `{code,data:{items}}`（首跑 18 例红即此因）。测试桩应打在组件消费边界。
6. **踩坑：端点层异常捕获**——list_instances_page 会抛 DatasetServiceError
   （cursor 非法），原端点无 try/except（旧实现不可能抛）→ FastAPI 500
   而非 code=1；端点补 ValueError→_err 包装（对齐本文件其他端点惯例）。

## 5. 验收对照

| 验收项 | 结果 | 落点 |
|---|---|---|
| 游标翻页闭环 | ✅ | `test_api_instances_cursor_pagination_roundtrip`（5 行 size=2 → 3 页不重不漏，id 降序，末页 hasMore=false+cursor=null） |
| 非法游标显式拒绝 | ✅ | `test_api_instances_cursor_invalid_rejected`（code=1 + 文案含 cursor） |
| status 过滤与游标组合 | ✅ | `test_api_instances_cursor_combined_with_status_filter` |
| limit 兼容回退（deprecated） | ✅ | `test_api_instances_limit_fallback_compat` |
| 前端加载更多 | ✅ | DatasetManageView.spec：追加不重置/末页提示/翻页失败保旧数据 |
| 绑定下拉适配零回归 | ✅ | StressForm.binding.spec 五例全绿（size:100 断言） |
| 门禁 | ✅ | perf-py 1711 passed, 2 skipped；ruff 全绿；npm run build 通过；vitest 44 passed |

## 6. 回归保障与回滚（§10.4）

- **契约零改动**：`/api/datagen` 13 端点 + `perf datagen` spec.json 形状
  与 golden 全绿；stress 绑定反查端点未触碰；数据集域为「非契约内端点族」
  （QW2 cleanup 先例延续）；
- 回滚：还原三处后端新增段 + 三处前端适配段 + 测试同步还原即可；无 DDL、
  无模型层变更。

## 7. 后续批次衔接（供下一 session 使用）

- **本批已闭环**：~~GET /api/datasets 游标分页~~（✅——后端 CursorPage
  首个实现 + 前端全链贯通；B7 §7「可立即推进项」全部清零）；
- **剩余未闭环项全部收敛到真库/切换批次**（原口径转记）：
  - T7.6/T7.7 接管切换（§13 门禁 2 双跑对照需真库；门禁 1 持续全绿）；
  - 真库执行体接线 + R5 真库装载基准（按批次 3 §4 决策 3 取消信号协议落
    装载段安全点；selfCheck 装配方传 `self_check_probe(repo_id)`）；
  - API 超时控制 / 外部观测接线 / R1R2 QW2 收编（随真库与部署形态批次）；
  - T7.9 legacy 整包退役（切换 + 观察期后；迁移脚本批次 4 已就位）；
  - 场景步骤级数据引用（依赖真库执行体与 Definition 数据契约细化）；
- **纯前端可推进项已清零**；perf-web 侧后续工作仅随数据契约细化展开。

---

*B7 续批次 6 交接完成：/api/datasets 游标分页全链贯通——后端 keyset 游标
（id 降序 + 不透明串显式拒绝）+ CursorPage 单形态，前端「加载更多」追加式
翻页与绑定下拉适配；B7 §7 可立即推进项全部清零，剩余项收敛到真库批次。*
