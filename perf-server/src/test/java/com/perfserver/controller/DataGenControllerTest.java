package com.perfserver.controller;

import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * {@link DataGenController#carriesConnection} 单测。
 *
 * <p>该判定决定「是否走方案连接回填」。此前用 {@code !conn.isEmpty()} 判断，
 * 只带 rowOverrides 的请求体也会被当成携带连接，进而抛
 * 「connId 与 dbUrl 必须且只能提供其一」——与用户实际操作（只改了行数）不符。</p>
 */
class DataGenControllerTest {

    @Test
    void rowOverridesOnlyIsNotTreatedAsConnection() {
        assertFalse(DataGenController.carriesConnection(
                Map.of("rowOverrides", java.util.List.of(Map.of("table", "perf_order", "rowCount", 100)))),
                "仅带 rowOverrides 不得触发连接回填");
    }

    @Test
    void connIdCountsAsConnection() {
        assertTrue(DataGenController.carriesConnection(Map.of("connId", 5L)));
        assertTrue(DataGenController.carriesConnection(Map.of("connId", "5")));
    }

    @Test
    void dbUrlCountsAsConnection() {
        assertTrue(DataGenController.carriesConnection(
                Map.of("dbUrl", "jdbc:mysql://127.0.0.1:3306/demo")));
        assertTrue(DataGenController.carriesConnection(Map.of("connId", 5L, "dbUrl",
                "jdbc:mysql://127.0.0.1:3306/demo", "rowOverrides", java.util.List.of())));
    }

    @Test
    void blankOrNullConnectionFieldsDoNotCount() {
        assertFalse(DataGenController.carriesConnection(null));
        assertFalse(DataGenController.carriesConnection(Map.of()));
        assertFalse(DataGenController.carriesConnection(Map.of("connId", "")), "空串视同未指定");
        assertFalse(DataGenController.carriesConnection(Map.of("dbUrl", "   ")), "纯空白视同未指定");
    }
}
