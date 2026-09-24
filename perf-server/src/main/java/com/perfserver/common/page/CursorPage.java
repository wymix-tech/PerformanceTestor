package com.perfserver.common.page;

import com.fasterxml.jackson.annotation.JsonInclude;

import java.util.List;

/**
 * 游标分页结果：{@code items + nextCursor + hasMore}。
 *
 * <p>取代原先各列表接口的 {@code LIMIT n} 硬顶口径——过去只能看最新一屏，
 * 历史数据既翻不了页也拿不到总数。改用游标而非 {@code OFFSET}：
 * 时序表数据持续写入，{@code OFFSET} 深翻既慢又会因新增行导致漏读/重读。
 *
 * <p>排序存在并列时（同一毫秒多条记录）必须有稳定的次序键，故排序键统一为
 * {@code (time, id)} 复合键，游标里同时携带两者，避免深翻时漏数据。
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public class CursorPage<T> {

    private List<T> items;

    /** 下一页游标；{@code hasMore=false} 时为 null */
    private String nextCursor;

    /** 是否还有下一页 */
    private boolean hasMore;

    public CursorPage() {
    }

    public CursorPage(List<T> items, String nextCursor, boolean hasMore) {
        this.items = items;
        this.nextCursor = nextCursor;
        this.hasMore = hasMore;
    }

    /** 空页（无命中时返回，游标为 null） */
    public static <T> CursorPage<T> empty() {
        return new CursorPage<>(List.of(), null, false);
    }

    public List<T> getItems() {
        return items;
    }

    public void setItems(List<T> items) {
        this.items = items;
    }

    public String getNextCursor() {
        return nextCursor;
    }

    public void setNextCursor(String nextCursor) {
        this.nextCursor = nextCursor;
    }

    public boolean isHasMore() {
        return hasMore;
    }

    public void setHasMore(boolean hasMore) {
        this.hasMore = hasMore;
    }
}
