# PTDE B7 续批次 3 交接文档 — 运行中取消端点 + selfCheck 抽检回调接线

> 状态：**已完成并通过门禁**（2026-11-19）
> 依据：`PTDE-B7交接文档-续批次2-压测绑定扩展.md` §7 遗留清单第 1/2 项；
> `PTDE-B7交接文档-R7平台集成前段.md` §7（取消端点挂起待状态机评审、
> selfCheck 接线）；`PTDE-B6交接文档-R6五层验证与数据集生命周期.md` §7F；
> `PTDE-实施方案-R0-R7分批执行计划.md` §3.5 转换表；总方案 §12.2/§15.2；
> 代码位置：`server/dataset_service.py`（cancel 编排）、`server/dataset_api.py`
> （cancel 端点）、`server/offline_pipeline.py`（信号触达 + probe 注入）、
> `server/datagen_service.py`（self_check_probe 适配器）；
> 前序交接：B7 前段 + B7 续批次 1 + 续批次 2 全部有效。

---

## 1. 交接基线与结果

- 入口基线：B7 续批次 2 交接口径 **1674 passed, 2 skipped**（skip 为 locust
  导入闸门既有口径）；
- 交付后门禁：
  - `uv run pytest tests/contract tests/test_datagen_* tests/test_ptde_* -q` →
    **1360 passed, 1 skipped**（与 B7 前段交接口径持平，零回归；本批新增
    用例全部落在 test_server_* 域）；
  - 全库 `uv run --extra server --extra locust pytest tests/` →
    **1696 passed, 2 skipped, 0 failed**（基线 1674 + 新增 22；skip 既有口径）；
  - `uv run ruff check ptde/ perf_cli/ server/ tests/` → **All checks passed**；
- 本批为 **server/ 侧新增 + 最小接线**；**未触碰** legacy `datagen/**`、
  `ptde/**` 全部模块（状态机转换表零变更——见 §2.1 评审结论）、
  `/api/datagen` 与 `perf datagen` 契约形状、stress wire 契约（既有
  golden/契约测试全绿确认）。

## 2. 交付内容（按任务）

### 2.1 任务 1 前置：状态机出边评审（结论：无需补边，交接误记修正）

对照实施方案 §3.5 转换表、总方案 §12.2 状态图、`ptde/models/dataset.py`
`ALLOWED_TRANSITIONS` 三方逐格核对（`git log` 确认自 R0/B0 提交起即如此）：

| 出边 | §3.5 转换表 | §12.2 状态图 | 代码转换表 | 结论 |
|---|---|---|---|---|
| PLANNED → CANCELLED | ✔ | ✔ | ✔ | 可取消 |
| GENERATING → CANCELLED | ✔ | ✔ | ✔ | 可取消 |
| **LOADING → CANCELLED** | **✔** | **✔** | **✔** | **可取消——B7 前段交接 §7「无出边」系误记，本批核实修正** |
| VERIFYING → CANCELLED | ✗ | ✗ | ✗ | 不补边（评审结论，见下） |
| PARTIAL / READY / 终态 → CANCELLED | ✗ | ✗ | ✗ | 不补边 |

- **评审结论**：取消语义落地**无需任何模型层变更**（§3.5 转换表零修订）；
  可取消状态集 = PLANNED / GENERATING / LOADING（代码内
  `CANCELABLE_STATES` 显式承载，注释记录评审出处）；
- **VERIFYING 不补边的理由**：验证为只读短任务（五层验证器全部 SELECT-only
  白名单查询），无长时占用资源的执行段；为其补 CANCELLED 出边属转换表
  放宽而无对应中断场景，收益为零、模型噪音为正——维持「等待 READY/FAILED
  终局」口径，用户文案显式指路（不静默）；
- **PARTIAL 不补边的理由**：PARTIAL 的既定出口是恢复（resume+manifest_id）
  或其后续失败清理；中途取消会破坏「恢复上下文锚点」语义。维持原状。

### 2.2 任务 1：运行中数据集任务主动取消端点

- **端点**：`POST /api/datasets/{id}/cancel`（形态对齐既有端点族，
  响应 `{code,message,data}`；请求体 `{operator, reason}` **必填**，
  缺失 code=1 显式拒绝）；
- **状态流转经唯一入口 `transition()`**：operator/reason 空值在服务层
  前置拒绝（transition 本身亦双重校验）；转换记录（from/to/operator/
  reason/occurred_at）随 Instance 持久化 = 审计（transition_log 落库）；
- **状态门禁**：
  - PLANNED / GENERATING / LOADING → 接受取消；
  - 已 CANCELLED → **幂等返回** `accepted=false, state=CANCELLED`
    （不二次流转，transition_log 不追加）；
  - READY / FAILED / CLEANING / CLEANED → 拒绝（终态/READY 无出边；
    文案指明 READY/FAILED 处置走 /cleanup）；
  - VERIFYING / PARTIAL → 拒绝（§2.1 评审结论；文案指明「等待终局 /
    resume 恢复」路径）；
- **执行体取消信号注入缝（协作式，不强制 kill）**：
  - `build()` 排队时登记 `threading.Event`（按 instance 行主键）并随任务
    传入工作线程；事件经 `ctx["cancel_event"]` 注入 pipeline 三段；
  - `cancel()` 在**服务层状态锁内原子完成「置位事件 + 经状态机流转
    CANCELLED + 落库」**——工作线程在段落边界（`_check_cancelled`）锁内
    判定，事件置位 ⇔ CANCELLED 已落库，两者不可能观察到中间态；
  - 工作线程协作退出路径：`DatasetCancelled` 异常 → 静默返回（状态已
    CANCELLED，**不落 FAILED、不落 error_msg**——取消是主动处置，非失败）；
    仅当「事件置位但状态未流转」协议违规时兜底走失败路径；
  - 协议约定（pipeline 实现方）：在安全点（长循环边界）检查
    `ctx["cancel_event"]`，置位即抛 `DatasetCancelled`（由
    `server.dataset_service` 导出）协作退出；
- **OfflinePipeline（离线执行体）信号触达**：generate 入口 + **逐分片
  边界**（单 worker 档从整批 `run_shards` 改为逐片 `run_shard` 循环，
  分片间为取消安全点）检查事件，置位抛 `DatasetCancelled`；**已产出
  分片保留**（staging 检查点可核查，不静默丢弃）；
- **已产生分片/staging 处理口径（显式说明）**：**保留供排查**——取消为
  终态（CANCELLED 无出边），不做自动物理清理；如需重建须新建实例重新
  构建（staging 目录残留属离线通道临时产物，真库批次随清理档位收编）。

### 2.3 任务 2：selfCheck 端点抽检回调接线

承接 B6 §7F 既定协议口径（db_ready 层 `validate_db_ready(endpoint_probe=...)`
缝隙，B7 前段 §7 挂起项），**不改 legacy `datagen/**` 行为**：

- **适配器**：`DatagenWriteService.self_check_probe(repo_id)` ——
  selfCheck 端点抽检能力 → `endpoint_probe` 协议回调
  `() -> {"checked": n, "nonEmpty": m}`；
  - 内部复用既有 `_scan_get_endpoints`（Controller AST 解析 GET 端点）
    与抽检探测循环（`httpx.get`，单端点失败计未命中不抛异常——
    selfCheck 尽力而为口径）；
  - 仓库未绑定应用 / 无实例信息 / 无 GET 端点 → `checked=0`
    （validate_db_ready 按「无端点可查」WARN——与 legacy「跳过」语义映射
    一致：从不阻断、不伪装通过）；
- **_self_check 行为零变化**：探测循环抽为 `_probe_get_endpoints` 共享
  （复用而非复制），报告形状（endpoints/nonEmptyRate/warning）回归锁定
  测试；`datagen/**` 零修改；
- **OfflinePipeline 注入**：构造参数新增 `endpoint_probe`，verify 段
  透传 `validate_db_ready`——未提供时维持 `[未执行]` WARN 既有口径；
  真库/平台侧接线时由装配方传入
  `datagen_service.self_check_probe(repo_id)` 即闭合。

## 3. 文件清单

```text
修改（生产代码）
perf-py/server/dataset_service.py
  # DatasetCancelled 协作退出异常 + CANCELABLE_STATES（出边评审结论常量）
  # cancel()：operator/reason 校验 / 状态门禁 / 幂等 / 状态锁内原子
  #          「置位事件 + transition CANCELLED + 落库」
  # build()：登记取消事件随任务传入；_run_build：段落边界
  #          _check_cancelled + DatasetCancelled 协作退出（不落失败痕迹）
  # _record_failure：CANCELLED 终局保护（取消 ≠ 失败）
perf-py/server/dataset_api.py
  # POST /api/datasets/{id}/cancel 端点（operator/reason 必填，code=1 文案原样）
perf-py/server/offline_pipeline.py
  # generate：入口 + 分片间取消安全点（单 worker 逐片循环）；_check_cancel
  # 构造参数 endpoint_probe → validate_db_ready 透传（B6 §7F 接线缝）
perf-py/server/datagen_service.py
  # _probe_get_endpoints 抽取（_self_check 行为零变化）+
  # self_check_probe(repo_id) 适配器（endpoint_probe 协议产出）

修改（测试，共 22 例）
perf-py/tests/test_server_dataset_api.py        # +12（取消路径全集，见 §5）
perf-py/tests/test_server_offline_pipeline.py   # +3（信号协议/分片间取消/probe 接线）
perf-py/tests/test_server_selfcheck_probe.py    # 新建 7（probe 计数/失败/未绑定/
                                                #   legacy 报告形状回归锁定/协议形态）

未改动：datagen/**（legacy 零修改）、ptde/** 全部（转换表零变更）、
perf_cli/**、perf-web/**（前端零改动，npm build 不适用）、sql/init.sql
```

## 4. 关键设计决策与踩坑记录（承接 B7 §4 / 批次 1/2）

1. **出边评审 = 文档考古而非模型变更**：任务书既定口径「LOADING→CANCELLED
   无出边」经三方（§3.5 / §12.2 / 代码转换表）+ git 历史核对为**误记**
   （自 R0 起三条出边即在）——评审工作量落在证据链上，避免了为补边而
   改模型层与实施方案的连锁修订。教训：交接文档的「转换表既定口径」
   引用应核对原始出处，而非转引上一批交接。
2. **取消 = 状态锁内原子「置位 + 流转」**：工作线程段落边界与 cancel()
   共用服务层 `threading.Lock`，保证「事件置位 ⇔ CANCELLED 已落库」的
   原子性——不存在「信号已置位但状态还在 GENERATING」的中间态，协作
   退出的分支判定因此只需检查状态/事件其一（双检查兜底协议违规）。
3. **协作式取消而非强制 kill**：杀线程不可靠（任务书既定口径）——信号
   协议为 `ctx["cancel_event"]`（threading.Event）+ 安全点检查 +
   `DatasetCancelled` 异常；offline 通道分片间为天然安全点（单 worker
   档逐片执行）；真库批次执行体接线时按同一协议在装载批次间落检查点。
4. **取消 ≠ 失败**：协作退出路径不落 FAILED、不落 error_msg（`_record_failure`
   对 CANCELLED 终局短路）——取消是用户主动处置，终局文案与审计由
   transition_log 承载；协议违规兜底（信号置位但状态未流转）才走失败路径。
5. **staging/分片保留供排查**：取消为终态无自动清理路径；已产出分片在
   staging 检查点可核查（测试锁定 generated==2 不静默丢弃）；与
   「重建须新建实例」口径一致，不做半自动化处置。
6. **selfCheck 接线 = 适配器复用而非复制语义**：`self_check_probe` 与
   legacy `_self_check` 共享探测循环（抽取共享后报告形状回归锁定）——
   「收编=复制语义」的 B6 口径在本批演化为「接线=共享实现+锁定形状」，
   语义漂移风险由测试钳制。
7. **checked=0 的语义映射**：legacy selfCheck「仓库未绑定 → 跳过（None）」
   在 endpoint_probe 协议（无 None 返回形态）下映射为
   `checked=0 → validate_db_ready「无端点可查」WARN`——两边都是「不阻断、
   不伪装通过」，验证层 WARN 明示比静默跳过更符合「未执行 ≠ 通过」口径。
8. **报告层 checks 落库粒度维持既有口径（踩坑记录）**：新增 probe 接线
   测试时发现 `ptde_validation_report` 落库仅 layer/status/metrics/
   tolerance/evidence 四类列（§12.1 DDL 既有粒度），LayerResult.checks
   不落库——B7 前段既定设计，本批不改（避免 DDL 变更连锁）；probe 产出
   断言经 `pipeline.verify` 返回的 ValidationReport 直读完成。

## 5. 验收对照（任务书 §6）

| 验收项 | 结果 | 落点 |
|---|---|---|
| 取消合法路径 | ✅ | `test_cancel_planned_instance_direct_transition`（PLANNED）；`test_cancel_running_build_signals_pipeline_and_exits_cooperatively`（GENERATING 运行中）；`test_cancel_loading_state_allowed`（LOADING）；API 流：`test_api_cancel_endpoint_running_build_flow` |
| 拒绝路径 | ✅ | `test_cancel_rejects_ready_and_failed` / `test_cancel_rejects_verifying_and_partial_with_guidance`；API：`test_api_cancel_rejects_terminal_state` + operator/reason 缺失两态 |
| 幂等 | ✅ | `test_cancel_idempotent_no_second_transition`（accepted=false + transition_log 不追加）；API：`test_api_cancel_idempotent_accepted_false` |
| 审计（transition_log） | ✅ | `test_cancel_transition_log_records_operator_and_reason`（operator/reason/from/to 全落）；运行中取消终局记录断言 |
| 执行体取消信号触达 | ✅ | `test_cancel_running_build_signals_pipeline_and_exits_cooperatively`（fake 通道：ctx 事件触达 generate 段 + 后续段不再执行 + 不落失败痕迹）；`test_offline_pipeline_generate_cancel_signal_protocol` + `test_offline_pipeline_cancel_between_shards_keeps_generated_shards`（offline 通道：入口 + 分片间安全点，已产出分片保留） |
| 门禁 | ✅ | contract/datagen/ptde 1360 passed, 1 skipped；全库 1696 passed, 2 skipped；ruff All checks passed；legacy `datagen/**` 与 `ptde/**` 零修改（转换表零变更） |

## 6. 回归保障与回滚（§10.4）

- **契约零改动**：`/api/datagen` + `perf datagen` 契约与 golden 全绿；
  stress wire 不含 dataset_*；cancel 为 `/api/datasets` 端点族新增端点
  （QW2 cleanup「非契约内新增」先例延续）；`datagen_service._self_check`
  报告形状回归锁定测试（重构零行为变化）；
- legacy `datagen/**` 零修改；`ptde/**` 全模块零修改（取消语义全在
  server/ 层，状态机经唯一入口消费）；`perf-web` 零改动；
- 回滚：还原 §3 所列 4 个生产文件（dataset_service 删 cancel 编排段 /
  dataset_api 删 cancel 端点 / offline_pipeline 还原 generate 循环与
  verify 签名 / datagen_service 还原 _self_check 内联探测循环）+ 删
  test_server_selfcheck_probe.py + 还原两测试文件追加段即可；无 DDL、
  无前端变更。

## 7. 后续批次衔接（B7 续 / 后续，供下一 session 使用）

- **本批已闭环**（承接 B7 前段 §7 清单）：
  - ~~运行中任务取消端点~~（本批 ✅——出边评审核实为交接误记，零模型
    变更落地；B7 前段 §5 门禁 3「取消」部分项可升级为 ✅）；
  - ~~selfCheck 端点抽检回调接线~~（本批 ✅——适配器 + OfflinePipeline
    注入缝就位；真库侧装配方传 `self_check_probe(repo_id)` 即闭合）；
- **本批如实记录的未闭环项**（任务书 §5 其余项原口径转记，本批不动）：
  - API 请求超时控制（随真库执行体接线一并落）；
  - 外部观测体系接线（Prometheus/日志管道，随部署形态定）；
  - R1/R2 QW2 清理收编（随 A4a 接管退役）；
  - 真库执行体接线 + R5 真库装载基准 + §13 门禁 2 双跑对照（真库环境
    批次；**接线时执行体须按本批 §4 决策 3 的取消信号协议落装载段
    安全点**）；
  - T7.6~T7.9 legacy 接管/存量迁移/退役（被门禁 2 前置；T7.8 迁移脚本
    本体可提前按幂等可重放口径开发）；
  - 前端侧：绑定还原（fillFrom/SchedulePanel 回填，走
    `GET /api/stress/tasks/{id}/dataset` 反查）、数据集管理页（**可顺带
    暴露取消按钮**——端点已就绪）、场景步骤级数据引用、
    `GET /api/datasets` 游标分页；
- **可立即推进项**（不依赖真库）：
  - T7.8 迁移脚本本体（幂等可重放，legacy_* 去重键）；
  - perf-web 数据集管理页 / 绑定还原（纯前端）。

---

*B7 续批次 3 交接完成：运行中取消端点落地（出边评审修正交接误记，零模型
变更），selfCheck 抽检回调接线闭合（B6 §7F 缝隙收编），执行体协作式取消
协议在 fake/offline 双通道验证。*
