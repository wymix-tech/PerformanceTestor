package com.perfdemo;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * PerformanceTestor 示例被测应用启动类。
 *
 * <p>该应用挂载 perf-agent（-javaagent）后，业务方法会被字节码增强，
 * 产生调用链耗时数据并上报平台；内含 N+1 查询等可分析性能问题的示例代码，
 * 用于端到端验证「检测 → 源码关联 → AI 修复建议」闭环。
 */
@SpringBootApplication
public class DemoApplication {

    public static void main(String[] args) {
        SpringApplication.run(DemoApplication.class, args);
    }
}
