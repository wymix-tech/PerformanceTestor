package com.perftestor.datagen.engine;

import org.junit.jupiter.api.Test;

import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * TopoSorter 精确环路提取测试。
 *
 * <p>回归旧缺陷：环依赖报错"列出所有未入序表"——a→b→c→a 环外加 d→a 时，
 * 不在环内的 d 也被列入报错，误导排障。新实现 DFS 只报真实环。
 */
class TopoSorterCycleTest {

    private static Map<String, Set<String>> deps(String... pairs) {
        Map<String, Set<String>> d = new LinkedHashMap<>();
        for (int i = 0; i + 1 < pairs.length; i += 2) {
            d.computeIfAbsent(pairs[i], k -> new LinkedHashSet<>()).add(pairs[i + 1]);
        }
        return d;
    }

    @Test
    void cycleErrorReportsOnlyActualCycleMembers() {
        // 环 a→b→c→a；d 依赖 a 但不在环内
        Map<String, Set<String>> dependencies = deps(
                "a", "b",   // a 依赖 b（子 → 父）
                "b", "c",
                "c", "a",
                "d", "a");

        IllegalArgumentException ex = assertThrows(IllegalArgumentException.class,
                () -> TopoSorter.sort(Set.of("a", "b", "c", "d"), dependencies));
        String msg = ex.getMessage();
        assertTrue(msg.contains("循环依赖"), msg);
        // 环上三表必须全部出现
        assertTrue(msg.contains("a") && msg.contains("b") && msg.contains("c"), msg);
        // 不在环内的 d 不得出现（旧实现缺陷回归）
        assertTrue(!msg.contains("d"), "环外表 d 不应出现在环报错中: " + msg);
    }

    @Test
    void twoNodeCycleDetected() {
        IllegalArgumentException ex = assertThrows(IllegalArgumentException.class,
                () -> TopoSorter.sort(Set.of("x", "y"), deps("x", "y", "y", "x")));
        assertTrue(ex.getMessage().contains("x") && ex.getMessage().contains("y"));
    }

    @Test
    void selfReferenceDoesNotFormCycle() {
        // 自引用边在 sort 内部被跳过（插行时按已插入池处理）
        List<String> order = TopoSorter.sort(Set.of("t", "u"),
                deps("t", "t", "u", "t"));
        assertEquals(List.of("t", "u"), order);
    }

    @Test
    void acyclicSortsParentsFirst() {
        List<String> order = TopoSorter.sort(Set.of("user", "order", "item"),
                deps("order", "user", "item", "order"));
        assertEquals(List.of("user", "order", "item"), order);
    }

    @Test
    void findCycleReturnsPureCycleSequence() {
        Set<String> nodes = Set.of("a", "b", "c", "d");
        List<String> cycle = TopoSorter.findCycle(nodes, deps("a", "b", "b", "c", "c", "a", "d", "a"));
        // 返回纯环序列（首尾相接），不含 d
        assertEquals(3, cycle.size());
        assertTrue(cycle.containsAll(List.of("a", "b", "c")));
    }
}
