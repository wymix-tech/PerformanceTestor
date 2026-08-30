package com.perftestor.datagen.engine;

import com.perftestor.datagen.model.DataGenSpec;
import com.perftestor.datagen.model.RelationHint;
import com.perftestor.datagen.model.SchemaMetadata;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * RelationResolver 无状态契约测试：orm_join 语义参数化后，
 * 同一实例交替/并发解析不同 ormHints 互不污染
 * （回归旧 registerSource 实例字段缺陷：任务 B 的 hints 覆盖任务 A）。
 */
class RelationResolverStatelessTest {

    private static SchemaMetadata table(String name) {
        SchemaMetadata m = new SchemaMetadata();
        m.setTableName(name);
        return m;
    }

    private static List<SchemaMetadata> metas() {
        return List.of(table("t_order"), table("t_user"), table("t_shop"));
    }

    @Test
    void alternateResolutionsAreIsolated() {
        RelationResolver resolver = new RelationResolver(); // 单实例复用（模拟引擎并发复用）
        List<RelationHint> orderToUser = List.of(
                new RelationHint("t_order", "user_id", "t_user", "id", RelationHint.SRC_ORM_JOIN, 0.9));
        List<RelationHint> orderToShop = List.of(
                new RelationHint("t_order", "shop_id", "t_shop", "id", RelationHint.SRC_ORM_JOIN, 0.9));

        // 第一次解析（任务 A：order → user）
        var rA = resolver.resolve(metas(), new DataGenSpec(), null, null, orderToUser);
        assertEquals(1, rA.getAutoEdges().size());
        assertEquals("t_user", rA.getAutoEdges().get(0).getParentTable());

        // 第二次解析（任务 B：order → shop）——旧实现 registerSource 后 A 的 hints 仍残留
        var rB = resolver.resolve(metas(), new DataGenSpec(), null, null, orderToShop);
        assertEquals(1, rB.getAutoEdges().size());
        assertEquals("t_shop", rB.getAutoEdges().get(0).getParentTable());

        // 第三次：无 orm 语义（任务 C）——不得读到前序任何残留
        var rC = resolver.resolve(metas(), new DataGenSpec(), null, null, null);
        assertTrue(rC.getAutoEdges().isEmpty());
    }

    @Test
    void concurrentResolutionsDoNotCrossContaminate() throws Exception {
        RelationResolver resolver = new RelationResolver();
        List<RelationHint> toUser = List.of(
                new RelationHint("t_order", "user_id", "t_user", "id", RelationHint.SRC_ORM_JOIN, 0.9));
        List<RelationHint> toShop = List.of(
                new RelationHint("t_order", "shop_id", "t_shop", "id", RelationHint.SRC_ORM_JOIN, 0.9));

        int iterations = 200;
        AtomicBoolean contaminated = new AtomicBoolean(false);
        CountDownLatch latch = new CountDownLatch(2);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        try {
            pool.submit(() -> runLoop(resolver, toUser, "t_user", iterations, contaminated, latch));
            pool.submit(() -> runLoop(resolver, toShop, "t_shop", iterations, contaminated, latch));
            assertTrue(latch.await(15, TimeUnit.SECONDS));
        } finally {
            pool.shutdownNow();
        }
        assertTrue(!contaminated.get(), "并发解析出现任务间 ormHints 交叉污染");
    }

    private static void runLoop(RelationResolver resolver, List<RelationHint> hints,
                                String expectedParent, int iterations,
                                AtomicBoolean contaminated, CountDownLatch latch) {
        try {
            for (int i = 0; i < iterations; i++) {
                var r = resolver.resolve(metas(), new DataGenSpec(), null, null, hints);
                if (r.getAutoEdges().size() != 1
                        || !expectedParent.equals(r.getAutoEdges().get(0).getParentTable())) {
                    contaminated.set(true);
                    return;
                }
            }
        } catch (Throwable t) {
            contaminated.set(true);
        } finally {
            latch.countDown();
        }
    }
}
