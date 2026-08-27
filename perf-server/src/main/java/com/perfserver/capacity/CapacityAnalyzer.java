package com.perfserver.capacity;

import com.perfserver.stress.StressTaskResult;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * 容量分析器：基于压测分档采样点（并发-QPS-P95 RT）分段线性插值，
 * 求目标 RT 阈值下可承受的最大并发与 QPS。
 *
 * <p>算法：按并发升序扫描，找到 P95 RT 首次越过目标阈值的相邻两点，
 * 线性插值出阈值交点（并发/QPS）；若错误率超限提前截断；若全程未达
 * 阈值则标记 extrapolated（容量未触顶）。
 */
@Component
public class CapacityAnalyzer {

    /** 错误率截断阈值（超过该错误率的档位视为已过载，不参与插值） */
    private static final double ERROR_CAP = 0.05;

    /**
     * 分析容量。
     *
     * @param points     压测分档采样点（stagePoints）
     * @param targetP95Ms 目标 P95 RT 阈值 ms
     */
    public CapacityResult analyze(List<StressTaskResult.StagePoint> points, double targetP95Ms) {
        CapacityResult result = new CapacityResult();
        result.setTargetP95Ms(targetP95Ms);
        if (points == null || points.isEmpty()) {
            return result;
        }

        List<StressTaskResult.StagePoint> sorted = new ArrayList<>(points);
        sorted.sort(Comparator.comparingInt(StressTaskResult.StagePoint::getConcurrency));

        // 构建曲线（跳过并发为 0 的点）
        List<CapacityResult.CurvePoint> curve = new ArrayList<>();
        for (StressTaskResult.StagePoint p : sorted) {
            if (p.getConcurrency() <= 0) {
                continue;
            }
            CapacityResult.CurvePoint c = new CapacityResult.CurvePoint();
            c.setConcurrency(p.getConcurrency());
            c.setQps(p.getQps());
            c.setP95Ms(p.getP95Ms());
            c.setP99Ms(p.getP99Ms());
            c.setErrorRate(p.getErrorRate());
            curve.add(c);
        }
        result.setCurve(curve);
        if (curve.isEmpty()) {
            return result;
        }

        // 截断到错误率阈值内（过载档位不可信）
        int effectiveEnd = curve.size();
        for (int i = 0; i < curve.size(); i++) {
            if (curve.get(i).getErrorRate() > ERROR_CAP) {
                effectiveEnd = i;
                break;
            }
        }
        if (effectiveEnd == 0) {
            effectiveEnd = 1;
        }

        double threshold = targetP95Ms;
        // 首档已超阈值：容量即为首档
        if (curve.get(0).getP95Ms() > threshold) {
            result.setMaxConcurrency(curve.get(0).getConcurrency());
            result.setMaxQps((int) curve.get(0).getQps());
            result.setExtrapolated(false);
            return result;
        }

        // 扫描交点
        for (int i = 1; i < effectiveEnd; i++) {
            CapacityResult.CurvePoint prev = curve.get(i - 1);
            CapacityResult.CurvePoint cur = curve.get(i);
            if (cur.getP95Ms() > threshold) {
                // 分段线性插值：求 P95 = threshold 的交点
                double ratio = (threshold - prev.getP95Ms()) / (cur.getP95Ms() - prev.getP95Ms());
                double concurrency = prev.getConcurrency() + ratio * (cur.getConcurrency() - prev.getConcurrency());
                double qps = prev.getQps() + ratio * (cur.getQps() - prev.getQps());
                result.setMaxConcurrency((int) Math.max(1, Math.floor(concurrency)));
                result.setMaxQps((int) Math.max(0, Math.floor(qps)));
                result.setExtrapolated(false);
                return result;
            }
        }

        // 全程未达阈值：容量未触顶，结果为测试上限（有效档位内）
        CapacityResult.CurvePoint last = curve.get(effectiveEnd - 1);
        result.setMaxConcurrency(last.getConcurrency());
        result.setMaxQps((int) last.getQps());
        result.setExtrapolated(true);
        return result;
    }
}
