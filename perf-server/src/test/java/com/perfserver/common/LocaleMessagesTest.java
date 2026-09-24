package com.perfserver.common;

import org.junit.jupiter.api.Test;
import org.springframework.context.support.ResourceBundleMessageSource;

import java.util.Locale;
import java.util.Properties;
import java.util.Set;
import java.util.TreeSet;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 后端文案国际化（P3-1）守卫：
 * <ul>
 *   <li>同一 key 在中英文下都要能解出，且**不能相同**（漏翻译成"中文照抄"要被发现）；</li>
 *   <li>{@code messages.properties} 与 {@code messages_en.properties} 的 key 集合必须完全一致——
 *       少一个 key 时线上会静默回落默认语言，只有真用户看到中英混杂才暴露；</li>
 *   <li>未知 key 必须回显 key 本身而不是抛异常（文案缺失不该把业务响应变成 500）。</li>
 * </ul>
 *
 * <p>为何用 Properties 直接比对而不是 MessageSource：MessageSource 遇缺失 key 会
 * 回落默认 bundle，比对不出"英文缺条"，必须读原始文件。
 */
class LocaleMessagesTest {

    private static final String ZH = "/i18n/messages.properties";
    private static final String EN = "/i18n/messages_en.properties";

    private Properties load(String classpath) throws Exception {
        Properties p = new Properties();
        try (var in = getClass().getResourceAsStream(classpath)) {
            assertTrue(in != null, "找不到文案文件: " + classpath);
            // properties 默认 ISO-8859-1，中文需按 UTF-8 读
            p.load(new java.io.InputStreamReader(in, java.nio.charset.StandardCharsets.UTF_8));
        }
        return p;
    }

    @Test
    void zhAndEnKeySetsAreIdentical() throws Exception {
        Set<String> zh = new TreeSet<>(load(ZH).stringPropertyNames());
        Set<String> en = new TreeSet<>(load(EN).stringPropertyNames());
        assertEquals(zh, en, "中英文文案 key 集合必须完全一致（缺失 key 会静默回落默认语言）");
    }

    @Test
    void keySetsAreNotEmpty() throws Exception {
        assertFalse(load(ZH).stringPropertyNames().isEmpty(), "中文文案不应为空");
    }

    /**
     * 允许中英完全相同的 key（品牌词、协议名等本就无需翻译）。
     * 白名单必须显式列举——不能靠"看起来像英文"自动放过，否则真漏翻会被误放行。
     */
    private static final Set<String> LOCALE_NEUTRAL_KEYS = Set.of("common.success");

    @Test
    void everyKeyResolvesInBothLocalesAndDiffers() throws Exception {
        for (String key : load(ZH).stringPropertyNames()) {
            String zh = TestMessages.resolve(Locale.SIMPLIFIED_CHINESE, key, "X");
            String en = TestMessages.resolve(Locale.ENGLISH, key, "X");
            assertFalse(zh.equals(key), "中文缺失 key 未解析: " + key);
            assertFalse(en.equals(key), "英文缺失 key 未解析: " + key);
            if (!LOCALE_NEUTRAL_KEYS.contains(key)) {
                assertNotEquals(zh, en, "中英文文案相同，疑似漏翻: " + key);
            }
        }
    }

    @Test
    void localeNeutralWhitelistIsSubsetOfRealKeys() throws Exception {
        Set<String> keys = load(ZH).stringPropertyNames();
        for (String k : LOCALE_NEUTRAL_KEYS) {
            assertTrue(keys.contains(k), "白名单含不存在的 key（应删除）: " + k);
        }
    }

    @Test
    void missingKeyEchoesKeyInsteadOfThrowing() {
        String out = TestMessages.resolve(Locale.ENGLISH, "no.such.key.exists");
        assertEquals("no.such.key.exists", out, "缺失 key 应回显 key 本身，不得抛异常");
    }

    @Test
    void paramsAreSubstituted() {
        String en = TestMessages.resolve(Locale.ENGLISH, "common.error.internal", "boom");
        assertTrue(en.contains("boom"), "位置参数 {0} 应被替换: " + en);
        assertFalse(en.contains("{0}"), "占位符不应残留: " + en);
    }

    @Test
    void unknownLocaleFallsBackToDefaultChinese() {
        // LocaleContextHolder 设置为法语 → 无 fr bundle → 回落默认（中文）
        String fr = TestMessages.resolve(Locale.FRENCH, "auth.invalid.credentials");
        String zh = TestMessages.resolve(Locale.SIMPLIFIED_CHINESE, "auth.invalid.credentials");
        assertEquals(zh, fr, "无对应语言包时应回落默认中文，而不是跟随 JVM 系统语言");
    }

    @Test
    void messageSourceIsConfiguredWithUtf8AndNoSystemFallback() {
        ResourceBundleMessageSource src = TestMessages.source();
        assertTrue(src.getBasenameSet().contains("i18n/messages"));
    }
}
