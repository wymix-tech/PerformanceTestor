package com.perfagent.jmx;

import com.sun.management.GarbageCollectionNotificationInfo;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import java.util.concurrent.atomic.LongAccumulator;

import javax.management.Notification;
import javax.management.NotificationListener;
import javax.management.openmbean.CompositeData;

/**
 * 单次 GC 停顿明细跟踪器（P2-2）：监听 {@code GarbageCollectionNotificationInfo}，
 * 记录本采集周期内最大 STW 停顿与最近一次 GC 原因/动作。
 *
 * <p>监听器红线（GC 通知线程是 JVM 内部线程，绝不允许阻塞应用）：
 * <ul>
 *   <li>仅做 {@link LongAccumulator#accumulate} 与 {@link AtomicReference#set}
 *       无锁写入，绝不做网络 IO / JSON 序列化 / 字符串大拼接 / 锁等待；</li>
 *   <li>{@link #drain()} 采集线程每周期读取并原子归零（{@code getThenReset}），
 *       明细随 jvm 指标的 extraMetrics 上报；</li>
 *   <li>任何异常吞掉（CompositeData 解析失败等），不影响 GC 监听链。</li>
 * </ul>
 */
final class GcPauseTracker implements NotificationListener {

    private final LongAccumulator maxPauseMs = new LongAccumulator(Math::max, 0);
    private final AtomicReference<String> lastGcCause = new AtomicReference<>();
    private final AtomicReference<String> lastGcAction = new AtomicReference<>();

    @Override
    public void handleNotification(Notification notification, Object handback) {
        try {
            if (!GarbageCollectionNotificationInfo.GARBAGE_COLLECTION_NOTIFICATION
                    .equals(notification.getType())) {
                return;
            }
            GarbageCollectionNotificationInfo info =
                    GarbageCollectionNotificationInfo.from((CompositeData) notification.getUserData());
            long duration = info.getGcInfo() == null ? 0 : info.getGcInfo().getDuration();
            maxPauseMs.accumulate(Math.max(0, duration));
            lastGcAction.set(info.getGcAction());
            lastGcCause.set(info.getGcCause());
        } catch (Throwable ignored) {
            // CompositeData 解析异常等：静默，不影响 GC 通知链
        }
    }

    /** 读取并原子归零（collectOnce 每周期调用）：返回 extraMetrics 键值，本周期无 GC 时为 null */
    Map<String, Object> drain() {
        long max = maxPauseMs.getThenReset();
        if (max <= 0 && lastGcCause.get() == null) {
            return null;
        }
        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("maxGcPauseMs", max);
        String cause = lastGcCause.get();
        String action = lastGcAction.get();
        if (cause != null) {
            detail.put("lastGcCause", cause);
        }
        if (action != null) {
            detail.put("lastGcAction", action);
        }
        return detail;
    }
}
