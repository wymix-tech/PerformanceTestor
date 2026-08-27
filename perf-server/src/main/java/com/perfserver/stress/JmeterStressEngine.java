package com.perfserver.stress;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

/**
 * JMeter 压测引擎适配器骨架（SPI 扩展点）。
 *
 * <p>接入方式：在 pom 引入 jmeter 相关依赖后，实现 run() 方法：
 * <pre>
 *   1. 根据 request 生成 JMX 测试计划（targetUrl/concurrency/durationSec）
 *   2. 调用 jmeter CLI：jmeter -n -t plan.jmx -l result.jtl -Jthreads=xx
 *   3. 解析 result.jtl（CSV 采样结果）填充 StressTaskResult（totalRequests/qps/RT 分位数）
 * </pre>
 * 当前为占位骨架，调用时抛出明确提示。
 */
@Component
public class JmeterStressEngine implements StressEngine {

    private static final Logger log = LoggerFactory.getLogger(JmeterStressEngine.class);

    @Override
    public String type() {
        return "jmeter";
    }

    @Override
    public StressTaskResult run(StressTaskRequest request, StressSampleListener listener) {
        log.warn("JMeter engine is a SPI extension skeleton, not implemented yet");
        throw new UnsupportedOperationException(
                "JMeter 引擎为 SPI 扩展点骨架：请在 JmeterStressEngine 中接入 jmeter CLI（生成 JMX 计划 → jmeter -n -t → 解析 result.jtl）");
    }
}
