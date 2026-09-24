package com.perfserver.common.page;

/**
 * 游标分页请求参数规范化：统一 {@code size} 上下限与游标解码口径。
 *
 * <p>各列表接口原先各写各的 {@code Math.max(1, Math.min(limit, 500))}，
 * 上限散落在 4 个 Controller 与 4 个 Mapper 中，改口径就得改八处。
 * 收口到此处后，默认值与上限只有一份事实。
 */
public final class CursorPageRequest {

    /** 默认每页条数（首屏） */
    public static final int DEFAULT_SIZE = 50;

    /** 每页条数上限：再大就失去分页意义，且会把响应体/前端渲染拖垮 */
    public static final int MAX_SIZE = 500;

    private CursorPageRequest() {
    }

    /**
     * 规范化每页条数。
     *
     * <p>{@code size <= 0}（含未传）时取默认值而非报错：首屏调用方习惯只传
     * {@code app}/{@code start}/{@code end}，对它们应保持"给一屏数据"的旧语义。
     */
    public static int normalizeSize(int size) {
        if (size <= 0) {
            return DEFAULT_SIZE;
        }
        return Math.min(size, MAX_SIZE);
    }

    /** 兼容旧 {@code limit} 参数：显式传 limit 时按 limit 取，否则用 size */
    public static int normalizeSize(int size, Integer legacyLimit) {
        if ((size <= 0) && legacyLimit != null) {
            return normalizeSize(legacyLimit);
        }
        return normalizeSize(size);
    }
}
