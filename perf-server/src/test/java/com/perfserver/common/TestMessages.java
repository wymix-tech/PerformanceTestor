package com.perfserver.common;

import org.springframework.context.support.ResourceBundleMessageSource;

import java.util.Locale;

/**
 * 测试用 Messages：直接读真实的 i18n/messages*.properties，
 * 这样单测断言的就是线上同一份文案，而不是另造的桩字符串。
 * （若用 mock 返回固定串，key 拼错或 properties 缺条目都不会被发现。）
 */
public final class TestMessages {

    private TestMessages() {
    }

    public static ResourceBundleMessageSource source() {
        ResourceBundleMessageSource src = new ResourceBundleMessageSource();
        src.setBasename("i18n/messages");
        src.setDefaultEncoding("UTF-8");
        src.setFallbackToSystemLocale(false);
        return src;
    }

    /**
     * 默认语言（zh_CN）的 Messages 实例。
     * 注意：Messages 从 LocaleContextHolder 取语言，而该 holder 是 ThreadLocal——
     * 测试 JVM 里前面若跑过设置英文的用例，这里会串语言。故显式重置为中文。
     */
    public static Messages zh() {
        org.springframework.context.i18n.LocaleContextHolder.setLocale(Locale.SIMPLIFIED_CHINESE);
        return new Messages(source());
    }

    /** 指定语言的 Messages 实例（需在调用线程设置 LocaleContextHolder，见 withLocale） */
    public static Messages of() {
        return new Messages(source());
    }

    /** 在指定语言下执行，返回 Messages 解出的文案 */
    public static String resolve(Locale locale, String key, Object... params) {
        Locale prev = org.springframework.context.i18n.LocaleContextHolder.getLocale();
        try {
            org.springframework.context.i18n.LocaleContextHolder.setLocale(locale);
            return new Messages(source()).get(key, params);
        } finally {
            org.springframework.context.i18n.LocaleContextHolder.setLocale(prev);
        }
    }
}
