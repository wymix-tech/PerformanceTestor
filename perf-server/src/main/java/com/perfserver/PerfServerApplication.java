package com.perfserver;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * PerformanceTestor 后端服务平台启动类。
 *
 * <p>平台与被测应用分离部署：本服务部署在平台服务器，负责
 * 采集 API（接收 agent 远程上报）、压测引擎、数据灌入、容量分析、
 * 源码管理与 AI 智能体编排。
 */
@EnableScheduling  // 定时压测调度（StressScheduler 30s 扫描）
@SpringBootApplication
public class PerfServerApplication {

    public static void main(String[] args) {
        SpringApplication.run(PerfServerApplication.class, args);
    }
}
