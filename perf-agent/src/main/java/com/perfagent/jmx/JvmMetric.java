package com.perfagent.jmx;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * JVM 指标数据模型（agent 侧）：单次 JMX 采集快照。
 *
 * <p>上报平台后按字段映射入库（jvm_metric 表），字段名与后端实体保持一致。
 */
public class JvmMetric {

    public String appName;
    /** 采集时间戳 ms */
    public long collectTimeMs;
    /** 堆已用 MB */
    public double heapUsedMb;
    /** 堆最大值 MB */
    public double heapMaxMb;
    /** 非堆已用 MB */
    public double nonHeapUsedMb;
    /** Young GC 累计次数与耗时 ms */
    public long youngGcCount;
    public long youngGcMs;
    /** Full GC 累计次数与耗时 ms */
    public long fullGcCount;
    public long fullGcMs;
    /** 当前线程数 */
    public int threadCount;
    /** 进程 CPU 使用率 %（0~100） */
    public double cpuUsage;
    /** 系统 CPU 使用率 %（0~100） */
    public double systemCpu;
    /**
     * 扩展指标（P1-5，可选）：连接池/容器线程池/直接内存/Metaspace/线程状态细分。
     * 上报为 {@code extraMetrics} 嵌套 JSON；不可用（类/MBean 不存在）时为 null 不上报，
     * 保持旧表字段零破坏。
     */
    public Map<String, Object> extraMetrics;

    /** 转为上报 JSON 用的 Map */
    public Map<String, Object> toMap() {
        Map<String, Object> map = new LinkedHashMap<>();
        map.put("appName", appName);
        map.put("collectTime", collectTimeMs);
        map.put("heapUsedMb", heapUsedMb);
        map.put("heapMaxMb", heapMaxMb);
        map.put("nonHeapUsedMb", nonHeapUsedMb);
        map.put("youngGcCount", youngGcCount);
        map.put("youngGcMs", youngGcMs);
        map.put("fullGcCount", fullGcCount);
        map.put("fullGcMs", fullGcMs);
        map.put("threadCount", threadCount);
        map.put("cpuUsage", cpuUsage);
        map.put("systemCpu", systemCpu);
        if (extraMetrics != null && !extraMetrics.isEmpty()) {
            map.put("extraMetrics", extraMetrics);
        }
        return map;
    }
}
