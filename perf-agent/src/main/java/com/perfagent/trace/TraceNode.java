package com.perfagent.trace;

import java.util.ArrayList;
import java.util.List;

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
    /** 耗时占父节点/根节点比例（0~100，由平台侧计算或本地计算） */
    public double ratio;
    /** 子调用节点 */
    public final List<TraceNode> children = new ArrayList<>();

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

    /** 汇总本节点（含子树）总耗时，用于计算占比 */
    public double totalCost() {
        double sum = costMs;
        for (TraceNode child : children) {
            sum += child.totalCost();
        }
        return sum;
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
