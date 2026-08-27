package com.perfagent.trace;

import java.util.concurrent.ThreadLocalRandom;

/**
 * 调用链采样器：按采样率决定是否记录当前请求的调用链，控制上报数据量。
 *
 * <p>默认采样率 0.1（10%），生产环境建议 0.1~0.5，演示环境可配置为 1.0 全量采样。
 * 使用 ThreadLocalRandom 避免并发竞争。
 */
public class TraceSampler {

    private final double sampleRate;

    public TraceSampler(double sampleRate) {
        this.sampleRate = Math.max(0.0, Math.min(1.0, sampleRate));
    }

    /** 是否采样本次调用（根方法进入时判断一次，整条链路共用判定结果） */
    public boolean shouldSample() {
        if (sampleRate >= 1.0) {
            return true;
        }
        if (sampleRate <= 0.0) {
            return false;
        }
        return ThreadLocalRandom.current().nextDouble() < sampleRate;
    }

    public double getSampleRate() {
        return sampleRate;
    }
}
