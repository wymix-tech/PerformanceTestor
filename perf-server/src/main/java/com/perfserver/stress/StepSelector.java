package com.perfserver.stress;

import java.util.List;
import java.util.concurrent.ThreadLocalRandom;

/**
 * 场景步骤加权选择器：按权重比例随机选中步骤，模拟真实流量配比。
 *
 * <p>实现：构造时预计算累积权重数组，每次选择用二分查找定位，
 * 单请求 O(log n)，避免每请求遍历全部步骤。
 *
 * <p>无状态、不可变、线程安全：多个 worker 可共用一个实例并发调用
 * （内部只用 {@link ThreadLocalRandom}，无共享可变状态、无锁）。
 */
public final class StepSelector {

    /** 累积权重数组：cumulative[i] = 前 i+1 个步骤权重之和 */
    private final int[] cumulative;
    private final int totalWeight;

    /**
     * @param weights 各步骤权重（<=0 视为 0，即永不被选中）
     */
    public StepSelector(int[] weights) {
        this.cumulative = new int[weights.length];
        int sum = 0;
        for (int i = 0; i < weights.length; i++) {
            sum += Math.max(0, weights[i]);
            cumulative[i] = sum;
        }
        this.totalWeight = sum;
    }

    /** 从步骤列表构造（读取各步权重，缺省按 1） */
    public static StepSelector of(List<StressTaskRequest.ScenarioStep> steps) {
        int[] weights = new int[steps.size()];
        for (int i = 0; i < steps.size(); i++) {
            weights[i] = Math.max(1, steps.get(i).getWeight());
        }
        return new StepSelector(weights);
    }

    /**
     * 随机选中一个步骤下标。
     *
     * @return 步骤下标；总权重为 0（全部步骤权重 <= 0）时返回 -1
     */
    public int select() {
        if (totalWeight <= 0) {
            return -1;
        }
        int target = ThreadLocalRandom.current().nextInt(totalWeight) + 1;
        // 二分查找第一个累积权重 >= target 的下标
        int lo = 0;
        int hi = cumulative.length - 1;
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (cumulative[mid] >= target) {
                hi = mid;
            } else {
                lo = mid + 1;
            }
        }
        return lo;
    }

    /** 总权重（用于校验与展示） */
    public int totalWeight() {
        return totalWeight;
    }

    /** 步骤数量 */
    public int size() {
        return cumulative.length;
    }
}
