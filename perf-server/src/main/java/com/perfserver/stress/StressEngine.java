package com.perfserver.stress;

/**
 * 压测引擎 SPI：自研 HttpStressEngine 与 JMeter 适配器实现同一接口即可无缝接入。
 */
public interface StressEngine {

    /** 引擎类型标识：http / jmeter */
    String type();

    /**
     * 引擎是否可用（外部依赖检测，如 JMeter 安装）。
     * 不可用的引擎在任务创建时即被拒绝（见 StressTaskService.validate），
     * 避免任务跑到一半才失败。默认可用。
     */
    default boolean available() {
        return true;
    }

    /**
     * 执行压测任务。
     *
     * @param request  压测请求（并发/时长/QPS 上限/分档配置）
     * @param listener 周期采样回调（用于实时图表落库，可为空）
     * @return 压测结果（统计 + 采样点）
     */
    StressTaskResult run(StressTaskRequest request, StressSampleListener listener);
}
