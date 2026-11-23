package com.perfagent.jmx;

import com.sun.management.GarbageCollectionNotificationInfo;
import org.junit.jupiter.api.Test;

import javax.management.Notification;
import javax.management.NotificationEmitter;
import java.lang.management.ManagementFactory;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * GcPauseTracker 测试（P2-2）：真实 GC 通知（System.gc 触发）+ drain 原子归零。
 */
class GcPauseTrackerTest {

    @Test
    void realGcEventsAreCapturedAndDrainResets() throws Exception {
        GcPauseTracker tracker = new GcPauseTracker();
        List<NotificationEmitter> emitters = new ArrayList<>();
        for (var gc : ManagementFactory.getGarbageCollectorMXBeans()) {
            if (gc instanceof NotificationEmitter emitter) {
                emitter.addNotificationListener(tracker, n ->
                        GarbageCollectionNotificationInfo.GARBAGE_COLLECTION_NOTIFICATION
                                .equals(n.getType()), null);
                emitters.add(emitter);
            }
        }
        try {
            System.gc();
            System.gc();
            Thread.sleep(200);
            Map<String, Object> detail = tracker.drain();
            assertNotNull(detail, "触发 System.gc 后应捕获 GC 通知");
            assertTrue((Long) detail.get("maxGcPauseMs") >= 0);
            assertNotNull(detail.get("lastGcAction"));
            assertNotNull(detail.get("lastGcCause"));
            // maxPause 原子归零，lastGcCause 为"最近一次"粘性语义（保留）
            Map<String, Object> after = tracker.drain();
            assertEquals(0L, after.get("maxGcPauseMs"));
            assertNotNull(after.get("lastGcCause"));
        } finally {
            for (NotificationEmitter e : emitters) {
                e.removeNotificationListener(tracker);
            }
        }
    }

    @Test
    void nonGcNotificationIsIgnored() {
        GcPauseTracker tracker = new GcPauseTracker();
        tracker.handleNotification(new Notification("other.type", "src", 1), null);
        assertNull(tracker.drain());
    }
}
