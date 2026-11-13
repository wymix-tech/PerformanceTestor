package com.perfserver.common.page;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

/**
 * 分页参数规范化：默认值、上限、旧 limit 兼容三条口径集中在此。
 */
class CursorPageRequestTest {

    /** 未传 size（<=0）取默认值而非报错：首屏调用方习惯只传过滤条件 */
    @Test
    void defaultsWhenSizeNotProvided() {
        assertEquals(CursorPageRequest.DEFAULT_SIZE, CursorPageRequest.normalizeSize(0));
        assertEquals(CursorPageRequest.DEFAULT_SIZE, CursorPageRequest.normalizeSize(-1));
    }

    @Test
    void keepsValidSize() {
        assertEquals(1, CursorPageRequest.normalizeSize(1));
        assertEquals(200, CursorPageRequest.normalizeSize(200));
    }

    @Test
    void clampsToMaxSize() {
        assertEquals(CursorPageRequest.MAX_SIZE, CursorPageRequest.normalizeSize(CursorPageRequest.MAX_SIZE + 1));
        assertEquals(CursorPageRequest.MAX_SIZE, CursorPageRequest.normalizeSize(Integer.MAX_VALUE));
    }

    /** 兼容旧前端：只传 limit 不传 size 时按 limit 生效 */
    @Test
    void legacyLimitUsedWhenSizeAbsent() {
        assertEquals(5, CursorPageRequest.normalizeSize(0, 5));
        assertEquals(CursorPageRequest.MAX_SIZE, CursorPageRequest.normalizeSize(0, 9999));
        assertEquals(CursorPageRequest.DEFAULT_SIZE, CursorPageRequest.normalizeSize(0, 0));
    }

    /** 新参数优先：同时传时以 size 为准，避免旧参数覆盖新语义 */
    @Test
    void sizeTakesPrecedenceOverLegacyLimit() {
        assertEquals(20, CursorPageRequest.normalizeSize(20, 5));
    }
}
