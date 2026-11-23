package com.perfagent.hotspot;

/**
 * 连续剖析后端统一契约（P2-1 预留扩展点）。
 *
 * <p>默认实现 {@link JfrProfiler}（JDK 17 内置 JFR，零外部依赖）；
 * 后续 Linux 宿主机存在 {@code libasyncProfiler.so} 时可实现原生后端
 * （async-profiler，Native + Java 混合火焰图），经同一接口无缝接入。
 */
public interface ProfilerBackend {

    /** 启动剖析（幂等：重复调用无副作用） */
    void start();

    /** 停止剖析并释放资源（关闭事件流、停定时器、释放磁盘临时 chunk） */
    void stop();
}
