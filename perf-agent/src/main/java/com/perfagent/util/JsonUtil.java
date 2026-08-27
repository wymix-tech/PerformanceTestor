package com.perfagent.util;

import java.util.Collection;
import java.util.Map;

/**
 * 轻量 JSON 序列化工具：支持 Map / Collection / String / Number / Boolean / null 与数组。
 *
 * <p>agent 依赖精简（不引入第三方 JSON 库），上报数据统一用 Map/List 构造后序列化。
 * 如需格式化输出，可调用 {@link #pretty(Map)}。
 */
public final class JsonUtil {

    private JsonUtil() {
    }

    /** 序列化为紧凑 JSON */
    public static String serialize(Object value) {
        StringBuilder sb = new StringBuilder(256);
        writeValue(sb, value);
        return sb.toString();
    }

    /** 序列化为带缩进的 JSON（调试用） */
    public static String pretty(Map<String, Object> value) {
        return pretty(value, 0);
    }

    private static void writeValue(StringBuilder sb, Object value) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String str) {
            writeString(sb, str);
        } else if (value instanceof Number || value instanceof Boolean) {
            sb.append(value);
        } else if (value instanceof Map<?, ?> map) {
            writeMap(sb, map);
        } else if (value instanceof Collection<?> col) {
            writeArray(sb, col);
        } else if (value.getClass().isArray()) {
            writeArray(sb, toCollection(value));
        } else {
            writeString(sb, String.valueOf(value));
        }
    }

    private static void writeMap(StringBuilder sb, Map<?, ?> map) {
        sb.append('{');
        boolean first = true;
        for (Map.Entry<?, ?> e : map.entrySet()) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            writeString(sb, String.valueOf(e.getKey()));
            sb.append(':');
            writeValue(sb, e.getValue());
        }
        sb.append('}');
    }

    private static void writeArray(StringBuilder sb, Collection<?> col) {
        sb.append('[');
        boolean first = true;
        for (Object o : col) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            writeValue(sb, o);
        }
        sb.append(']');
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default -> {
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
                }
            }
        }
        sb.append('"');
    }

    private static Collection<?> toCollection(Object array) {
        int len = java.lang.reflect.Array.getLength(array);
        java.util.List<Object> list = new java.util.ArrayList<>(len);
        for (int i = 0; i < len; i++) {
            list.add(java.lang.reflect.Array.get(array, i));
        }
        return list;
    }

    private static String pretty(Object value, int indent) {
        // 简单缩进实现，便于调试阅读
        StringBuilder sb = new StringBuilder();
        String pad = "  ".repeat(indent);
        if (value instanceof Map<?, ?> map) {
            sb.append("{\n");
            int i = 0;
            for (Map.Entry<?, ?> e : map.entrySet()) {
                sb.append(pad).append("  \"").append(e.getKey()).append("\": ");
                sb.append(pretty(e.getValue(), indent + 1));
                if (++i < map.size()) {
                    sb.append(',');
                }
                sb.append('\n');
            }
            sb.append(pad).append('}');
        } else if (value instanceof Collection<?> col) {
            sb.append("[\n");
            int i = 0;
            for (Object o : col) {
                sb.append(pad).append("  ").append(pretty(o, indent + 1));
                if (++i < col.size()) {
                    sb.append(',');
                }
                sb.append('\n');
            }
            sb.append(pad).append(']');
        } else {
            StringBuilder raw = new StringBuilder();
            writeValue(raw, value);
            sb.append(raw);
        }
        return sb.toString();
    }
}
