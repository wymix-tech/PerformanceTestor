package com.perfserver.common;

/**
 * 字符串工具（项目内通用小函数收敛处）。
 */
public final class Texts {

    private Texts() {
    }

    /** null 安全截断：超长追加省略号 */
    public static String truncate(String s, int max) {
        if (s == null) {
            return "";
        }
        return s.length() <= max ? s : s.substring(0, max) + "...";
    }
}
