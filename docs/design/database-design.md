# PerformanceTestor 数据库设计说明

> 生成日期：2026-08-27　模块：perf-server　状态：已定稿
> 详细建表 DDL 见 `sql/init.sql`（幂等脚本，含演示数据）。

## 1. 设计原则

- MySQL 单库承载，避免引入额外时序中间件；
- 时序类数据（JVM 指标、压测采样）按天分区，便于归档与清理；
- 调用链以 JSON 树存储（trace_json），分析时直接反序列化；
- 容量评估结果随压测任务结果 JSON 字段落库。

## 2. 核心表清单

| 表名 | 用途 | 备注 |
| ---- | ---- | ---- |
| `app_instance` | 被测应用实例（agent 注册） | agent 首次上报自动注册；含 app_host / app_port（压测表单服务地址自动预填） |
| `jvm_metric` | JVM 指标时序数据（堆/非堆/GC/线程/CPU） | 按天分区（collect_time） |
| `trace_chain` | 业务方法调用链 | trace_json 存储调用树 |
| `slow_sql` | 慢 SQL 记录（JDBC 拦截 + 慢日志解析双通道） | source 字段区分来源 |
| `db_conn` | 数据库连接配置（慢 SQL 分析 EXPLAIN / 灌数据复用） | 新增 |
| `stress_task` | 压测任务 | 含配置、状态、结果 JSON |
| `stress_sample` | 压测周期采样（QPS/RT/错误率） | 按天分区（sample_time） |
| `data_gen_task` | 灌数据任务记录 | 含目标表、行数、状态 |
| `source_repo` | 源码仓库（zip/git/本地路径导入记录） | 新增 |
| `source_file` | 源码文件索引 | 文件存磁盘，表存元数据，新增 |
| `performance_issue` | 性能问题（关联检测证据与源码位置） | 新增 |
| `fix_suggestion` | 修复方案（每个问题 2-3 条） | 含 prompt 文本 + 修复回写字段（applied_at/apply_ref/apply_type/verify_task_id） |
| `stress_template` | 压测模板（常用场景参数沉淀） | 新增，`app_name=''` = 通用模板 |
| `chat_message` | AI 对话消息记录 | 可选，用于对话历史 |

## 3. 新增五张表设计要点

### source_repo（源码仓库）
- 字段：id、name、type（zip/git/local）、source_path（磁盘路径或 git url）、status、file_count、total_size、create_time；
- 约束：总量 ≤200MB、文件数 ≤5000（导入时校验）。

### source_file（源码文件索引）
- 字段：id、repo_id、relative_path、file_name、language、size、content_hash、create_time；
- 约束：单文件 ≤1MB；
- 文件内容存磁盘，表内仅存索引与元数据；按需懒加载读取。

### performance_issue（性能问题）
- 字段：id、issue_type（slow_sql/hot_method/stress_bottleneck/jvm_alert）、app_name、evidence_json（检测证据）、source_repo_id、source_file_id、line_start/line_end（源码位置）、root_cause、status（pending/analyzed/fixed）、create_time；
- IssueAssociator 完成检测结果与源码的关联定位后写入。

### fix_suggestion（修复方案）
- 字段：id、issue_id、title、files_json（涉及文件）、changes_json（修改点）、code_changes_json（代码改动要点）、benefit、risk、prompt_md（修复提示词）、selected（是否被用户选定）、create_time；
- 每个问题 2-3 条方案，用户选定后生成 prompt_md。
- **修复回写字段**（P2）：applied_at（回写时间，NULL = 未回写）、apply_ref（分支名 / PR 链接 / 补丁摘要，≤512）、apply_type（branch / pr / patch）、verify_task_id（回写后自动触发的重放验证任务，0 = 无）；
- 老库升级由 `add_column_if_missing('fix_suggestion', ...)` 幂等补列，新库直接在建表语句里定义。

### stress_template（压测模板）
- 字段：id、template_name、app_name（空 = 通用模板，任何项目可见）、task_json（StressTaskRequest 参数子集）、description、creator、create_time、update_time；
- 索引 `idx_app_time (app_name, create_time)`；
- 列表查询按应用过滤时**同时返回通用模板**（`app_name = #{appName} OR app_name = ''`），与知识库检索语义一致；
- `task_json` 在保存时校验为合法 JSON 对象且体积 ≤64KB（存坏 JSON 会让"从模板填充"时才报错，用户已无从定位）；
- 模板只是参数快照，**不绕过压测校验**：真正发起时仍走 `StressTaskService.validate`。

### db_conn（数据库连接配置）
- 字段：id、conn_name、host（host:port/db，与慢 SQL dbHost 匹配）、app_name（绑定应用，空=不限）、db_url（仅允许 jdbc:mysql://）、username、password、is_default（默认连接）、create_time；
- 用途：慢 SQL 分析时对执行计划跑 EXPLAIN、灌数据任务复用连接配置；
- 安全：内网演示明文存储，生产环境应加密；agent 上报自动按 host/appName 关联匹配连接。

## 4. 分区策略与数据保留

分区的**创建与回收由 `PartitionMaintainer` 自动完成**（原先建表脚本里的
`create_daily_partition` 存储过程全仓库无调用，分区会全部退化进 `p_max`）：

- `jvm_metric`：`RANGE COLUMNS (collect_time)` 按天分区，分区名 `pYYYYMMDD`；
- `stress_sample`：`RANGE COLUMNS (sample_time)` 按天分区，分区名 `pYYYYMMDD`；
- 两者均保留 `p_max VALUES LESS THAN (MAXVALUE)` 兜底分区，缺分区时不会写入失败。

`PartitionMaintainer` 行为（配置见 `application.yml` → `perf.retention.*`）：

| 动作 | 说明 |
| ---- | ---- |
| 补分区 | 启动时执行一次 + 每 `interval-hours`（默认 1h）一次，为未来 `ahead-days`（默认 2）天补齐日分区；`p_max` 存在时若 `ADD PARTITION` 被拒则改走 `REORGANIZE p_max INTO (新分区, p_max)` |
| 删分区 | 保留期 `partition-days`（默认 30，`0` = 不清理）：只 `DROP` **整分区数据全部过期**的日分区。保留窗口定义为「今天起往前数 keepDays 个完整日期」，即最早保留 `today-(keepDays-1)`，故第 keepDays 天的分区仍保留 |
| 普通表清理 | `trace_chain` / `slow_sql` / `slow_command` 无分区，按 `trace-chain-days` / `slow-sql-days` / `slow-command-days`（默认 30，`0` = 不清理）分批 `DELETE ... LIMIT delete-batch-size`，每轮每表至多 `max-batches-per-run` 批，避免长事务阻塞上报写入 |

安全性：全部 SQL try/catch 且只告警不抛出；表名/列名来自代码内白名单常量，分区名经 `^p\d{8}$` 校验后拼接（DDL 不支持参数化标识符），时间边界与批大小一律 `#{}` 参数化（见 `RetentionSqlScriptTest`）。首次上线建议先保持 `partition-days: 0`（只建分区不删），观察一轮后再开启回收。

### 项目删除（P2）

`DELETE /api/source/repos/{repoId}`（`ProjectLifecycleService`）编排三件事，顺序固定：

1. **关联历史数据清理**（可选，`cleanData` 默认 true）：复用 `cleanupByAppName(appName, false)`——
   全局 AI 结论（`app_name=''`）始终保留，原因与上一节同一语义：它不隶属于任何项目；
2. **删源码文件索引 + 仓库记录**；
3. **删磁盘源码目录**（`storage-dir/<sanitizeDirName(repoName)>-<repoId>`）。

前置校验：请求体 `repoName` 必须与项目名**完全一致**（trim 后比较，二次确认防误删）；
该项目绑定的应用存在进行中压测（`pending`/`running`）时拒绝删除——否则压测线程会继续往已删项目写数据。

**路径穿越防护**：仓库名来自用户输入，`resolveRepoTarget` 在解析后校验目标严格位于存储根目录内，
且**不等于根目录本身**（否则一次误调用会删掉整块存储）。`GET /repos/{repoId}/delete-preview`
返回将删除的影响面（文件数 / 总大小 / 绑定应用 / 进行中压测数），供前端确认弹窗展示。

**事务边界**：数据库三步中 `DataCleanupService` 自身带事务、索引与记录删除为单语句原子操作；
磁盘目录删除**刻意放在数据库之后**（失败只告警不回滚）——回滚会留下"记录回滚了但目录已删"
的不一致，而"记录已删、目录残留"可由运维手工清理。

### 项目数据清理与全局 AI 结论

`POST /api/source/repos/{repoId}/cleanup-data`（`DataCleanupService#cleanupByAppName`）按 `app_name`
删除该项目的采集与派生数据，覆盖：`stress_sample` / `stress_task` / `fix_suggestion` /
`performance_issue` / `trace_chain` / `jvm_metric` / `slow_sql` / `slow_command` /
`app_instance` / `db_conn` / `ai_knowledge`。

- `ai_knowledge` 中 `app_name=''` 的**全局结论（跨项目共性经验）默认保留**，返回计数拆
  `aiKnowledge`（本应用）与 `aiKnowledgeGlobal`（全局，未开启恒为 0）；
- 需连带删除全局结论时传 `includeGlobalKnowledge=true` **且** `confirmAppName` 回显项目绑定的
  appName 做二次确认，后端另打 `warn` 审计日志（操作人 / appName / 条数）；
- `chat_message` 表无 `app_name` 列，**补列前不纳入清理**（避免误删跨应用会话）。

## 5. 列表查询游标分页

时序列表（`/api/traces`、`/api/slow-sqls`、`/api/slow-commands`、`/api/jvm/metrics`）原先只有
`LIMIT n` 硬顶（500/5000），历史数据一多既翻不了页也看不到总数。现统一改为**游标分页**。

返回体 `{ items, nextCursor, hasMore }`；请求参数 `cursor`（上一页原样回传）+ `size`
（默认 50，上限 500），旧 `limit` 仅在未传 `size` 时生效，保证存量前端不改也能用。

### 为什么是游标而不是 OFFSET

时序表持续写入，`OFFSET` 深翻有两个硬伤：翻页越深扫描行数越多（`LIMIT 500 OFFSET 50000` 要
先丢掉前 5 万行）；翻页期间新数据插入会让窗口整体后移，导致**漏读或重读**。

### 复合游标 `(time, id)`

| 表的排序键 | 说明 |
| ---- | ---- |
| `trace_chain` | `(start_time, id)` |
| `jvm_metric` | `(collect_time, id)` |
| `slow_sql` / `slow_command` | `(execute_time, id)` |

游标条件固定为 `(time < #{cursorTime} OR (time = #{cursorTime} AND id < #{cursorId}))`，
`ORDER BY time DESC, id DESC`。**不能简化成 `time < ?`**：时间列是 `DATETIME`（秒精度），
同一秒内的多条记录靠时间无法定序，只比较时间会整批跳过它们。

游标值是 `Base64Url("epochMillis|id")`（毫秒而非格式化字符串：唯一能跨时区、跨驱动精度无损
往返的形式）。非法游标一律按"无游标 = 首屏"处理，不返回 400——前端刷新/手改 URL 不该变成页面报错。

### 两处刻意的口径差异

- `jvm_metric` 的图表查询 `query` 保持 `ORDER BY collect_time ASC`（图表要正序画线），
  分页查询 `queryByCursor` 走 `DESC`（列表看最新）。两条语句分开，互不污染。
- `slow_sql` / `slow_command` 的分页按 **执行时间** 倒序，而非页面热点的 **耗时** 排名：
  耗时可被后到的慢 SQL 反超，用它当游标键会漏读/重读。

约束由 `CursorPagingSqlScriptTest` 静态回查（复合条件、排序一致、全参数化），
边界语义由 `MetricQueryServicePagingTest` 覆盖（多取一条判 `hasMore`、末页游标为 null、
游标取"实际返回的最后一条"而非多取的那条）。

## 6. 健康检查与启动自检

- **运行期**：引入 `spring-boot-starter-actuator`，`management.endpoints.web.exposure.include`
  白名单只放 `health,info,metrics`（`env`/`heapdump` 等敏感端点不暴露）；
  自定义 `PlatformHealthIndicator`（bean 名 `platform`）在 `/actuator/health` 下输出
  `database` / `storage` 细节，DB 不可达或磁盘余量低于 `perf.health.min-free-disk-mb` 即 `DOWN`。
- **启动期**：`StartupSelfCheck`（`ApplicationRunner`）就绪后体检一次：库连通与版本
  （低于 `min-mysql-version` 说明分区维护不可靠）、必需表存在（缺失 → 提示执行
  `sql/init.sql`）、分区表确实建了分区、源码目录可写、磁盘余量。
  默认**不阻断启动**（容器编排下平台常先于 MySQL 启动，fail-fast 会造成循环依赖），
  需要严格把关时置 `perf.health.fail-fast=true`。

AI 连通性不进健康检查：无 `api-key` 是正常部署形态（自动降级规则模式），
计入会让健康检查常态化误报。

## 7. 修复回写与闭环验证（P2）

原闭环终点是「生成修复提示词」，之后靠人工复制到 IDE、再手工点一次验证，
中间"改了什么、改在哪、验证的是哪一版"没有任何记录。`POST /api/source/issues/{issueId}/apply-fix`
（`FixApplyService`）补上这一段：

1. 记录修复来源：`applyType`（`branch` / `pr` / `patch`）+ `applyRef`（分支名 / PR 链接 / 补丁摘要）；
2. 把对应 `fix_suggestion` 标记为已回写（`applied_at` / `apply_ref` / `apply_type`）；
3. 复用 `IssueVerifyService` 自动触发一次重放验证，`verify_task_id` 落库。

关键语义：

| 场景 | 行为 |
| ---- | ---- |
| 同问题 + 同来源类型 + 同来源标识 重复提交 | **幂等**：直接返回既有记录，不重复触发重放压测（一次重放就是对目标服务打一轮真实压力） |
| 换来源（又改了一版，如换 PR） | 视为新一版：更新记录并重新触发验证 |
| 问题未关联压测场景快照（`stress_task_id = 0`） | 只回写记录，`verify_task_id=0`，消息说明"未关联快照，无法自动验证" |
| 验证触发异常（线程池满 / 引擎异常） | **回写照常落库**，`verify_task_id=0`，消息记录异常原因——"改了什么"比"验证结论"更基础 |
| `suggestionId` 缺省 | 依次取：该问题已选定方案 → 首条方案；都没有则拒绝（无处记录来源） |

来源标识是外部输入且列宽 512，超长（如整份补丁）截断为摘要后落库，避免插入报错。

## 8. 演示数据

- app_instance：1 条演示应用（perf-demo）；
- 若干 JVM 指标 / 调用链 / 慢 SQL 演示记录，便于前端首屏展示；
- 1 条示例 performance_issue（关联 perf-demo 源码位置）与 2 条 fix_suggestion。
