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
            Set<String> unfinished = new LinkedHashSet<>(inDegree.keySet());
            unfinished.removeAll(order);
            // DFS 提取真实环路（仅环中表），替代"列出全部未入序表"的误导性报错
            List<String> cycle = findCycle(unfinished, dependencies);
            String target = cycle.isEmpty()
                    ? String.join(", ", unfinished)
                    : String.join(" -> ", cycle) + " -> " + cycle.get(0);
            throw new IllegalArgumentException("表组存在循环依赖，无法确定生成顺序: " + target);
        }
        return order;
    }

    /**
     * 从未完成节点中提取一条真实环路（DFS 三色标记，沿 child → parent 依赖方向）。
     *
     * @return 环路节点序列（如 [a, b, c] 表示 a→b→c→a）；无环（理论不可达）返回空列表
     */
    static List<String> findCycle(Set<String> nodes, Map<String, Set<String>> dependencies) {
        Map<String, Integer> state = new java.util.HashMap<>(); // 0/缺失=白 1=灰(栈上) 2=黑(完成)
        for (String start : nodes) {
            if (state.containsKey(start)) {
                continue;
            }
            List<String> path = new ArrayList<>();
            if (dfs(start, nodes, dependencies, state, path)) {
                return path;
            }
        }
        return List.of();
    }

    /** DFS：找到环时 path 即环路节点序列（首尾相接） */
    private static boolean dfs(String node, Set<String> nodes, Map<String, Set<String>> dependencies,
                               Map<String, Integer> state, List<String> path) {
        state.put(node, 1);
        path.add(node);
        for (String next : dependencies.getOrDefault(node, Set.of())) {
            if (!nodes.contains(next) || next.equals(node)) {
                continue; // 组外依赖/自引用不构成组内环
            }
            Integer s = state.get(next);
            if (s != null && s == 1) {
                // 命中灰节点：截取 path 中 next 起始的完整环（丢弃环外 DFS 前缀）
                int from = path.indexOf(next);
                if (from >= 0) {
                    List<String> cycle = new ArrayList<>(path.subList(from, path.size()));
                    path.clear();
                    path.addAll(cycle);
                    return true;
                }
                return false;
            }
            if (s == null && dfs(next, nodes, dependencies, state, path)) {
                return true;
            }
        }
        path.remove(path.size() - 1);
        state.put(node, 2);
        return false;
    }
}
