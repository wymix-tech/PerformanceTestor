package com.perfdemo.config;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.CommandLineRunner;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

/**
 * 演示数据初始化：启动时幂等建表并灌入测试数据。
 * <p>数据量（1 万用户 + 5 万订单，无 status 索引）保证：
 * - /api/user/list 的 N+1 查询耗时明显
 * - /api/order/list 按无索引列过滤产生真实慢 SQL
 * - /api/order/stats 大表聚合耗时显著
 * </p>
 */
@Component
public class DemoDataInitializer implements CommandLineRunner {

    private static final Logger log = LoggerFactory.getLogger(DemoDataInitializer.class);

    private final JdbcTemplate jdbc;

    public DemoDataInitializer(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @Override
    public void run(String... args) {
        createTables();
        Long userCount = jdbc.queryForObject("SELECT COUNT(*) FROM perf_user", Long.class);
        if (userCount == null || userCount == 0) {
            seedUsers();
        }
        // 仅空表时初始化：避免中断后再次启动产生累积重复数据（保持幂等）
        Long orderCount = jdbc.queryForObject("SELECT COUNT(*) FROM perf_order", Long.class);
        if (orderCount == null || orderCount == 0) {
            seedOrders();
        }
        log.info("[perf-demo] 演示数据就绪: perf_user={}, perf_order={}",
                jdbc.queryForObject("SELECT COUNT(*) FROM perf_user", Long.class),
                jdbc.queryForObject("SELECT COUNT(*) FROM perf_order", Long.class));
    }

    private void createTables() {
        jdbc.execute("CREATE TABLE IF NOT EXISTS perf_user ("
                + "id BIGINT AUTO_INCREMENT PRIMARY KEY,"
                + "name VARCHAR(64) NOT NULL,"
                + "age INT NOT NULL,"
                + "email VARCHAR(128),"
                + "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
                + ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4");
        // 注意：status 刻意不加索引，用于演示无索引全表扫描慢 SQL
        jdbc.execute("CREATE TABLE IF NOT EXISTS perf_order ("
                + "id BIGINT AUTO_INCREMENT PRIMARY KEY,"
                + "user_id BIGINT NOT NULL,"
                + "amount DECIMAL(12,2) NOT NULL,"
                + "status VARCHAR(16) NOT NULL DEFAULT 'PENDING',"
                + "remark VARCHAR(255),"
                + "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                + "KEY idx_user (user_id)"
                + ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4");
    }

    private void seedUsers() {
        log.info("[perf-demo] 灌入 10000 条用户数据 ...");
        jdbc.execute("INSERT INTO perf_user(name, age, email) "
                + "SELECT CONCAT('user_', n), 18 + (n % 50), CONCAT('user_', n, '@demo.com') "
                + "FROM (SELECT @rn := @rn + 1 AS n FROM "
                + "(SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5) a, "
                + "(SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5) b, "
                + "(SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5) c, "
                + "(SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5) d, "
                + "(SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5) e, "
                + "(SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5) f, "
                + "(SELECT @rn := 0) init) seq LIMIT 10000");
    }

    private void seedOrders() {
        log.info("[perf-demo] 灌入 50000 条订单数据 ...");
        jdbc.execute("INSERT INTO perf_order(user_id, amount, status, remark) "
                + "SELECT 1 + (n % 10000), ROUND(RAND() * 5000, 2), "
                + "ELT(1 + (n % 4), 'PENDING', 'PAID', 'SHIPPED', 'DONE'), "
                + "CONCAT('order_', n) "
                + "FROM (SELECT @rn2 := @rn2 + 1 AS n FROM "
                + "(SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5) a, "
                + "(SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5) b, "
                + "(SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5) c, "
                + "(SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5) d, "
                + "(SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5) e, "
                + "(SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5) f, "
                + "(SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5) g, "
                + "(SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5) h, "
                + "(SELECT @rn2 := 0) init) seq LIMIT 50000");
    }
}
