package com.perftestor.datagen.engine;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ThreadLocalRandom;

/**
 * 父表主键池：插入完成后回查主键填充（对三家数据库通用的主键回填策略，
 * 规避 getGeneratedKeys/RETURNING/Sequence 方言差异）。
 *
 * <p>支持两种采样：</p>
 * <ul>
 *   <li>均匀采样（ref）：二分均匀随机取父 ID</li>
 *   <li>热点倾斜采样（refSkew）：按 hotPercent/hotShare 构建累计权重数组，
 *       二分查找 O(log n)/行——hotPercent% 的父行承接 hotShare% 的子行</li>
 * </ul>
 *
 * <p>池上限 50 万（超限分段采样截断）；自引用表在插行过程中动态追加已插入 ID。</p>
 */
public class IdPool {

    /** 池上限：超过则分段采样截断（内存保护） */
    public static final int MAX_POOL_SIZE = 500_000;

    private final List<Long> ids = new ArrayList<>();
    /** refSkew 热点索引：ids 中前 hotCount 个为热点行（构建权重时把前 hotCount 行权重设为热区） */
    private double[] cumulativeWeights;
    private volatile boolean weighted;

    public synchronized void addAll(List<Long> newIds) {
        int remain = MAX_POOL_SIZE - ids.size();
        if (remain <= 0) {
            return;
        }
        if (newIds.size() > remain) {
            newIds = newIds.subList(0, remain);
        }
        ids.addAll(newIds);
        weighted = false; // 池变更后需重建权重
    }

    public synchronized void add(long id) {
        if (ids.size() < MAX_POOL_SIZE) {
            ids.add(id);
            weighted = false;
        }
    }

    public synchronized int size() {
        return ids.size();
    }

    public synchronized boolean isEmpty() {
        return ids.isEmpty();
    }

    public synchronized Long min() {
        return ids.isEmpty() ? null : ids.get(0);
    }

    public synchronized Long max() {
        return ids.isEmpty() ? null : ids.get(ids.size() - 1);
    }

    /** 均匀采样一个父 ID */
    public synchronized long sampleUniform() {
        if (ids.isEmpty()) {
            throw new IllegalStateException("父 ID 池为空，无法采样 ref 值");
        }
        return ids.get(ThreadLocalRandom.current().nextInt(ids.size()));
    }

    /**
     * 构建热点倾斜权重：前 hotCount 行（按 ids 顺序取前 hotPercent%）合计权重 hotShare，
     * 其余行合计 (100 - hotShare)。
     */
    public synchronized void buildSkewWeights(int hotPercent, int hotShare) {
        if (ids.isEmpty() || hotPercent <= 0 || hotPercent >= 100) {
            cumulativeWeights = null;
            weighted = false;
            return;
        }
        int hotCount = Math.max(1, (int) Math.round(ids.size() * hotPercent / 100.0));
        double[] w = new double[ids.size()];
        double hotEach = (double) hotShare / hotCount;
        double coldEach = (double) (100 - hotShare) / Math.max(1, ids.size() - hotCount);
        double acc = 0;
        for (int i = 0; i < ids.size(); i++) {
            acc += i < hotCount ? hotEach : coldEach;
            w[i] = acc;
        }
        this.cumulativeWeights = w;
        this.weighted = true;
    }

    /** 热点倾斜采样：累计权重 + 二分查找 O(log n)/行 */
    public synchronized long sampleSkewed() {
        if (ids.isEmpty()) {
            throw new IllegalStateException("父 ID 池为空，无法采样 refSkew 值");
        }
        if (!weighted || cumulativeWeights == null) {
            return sampleUniform();
        }
        double pick = ThreadLocalRandom.current().nextDouble() * cumulativeWeights[cumulativeWeights.length - 1];
        int lo = 0, hi = cumulativeWeights.length - 1;
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (cumulativeWeights[mid] < pick) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        return ids.get(lo);
    }

    /** 无效 ID（orphanRate 替换用）：池最大值 + 1 + 随机偏移，必然不命中真实行 */
    public synchronized long sampleInvalidId() {
        long base = ids.isEmpty() ? 1L : ids.get(ids.size() - 1);
        return base + 1 + ThreadLocalRandom.current().nextLong(1_000);
    }
}
