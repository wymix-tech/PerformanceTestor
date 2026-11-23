-- =====================================================================
-- PerformanceTestor 存量 MySQL 库 P0/P1 幂等升级脚本（Fix-7）
-- =====================================================================
-- 适用场景：存量库（已按旧版 init.sql 建表）升级到 P0/P1 基线——
-- 直接启动新版本服务会报 Unknown column / Unknown index，先执行本脚本。
--
-- 幂等保证：所有变更前先查 information_schema.COLUMNS / STATISTICS，
-- 列/索引已存在则跳过（SELECT 1 占位），可重复执行不报错。
--
-- 执行方式（须显式指定客户端字符集，与 init.sql 同口径）：
--   mysql --default-character-set=utf8mb4 -u root -p performance_testor < migrate_p0_p1.sql
-- =====================================================================
USE performance_testor;

-- ---------------------------------------------------------------------
-- 1. stress_task.request_json：P0-4 修复验证重放的完整请求快照
-- ---------------------------------------------------------------------
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'stress_task'
      AND COLUMN_NAME = 'request_json');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE stress_task ADD COLUMN request_json MEDIUMTEXT NULL '
    'COMMENT ''创建时的完整压测请求快照（修复验证重放还原用）'' AFTER result_json',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---------------------------------------------------------------------
-- 2. trace_chain.stress_task_id：P1-1 压测流量染色 + 染色索引
-- ---------------------------------------------------------------------
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'trace_chain'
      AND COLUMN_NAME = 'stress_task_id');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE trace_chain ADD COLUMN stress_task_id BIGINT NULL '
    'COMMENT ''触发压测任务 ID（压测流量染色，非压测流量为 NULL）'' AFTER start_time',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @idx_exists := (SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'trace_chain'
      AND INDEX_NAME = 'idx_trace_stress_task');
SET @ddl := IF(@idx_exists = 0,
    'ALTER TABLE trace_chain ADD INDEX idx_trace_stress_task (stress_task_id, start_time)',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---------------------------------------------------------------------
-- 3. slow_sql.stress_task_id：P1-1 压测流量染色 + 染色索引
-- ---------------------------------------------------------------------
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'slow_sql'
      AND COLUMN_NAME = 'stress_task_id');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE slow_sql ADD COLUMN stress_task_id BIGINT NULL '
    'COMMENT ''触发压测任务 ID（压测流量染色，非压测流量为 NULL）'' AFTER execute_time',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @idx_exists := (SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'slow_sql'
      AND INDEX_NAME = 'idx_slow_sql_stress_task');
SET @ddl := IF(@idx_exists = 0,
    'ALTER TABLE slow_sql ADD INDEX idx_slow_sql_stress_task (stress_task_id, execute_time)',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---------------------------------------------------------------------
-- 4. slow_command.stress_task_id：P1-1 压测流量染色 + 染色索引
-- ---------------------------------------------------------------------
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'slow_command'
      AND COLUMN_NAME = 'stress_task_id');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE slow_command ADD COLUMN stress_task_id BIGINT NULL '
    'COMMENT ''触发压测任务 ID（压测流量染色，非压测流量为 NULL）'' AFTER execute_time',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @idx_exists := (SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'slow_command'
      AND INDEX_NAME = 'idx_slow_cmd_stress_task');
SET @ddl := IF(@idx_exists = 0,
    'ALTER TABLE slow_command ADD INDEX idx_slow_cmd_stress_task (stress_task_id, execute_time)',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---------------------------------------------------------------------
-- 5. jvm_metric.extra_metrics：P1-5 扩展指标 JSON（旧探针为 NULL）
-- ---------------------------------------------------------------------
SET @col_exists := (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'jvm_metric'
      AND COLUMN_NAME = 'extra_metrics');
SET @ddl := IF(@col_exists = 0,
    'ALTER TABLE jvm_metric ADD COLUMN extra_metrics TEXT NULL '
    'COMMENT ''P1-5 扩展指标 JSON（连接池/容器线程池/直接内存/Metaspace/线程状态，旧探针为 NULL）'' '
    'AFTER system_cpu',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---------------------------------------------------------------------
-- 6. 四张时序表 (app_name, <time_col>, id) 游标分页复合索引
--    （keyset 分页 WHERE app_name = ? AND (<time>, id) < (?, ?) ORDER BY ...）
--    已存在同名索引时跳过；已有等价 idx_app_time 的库可保留双索引（幂等不冲突）
-- ---------------------------------------------------------------------
SET @idx_exists := (SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'jvm_metric'
      AND INDEX_NAME = 'idx_cursor_app_time');
SET @ddl := IF(@idx_exists = 0,
    'ALTER TABLE jvm_metric ADD INDEX idx_cursor_app_time (app_name, collect_time, id)',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @idx_exists := (SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'trace_chain'
      AND INDEX_NAME = 'idx_cursor_app_time');
SET @ddl := IF(@idx_exists = 0,
    'ALTER TABLE trace_chain ADD INDEX idx_cursor_app_time (app_name, start_time, id)',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @idx_exists := (SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'slow_sql'
      AND INDEX_NAME = 'idx_cursor_app_time');
SET @ddl := IF(@idx_exists = 0,
    'ALTER TABLE slow_sql ADD INDEX idx_cursor_app_time (app_name, execute_time, id)',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @idx_exists := (SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'slow_command'
      AND INDEX_NAME = 'idx_cursor_app_time');
SET @ddl := IF(@idx_exists = 0,
    'ALTER TABLE slow_command ADD INDEX idx_cursor_app_time (app_name, execute_time, id)',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
