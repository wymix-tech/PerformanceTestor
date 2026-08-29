package com.perftestor.datagen.engine;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Deque;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 拓扑排序与环检测（Kahn 算法）。
 * 决定表组生成顺序：父表（被依赖方）先插，子表后插；出现环时报错并定位环中表名。
 */
public final class TopoSorter {

    private TopoSorter() {
    }

    /**
     * @param tables       待排序表名（小写）
     * @param dependencies 表 → 其依赖的父表集合（仅含 tables 内的表；外部依赖忽略）
     * @return 生成顺序（父先子后）
     * @throws IllegalArgumentException 环依赖时报错，消息定位到环中表名
     */
    public static List<String> sort(Collection<String> tables, Map<String, Set<String>> dependencies) {
        Map<String, Integer> inDegree = new java.util.HashMap<>();
        Map<String, Set<String>> children = new java.util.HashMap<>();
        for (String t : tables) {
            inDegree.putIfAbsent(t, 0);
        }
        for (Map.Entry<String, Set<String>> e : dependencies.entrySet()) {
            String child = e.getKey();
            if (!inDegree.containsKey(child)) {
                continue;
            }
            for (String parent : e.getValue()) {
                if (!inDegree.containsKey(parent) || parent.equals(child)) {
                    continue; // 组外依赖或自引用（自引用不影响排序，插行时按已插入池处理）
                }
                children.computeIfAbsent(parent, k -> new LinkedHashSet<>()).add(child);
                inDegree.merge(child, 1, Integer::sum);
            }
        }

        Deque<String> ready = new ArrayDeque<>();
        for (Map.Entry<String, Integer> e : inDegree.entrySet()) {
            if (e.getValue() == 0) {
                ready.add(e.getKey());
            }
        }

        List<String> order = new ArrayList<>(tables.size());
        while (!ready.isEmpty()) {
            String t = ready.poll();
            order.add(t);
            Set<String> cs = children.get(t);
            if (cs != null) {
                for (String c : cs) {
                    int left = inDegree.merge(c, -1, Integer::sum);
                    if (left == 0) {
                        ready.add(c);
                    }
                }
            }
        }

        if (order.size() != inDegree.size()) {
            Set<String> cycle = new LinkedHashSet<>(inDegree.keySet());
            cycle.removeAll(order);
            throw new IllegalArgumentException("表组存在循环依赖，无法确定生成顺序: " + String.join(", ", cycle));
        }
        return order;
    }
}
