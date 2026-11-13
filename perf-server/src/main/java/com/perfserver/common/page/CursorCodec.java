package com.perfserver.common.page;

import java.nio.charset.StandardCharsets;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.Base64;

/**
 * 游标编解码：{@code (时间, 主键 id)} 复合键 ↔ URL 安全字符串。
 *
 * <p>用 {@code Base64Url} 包裹 {@code 时间|id}，而不是把时间和 id 作为两个查询参数
 * 透传给前端：调用方只需原样回传一个字符串，无法选择性只传 half 游标
 * （只传时间会让并列时间的记录被整批跳过，是这类分页最隐蔽的漏数据方式）。
 *
 * <p>时间用 {@code epoch 毫秒}而非格式化字符串编进游标：毫秒是唯一能跨时区、
 * 跨驱动精度无损往返的形式（{@code DATETIME} 无时区，格式化字符串在
 * 解析端要靠隐式时区补全，容易在跨机部署时偏移几小时）。
 */
public final class CursorCodec {

    private static final DateTimeFormatter FORMAT = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss");
    private static final String SEPARATOR = "|";

    private CursorCodec() {
    }

    /** {@code (时间, id)} 复合游标 */
    public record Cursor(LocalDateTime time, long id) {
    }

    /** 编码：{@code Base64Url("epochMillis|id")}，无需 padding 便于放进 URL */
    public static String encode(LocalDateTime time, long id) {
        if (time == null) {
            return null;
        }
        long epochMilli = time.atZone(java.time.ZoneId.systemDefault()).toInstant().toEpochMilli();
        String raw = epochMilli + SEPARATOR + id;
        return Base64.getUrlEncoder().withoutPadding()
                .encodeToString(raw.getBytes(StandardCharsets.UTF_8));
    }

    /**
     * 解码：任何非法输入返回 {@code null}（调用方按"无游标"处理，即返回首屏）。
     *
     * <p>不抛异常是刻意的：游标是前端持有的不透明串，会因刷新、手工拼 URL、
     * 旧前端缓存而传进脏值，为此返回 400 会把"翻页时手滑"升级成页面报错。
     */
    public static Cursor decode(String cursor) {
        if (cursor == null || cursor.isBlank()) {
            return null;
        }
        try {
            String raw = new String(Base64.getUrlDecoder().decode(cursor.trim()), StandardCharsets.UTF_8);
            int sep = raw.lastIndexOf(SEPARATOR);
            if (sep <= 0 || sep == raw.length() - 1) {
                return null;
            }
            long epochMilli = Long.parseLong(raw.substring(0, sep));
            long id = Long.parseLong(raw.substring(sep + 1));
            LocalDateTime time = java.time.Instant.ofEpochMilli(epochMilli)
                    .atZone(java.time.ZoneId.systemDefault()).toLocalDateTime();
            return new Cursor(time, id);
        } catch (Exception e) {
            return null;
        }
    }

    /** 时间格式（供日志/调试打印，不参与游标编解码） */
    static String format(LocalDateTime time) {
        return time == null ? "" : time.format(FORMAT);
    }
}
