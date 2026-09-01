package com.perfserver.stress;

import org.junit.jupiter.api.Test;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * StepSelector 加权选择单元测试：分布比例、边界与线程安全性状。
 */
class StepSelectorTest {

    @Test
    void respectsWeightRatios() {
        StepSelector selector = new StepSelector(new int[]{1, 3});
        Map<Integer, Integer> counts = new HashMap<>();
        int n = 40_000;
        for (int i = 0; i < n; i++) {
            counts.merge(selector.select(), 1, Integer::sum);
        }
        double ratio = counts.getOrDefault(1, 0) * 1.0 / n;
        // 权重 3/(1+3) = 0.75，允许统计波动
        assertEquals(0.75, ratio, 0.03);
        assertEquals(n, counts.getOrDefault(0, 0) + counts.getOrDefault(1, 0));
    }

    @Test
    void singleStepAlwaysSelected() {
        StepSelector selector = new StepSelector(new int[]{5});
        for (int i = 0; i < 100; i++) {
            assertEquals(0, selector.select());
        }
    }

    @Test
    void zeroWeightStepNeverSelected() {
        StepSelector selector = new StepSelector(new int[]{0, 1, 0});
        for (int i = 0; i < 500; i++) {
            assertEquals(1, selector.select());
        }
    }

    @Test
    void returnsMinusOneWhenAllWeightsAreZero() {
        StepSelector selector = new StepSelector(new int[]{0, 0, -5});
        assertEquals(0, selector.totalWeight());
        assertEquals(-1, selector.select());
    }

    @Test
    void negativeWeightsAreTreatedAsZero() {
        StepSelector selector = new StepSelector(new int[]{-3, 2});
        // 总权重只按正值累计
        assertEquals(2, selector.totalWeight());
        for (int i = 0; i < 200; i++) {
            assertEquals(1, selector.select());
        }
    }

    @Test
    void selectsEveryPositiveWeightStepEventually() {
        StepSelector selector = new StepSelector(new int[]{1, 1, 1, 1, 1});
        boolean[] seen = new boolean[5];
        for (int i = 0; i < 5_000; i++) {
            seen[selector.select()] = true;
        }
        for (boolean b : seen) {
            assertTrue(b, "每个正权重步骤都应可能被选中");
        }
    }

    @Test
    void buildsFromScenarioSteps() {
        List<StressTaskRequest.ScenarioStep> steps = List.of(
                step("login", 1), step("query", 9));
        StepSelector selector = StepSelector.of(steps);
        assertEquals(10, selector.totalWeight());
        assertEquals(2, selector.size());
    }

    private static StressTaskRequest.ScenarioStep step(String name, int weight) {
        StressTaskRequest.ScenarioStep s = new StressTaskRequest.ScenarioStep();
        s.setName(name);
        s.setWeight(weight);
        return s;
    }
}
