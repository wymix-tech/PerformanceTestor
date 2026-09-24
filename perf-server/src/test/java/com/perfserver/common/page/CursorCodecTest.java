package com.perfserver.common.page;

import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 游标编解码：往返无损、脏输入不抛异常（脏游标按"无游标"处理）。
 */
class CursorCodecTest {

    @Test
    void roundTripsTimeAndId() {
        LocalDateTime time = LocalDateTime.of(2026, 9, 24, 13, 45, 12, 345_000_000);
        CursorCodec.Cursor cursor = CursorCodec.decode(CursorCodec.encode(time, 987654321L));

        assertNotNull(cursor);
        assertEquals(time, cursor.time());
        assertEquals(987654321L, cursor.id());
    }

    /** 毫秒必须无损：DATETIME 精度到秒，但内存里的毫秒若不保留会让同秒内多条记录被跳过 */
    @Test
    void preservesMilliseconds() {
        LocalDateTime time = LocalDateTime.of(2026, 1, 2, 3, 4, 5, 678_000_000);
        CursorCodec.Cursor cursor = CursorCodec.decode(CursorCodec.encode(time, 1L));
        assertEquals(678_000_000, cursor.time().getNano());
    }

    /** URL 安全：游标会放进 query string，不能出现 + / = 等需转义字符 */
    @Test
    void encodedCursorIsUrlSafe() {
        String encoded = CursorCodec.encode(LocalDateTime.of(2026, 9, 24, 0, 0), Long.MAX_VALUE);
        assertTrue(encoded.matches("[A-Za-z0-9_-]+"), "游标应只含 URL 安全字符，实际 " + encoded);
    }

    @Test
    void nullTimeEncodesToNull() {
        assertNull(CursorCodec.encode(null, 1L));
    }

    // ==================== 脏输入：一律返回 null（按无游标 = 首屏处理）====================

    @Test
    void decodeRejectsNullAndBlank() {
        assertNull(CursorCodec.decode(null));
        assertNull(CursorCodec.decode(""));
        assertNull(CursorCodec.decode("   "));
    }

    @Test
    void decodeRejectsNonBase64() {
        assertNull(CursorCodec.decode("!!!not-base64!!!"));
    }

    /** 合法 Base64 但内容不是 {epoch}|{id} 形式（例如旧版本或手拼） */
    @Test
    void decodeRejectsMalformedPayload() {
        String bad = java.util.Base64.getUrlEncoder().withoutPadding()
                .encodeToString("garbage".getBytes(java.nio.charset.StandardCharsets.UTF_8));
        assertNull(CursorCodec.decode(bad));
        assertNull(CursorCodec.decode(encode("1234567890|")));   // id 缺失
        assertNull(CursorCodec.decode(encode("|42")));            // 时间缺失
        assertNull(CursorCodec.decode(encode("abc|42")));         // 时间非数字
        assertNull(CursorCodec.decode(encode("1234567890|xyz"))); // id 非数字
    }

    private static String encode(String raw) {
        return java.util.Base64.getUrlEncoder().withoutPadding()
                .encodeToString(raw.getBytes(java.nio.charset.StandardCharsets.UTF_8));
    }
}
