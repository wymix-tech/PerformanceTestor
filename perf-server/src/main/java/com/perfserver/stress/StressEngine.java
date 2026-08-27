package com.perfserver.stress;

/**
 * 压测引擎 SPI：自研 HttpStressEngine 与 JMeter 适配器实现同一接口即可无缝接入。
 */
public interface StressEngine {

    /** 引擎类型标识：http / jmeter */
    String type();

    /**
     * 执行压测任务。
     *
     * @param request  压测请求（并发/时长/QPS 上限/分档配置）
     * @param listener 周期采样回调（用于实时图表落库，可为空）
     * @return 压测结果（统计 + 采样点）
     */
    StressTaskResult run(StressTaskRequest request, StressSampleListener listener);
}
