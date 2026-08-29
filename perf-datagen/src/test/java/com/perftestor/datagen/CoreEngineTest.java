package com.perftestor.datagen;

import com.perftestor.datagen.engine.IdPool;
import com.perftestor.datagen.engine.TopoSorter;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;
import java.util.stream.IntStream;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** 核心引擎纯逻辑单测：拓扑排序 / ID 池倾斜采样。 */
class CoreEngineTest {

    @Test
    void topoSortParentsFirst() {
        List<String> order = TopoSorter.sort(
                Set.of("order_item", "user", "order"),
                Map.of("order", Set.of("user"), "order_item", Set.of("order")));
        assertEquals(List.of("user", "order", "order_item"), order);
    }

    @Test
    void topoSortCycleDetected() {
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> TopoSorter.sort(Set.of("a", "b"), Map.of("a", Set.of("b"), "b", Set.of("a"))));
        assertTrue(e.getMessage().contains("循环依赖"));
    }

    @Test
    void idPoolSkewDistribution() {
        IdPool pool = new IdPool();
        pool.addAll(IntStream.rangeClosed(1, 1000).mapToObj(Long::valueOf).collect(Collectors.toList()));
        pool.buildSkewWeights(20, 80);
        int hotHits = 0, total = 20_000;
        for (int i = 0; i < total; i++) {
            if (pool.sampleSkewed() <= 200) {
                hotHits++;
            }
        }
        double share = hotHits * 100.0 / total;
        assertTrue(share > 75 && share < 85, "hotShare actual " + share);
    }

    @Test
    void idPoolUniformAndInvalid() {
        IdPool pool = new IdPool();
        pool.addAll(List.of(1L, 2L, 3L));
        long sampled = pool.sampleUniform();
        assertTrue(sampled >= 1 && sampled <= 3);
        assertTrue(pool.sampleInvalidId() > 3);
    }
}

