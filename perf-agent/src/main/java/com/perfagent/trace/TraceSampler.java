package com.perfagent.trace;

import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

/**
 * 调用链采样器：采样率控制比例 + 每秒令牌桶控制绝对量。
 *
 * <p>采样率（sampleRate，默认 0.1）决定"多少比例的请求被记录"；
 * 令牌桶（maxTracesPerSec，默认 50/s）防高 QPS 洪峰——
 * 10000 QPS × 10% 仍会产生 1000 trace/s，令牌桶把绝对量压在常数级，
 * 保证 agent 内存/CPU 开销与业务流量规模解耦。
 *
 * <p>无锁实现：ThreadLocalRandom 判比例 + AtomicInteger CAS 取令牌，
 * 秒窗口切换允许微小竞争误差（限流是软目标，不追求精确）。
 */
public class TraceSampler {

    private final double sampleRate;
    /** 每秒最大采样条数（0 = 不限流，仅按比例采样） */
    private final int maxPerSecond;

    /** 当前秒剩余令牌（竞争下可能短暂为负，视为本秒已用尽） */
    private final AtomicInteger permits = new AtomicInteger(0);
    /** 当前令牌窗口（epoch 秒） */
    private final AtomicLong windowSec = new AtomicLong(0);

    public TraceSampler(double sampleRate) {
        this(sampleRate, 50);
    }

    public TraceSampler(double sampleRate, int maxPerSecond) {
        this.sampleRate = Math.max(0.0, Math.min(1.0, sampleRate));
        this.maxPerSecond = maxPerSecond;
    }

    /** 是否采样本次调用（根方法进入时判断一次，整条链路共用判定结果） */
    public boolean shouldSample() {
        if (sampleRate <= 0.0) {
            return false;
        }
        if (sampleRate < 1.0 && ThreadLocalRandom.current().nextDouble() >= sampleRate) {
            return false;
        }
        // 通过比例判定后还需拿到本秒令牌（洪峰保护）
        return acquirePermit();
    }

    /** 取一个本秒令牌；窗口切换时重置配额 */
    private boolean acquirePermit() {
        if (maxPerSecond <= 0) {
            return true;
        }
        long sec = System.currentTimeMillis() / 1000;
        long w = windowSec.get();
        if (sec != w && windowSec.compareAndSet(w, sec)) {
            permits.set(maxPerSecond);
        }
        return permits.decrementAndGet() >= 0;
    }

    public double getSampleRate() {
        return sampleRate;
    }

    public int getMaxPerSecond() {
        return maxPerSecond;
    }
}
