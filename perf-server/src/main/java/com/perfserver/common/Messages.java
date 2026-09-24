package com.perfserver.common;

import org.springframework.context.MessageSource;
import org.springframework.context.i18n.LocaleContextHolder;
import org.springframework.stereotype.Component;

import java.util.Locale;

/**
 * 后端文案解析（P3-1）。
 *
 * <p>用法：{@code messages.get("auth.invalid.credentials")}，
 * 带参：{@code messages.get("common.error.internal", e.getMessage())}。
 *
 * <p>语言取自 {@link LocaleContextHolder}——Spring MVC 会依据请求的
 * {@code Accept-Language} 头自动写入，无需各处手动透传。非 Web 线程
 * （定时任务、启动自检）取不到时回落默认 zh_CN。
 *
 * <p>取不到 key 时**不抛异常**，而是回显 key 本身：文案缺失不该把一次业务
 * 响应变成 500，回显 key 也让缺失点一眼可见（测试会另行断言 key 完整性）。
 */
@Component
public class Messages {

    private final MessageSource messageSource;

    public Messages(MessageSource messageSource) {
        this.messageSource = messageSource;
    }

    /** 取当前请求语言的文案；无参 */
    public String get(String key) {
        return get(key, new Object[0]);
    }

    /** 取当前请求语言的文案；params 依次对应 {0} {1}… */
    public String get(String key, Object... params) {
        Locale locale = LocaleContextHolder.getLocale();
        try {
            return messageSource.getMessage(key, params, locale);
        } catch (org.springframework.context.NoSuchMessageException e) {
            return key;
        }
    }
}
