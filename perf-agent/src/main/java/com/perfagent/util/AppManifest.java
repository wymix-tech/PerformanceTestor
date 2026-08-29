package com.perfagent.util;

import java.util.jar.Attributes;
import java.util.jar.JarFile;
import java.util.jar.Manifest;

/**
 * 被测应用 jar 的 MANIFEST 读取（懒探测一次缓存，premain 期即可用）。
 *
 * <p>jar 定位策略：
 * <ol>
 *   <li>{@code sun.java.command} 形如 "-jar /path/app.jar --spring..." —— java -jar 启动，最精确；</li>
 *   <li>兜底 java.class.path 中的 jar（排除 agent 自身；IDE exploded 运行时可能取到依赖 jar，返回 null 更保守）。</li>
 * </ol>
 *
 * <p>用途：
 * <ul>
 *   <li>Start-Class / Main-Class 包名 → 推导业务根包（埋点范围）；</li>
 *   <li>Implementation-Version → 应用版本上报。</li>
 * </ul>
 */
public final class AppManifest {

    private static volatile boolean resolved = false;
    private static volatile Manifest manifest;

    private AppManifest() {
    }

    /** 启动类全限定名：优先 Spring Boot 的 Start-Class，退回 Main-Class；未知返回 null */
    public static String startClass() {
        Manifest mf = manifest();
        if (mf == null) {
            return null;
        }
        String v = mf.getMainAttributes().getValue("Start-Class");
        if (v == null || v.isBlank()) {
            v = mf.getMainAttributes().getValue(Attributes.Name.MAIN_CLASS);
        }
        return v == null ? null : v.trim();
    }

    /** 应用版本：Implementation-Version（退回 Build-Version）；未知返回空串 */
    public static String implementationVersion() {
        Manifest mf = manifest();
        if (mf == null) {
            return "";
        }
        String v = mf.getMainAttributes().getValue(Attributes.Name.IMPLEMENTATION_VERSION);
        if (v == null || v.isBlank()) {
            v = mf.getMainAttributes().getValue("Build-Version");
        }
        return v == null ? "" : v.trim();
    }

    /** 懒读取并缓存被测应用 jar 的 MANIFEST（只读一次） */
    private static Manifest manifest() {
        if (!resolved) {
            synchronized (AppManifest.class) {
                if (!resolved) {
                    manifest = readManifest();
                    resolved = true;
                }
            }
        }
        return manifest;
    }

    private static Manifest readManifest() {
        try {
            String jarPath = locateAppJar();
            if (jarPath == null) {
                System.err.println("[perf-agent] AppManifest: no app jar located"
                        + " (sun.java.command=" + System.getProperty("sun.java.command")
                        + ", java.class.path=" + System.getProperty("java.class.path") + ")");
                return null;
            }
            try (JarFile jf = new JarFile(jarPath)) {
                Manifest mf = jf.getManifest();
                if (mf == null) {
                    System.err.println("[perf-agent] AppManifest: jar has no manifest: " + jarPath);
                }
                return mf;
            }
        } catch (Throwable e) {
            System.err.println("[perf-agent] AppManifest: read failed - " + e);
            return null;
        }
    }

    /** 定位被测应用 jar（-jar 启动优先，classpath 兜底并排除 agent 自身） */
    private static String locateAppJar() {
        // 1) -jar 启动：命令行第二个参数即应用 jar
        String cmd = System.getProperty("sun.java.command", "");
        if (cmd.startsWith("-jar ")) {
            String[] parts = cmd.split("\\s+");
            if (parts.length > 1 && parts[1].endsWith(".jar")) {
                return parts[1];
            }
        }
        // 2) 兜底：classpath 中的 jar（排除 -javaagent 自身的 jar）
        for (String p : System.getProperty("java.class.path", "").split(java.io.File.pathSeparator)) {
            if (p.endsWith(".jar") && !p.toLowerCase().contains("perf-agent")) {
                return p;
            }
        }
        return null;
    }
}
