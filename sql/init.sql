-- =====================================================================
-- PerformanceTestor 数据库初始化脚本（幂等，可重复执行）
-- 说明：
--   1. 全部使用 CREATE TABLE IF NOT EXISTS，可重复执行；
--   2. 时序表（jvm_metric / stress_sample）使用 RANGE COLUMNS 按天分区，
--      末尾保留 MAXVALUE 兜底分区，保证任意时间数据可写入；
--   3. 末尾插入演示数据，便于前端首屏展示与端到端联调；
--   4. 含中文演示数据：导入必须显式指定客户端字符集，否则中文双重编码乱码：
--      mysql --default-character-set=utf8mb4 -u root -p < init.sql
-- =====================================================================

CREATE DATABASE IF NOT EXISTS performance_testor DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
USE performance_testor;

-- ---------------------------------------------------------------------
-- 1. app_instance：被测应用实例（agent 首次上报自动注册）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_instance (
    id            BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    app_name      VARCHAR(128)  NOT NULL COMMENT '应用名称',
    app_host      VARCHAR(255)  NOT NULL DEFAULT '' COMMENT '应用所在主机（agent 上报来源）',
    app_port      INT           NULL COMMENT '应用 HTTP 端口（agent 上报，压测表单服务地址自动预填用）',
    app_version   VARCHAR(64)   NOT NULL DEFAULT '' COMMENT '应用版本号（agent 上报或探测 jar MANIFEST，压测任务快照记录）',
    agent_version VARCHAR(32)   NOT NULL DEFAULT '' COMMENT 'agent 版本',
    status        TINYINT       NOT NULL DEFAULT 1 COMMENT '状态：1 在线 0 离线',
    last_report_time DATETIME   NULL COMMENT '最近上报时间',
    create_time   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_app_name (app_name)
) ENGINE = InnoDB COMMENT = '被测应用实例';

-- 兼容旧库：app_port / app_version 列不存在时补充（幂等，MySQL/MariaDB 通用）
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'app_instance' AND COLUMN_NAME = 'app_port');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE app_instance ADD COLUMN app_port INT NULL COMMENT ''应用 HTTP 端口（agent 上报，压测表单服务地址自动预填用）'' AFTER app_host',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'app_instance' AND COLUMN_NAME = 'app_version');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE app_instance ADD COLUMN app_version VARCHAR(64) NOT NULL DEFAULT '''' COMMENT ''应用版本号（agent 上报或探测 jar MANIFEST，压测任务快照记录）'' AFTER app_port',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---------------------------------------------------------------------
-- 2. jvm_metric：JVM 指标时序数据（按天分区）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jvm_metric (
    id               BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    app_name         VARCHAR(128)  NOT NULL COMMENT '应用名称',
    collect_time     DATETIME      NOT NULL COMMENT '采集时间',
    heap_used_mb     DOUBLE        NOT NULL DEFAULT 0 COMMENT '堆已用 MB',
    heap_max_mb      DOUBLE        NOT NULL DEFAULT 0 COMMENT '堆最大值 MB',
    non_heap_used_mb DOUBLE        NOT NULL DEFAULT 0 COMMENT '非堆已用 MB',
    young_gc_count   BIGINT        NOT NULL DEFAULT 0 COMMENT 'Young GC 次数',
    young_gc_ms      BIGINT        NOT NULL DEFAULT 0 COMMENT 'Young GC 累计耗时 ms',
    full_gc_count    BIGINT        NOT NULL DEFAULT 0 COMMENT 'Full GC 次数',
    full_gc_ms       BIGINT        NOT NULL DEFAULT 0 COMMENT 'Full GC 累计耗时 ms',
    thread_count     INT           NOT NULL DEFAULT 0 COMMENT '线程数',
    cpu_usage        DOUBLE        NOT NULL DEFAULT 0 COMMENT '进程 CPU 使用率 %',
    system_cpu       DOUBLE        NOT NULL DEFAULT 0 COMMENT '系统 CPU 使用率 %',
    extra_metrics    TEXT          NULL COMMENT 'P1-5 扩展指标 JSON（连接池/容器线程池/直接内存/Metaspace/线程状态，旧探针为 NULL）',
    PRIMARY KEY (id, collect_time),
    KEY idx_app_time (app_name, collect_time)
) ENGINE = InnoDB COMMENT = 'JVM 指标时序数据'
PARTITION BY RANGE COLUMNS (collect_time) (
    PARTITION p20260801 VALUES LESS THAN ('2026-08-02 00:00:00'),
    PARTITION p20260827 VALUES LESS THAN ('2026-08-28 00:00:00'),
    PARTITION p20260828 VALUES LESS THAN ('2026-08-29 00:00:00'),
    PARTITION p20260829 VALUES LESS THAN ('2026-08-30 00:00:00'),
    PARTITION p20260830 VALUES LESS THAN ('2026-08-31 00:00:00'),
    PARTITION p_max VALUES LESS THAN (MAXVALUE)
);

-- ---------------------------------------------------------------------
-- 3. trace_chain：业务方法调用链
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trace_chain (
    id           BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    trace_id     VARCHAR(64)   NOT NULL COMMENT '链路 ID（P2-2 W3C：32 位 hex，微服务共享）',
    app_name     VARCHAR(128)  NOT NULL COMMENT '应用名称',
    span_id      VARCHAR(32)   NOT NULL DEFAULT 'root' COMMENT '本服务 span ID（P2-2，旧探针缺省 root）',
    parent_span_id VARCHAR(32) NULL COMMENT '上游服务 span ID（P2-2，无上游为 NULL）',
    root_method  VARCHAR(512)  NOT NULL DEFAULT '' COMMENT '根方法（类#方法）',
    trace_json   MEDIUMTEXT    NOT NULL COMMENT '调用树 JSON（TraceNode 树，含每层耗时与占比）',
    total_ms     DOUBLE        NOT NULL DEFAULT 0 COMMENT '总耗时 ms',
    start_time   DATETIME      NOT NULL COMMENT '链路开始时间',
    stress_task_id BIGINT      NULL COMMENT '触发压测任务 ID（压测流量染色，非压测流量为 NULL）',
    PRIMARY KEY (id),
    UNIQUE KEY uk_trace_span (trace_id, app_name, span_id),
    KEY idx_app_time (app_name, start_time, id),
    KEY idx_stress_task (stress_task_id, start_time)
) ENGINE = InnoDB COMMENT = '业务方法调用链';

-- ---------------------------------------------------------------------
-- 4. slow_sql：慢 SQL 记录（JDBC 拦截 + 慢查询日志解析双通道）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS slow_sql (
    id            BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    app_name      VARCHAR(128)  NOT NULL COMMENT '应用名称',
    sql_text      MEDIUMTEXT    NOT NULL COMMENT 'SQL 文本（已脱敏，参数以 ? 占位）',
    cost_ms       DOUBLE        NOT NULL DEFAULT 0 COMMENT '执行耗时 ms',
    source        VARCHAR(32)   NOT NULL DEFAULT 'jdbc' COMMENT '来源：jdbc 拦截 / slowlog 日志',
    db_host       VARCHAR(255)  NOT NULL DEFAULT '' COMMENT '数据库地址（慢日志解析来源）',
    method_chain  TEXT          NULL COMMENT '触发方法链 JSON 数组（根 → 叶，如 Controller → Service → Mapper）',
    execute_time  DATETIME      NOT NULL COMMENT '执行时间',
    sql_fingerprint VARCHAR(16) NULL COMMENT 'SQL 指纹（P2-2：字面量归一化模板 SHA-1 前 16 位，旧探针为 NULL）',
    stress_task_id BIGINT      NULL COMMENT '触发压测任务 ID（压测流量染色，非压测流量为 NULL）',
    PRIMARY KEY (id),
    KEY idx_app_time (app_name, execute_time, id),
    KEY idx_stress_task (stress_task_id, execute_time),
    KEY idx_cost (cost_ms),
    KEY idx_fingerprint (sql_fingerprint, execute_time)
) ENGINE = InnoDB COMMENT = '慢 SQL 记录';

-- ---------------------------------------------------------------------
-- 4.0 slow_command：中间件慢命令（agent 拦截 Redis / ES / RabbitMQ）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS slow_command (
    id            BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    app_name      VARCHAR(128)  NOT NULL COMMENT '应用名称',
    type          VARCHAR(16)   NOT NULL COMMENT '中间件类型：redis / es / mq',
    command       VARCHAR(1024) NOT NULL DEFAULT '' COMMENT '命令文本（方法名 + 参数，已截断）',
    cost_ms       DOUBLE        NOT NULL DEFAULT 0 COMMENT '执行耗时 ms',
    trace_id      VARCHAR(64)   NULL COMMENT '关联调用链 ID（未采样时为空）',
    method_chain  TEXT          NULL COMMENT '触发方法链 JSON 数组（根 → 叶）',
    execute_time  DATETIME      NOT NULL COMMENT '执行时间',
    stress_task_id BIGINT      NULL COMMENT '触发压测任务 ID（压测流量染色，非压测流量为 NULL）',
    PRIMARY KEY (id),
    KEY idx_app_time (app_name, execute_time, id),
    KEY idx_stress_task (stress_task_id, execute_time),
    KEY idx_type (type, execute_time),
    KEY idx_trace (trace_id),
    KEY idx_cost (cost_ms)
) ENGINE = InnoDB COMMENT = '中间件慢命令记录';

-- ---------------------------------------------------------------------
-- 4.1 hotspot_method：方法级 CPU 热点（agent HotspotSampler 弱采样聚合上报）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hotspot_method (
    id           BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    app_name     VARCHAR(128) NOT NULL COMMENT '应用名称',
    method_name  VARCHAR(512) NOT NULL COMMENT '方法全限定名（class#method）',
    samples      BIGINT       NOT NULL DEFAULT 0 COMMENT '采样命中次数（周期内帧命中计数）',
    sample_time  DATETIME     NOT NULL COMMENT '聚合采样时间',
    PRIMARY KEY (id),
    KEY idx_app_time (app_name, sample_time)
) ENGINE = InnoDB COMMENT = '方法级 CPU 热点';

-- ---------------------------------------------------------------------
-- 4.1.1 profile_snapshot：JFR 连续剖析折叠栈快照（P2-1，cpu / alloc / lock）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS profile_snapshot (
    id             BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    app_name       VARCHAR(128) NOT NULL COMMENT '应用名称',
    stress_task_id BIGINT       NULL COMMENT '压测任务 ID（profiler 开启后随染色流量周期对齐，非压测期为 NULL）',
    profile_type   VARCHAR(16)  NOT NULL COMMENT '剖析维度：cpu / alloc / lock',
    folded_stacks  MEDIUMTEXT   NOT NULL COMMENT '标准折叠栈（每行 frame1;frame2;... count）',
    total_samples  BIGINT       NOT NULL DEFAULT 0 COMMENT '本批样本总量（cpu=次数 / alloc=分配字节 / lock=阻塞毫秒）',
    sample_time    DATETIME     NOT NULL COMMENT '聚合采样时间',
    PRIMARY KEY (id),
    KEY idx_profile_app_type_time (app_name, profile_type, sample_time),
    KEY idx_profile_stress_task (stress_task_id, profile_type)
) ENGINE = InnoDB COMMENT = 'JFR 连续剖析折叠栈快照';

-- ---------------------------------------------------------------------
-- 4.2 stress_schedule：定时压测调度（周期执行 + 版本基线对比 + webhook 回归告警）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stress_schedule (
    id              BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    schedule_name   VARCHAR(128) NOT NULL COMMENT '调度名称',
    task_json       TEXT         NOT NULL COMMENT '压测任务模板 JSON（StressTaskRequest）',
    interval_minutes INT         NOT NULL DEFAULT 60 COMMENT '执行间隔（分钟）',
    enabled         TINYINT      NOT NULL DEFAULT 1 COMMENT '是否启用：1 是 0 否',
    baseline_task_id BIGINT      NOT NULL DEFAULT 0 COMMENT '基线任务（0=自动取同场景最近完成）',
    webhook_url     VARCHAR(512) NOT NULL DEFAULT '' COMMENT '告警 webhook（钉钉/飞书/自定义）',
    last_task_id    BIGINT       NOT NULL DEFAULT 0 COMMENT '上次触发任务（完成后做基线对比）',
    last_run_time   DATETIME     NULL COMMENT '上次触发时间',
    next_run_time   DATETIME     NULL COMMENT '下次触发时间',
    create_time     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    KEY idx_enabled_next (enabled, next_run_time)
) ENGINE = InnoDB COMMENT = '定时压测调度';

-- 4.5 db_conn：数据库连接配置（供慢 SQL 分析时执行 EXPLAIN 等检测语句）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS db_conn (
    id           BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    conn_name    VARCHAR(128) NOT NULL COMMENT '连接名称（如 perf-demo-主库）',
    host         VARCHAR(255) NOT NULL DEFAULT '' COMMENT '主机标识（host:port/db，与慢 SQL dbHost 匹配）',
    app_name     VARCHAR(128) NOT NULL DEFAULT '' COMMENT '绑定应用（与慢 SQL appName 匹配，空=不限应用）',
    db_url       VARCHAR(512) NOT NULL COMMENT 'JDBC URL（仅允许 jdbc:mysql://）',
    username     VARCHAR(128) NOT NULL COMMENT '用户名',
    password     VARCHAR(256) NOT NULL DEFAULT '' COMMENT '密码（内网演示明文存储，生产环境应加密）',
    is_default   TINYINT      NOT NULL DEFAULT 0 COMMENT '是否默认连接：1 是 0 否',
    create_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id)
) ENGINE = InnoDB COMMENT = '数据库连接配置';

-- ---------------------------------------------------------------------
-- 5. stress_task：压测任务
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stress_task (
    id            BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    task_name     VARCHAR(128)  NOT NULL COMMENT '任务名称',
    engine_type   VARCHAR(32)   NOT NULL DEFAULT 'http' COMMENT '引擎类型：http / jmeter',
    target_url    VARCHAR(512)  NOT NULL COMMENT '压测目标 URL',
    app_name      VARCHAR(128)  NOT NULL DEFAULT '' COMMENT '归属应用（agent 上报应用名）',
    app_version   VARCHAR(64)   NOT NULL DEFAULT '' COMMENT '发起压测时的应用版本快照（agent 上报，版本间比对）',
    method        VARCHAR(16)   NOT NULL DEFAULT 'GET' COMMENT 'HTTP 方法',
    concurrency   INT           NOT NULL DEFAULT 1 COMMENT '并发数',
    duration_sec  INT           NOT NULL DEFAULT 60 COMMENT '持续时长秒',
    qps_limit     INT           NOT NULL DEFAULT 0 COMMENT 'QPS 上限（0 不限制）',
    status        VARCHAR(32)   NOT NULL DEFAULT 'pending' COMMENT '状态：pending/running/finished/failed',
    result_json   MEDIUMTEXT    NULL COMMENT '结果 JSON（含统计、容量评估、扩容建议）',
    error_msg     TEXT          NULL COMMENT '失败信息',
    create_time   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    finish_time   DATETIME      NULL COMMENT '完成时间',
    PRIMARY KEY (id),
    KEY idx_status_time (status, create_time)
) ENGINE = InnoDB COMMENT = '压测任务';

-- 兼容旧库：stress_task.app_version 列不存在时补充（幂等）
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'stress_task' AND COLUMN_NAME = 'app_version');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE stress_task ADD COLUMN app_version VARCHAR(64) NOT NULL DEFAULT '''' COMMENT ''发起压测时的应用版本快照（agent 上报，版本间比对）'' AFTER app_name',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- T7.3 压测绑定（实施方案 §12.6）：stress_task 幂等加列——
-- 压测任务记录 DatasetInstance ID + Definition 版本 + Schema 指纹 + 验证结果摘要
-- （§10.3 门禁 1/2：数据集可绑定复用、压测结果可反查数据集全链）
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'stress_task' AND COLUMN_NAME = 'dataset_instance_id');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE stress_task ADD COLUMN dataset_instance_id BIGINT UNSIGNED NULL COMMENT ''绑定的 PTDE 数据集实例''',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'stress_task' AND COLUMN_NAME = 'dataset_definition_version');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE stress_task ADD COLUMN dataset_definition_version VARCHAR(32) NULL COMMENT ''绑定时的 Definition 版本''',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'stress_task' AND COLUMN_NAME = 'dataset_schema_fingerprint');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE stress_task ADD COLUMN dataset_schema_fingerprint CHAR(64) NULL COMMENT ''绑定时的 Schema 指纹''',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'stress_task' AND COLUMN_NAME = 'dataset_validation_json');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE stress_task ADD COLUMN dataset_validation_json MEDIUMTEXT NULL COMMENT ''绑定时的验证结果摘要''',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- P0-4 修复验证重放：stress_task.request_json 完整请求快照列（幂等）
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'stress_task' AND COLUMN_NAME = 'request_json');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE stress_task ADD COLUMN request_json MEDIUMTEXT NULL COMMENT ''创建时的完整压测请求快照（修复验证重放还原用）'' AFTER result_json',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- P1-5 扩展指标：jvm_metric.extra_metrics 幂等加列（可选，旧探针为 NULL）
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'jvm_metric' AND COLUMN_NAME = 'extra_metrics');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE jvm_metric ADD COLUMN extra_metrics TEXT NULL COMMENT ''P1-5 扩展指标 JSON（连接池/容器线程池/直接内存/Metaspace/线程状态，旧探针为 NULL）'' AFTER system_cpu',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- P1-1 压测流量染色：trace_chain / slow_sql / slow_command 三表幂等加 stress_task_id 列（可选，旧探针为 NULL）
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'trace_chain' AND COLUMN_NAME = 'stress_task_id');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE trace_chain ADD COLUMN stress_task_id BIGINT NULL COMMENT ''触发压测任务 ID（压测流量染色，非压测流量为 NULL）'' AFTER start_time',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'slow_sql' AND COLUMN_NAME = 'stress_task_id');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE slow_sql ADD COLUMN stress_task_id BIGINT NULL COMMENT ''触发压测任务 ID（压测流量染色，非压测流量为 NULL）'' AFTER execute_time',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'slow_command' AND COLUMN_NAME = 'stress_task_id');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE slow_command ADD COLUMN stress_task_id BIGINT NULL COMMENT ''触发压测任务 ID（压测流量染色，非压测流量为 NULL）'' AFTER execute_time',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- P1-1 压测流量染色：三表按 (stress_task_id, <time_col>) 幂等建索引
SET @idx_exists := (SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'trace_chain' AND INDEX_NAME = 'idx_stress_task');
SET @ddl := IF(@idx_exists = 0,
    'ALTER TABLE trace_chain ADD INDEX idx_stress_task (stress_task_id, start_time)',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @idx_exists := (SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'slow_sql' AND INDEX_NAME = 'idx_stress_task');
SET @ddl := IF(@idx_exists = 0,
    'ALTER TABLE slow_sql ADD INDEX idx_stress_task (stress_task_id, execute_time)',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @idx_exists := (SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'slow_command' AND INDEX_NAME = 'idx_stress_task');
SET @ddl := IF(@idx_exists = 0,
    'ALTER TABLE slow_command ADD INDEX idx_stress_task (stress_task_id, execute_time)',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- P2-2 微服务串联：trace_chain span 列幂等补列（旧探针数据缺省 root）
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'trace_chain' AND COLUMN_NAME = 'span_id');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE trace_chain ADD COLUMN span_id VARCHAR(32) NOT NULL DEFAULT ''root'' COMMENT ''本服务 span ID（P2-2，旧探针缺省 root）'' AFTER app_name',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'trace_chain' AND COLUMN_NAME = 'parent_span_id');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE trace_chain ADD COLUMN parent_span_id VARCHAR(32) NULL COMMENT ''上游服务 span ID（P2-2，无上游为 NULL）'' AFTER span_id',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
-- P2-2 唯一键升级：uk_trace(trace_id) → uk_trace_span(trace_id, app_name, span_id)（幂等）
SET @uk_exists := (SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'trace_chain'
      AND INDEX_NAME = 'uk_trace_span');
SET @ddl := IF(@uk_exists = 0,
    'ALTER TABLE trace_chain DROP INDEX uk_trace, ADD UNIQUE KEY uk_trace_span (trace_id, app_name, span_id)',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
-- P2-2 慢 SQL 指纹列与索引幂等补齐
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'slow_sql' AND COLUMN_NAME = 'sql_fingerprint');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE slow_sql ADD COLUMN sql_fingerprint VARCHAR(16) NULL COMMENT ''SQL 指纹（P2-2：字面量归一化模板 SHA-1 前 16 位，旧探针为 NULL）'' AFTER execute_time, ADD KEY idx_fingerprint (sql_fingerprint, execute_time)',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---------------------------------------------------------------------
-- 6. stress_sample：压测周期采样（按天分区）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stress_sample (
    id            BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    task_id       BIGINT UNSIGNED NOT NULL COMMENT '压测任务 ID',
    sample_time   DATETIME      NOT NULL COMMENT '采样时间',
    concurrency   INT           NOT NULL DEFAULT 0 COMMENT '当前并发',
    qps           DOUBLE        NOT NULL DEFAULT 0 COMMENT '周期内 QPS',
    p50_ms        DOUBLE        NOT NULL DEFAULT 0 COMMENT 'P50 RT ms',
    p95_ms        DOUBLE        NOT NULL DEFAULT 0 COMMENT 'P95 RT ms',
    p99_ms        DOUBLE        NOT NULL DEFAULT 0 COMMENT 'P99 RT ms',
    error_count   BIGINT        NOT NULL DEFAULT 0 COMMENT '周期内错误数',
    PRIMARY KEY (id, sample_time),
    KEY idx_task_time (task_id, sample_time)
) ENGINE = InnoDB COMMENT = '压测周期采样'
PARTITION BY RANGE COLUMNS (sample_time) (
    PARTITION p20260827 VALUES LESS THAN ('2026-08-28 00:00:00'),
    PARTITION p20260828 VALUES LESS THAN ('2026-08-29 00:00:00'),
    PARTITION p20260829 VALUES LESS THAN ('2026-08-30 00:00:00'),
    PARTITION p20260830 VALUES LESS THAN ('2026-08-31 00:00:00'),
    PARTITION p_max VALUES LESS THAN (MAXVALUE)
);

-- ---------------------------------------------------------------------
-- 7. data_gen_task：灌数据任务记录
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS data_gen_task (
    id            BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    task_name     VARCHAR(128)  NOT NULL COMMENT '任务名称',
    db_url        VARCHAR(255)  NOT NULL COMMENT '目标库 JDBC URL',
    table_name    VARCHAR(128)  NOT NULL COMMENT '目标表名',
    row_count     INT           NOT NULL DEFAULT 1000 COMMENT '目标行数',
    rule_json     TEXT          NULL COMMENT '字段生成规则 JSON',
    status        VARCHAR(32)   NOT NULL DEFAULT 'pending' COMMENT '状态：pending/running/finished/failed',
    inserted      INT           NOT NULL DEFAULT 0 COMMENT '已插入行数',
    cost_ms       BIGINT        NOT NULL DEFAULT 0 COMMENT '总耗时 ms',
    error_msg     TEXT          NULL COMMENT '失败信息',
    create_time   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id)
) ENGINE = InnoDB COMMENT = '灌数据任务记录';

-- 兼容旧库：data_gen_task 联表/详情三列（total_rows / tables_json / detail_json）不存在时补充（幂等）
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'data_gen_task' AND COLUMN_NAME = 'total_rows');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE data_gen_task ADD COLUMN total_rows INT NOT NULL DEFAULT 0 COMMENT ''总目标行数（表组求和）'' AFTER row_count',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'data_gen_task' AND COLUMN_NAME = 'tables_json');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE data_gen_task ADD COLUMN tables_json TEXT NULL COMMENT ''表组请求 JSON（联表形态）'' AFTER total_rows',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'data_gen_task' AND COLUMN_NAME = 'detail_json');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE data_gen_task ADD COLUMN detail_json MEDIUMTEXT NULL COMMENT ''执行详情 JSON（逐表统计/警告/试运行报告/自检报告）'' AFTER rule_json',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 7.5 semantic_hint：语义提示库（魔法值候选 / 表间关联建议，灌数引擎回调产出，前端确认采纳）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS semantic_hint (
    id            BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    repo_id       BIGINT UNSIGNED NULL COMMENT '源码仓库 ID（纯 DB 元数据来源时为空）',
    hint_type     VARCHAR(16)   NOT NULL COMMENT '类型：value 魔法值 / relation 关联关系',
    table_name    VARCHAR(128)  NOT NULL COMMENT '表名（relation 时为子表）',
    column_name   VARCHAR(128)  NULL COMMENT '列名（relation 时为子表外键列）',
    values_json   TEXT          NULL COMMENT '候选值 JSON 数组（value 类型）',
    parent_table  VARCHAR(128)  NOT NULL DEFAULT '' COMMENT '父表名（relation 类型；value 类型为空串）',
    parent_column VARCHAR(128)  NOT NULL DEFAULT '' COMMENT '父表主键列（relation 类型；value 类型为空串）',
    source_refs   TEXT          NULL COMMENT '来源定位 JSON 数组（文件:行号 / metadata / sample）',
    source        VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源：literal/constant/enum/range/db_fk/orm_join/naming/naming_empty_fallback',
    confidence    DOUBLE        NULL COMMENT '置信度 0~1',
    status        VARCHAR(16)   NOT NULL DEFAULT 'pending' COMMENT '状态：pending/adopted/dismissed',
    create_time   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_hint (repo_id, hint_type, table_name, column_name, parent_table),
    KEY idx_table_status (table_name, status)
) ENGINE = InnoDB COMMENT = '语义提示库（魔法值/关联建议）';

-- 7.6 data_gen_plan：接口驱动数据方案（按压测接口分析源码推导，可回看/可复用）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS data_gen_plan (
    id            BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    repo_id       BIGINT UNSIGNED NULL     COMMENT '源码仓库 ID',
    plan_name     VARCHAR(128)   NOT NULL COMMENT '方案名（默认取接口名）',
    endpoint_json MEDIUMTEXT     NULL     COMMENT '接口/场景步骤快照 JSON [{httpMethod,path,weight}]',
    stress_json   MEDIUMTEXT     NULL     COMMENT '压测参数快照 JSON（并发/时长/QPS/分档/场景权重）',
    conn_id       BIGINT UNSIGNED NULL    COMMENT '数据库连接配置 ID（与 db_url 二选一，服务层强校验；DDL 不加 CHECK）',
    db_url        VARCHAR(512)   NULL     COMMENT '目标库 JDBC URL',
    tables_json   MEDIUMTEXT     NULL     COMMENT '表清单 JSON（表名/行数/规则/读写类型/证据）',
    warnings_json MEDIUMTEXT     NULL     COMMENT '降级与告警 JSON 数组',
    status        VARCHAR(16)    NOT NULL DEFAULT 'pending' COMMENT '状态：pending/adopted/executed/discarded/failed',
    task_id       BIGINT UNSIGNED NULL    COMMENT '关联灌数据任务 ID',
    create_time   DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    update_time   DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (id),
    KEY idx_repo_time (repo_id, create_time),
    KEY idx_status (status)
) ENGINE = InnoDB COMMENT = '接口驱动数据方案（按接口分析源码推导，可回看/可复用）';

-- ---------------------------------------------------------------------
-- 8. source_repo：源码仓库（zip / git / 本地路径导入记录）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_repo (
    id            BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    repo_name     VARCHAR(128)  NOT NULL COMMENT '仓库名称',
    repo_type     VARCHAR(16)   NOT NULL COMMENT '类型：zip / git / local',
    source_path   VARCHAR(512)  NOT NULL COMMENT '磁盘根路径或 git url 或本地路径',
    branch        VARCHAR(128)  NOT NULL DEFAULT '' COMMENT 'git 分支（空 = 默认分支）',
    app_name      VARCHAR(128)  NOT NULL DEFAULT '' COMMENT '绑定应用（压测/问题自动归属）',
    git_user      VARCHAR(255)  NOT NULL DEFAULT '' COMMENT 'git 用户名（私有仓库，可空）',
    git_token     VARCHAR(512)  NOT NULL DEFAULT '' COMMENT 'git 密码/访问令牌（内网演示明文存储，生产环境应加密）',
    status        VARCHAR(32)   NOT NULL DEFAULT 'importing' COMMENT '状态：importing/ready/failed',
    file_count    INT           NOT NULL DEFAULT 0 COMMENT '文件数（≤5000）',
    total_size    BIGINT        NOT NULL DEFAULT 0 COMMENT '总大小字节（≤200MB）',
    error_msg     TEXT          NULL COMMENT '失败信息',
    create_time   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id)
) ENGINE = InnoDB COMMENT = '源码仓库';

-- ---------------------------------------------------------------------
-- 9. source_file：源码文件索引（文件内容存磁盘，表内仅存元数据）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_file (
    id             BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    repo_id        BIGINT UNSIGNED NOT NULL COMMENT '仓库 ID',
    relative_path  VARCHAR(512)  NOT NULL COMMENT '相对路径',
    file_name      VARCHAR(256)  NOT NULL COMMENT '文件名',
    language       VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '语言类型',
    size           BIGINT        NOT NULL DEFAULT 0 COMMENT '文件大小字节（≤1MB）',
    content_hash   VARCHAR(64)   NOT NULL DEFAULT '' COMMENT '内容哈希',
    create_time    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    KEY idx_repo_path (repo_id, relative_path)
) ENGINE = InnoDB COMMENT = '源码文件索引';

-- ---------------------------------------------------------------------
-- 10. performance_issue：性能问题（关联检测证据与源码位置）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS performance_issue (
    id             BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    issue_type     VARCHAR(32)   NOT NULL COMMENT '类型：slow_sql/hot_method/stress_bottleneck/jvm_alert',
    app_name       VARCHAR(128)  NOT NULL DEFAULT '' COMMENT '关联应用',
    title          VARCHAR(512)  NOT NULL COMMENT '问题标题',
    evidence_json  MEDIUMTEXT    NULL COMMENT '检测证据 JSON（慢 SQL/热点方法/压测数据等）',
    source_repo_id BIGINT UNSIGNED NULL COMMENT '关联源码仓库',
    source_file_id BIGINT UNSIGNED NULL COMMENT '关联源码文件',
    line_start     INT           NULL COMMENT '问题起始行',
    line_end       INT           NULL COMMENT '问题结束行',
    root_cause     TEXT          NULL COMMENT '大模型根因分析',
    status         VARCHAR(32)   NOT NULL DEFAULT 'pending' COMMENT '状态：pending/analyzed/fixed/resolved',
    -- M4 修复效果自动验证：问题创建时的压测场景快照 + 重放验证结论
    stress_task_id BIGINT        NOT NULL DEFAULT 0 COMMENT '场景快照压测任务（0=未关联；一键重放时作为基线）',
    verify_json    MEDIUMTEXT    NULL COMMENT '重放验证结论 JSON（重放任务/前后 diff/达标判定）',
    verify_time    DATETIME      NULL COMMENT '最近一次验证时间',
    create_time    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    KEY idx_type_status (issue_type, status)
) ENGINE = InnoDB COMMENT = '性能问题';

-- ---------------------------------------------------------------------
-- 10.1 历史库补列（老库升级：MySQL 无 ADD COLUMN IF NOT EXISTS，走 information_schema 判定）
-- 用法：CALL add_column_if_missing('performance_issue', 'stress_task_id', 'BIGINT NOT NULL DEFAULT 0');
-- ---------------------------------------------------------------------
DELIMITER $$
CREATE PROCEDURE IF NOT EXISTS add_column_if_missing(IN tbl VARCHAR(64), IN col VARCHAR(64), IN col_def TEXT)
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = tbl AND COLUMN_NAME = col
    ) THEN
        SET @sql = CONCAT('ALTER TABLE `', tbl, '` ADD COLUMN `', col, '` ', col_def);
        PREPARE stmt FROM @sql;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
    END IF;
END$$
DELIMITER ;

CALL add_column_if_missing('performance_issue', 'stress_task_id', 'BIGINT NOT NULL DEFAULT 0 COMMENT ''场景快照压测任务''');
CALL add_column_if_missing('performance_issue', 'verify_json', 'MEDIUMTEXT NULL COMMENT ''重放验证结论 JSON''');
CALL add_column_if_missing('performance_issue', 'verify_time', 'DATETIME NULL COMMENT ''最近一次验证时间''');

-- ---------------------------------------------------------------------
-- 11. fix_suggestion：修复方案（每个问题 2-3 条）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fix_suggestion (
    id               BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    issue_id         BIGINT UNSIGNED NOT NULL COMMENT '问题 ID',
    title            VARCHAR(512)  NOT NULL COMMENT '方案标题',
    files_json       MEDIUMTEXT    NULL COMMENT '涉及文件 JSON',
    changes_json     MEDIUMTEXT    NULL COMMENT '修改点 JSON',
    code_changes_json MEDIUMTEXT   NULL COMMENT '代码改动要点 JSON',
    benefit          TEXT          NULL COMMENT '预期收益',
    risk             TEXT          NULL COMMENT '风险',
    prompt_md        MEDIUMTEXT    NULL COMMENT '修复提示词 Markdown（选定后生成）',
    selected         TINYINT       NOT NULL DEFAULT 0 COMMENT '是否被用户选定：1 是 0 否',
    create_time      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    KEY idx_issue (issue_id)
) ENGINE = InnoDB COMMENT = '修复方案';

-- ---------------------------------------------------------------------
-- 12. chat_message：AI 对话消息记录
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_message (
    id          BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    session_id  VARCHAR(64)   NOT NULL COMMENT '会话 ID',
    role        VARCHAR(16)   NOT NULL COMMENT '角色：user/assistant/tool',
    content     MEDIUMTEXT    NULL COMMENT '消息内容',
    tool_calls  MEDIUMTEXT    NULL COMMENT '工具调用 JSON',
    create_time DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    KEY idx_session (session_id, create_time)
) ENGINE = InnoDB COMMENT = 'AI 对话消息记录';

-- =====================================================================
-- 演示数据
-- =====================================================================

-- 演示应用实例
INSERT IGNORE INTO app_instance (app_name, app_host, agent_version, status, last_report_time)
VALUES ('perf-demo', 'target-host:8080', '1.0.0', 1, NOW());

-- 演示 JVM 指标（近 30 分钟，每 5 分钟一条）
INSERT INTO jvm_metric (app_name, collect_time, heap_used_mb, heap_max_mb, non_heap_used_mb,
                        young_gc_count, young_gc_ms, full_gc_count, full_gc_ms, thread_count, cpu_usage, system_cpu)
SELECT 'perf-demo',
       DATE_SUB(NOW(), INTERVAL (30 - seq) * 5 MINUTE),
       380 + (seq * 7) % 160, 2048, 90 + seq % 40,
       120 + seq * 3, 900 + seq * 15, 1 + seq / 10, 50 + seq * 8, 40 + seq % 20, 25 + (seq * 13) % 40, 30 + seq % 25
FROM (SELECT 0 AS seq UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5
      UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9 UNION SELECT 10 UNION SELECT 11
      UNION SELECT 12 UNION SELECT 13 UNION SELECT 14 UNION SELECT 15 UNION SELECT 16 UNION SELECT 17
      UNION SELECT 18 UNION SELECT 19 UNION SELECT 20 UNION SELECT 21 UNION SELECT 22 UNION SELECT 23
      UNION SELECT 24 UNION SELECT 25 UNION SELECT 26 UNION SELECT 27 UNION SELECT 28 UNION SELECT 29) t;

-- 演示调用链（简单 3 层链路：Controller -> Service -> DAO）
INSERT IGNORE INTO trace_chain (trace_id, app_name, root_method, trace_json, total_ms, start_time)
VALUES ('demo-trace-001', 'perf-demo', 'com.perfdemo.controller.OrderController#listOrders',
        '{"method":"com.perfdemo.controller.OrderController#listOrders","costMs":185.2,"ratio":100.0,' ||
        '"children":[{"method":"com.perfdemo.service.OrderService#listOrders","costMs":176.5,"ratio":95.3,' ||
        '"children":[{"method":"com.perfdemo.mapper.OrderMapper#selectList","costMs":150.1,"ratio":81.0,"children":[]},' ||
        '{"method":"com.perfdemo.mapper.UserMapper#selectById","costMs":24.6,"ratio":13.3,"children":[]}]}]}',
        185.2, DATE_SUB(NOW(), INTERVAL 2 MINUTE));

-- 演示慢 SQL（JDBC 拦截来源，模拟无索引全表扫描场景）
INSERT INTO slow_sql (app_name, sql_text, cost_ms, source, db_host, execute_time)
VALUES ('perf-demo',
        'SELECT * FROM t_order WHERE user_id = ? AND status = ? ORDER BY create_time DESC',
        1280.5, 'jdbc', 'target-db:3306', DATE_SUB(NOW(), INTERVAL 3 MINUTE));
INSERT INTO slow_sql (app_name, sql_text, cost_ms, source, db_host, execute_time)
VALUES ('perf-demo',
        'SELECT o.* FROM t_order o WHERE o.user_id = ?',
        860.2, 'jdbc', 'target-db:3306', DATE_SUB(NOW(), INTERVAL 8 MINUTE));
INSERT INTO slow_sql (app_name, sql_text, cost_ms, source, db_host, execute_time)
VALUES ('perf-demo',
        'SELECT * FROM t_product WHERE category_id = ? AND price > ?',
        540.8, 'slowlog', 'target-db:3306', DATE_SUB(NOW(), INTERVAL 15 MINUTE));

-- 演示压测任务（已完成的容量评估示例）
INSERT IGNORE INTO stress_task (id, task_name, engine_type, target_url, method, concurrency, duration_sec,
                                status, result_json, finish_time)
VALUES (1, '订单查询压测-200并发', 'http', 'http://target-host:8080/api/orders', 'GET',
        200, 120, 'finished',
        '{"totalRequests":32400,"qps":270.0,"p50Ms":62,"p95Ms":158,"p99Ms":312,"errorRate":0.0012,' ||
        '"capacity":{"maxConcurrency":326,"maxQps":410,"targetP95Ms":200,' ||
        '"curve":[{"concurrency":50,"qps":98,"p95Ms":42},{"concurrency":100,"qps":185,"p95Ms":68},' ||
        '{"concurrency":200,"qps":270,"p95Ms":158},{"concurrency":400,"qps":320,"p95Ms":286}],' ||
        '"advices":[{"trigger":"RT_P99_HIGH","level":"WARN","message":"P99 达 312ms 超过阈值 300ms","action":"水平扩容至 2 实例"}]}}',
        DATE_SUB(NOW(), INTERVAL 30 MINUTE));

-- 演示压测采样（对应任务 1）
INSERT INTO stress_sample (task_id, sample_time, concurrency, qps, p50_ms, p95_ms, p99_ms, error_count)
SELECT 1, DATE_SUB(NOW(), INTERVAL (20 - seq) * 6 SECOND), 200,
       240 + (seq * 11) % 60, 55 + seq % 15, 140 + (seq * 17) % 40, 280 + (seq * 21) % 60, seq % 4
FROM (SELECT 0 AS seq UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5
      UNION SELECT 6 UNION SELECT 7 UNION SELECT 8 UNION SELECT 9 UNION SELECT 10 UNION SELECT 11
      UNION SELECT 12 UNION SELECT 13 UNION SELECT 14 UNION SELECT 15 UNION SELECT 16 UNION SELECT 17
      UNION SELECT 18 UNION SELECT 19 UNION SELECT 20) t;

-- 演示灌数据任务
INSERT IGNORE INTO data_gen_task (task_name, db_url, table_name, row_count, status, inserted, cost_ms)
VALUES ('订单表 10 万行灌数', 'jdbc:mysql://target-db:3306/demo', 't_order', 100000, 'finished', 100000, 45230);

-- 演示源码仓库与文件（与 perf-demo 源码对应）
INSERT IGNORE INTO source_repo (id, repo_name, repo_type, source_path, status, file_count, total_size)
VALUES (1, 'perf-demo', 'local', '/data/perf-source/perf-demo', 'ready', 28, 312000);
INSERT IGNORE INTO source_file (repo_id, relative_path, file_name, language, size, content_hash)
VALUES (1, 'src/main/java/com/perfdemo/mapper/OrderMapper.java', 'OrderMapper.java', 'java', 4210, 'hash-order-mapper');
INSERT IGNORE INTO source_file (repo_id, relative_path, file_name, language, size, content_hash)
VALUES (1, 'src/main/resources/mapper/OrderMapper.xml', 'OrderMapper.xml', 'xml', 3640, 'hash-order-xml');
INSERT IGNORE INTO source_file (repo_id, relative_path, file_name, language, size, content_hash)
VALUES (1, 'src/main/java/com/perfdemo/service/OrderService.java', 'OrderService.java', 'java', 6230, 'hash-order-service');

-- 演示性能问题与修复方案（对应慢 SQL 无索引全表扫描）
INSERT IGNORE INTO performance_issue (id, issue_type, app_name, title, evidence_json, source_repo_id, source_file_id,
                                      line_start, line_end, root_cause, status)
VALUES (1, 'slow_sql', 'perf-demo', '订单列表查询慢 SQL：全表扫描且 N+1 关联查询',
        '{"slowSqls":[{"sql":"SELECT * FROM t_order WHERE user_id = ? AND status = ?","costMs":1280.5,"count":3}],' ||
        '"avgCostMs":1280.5}',
        1, 1, 24, 30,
        't_order 表 user_id 与 status 字段缺少复合索引，导致查询走全表扫描；同时 OrderService.listOrders 在循环内逐条查询 UserMapper，存在 N+1 查询问题。',
        'analyzed');

INSERT IGNORE INTO fix_suggestion (issue_id, title, files_json, changes_json, code_changes_json, benefit, risk, prompt_md, selected)
VALUES (1, '方案 A：添加复合索引 + 联表查询改写',
        '[{"file":"src/main/resources/mapper/OrderMapper.xml","action":"新增复合索引 DDL"},{"file":"src/main/java/com/perfdemo/mapper/OrderMapper.java","action":"新增联表查询方法"}]',
        '[{"point":"在 t_order 表创建 (user_id,status) 复合索引"},{"point":"将 N+1 的循环查询改写为一次 LEFT JOIN 联表查询"}]',
        '[{"file":"src/main/resources/db/migration/V2__add_index.sql","code":"ALTER TABLE t_order ADD INDEX idx_user_status (user_id, status);"},{"file":"src/main/resources/mapper/OrderMapper.xml","code":"<select id=\"selectOrdersWithUser\" resultType=\"OrderVO\">SELECT o.*, u.name AS userName FROM t_order o LEFT JOIN t_user u ON o.user_id = u.id WHERE o.user_id = #{userId} AND o.status = #{status}</select>"}]',
        '查询由全表扫描变为索引查找，预计耗时由 1280ms 降至 50ms 以内；消除 N+1，总请求数由 1+N 降为 1。',
        '需执行 DDL 变更，低风险；联表查询需注意返回字段映射。',
        '## 修复提示词：订单列表慢 SQL\n\n### 目标文件\n- src/main/resources/mapper/OrderMapper.xml\n- src/main/java/com/perfdemo/mapper/OrderMapper.java\n- src/main/resources/db/migration/V2__add_index.sql\n\n### 问题摘要\n...', 1);

INSERT IGNORE INTO fix_suggestion (issue_id, title, files_json, changes_json, code_changes_json, benefit, risk, prompt_md, selected)
VALUES (1, '方案 B：批处理 + 应用层缓存',
        '[{"file":"src/main/java/com/perfdemo/service/OrderService.java","action":"批查询与缓存改造"}]',
        '[{"point":"循环查询改为 IN 批查询"},{"point":"热点用户数据使用 Caffeine 本地缓存"}]',
        '[{"file":"src/main/java/com/perfdemo/service/OrderService.java","code":"// 批量查询用户：selectByIds(ids) 一次查出，减少 N 次单查"}]',
        '降低数据库压力，缓存命中后查询用户信息耗时趋近 0。',
        '需引入缓存依赖并处理缓存一致性，中等风险。',
        NULL, 0);

-- =====================================================================
-- 自动新增日分区存储过程（可选：生产环境按需调用）
-- 用法：CALL create_daily_partition('jvm_metric', 'p20260828', '2026-08-29 00:00:00');
-- =====================================================================
DELIMITER $$
CREATE PROCEDURE IF NOT EXISTS create_daily_partition(IN tbl VARCHAR(64), IN part_name VARCHAR(64), IN less_than DATETIME)
BEGIN
    SET @sql = CONCAT('ALTER TABLE `', tbl, '` ADD PARTITION (PARTITION `', part_name,
                      '` VALUES LESS THAN (''', DATE_FORMAT(less_than, '%Y-%m-%d %H:%i:%s'), '''))');
    PREPARE stmt FROM @sql;
    EXECUTE stmt;
    DEALLOCATE PREPARE stmt;
END$$
DELIMITER ;

-- 7.6 deploy_conn：被测服务器 SSH 连接配置（AI 编排远程编译部署启动）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS deploy_conn (
    id            BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    conn_name     VARCHAR(128)  NOT NULL COMMENT '连接名称',
    host          VARCHAR(128)  NOT NULL COMMENT 'SSH 主机',
    port          INT           NOT NULL DEFAULT 22 COMMENT 'SSH 端口',
    username      VARCHAR(64)   NOT NULL COMMENT 'SSH 用户',
    auth_type     VARCHAR(16)   NOT NULL DEFAULT 'password' COMMENT '认证：password/privateKey',
    password      VARCHAR(255)  NULL COMMENT '密码（auth_type=password）',
    private_key   TEXT          NULL COMMENT '私钥（auth_type=privateKey）',
    work_dir      VARCHAR(255)  NOT NULL DEFAULT '/opt/perftestor' COMMENT '远程工作目录（上传包与启动脚本）',
    remark        VARCHAR(255)  NULL COMMENT '备注',
    create_time   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_conn_name (conn_name)
) ENGINE = InnoDB COMMENT = '被测服务器 SSH 连接配置';

-- ---------------------------------------------------------------------
-- 7.7 jvm_alert_rule：JVM 指标阈值告警规则（持续性能守护）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jvm_alert_rule (
    id            BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    app_name      VARCHAR(128)  NOT NULL COMMENT '应用名称（空串=全部应用）',
    metric_key    VARCHAR(32)   NOT NULL COMMENT '指标键：heapUsedRatio/fullGcCountDelta/threadCount',
    threshold     DOUBLE        NOT NULL COMMENT '告警阈值（堆使用率 %、GC 次数增量、线程数）',
    window_sec    INT           NOT NULL DEFAULT 300 COMMENT '统计窗口秒',
    quiet_sec     INT           NOT NULL DEFAULT 900 COMMENT '冷却期秒（同规则两次告警最小间隔）',
    enabled       TINYINT       NOT NULL DEFAULT 1 COMMENT '是否启用：1 启用 0 停用',
    webhook       VARCHAR(512)  NULL COMMENT '通知地址（复用 NotifyService，空=不发）',
    last_alert_time DATETIME    NULL COMMENT '上次告警时间（冷却期判定）',
    create_time   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    KEY idx_app_enabled (app_name, enabled),
    UNIQUE KEY uk_app_metric (app_name, metric_key)
) ENGINE = InnoDB COMMENT = 'JVM 指标阈值告警规则';

-- ---------------------------------------------------------------------
-- 7.8 ai_knowledge：AI 性能知识库（分析结论沉淀 + 中文全文检索）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_knowledge (
    id          BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
    app_name    VARCHAR(128)  NOT NULL DEFAULT '' COMMENT '关联应用名（空=全局结论）',
    title       VARCHAR(256)  NOT NULL COMMENT '结论标题',
    content     TEXT          NOT NULL COMMENT '结论正文（根因分析/修复建议）',
    tags        VARCHAR(256)  NOT NULL DEFAULT '' COMMENT '标签（逗号分隔：角色/意图/工具）',
    source      VARCHAR(16)   NOT NULL DEFAULT 'ai' COMMENT '来源：ai 自动沉淀 / manual 手工录入',
    create_time DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    KEY idx_app_time (app_name, create_time),
    -- MySQL 8 默认 ngram 解析器支持中文分词
    FULLTEXT KEY ft_content (title, content) WITH PARSER ngram
) ENGINE = InnoDB COMMENT = 'AI 性能知识库（FULLTEXT ngram 中文检索）';

INSERT IGNORE INTO ai_knowledge (id, app_name, title, content, tags, source)
VALUES (1, 'perf-demo', '订单列表慢 SQL：全表扫描 + N+1 查询',
        '现象：GET /api/user/list?limit=200 压测 P95 偏高，慢 SQL 平均 1280ms。\n'
        '证据：t_order 表 user_id 与 status 无复合索引，EXPLAIN 走全表扫描；OrderService.listOrders 在循环内逐条查 UserMapper，形成 N+1。\n'
        '根因：缺少 (user_id,status) 复合索引 + 循环单查未改写为联表/批查。\n'
        '修复：1) ALTER TABLE t_order ADD INDEX idx_user_status (user_id, status)；'
        '2) 循环查询改写为一次 LEFT JOIN 或 IN 批查询。\n'
        '预期：单次查询耗时由 1280ms 降至 50ms 以内，请求数由 1+N 降为 1。',
        'root_cause,slow_sql,n_plus_1', 'manual');

INSERT IGNORE INTO ai_knowledge (id, app_name, title, content, tags, source)
VALUES (2, '', '压测预热与阶梯加压的必要性',
        '结论：直接冷启动压测会把 JIT 编译、连接池建连、缓存未命中的一次性开销计入统计，导致 P99 出现冷启动尖刺。\n'
        '做法：warmupSec 默认 10s 且预热期不计入正式统计；rampUpSec 阶梯升并发，让服务端预热与连接池填充平滑完成。\n'
        '判读：容量评估应取预热后的稳定段数据，否则拟合出的拐点并发会偏保守。',
        'stress,warmup,capacity', 'manual');

-- =====================================================================
-- 13. PTDE：智能测试数据引擎新表（实施方案 §12.1，R0 批次 B0）
-- 原则：新表一律追加，幂等 CREATE TABLE IF NOT EXISTS；不改既有表结构。
-- =====================================================================

-- 13.1 ptde_dataset_definition：数据集定义（可复用，def_key+version 唯一）
CREATE TABLE IF NOT EXISTS ptde_dataset_definition (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  def_key VARCHAR(128) NOT NULL COMMENT '逻辑名',
  version VARCHAR(32) NOT NULL DEFAULT '1',
  name VARCHAR(128) NOT NULL,
  spec_json MEDIUMTEXT NOT NULL COMMENT 'DatasetSpec 落库形态（A6：pydantic 规范 JSON）',
  spec_fingerprint CHAR(64) NOT NULL,
  profile_fingerprint CHAR(64) NULL COMMENT '关联统计画像指纹',
  seed BIGINT NULL,
  created_by VARCHAR(64) NULL,
  create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_def_version (def_key, version),
  KEY idx_create_time (create_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='PTDE 数据集定义';

-- 13.2 ptde_dataset_instance：数据集实例（状态机宿主；含 A4a 存量迁移映射字段）
CREATE TABLE IF NOT EXISTS ptde_dataset_instance (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  definition_id BIGINT UNSIGNED NOT NULL,
  definition_version VARCHAR(32) NOT NULL,
  plan_id BIGINT UNSIGNED NULL COMMENT 'GenerationPlan 引用',
  environment_label VARCHAR(128) NOT NULL COMMENT '目标环境标识（product/version/environment_id）',
  schema_fingerprint CHAR(64) NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'PLANNED' COMMENT '状态机：PLANNED/GENERATING/LOADING/VERIFYING/READY/PARTIAL/FAILED/CLEANING/CLEANED/CANCELLED',
  transition_log MEDIUMTEXT NULL COMMENT '转换记录 JSON 数组：from/to/operator/reason/occurred_at',
  actual_rows BIGINT NULL,
  actual_storage_bytes BIGINT NULL,
  owner_token CHAR(64) NULL COMMENT '清理所有权标记（总方案 §12.5）',
  validation_summary MEDIUMTEXT NULL,
  error_msg TEXT NULL,
  legacy_plan_id BIGINT UNSIGNED NULL COMMENT 'A4a 迁移映射：来源 data_gen_plan.id',
  legacy_task_ids TEXT NULL COMMENT 'A4a 迁移映射：来源 data_gen_task.id JSON 数组',
  create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_definition (definition_id),
  KEY idx_status (status),
  KEY idx_legacy_plan (legacy_plan_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='PTDE 数据集实例';

-- 13.3 ptde_execution_manifest：执行清单（可复现要素汇总，总方案 §12.4）
CREATE TABLE IF NOT EXISTS ptde_execution_manifest (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  instance_id BIGINT UNSIGNED NOT NULL,
  seed BIGINT NULL,
  generator_version VARCHAR(32) NOT NULL,
  partition_algorithm_version VARCHAR(16) NOT NULL,
  spec_fingerprint CHAR(64) NOT NULL,
  plan_fingerprint CHAR(64) NULL,
  profile_fingerprint CHAR(64) NULL,
  schema_fingerprint CHAR(64) NULL,
  adapter_capability_json MEDIUMTEXT NULL COMMENT 'Capability Probe 快照',
  adapter_version VARCHAR(32) NULL,
  time_inputs_json TEXT NULL COMMENT '时间输入固定值',
  actual_id_ranges_json MEDIUMTEXT NULL COMMENT '各实体实际 ID 区间',
  shard_summary_json MEDIUMTEXT NULL COMMENT '分片摘要',
  start_time DATETIME NULL,
  end_time DATETIME NULL,
  create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_instance (instance_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='PTDE 执行清单';

-- 13.4 ptde_validation_report：验证报告（五层）
CREATE TABLE IF NOT EXISTS ptde_validation_report (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  instance_id BIGINT UNSIGNED NOT NULL,
  layer VARCHAR(16) NOT NULL COMMENT 'count/relationship/distribution/business/db_ready',
  status VARCHAR(8) NOT NULL COMMENT 'PASS/WARN/FAIL',
  metrics_json MEDIUMTEXT NULL,
  tolerance_json TEXT NULL,
  evidence_json MEDIUMTEXT NULL,
  create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_instance_layer (instance_id, layer)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='PTDE 验证报告';

-- 13.5 ptde_finding：分析发现与采纳流（R1，实施方案 §12.2）
-- 复用 semantic_hint 的 pending/adopted/dismissed 工作流；uk_finding 幂等 upsert
CREATE TABLE IF NOT EXISTS ptde_finding (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  repo_id BIGINT UNSIGNED NULL,
  finding_type VARCHAR(32) NOT NULL COMMENT 'relation/access_pattern/enum/business_rule/tech_stack/...',
  value_json TEXT NOT NULL COMMENT '发现内容（结构化）',
  status_three VARCHAR(16) NOT NULL COMMENT '证据三态：CONFIRMED/INFERRED/UNKNOWN',
  source_object VARCHAR(255) NULL COMMENT 'source_file 或 database_object',
  evidence_json TEXT NULL,
  confidence DOUBLE NULL,
  analyzer_version VARCHAR(32) NOT NULL,
  adopt_status VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending/adopted/dismissed（复用 semantic_hint 工作流）',
  legacy_hint_id BIGINT UNSIGNED NULL COMMENT 'A4a 迁移映射：来源 semantic_hint.id',
  value_fingerprint CHAR(64) NOT NULL COMMENT 'value_json 规范哈希（幂等 upsert 键）',
  create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_finding (repo_id, finding_type, source_object, value_fingerprint),
  KEY idx_type_status (finding_type, adopt_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='PTDE 分析发现与采纳流';

-- ---------------------------------------------------------------------
-- 12.2 QW2 按任务清理最小版（落地方案 §6.1；总方案 §12.5 清理策略）
-- ---------------------------------------------------------------------
-- 灌数任务所有权范围：任务执行成功后逐表记录自增主键灌数区间
-- (watermark_from, watermark_to]，是档位 2「按任务 ID 标记删除」的唯一清理依据；
-- 无范围记录的表视为无所有权标记 → 拒绝清理并报告（总方案 §12.5）。
CREATE TABLE IF NOT EXISTS data_gen_task_scope (
  id             BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
  task_id        BIGINT UNSIGNED NOT NULL COMMENT '灌数任务 ID',
  table_name     VARCHAR(128)   NOT NULL COMMENT '表名',
  pk_column      VARCHAR(128)   NOT NULL COMMENT '自增主键列名（所有权标记列）',
  watermark_from BIGINT         NOT NULL DEFAULT 0 COMMENT '灌数前主键水位（区间开）',
  watermark_to   BIGINT         NOT NULL DEFAULT 0 COMMENT '灌数后主键水位（区间闭）',
  row_count      INT            NOT NULL DEFAULT 0 COMMENT '本任务灌入行数（watermark_to - watermark_from）',
  create_time    DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_task_table (task_id, table_name),
  KEY idx_task (task_id)
) ENGINE = InnoDB COMMENT = '灌数任务所有权范围（按任务清理依据）';

-- 清理审计：所有破坏性动作（rebuild/task_rows 两档）逐次留痕，拒绝亦留痕。
CREATE TABLE IF NOT EXISTS data_gen_cleanup_audit (
  id          BIGINT UNSIGNED AUTO_INCREMENT COMMENT '主键',
  task_id     BIGINT UNSIGNED NOT NULL COMMENT '灌数任务 ID',
  mode        VARCHAR(16)    NOT NULL COMMENT '清理档位：rebuild 整库重建 / task_rows 按任务标记删除',
  db_url      VARCHAR(255)   NOT NULL COMMENT '目标库 JDBC URL',
  tables_json MEDIUMTEXT     NULL COMMENT '清理范围表清单 JSON',
  deleted_rows INT           NOT NULL DEFAULT 0 COMMENT '删除行数（rebuild 为清理前行数合计）',
  status      VARCHAR(16)    NOT NULL COMMENT '结果：success/failed/rejected',
  message     TEXT           NULL COMMENT '结果说明或拒绝/失败原因',
  create_time DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (id),
  KEY idx_task (task_id)
) ENGINE = InnoDB COMMENT = '数据清理审计（QW2）';

-- ---------------------------------------------------------------------
-- 13.6 ptde_statistics_profile：统计画像（R2，实施方案 §12.3 原文 DDL）
-- ---------------------------------------------------------------------
-- 画像落库供复用（StatisticsProfile 落库形态 + 指纹）；A5 脱敏 credential_label
-- 记录所用凭据身份（不含密码）；uk_profile_version 幂等 upsert 键。
CREATE TABLE IF NOT EXISTS ptde_statistics_profile (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  profile_key VARCHAR(128) NOT NULL,
  version VARCHAR(32) NOT NULL DEFAULT '1',
  source_type VARCHAR(32) NOT NULL COMMENT 'db_readonly/file_upload/test_db_self',
  database_type VARCHAR(32) NULL,
  collected_at DATETIME NOT NULL,
  sampling_rate DOUBLE NULL,
  accuracy VARCHAR(16) NOT NULL DEFAULT 'estimated' COMMENT 'exact/approximate/estimated',
  credential_label VARCHAR(64) NULL COMMENT '所用凭据身份（脱敏标识，A5）',
  profile_json MEDIUMTEXT NOT NULL COMMENT 'StatisticsProfile 落库形态',
  fingerprint CHAR(64) NOT NULL,
  create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_profile_version (profile_key, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='PTDE 统计画像';

