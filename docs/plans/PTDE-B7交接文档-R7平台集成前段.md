# PTDE B7 批次交接文档 — R7 平台集成前段：落库层 + CLI + API + 压测绑定 + 可观测性

> 状态：**已完成并通过门禁**（2026-11-18）
> 依据：`PTDE-实施方案-R0-R7分批执行计划.md` §10（R7 任务表 T7.1~T7.4）、§12.6
> （压测绑定加列）、§10.3（验收门禁）；`性能测试平台-智能测试数据引擎-完整技术方案.md`
> §15.1/§15.2/§15.3、§16.1/§16.2、§13.1；B6 交接 §7/§7F（承接清单）；
> 代码位置：`server/ptde_repo.py`（落库层）、`server/dataset_service.py` +
> `server/dataset_api.py`（T7.2）、`perf_cli/ptde_runner.py`（T7.1）、
> `ptde/observability.py`（T7.4）、`server/stress_*` + `sql/init.sql`（T7.3）；
> 前序交接：B6 交接（含续记）全部有效。

---

## 1. 交接基线与结果

- 入口基线：B6 续记交接口径 **1360-37=1323 前值**（contract+datagen+ptde 全绿）；
- 交付后门禁：
  - `uv run pytest tests/contract tests/test_datagen_* tests/test_ptde_* -q` →
    **1360 passed, 1 skipped**（新增 37 计入口径；1 skipped 为 locust 导入闸门既有口径）；
  - 全库 `uv run pytest tests/ -q` → **1655 passed, 2 skipped, 0 failed**
    （skip 为 locust 导入闸门既有口径）；
  - `uv run ruff check ptde/ perf_cli/ server/ tests/`（新改文件全集）→
    **All checks passed**；
- 本批为 **ptde/、server/、perf_cli/ 三侧新增 + 最小接线**；**未触碰**
  legacy `datagen/**`、`ptde/models/**`、`ptde/generator/**`、`ptde/planner/**`、
  `ptde/loader/**`、`ptde/validator/**`、`ptde/lifecycle/**`（全部只读复用）、
  `/api/datagen` 与 `perf datagen` 契约形状（契约测试全绿确认）。

## 2. 交付内容（按任务）

### T7.1 CLI — `perf_cli/ptde_runner.py` + `perf_cli/main.py`

- `perf ptde` 子命令族注册（typer 子 app，命令形状对齐总方案 §15.1）：
  - `perf ptde analyze <dir>`：SnapshotBuilder 纯源码证据快照（JSON 输出）；
  - `perf ptde profile <file>`：文件画像导入（inspect）；`--collect --config`
    走只读采集（StatisticsCollector，A5 双 DSN 口径）；
  - `perf ptde plan <spec> --preview [--seed]`：离线规划预览（build_plan，
    seed 写入 `spec.reproducibility.seed`，QW1/R4 Seed 链路入口）；
    缺省（`--def-key`）注册 Definition（spec 校验 + 指纹封存 + 落库）；
  - `perf ptde dataset <action> [id]`：`create/build/status/verify/list/cleanup`，
    直连落库层与 READY 门禁（B6 §7F「CLI 现可直连 evaluate_ready/
    execute_cleanup」承接）；
- flag 全集：`--dry-run/--seed/--batch/--resume/--manifest-id/--config/
  --db/--environment/--env-class/--approvals/--output`；
- **生产环境不提供构建与清理入口**（总方案 §15.1 / K5）：构建在服务层按
  environmentId 拒绝；清理在 `plan_cleanup` 按环境类别拒绝（生产/未知环境）；
- CLI 落库地址：`--db` → `PTDE_CLI_DB` → 本地 sqlite 文件（sqlite 便捷建表；
  **生产 MySQL 由 init.sql 建表，CLI 不越权 DDL**）；
- 退出码：0 = 成功；1 = 领域失败（门禁拒绝/校验失败）；2 = 调用异常。

### T7.2 API — `server/dataset_api.py` + `server/dataset_service.py` + `server/ptde_repo.py`

- **落库层**（B6 §7F「validator/lifecycle 落库层 repo」承接）：
  `ptde_dataset_definition` / `ptde_dataset_instance` / `ptde_validation_report`
  三表 ORM + 读写仓储（对齐 datagen_repo/stress_repo 平级约定）；
  - id 形态映射：DB 自增主键 ↔ ptde 模型 str id（链路一致，READY 门禁
    条件 7 `manifest.instance_id == instance.id` 可核对）；
    `instance.definition_id` 内存侧维持 `def_key@version`（B6 §4 决策 10），
    DB 侧为 Definition 行主键外键化；
  - ValidationReport 落库：LayerResult → layer/status/metrics_json/
    tolerance_json/evidence_json（列形态已对齐，R6 §7 待接线项收编）；
  - **repo 会话锁序化**：sqlite 内存库 StaticPool 单连接 + 应用线程池/工作
    线程双写场景下，跨线程会话 commit/rollback 互相覆盖（本轮实测复现：
    五层报告行随机丢失）——repo 全部会话块经 `threading.Lock` 序化；
    MySQL 池化连接下为无竞争快速路径；
- **服务层 `DatasetLifecycleService`**：
  - Definition 注册（def_key+version 唯一，重复显式失败不覆盖）/
    Instance 创建（PLANNED 起步，owner_token 随建即发）；
  - 状态流转唯一入口 = R0 `transition()`，转换记录随 Instance 持久化
    （transition_log 落库 = 审计）；
  - 构建异步编排（`ThreadPoolExecutor("ptde-dataset")`，不占 HTTP 线程）：
    GENERATING→`pipeline.generate`→LOADING→`pipeline.load`→VERIFYING→
    `pipeline.verify`→**报告落库 + `evaluate_ready` 八项程序化判定**→
    READY/FAILED（经状态机，原因入 transition_log）；失败兜底：error_msg
    落库 + 状态机允许范围内转 FAILED（不伪装成功）；
  - 验证报告 `instance_id` 链路一致性前置校验（不一致显式失败，门禁 4）；
  - READY 判定结果入 `instance.validation_summary`（八项 checks 全量留痕）；
  - **幂等**：任务 ID = instance id，执行中重复 build/verify 返回
    `accepted=false`（不重复排队）；PARTIAL 恢复须 `manifest_id`
    （§3.5 约束 3，经状态机强制）；
  - 清理（T6.8 收编）：决策（`plan_cleanup`）与执行（`execute_cleanup` +
    SecurityGate CLEANUP 授权 + 三级审计）分离，READY/FAILED 才可清理，
    终局 CLEANED 经状态机；
- **端点族**（前缀 `/api/datasets`，§15.2 端点集映射；响应形态
  `{code,message,data}` 对齐既有域）：`POST /plan`、`GET /plan`、`GET /plans`、
  `GET /plan/{defKey}/versions`、`POST /`（建实例）、`GET /`、`GET /{id}`
  （`withReports=true` 带五层报告）、`GET /{id}/progress`、`GET /{id}/task`、
  `POST /{id}/build`（dryRun/seed/batch/resume+manifestId）、
  `POST /{id}/verify`、`POST /{id}/cleanup`（envClass/approvals）；
  领域失败 → code=1 + 文案原样；JWT 拦截经 app 工厂统一生效；
  **不改 `/api/datagen` 契约**（契约测试全绿确认）；
- **执行体注入缝**：`generate/load/verify` 三段 + `cleanup_executor` 回调
  由调用方注入（app 工厂 `dataset_pipeline` 参数）；真库端到端执行体随
  A2 真库环境批次接线（B6 §7F 口径），缺省 `UnwiredPipeline` **显式失败
  不伪装可用**。

### T7.3 压测绑定 — `server/stress_repo.py` / `stress_service.py` / `stress_api.py` + `sql/init.sql`

- **幂等加列**（§12.6 原文，`@col_exists` 判定式 ALTER 沿用 init.sql 风格）：
  `stress_task` 加 `dataset_instance_id` / `dataset_definition_version` /
  `dataset_schema_fingerprint` / `dataset_validation_json` 四列；
- ORM 加列（列名对齐 DDL）；**wire 契约零改动**（`to_camel` 不含 dataset_*
  字段，既有 stress 契约测试全绿）；
- 绑定落点：创建压测任务请求体可选 `dataset:{instanceId}` →
  `_resolve_dataset_binding`：查询缝（app 工厂注入 dataset 域查询）→
  **仅 READY 实例可绑定**（非 READY/不存在/查询缝未注入显式拒绝，
  不静默忽略）→ 绑定快照（Definition 版本/Schema 指纹/验证结果摘要 JSON）
  随任务落库；
- 反查端点 `GET /api/stress/tasks/{id}/dataset`：task → dataset_* 快照 +
  dataset 域详情（Instance → Definition 版本 + Schema 指纹 + 五层验证报告
  全链可查，§10.3 门禁 2）；未绑定返回 data=null（非错误）。

### T7.4 可观测性 — `ptde/observability.py`

- 指标：§16.1 指标名全集（15 项）逐项常量；`ObservabilityRecorder` 线程
  安全记录 + 按指标名聚合快照（count/sum/min/max/last/unit，可按 dataset
  维度过滤）；容量上限环形截断；**全集外指标名显式拒绝**（防口径漂移）；
  `observe_duration` 耗时便捷记录；
- 结构化日志：§16.2 字段集逐条在位（`build_log_record`：dataset_id/plan_id/
  task_id/table_id/shard_id/chunk_id/worker_id/database_id/operation/status/
  duration/错误分类 + ts）；错误分类自动映射（timeout/auth_denied/
  connect_error/load_error/validation_error/state_error/gate_blocked）；
- **脱敏硬约束**：键名敏感字段（password/passwd/pwd/secret/token/credential/
  api_key/authorization/private_key/dsn）递归打码 `***`；DSN 内嵌凭据
  `scheme://user:***@host`（口令永不入日志，A5 口径延续）；
  `emit_log_record` 单行 JSON 落 logging（打敏先于输出）。

## 3. 文件清单

```text
新建（生产代码）
perf-py/ptde/observability.py                  # T7.4（§16.1 指标 + §16.2 日志字段 + 脱敏）
perf-py/server/ptde_repo.py                    # 落库层：三表 ORM + 读写仓储 + 模型映射
perf-py/server/dataset_service.py              # 生命周期服务（异步编排/READY 终局/清理）
perf-py/server/dataset_api.py                  # /api/datasets/* 端点族
perf-py/perf_cli/ptde_runner.py                # T7.1 CLI 宿主装配（analyze/profile/plan/dataset）

修改（生产代码，最小接线）
perf-py/perf_cli/main.py                       # ptde 子 app 注册 + 命令定义
perf-py/server/app.py                          # dataset_repo/service 装配 + dataset 查询缝注入 stress
perf-py/server/stress_repo.py                  # StressTaskModel 四列 + insert_task 可选绑定 + dataset_binding
perf-py/server/stress_service.py               # StressTaskService dataset_lookup 注入 + 绑定解析
perf-py/server/stress_api.py                   # GET /tasks/{id}/dataset 反查端点
sql/init.sql                                   # §12.6 stress_task 四列幂等 ALTER

新建（测试，共 68 例）
perf-py/tests/test_ptde_observability.py       # 14（指标全集/聚合/环形截断/日志字段/脱敏）
perf-py/tests/test_server_dataset_api.py       # 28（落库层/构建编排/READY 终局/清理/端点族）
perf-py/tests/test_ptde_cli.py                 # 19（命令形状/退出码/flag 透传/生产守卫）
perf-py/tests/test_stress_dataset_binding.py   # 7（READY 门禁/绑定快照/反查链路/契约零改动）
```

## 4. 关键设计决策与踩坑记录（承接 B6 §4/§7D）

1. **落库层放 server/ 侧、领域语义仍归 ptde/lifecycle**：repo 只做读写
   映射（状态机/READY 门禁/重复注册判定不落 repo 重复实现）；ptde 包保持
   不依赖 server（依赖方向：server → ptde）。
2. **Instance.id 以 DB 自增主键字符串形态承载**：ptde 模型 str id 与
   `ptde_dataset_instance.id BIGINT` 的映射在 repo 完成（`str(row.id)`）；
   内存层 UUID 形态（B6 DatasetManager）不落库——平台侧统一走数值主键，
   链路一致性（manifest.instance_id == instance.id）在服务层前置校验。
3. **任务 ID = instance id + transition_log 即审计**：构建/验证异步任务
   不另建任务表——实例状态机本身就是任务状态（§12.2 状态集覆盖
   pending/running/终态语义），转换记录（operator/reason/occurred_at）
   持久化即满足 §15.2「任务 ID/异步执行状态/审计」；幂等以执行中
   future 判定（重复请求 accepted=false 不重复排队）。
4. **执行体三段缝而非单段 build**：generate/load/verify 三段与服务层状态
   推进一一对应（每段前后状态机流转落库），终局 READY 八项判定在服务层
   程序化执行——执行体替换（真库接线）不影响门禁语义；
   `UnwiredPipeline` 显式失败（含 `cleanup_executor`），延续「未接线 ≠
   可用」口径。
5. **repo 会话锁序化（本轮踩坑）**：sqlite 内存库 StaticPool 单连接被
   应用线程池与 ptde 工作线程的会话并发使用时，跨线程 commit/rollback
   互相覆盖——五层报告行**非确定性丢失**（首轮脚本化复现：恰好丢
   relationship/distribution 两行且自增 id 跳号）。修复：repo 全部会话块
   经 `threading.Lock` 序化；MySQL（池化连接）下锁为无竞争路径。
   教训：内存库测试 + 多线程写路径必须审视连接池形态（B5/B6 假游标坑
   的连接层姊妹坑）。
6. **压测绑定 wire 契约零改动**：dataset_* 快照只入列不进 `to_camel`——
   stress 契约 golden（键集合比对）保持全绿；绑定可见性走新反查端点
   （QW2 cleanup 端点「非契约内新增」先例延续）。
7. **绑定仅 READY**：压测语义隔离（§9.5 门禁 5 / §15.3）——只有验证通过
   的数据集可承载压测；绑定请求在创建时快照（Definition 版本/Schema 指纹/
   验证摘要），压测报告经反查端点全链可溯（门禁 2）。
8. **API 授权条目 camelCase 归一**：`authorizedBy` → `authorized_by`
   （pydantic extra=forbid 拦下 camelCase 直传；服务层归一而非模型放行，
   保持领域模型字段形态纯净——B4.3 extra=forbid 坑延续）。
9. **CLI 不越权 DDL**：sqlite 本地库便捷 create_all；生产 MySQL 由
   init.sql 建表——CLI 侧 schema 漂移风险显式规避。
10. **指标名全集外显式拒绝**：`ObservabilityRecorder.record` 拒绝未知
    指标名（防调用方私造口径，§16.1 全集即契约）。

## 5. 验收对照（实施方案 §10.3 属本批范围条目）

| 门禁 | 结果 | 落点 |
|---|---|---|
| 1. 数据集可绑定复用：stress 任务创建时引用 DatasetInstance；重复压测可复用同一 Instance | ✅ | `test_bind_ready_dataset_snapshot_recorded`（绑定快照落库）；同一 READY Instance 可被多任务绑定（绑定只读快照不消耗实例） |
| 2. 压测结果可反查：报告 → DatasetInstance → Definition 版本 + Schema 指纹 + 验证结果全链可查 | ✅ | `GET /api/stress/tasks/{id}/dataset`：`test_bind_ready_dataset_snapshot_recorded`（含 definitionId=orders@1 + 五层报告） |
| 3. 任务取消/重试/清理完整可控：状态机内合法流转且审计完整 | 部分 | 清理 ✅（`test_api_cleanup_endpoint_flow` + 三级审计）；**取消/重试**：状态机无 LOADING→CANCELLED 出边（§3.5 转换表既定），恢复走 PARTIAL→GENERATING（manifest_id 强制，`test_resume_requires_partial_and_manifest_id`）——运行中任务的主动取消端点未落（状态机无出边支撑，见 §7 待办） |
| 4. 切换后契约不变 | ✅（本批不动切换） | `/api/datagen` + `perf datagen` 契约测试全绿；T7.6/T7.7 随 §13 两道门禁通过后切换 |
| 5. 监控指标可查；日志字段齐全且无敏感值 | 部分 | 指标/日志组件已就绪（`test_ptde_observability.py` 14 例，含脱敏抽检 `test_emit_log_record_has_no_secret`）；**外部观测体系接线**（指标上报/日志采集管道）未落——进程内 recorder 先行，接线随部署形态定 |

## 6. 回归保障与回滚（§10.4）

- **契约零改动**：`/api/datagen` 与 `perf datagen` 全部契约测试 + golden
  快照全绿；stress wire 形态不含 dataset_* 新列（`test_unbound_task_wire_contract_unchanged`）；
- legacy `datagen/` 零修改；ptde 既有模块零修改（observability 为纯新增）；
  server 侧仅最小接线（app 装配 + stress 三处可选扩展，全部缺省 None 路径
  行为不变——既有 20+ 例 stress/collector 测试零改动通过）；
- 回滚：删除 §3 所列 5 个新模块 + 4 个新测试文件，还原 app.py/main.py/
  stress 三件/init.sql 对应段落即可；ptde 三表为幂等建表，无破坏性 DDL。

## 7. 后续批次衔接（B7 续 / 后续，供下一 session 使用）

- **本批如实记录的未闭环项**：
  - **运行中任务主动取消端点**：状态机 LOADING→CANCELLED 无出边（§3.5
    转换表既定口径），须先模型层变更再落 API——挂起待状态机评审；
  - **API 请求超时控制**（§15.2「请求超时」）：端点本身快速返回（异步
    任务化），长任务超时中断属执行体/编排层语义，随真库执行体接线一并落；
  - **外部观测体系接线**：`ObservabilityRecorder` 为进程内组件，指标上报
    （Prometheus/日志管道）随部署形态落地；日志脱敏已就绪；
  - **selfCheck 端点抽检回调接线**（datagen_service → endpoint_probe 适配器）：
    维持 B6 §7F 口径，随 T7.2 后续批落；
  - **R1/R2 QW2 清理收编为 T6.8 完整版子项**：协议就位（本批 cleanup 走
    ptde 完整版），legacy 最小版退役随 A4a 接管。
- **T7.5 分布式 Worker**：按需预留（单机吞吐不足时启用，总方案 §14.2），
  本期默认不实施（方案既定）。
- **T7.6~T7.9（legacy 接管/迁移/退役）**：被 §13 门禁 2（真库双跑对照）
  前置——**需真库环境批次**，与 R5 §13 真库装载基准归档同批推进；
  接管时 CLI/API 执行体缝换真库管线（fake 层已验证协议兼容）。
- **B7 续可立即推进项**（不依赖真库）：
  - dataset 构建执行体的离线组装（generator worker + staging + orchestrator
    三段实现落到 `pipeline` 缝——生成侧映射知识在生成侧，编排协议 B6 §7A
    已定，可在 fake DB 通道上先组装出可测执行体）；
  - `ptde_execution_manifest` 表 repo（本批 manifest 仅经服务层校验，
    未落 manifest 表——READY 条件 7 的持久化侧）；
  - 压测绑定扩展：scenario 模式请求体的 dataset 透传对齐、perf-web 绑定
    表单（前端零改动验收面）。
- **存量迁移脚本**（T7.8，§12.5）：依赖 T7.6/T7.7 切换前置，脚本本体可
  提前按幂等可重放口径开发（以 legacy_* 字段为去重键）。

---

*B7 前段交接完成：落库层 + CLI + API + 压测绑定 + 可观测性落地，真库执行体缝就位。*
