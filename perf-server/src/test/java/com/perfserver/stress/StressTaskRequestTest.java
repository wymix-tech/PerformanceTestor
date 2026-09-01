package com.perfserver.stress;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * StressTaskRequest 单元测试：模式判定与分档配置归一化。
 */
class StressTaskRequestTest {

    @Test
    void defaultsToSingleInterfaceMode() {
        StressTaskRequest request = StressTaskRequest.single("http://localhost/api", 10, 30);
        assertFalse(request.isScenarioMode());
        assertNull(request.resolveStages(), "未配置分档时应为单档模式");
        assertNotNull(request.getVariables(), "变量池应有默认值，避免调用方判空");
    }

    @Test
    void scenarioModeIsDetected() {
        StressTaskRequest request = StressTaskRequest.single("http://localhost/api", 10, 30);
        request.setScenario(List.of(new StressTaskRequest.ScenarioStep()));
        assertTrue(request.isScenarioMode());
        request.setScenario(List.of());
        assertFalse(request.isScenarioMode(), "空步骤列表应退化为单接口模式");
        request.setScenario(null);
        assertFalse(request.isScenarioMode());
    }

    @Test
    void explicitStagesTakePrecedence() {
        StressTaskRequest request = StressTaskRequest.single("http://localhost/api", 10, 30);
        List<StressTaskRequest.Stage> stages = List.of(stage(10, 20), stage(50, 20));
        request.setStages(stages);
        request.setStageConcurrencies(List.of(1, 2, 3));

        assertSame(stages, request.resolveStages(), "显式 stages 优先，不被简写覆盖");
    }

    @Test
    void stageConcurrenciesAreExpandedToStages() {
        // 回归防护：前端表单提交的是并发数简写列表，必须能展开为完整分档，
        // 否则用户填了分档并发却静默退化成单档压测，容量曲线无从产生
        StressTaskRequest request = StressTaskRequest.single("http://localhost/api", 10, 90);
        request.setStageConcurrencies(List.of(10, 50, 100));

        List<StressTaskRequest.Stage> resolved = request.resolveStages();
        assertNotNull(resolved);
        assertEquals(3, resolved.size());
        assertEquals(List.of(10, 50, 100), resolved.stream().map(StressTaskRequest.Stage::getConcurrency).toList());
        // 总时长 90s / 3 档 = 30s 每档，总耗时与 durationSec 相当
        assertEquals(List.of(30, 30, 30), resolved.stream().map(StressTaskRequest.Stage::getDurationSec).toList());
    }

    @Test
    void perStageDurationIsAtLeastOneSecond() {
        StressTaskRequest request = StressTaskRequest.single("http://localhost/api", 10, 2);
        request.setStageConcurrencies(List.of(10, 50, 100, 200));
        // 2s / 4 档 = 0 → 钳制为 1s
        assertEquals(List.of(1, 1, 1, 1),
                request.resolveStages().stream().map(StressTaskRequest.Stage::getDurationSec).toList());
    }

    @Test
    void invalidStageConcurrencyEntriesAreFiltered() {
        StressTaskRequest request = StressTaskRequest.single("http://localhost/api", 10, 60);
        // 含 null、0、负数：均视为无效并剔除
        request.setStageConcurrencies(java.util.Arrays.asList(null, 0, -5, 20));
        List<StressTaskRequest.Stage> resolved = request.resolveStages();
        assertNotNull(resolved);
        assertEquals(1, resolved.size());
        assertEquals(20, resolved.get(0).getConcurrency());
    }

    @Test
    void allInvalidStageConcurrenciesYieldNoStages() {
        StressTaskRequest request = StressTaskRequest.single("http://localhost/api", 10, 60);
        request.setStageConcurrencies(List.of(0, -1));
        assertNull(request.resolveStages(), "全部无效时应退回单档模式");
        request.setStageConcurrencies(List.of());
        assertNull(request.resolveStages());
    }

    private static StressTaskRequest.Stage stage(int concurrency, int durationSec) {
        StressTaskRequest.Stage s = new StressTaskRequest.Stage();
        s.setConcurrency(concurrency);
        s.setDurationSec(durationSec);
        return s;
    }
}
