-- =====================================================================
-- PerformanceTestor 数据库初始化脚本（幂等，可重复执行）
-- 说明：
--   1. 全部使用 CREATE TABLE IF NOT EXISTS，可重复执行；
--   2. 时序表（jvm_metric / stress_sample）使用 RANGE COLUMNS 按天分区，
--      末尾保留 MAXVALUE 兜底分区，保证任意时间数据可写入；
--   3. 末尾插入演示数据，便于前端首屏展示与端到端联调。
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
    trace_id     VARCHAR(64)   NOT NULL COMMENT '链路 ID',
    app_name     VARCHAR(128)  NOT NULL COMMENT '应用名称',
    root_method  VARCHAR(512)  NOT NULL DEFAULT '' COMMENT '根方法（类#方法）',
    trace_json   MEDIUMTEXT    NOT NULL COMMENT '调用树 JSON（TraceNode 树，含每层耗时与占比）',
    total_ms     DOUBLE        NOT NULL DEFAULT 0 COMMENT '总耗时 ms',
    start_time   DATETIME      NOT NULL COMMENT '链路开始时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_trace (trace_id),
    KEY idx_app_time (app_name, start_time)
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
    PRIMARY KEY (id),
    KEY idx_app_time (app_name, execute_time),
    KEY idx_cost (cost_ms)
) ENGINE = InnoDB COMMENT = '慢 SQL 记录';

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
    status         VARCHAR(32)   NOT NULL DEFAULT 'pending' COMMENT '状态：pending/analyzed/fixed',
    create_time    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    KEY idx_type_status (issue_type, status)
) ENGINE = InnoDB COMMENT = '性能问题';

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
