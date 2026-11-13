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
| `fix_suggestion` | 修复方案（每个问题 2-3 条） | 含 prompt 文本，新增 |
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

## 7. 演示数据

- app_instance：1 条演示应用（perf-demo）；
- 若干 JVM 指标 / 调用链 / 慢 SQL 演示记录，便于前端首屏展示；
- 1 条示例 performance_issue（关联 perf-demo 源码位置）与 2 条 fix_suggestion。
