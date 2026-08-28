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

## 4. 分区策略

- `jvm_metric`：按 `collect_time` RANGE 按天分区，保留 N 天（可配）；
- `stress_sample`：按 `sample_time` RANGE 按天分区。

## 5. 演示数据

- app_instance：1 条演示应用（perf-demo）；
- 若干 JVM 指标 / 调用链 / 慢 SQL 演示记录，便于前端首屏展示；
- 1 条示例 performance_issue（关联 perf-demo 源码位置）与 2 条 fix_suggestion。
