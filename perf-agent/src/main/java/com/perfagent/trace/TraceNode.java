package com.perfagent.trace;

import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * 调用链树节点：记录单个业务方法调用的类名#方法名、耗时与子节点。
 *
 * <p>树的构建由 {@link TraceContext} 维护（ThreadLocal 调用栈），
 * 上报平台后由前端渲染调用树/火焰图，并可计算每层耗时占比。
 */
public class TraceNode {

    /** 完整方法签名，如 com.perfdemo.controller.OrderController#listOrders */
    public String method;
    /** 方法耗时 ms */
    public double costMs;
    /** 相对根节点开始的偏移 ms（0 = 与根节点同时开始，用于前端时间轴/甘特图定位） */
    public double offsetMs;
    /** 耗时占父节点/根节点比例（0~100，由平台侧计算或本地计算） */
    public double ratio;
    /**
     * 子调用节点（P0 修复：CopyOnWriteArrayList）。
     *
     * <p>异步场景（AsyncSupport 透传）下多个子线程并发向同一父节点 {@code children}
     * 挂接子调用，ArrayList 非线程安全会触发 ConcurrentModificationException；
     * COW 列表使业务线程写入与 MetricsReporter 遍历线程序列化并发安全，
     * 且调用树节点普遍子节点数很少（读多写少），复制开销极低。
     */
    public final List<TraceNode> children = new CopyOnWriteArrayList<>();

    public TraceNode() {
    }

    public TraceNode(String method) {
        this.method = method;
    }

    /** 快速构造（便于测试与演示数据） */
    public static TraceNode of(String method, double costMs) {
        TraceNode node = new TraceNode(method);
        node.costMs = costMs;
        return node;
    }

    public String getMethod() {
        return method;
    }

    public void setMethod(String method) {
        this.method = method;
    }

    public double getCostMs() {
        return costMs;
    }

    public void setCostMs(double costMs) {
        this.costMs = costMs;
    }

    public double getOffsetMs() {
        return offsetMs;
    }

    public void setOffsetMs(double offsetMs) {
        this.offsetMs = offsetMs;
    }

    public double getRatio() {
        return ratio;
    }

    public void setRatio(double ratio) {
        this.ratio = ratio;
    }

    public List<TraceNode> getChildren() {
        return children;
    }
}
