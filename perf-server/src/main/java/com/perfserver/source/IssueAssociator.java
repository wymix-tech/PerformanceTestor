package com.perfserver.source;

import com.perfserver.entity.SourceFile;
import com.perfserver.entity.SourceRepo;
import com.perfserver.mapper.SourceFileMapper;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 检测结果与源码关联定位（ORM 无关，多级策略）：
 * <ol>
 *   <li>调用方法链定位（首选，ORM 无关）：慢 SQL 等事件携带的方法链按类名/方法名精确匹配</li>
 *   <li>MyBatis：Mapper XML 模糊匹配 SQL 片段 → DAO 接口方法（仅 MyBatis 项目命中）</li>
 *   <li>JPA / Hibernate / Spring Data JDBC：SQL 表名 → @Table(name) / @Entity 驼峰推断实体类</li>
 *   <li>热点方法 → 类名/方法名精确匹配</li>
 *   <li>压测瓶颈 → URL 关联 Controller 的 RequestMapping</li>
 * </ol>
 */
@Component
public class IssueAssociator {

    private static final Pattern XML_METHOD_ID = Pattern.compile("<(select|insert|update|delete)\\s+id\\s*=\\s*\"([\\w.]+)\"");
    private static final Pattern METHOD_DEF = Pattern.compile("(public|private|protected)?\\s*[\\w<>\\[\\],\\s.]+\\s+([a-zA-Z_$][\\w$]*)\\s*\\(");
    private static final Pattern REQUEST_MAPPING = Pattern.compile("@(?:RequestMapping|GetMapping|PostMapping|PutMapping|DeleteMapping)\\s*\\(?\\s*[\"']?([^\"'\\)]*)");

    private final SourceFileMapper sourceFileMapper;
    private final SourceFileService sourceFileService;
    private final SourceManager sourceManager;
    private final com.fasterxml.jackson.databind.ObjectMapper objectMapper = new com.fasterxml.jackson.databind.ObjectMapper();

    public IssueAssociator(SourceFileMapper sourceFileMapper, SourceFileService sourceFileService,
                           SourceManager sourceManager) {
        this.sourceFileMapper = sourceFileMapper;
        this.sourceFileService = sourceFileService;
        this.sourceManager = sourceManager;
    }

    /** 关联结果：源码文件与行区间 */
    public static class IssueLocation {
        public long repoId;
        public long fileId;
        public int lineStart;
        public int lineEnd;

        public IssueLocation(long repoId, long fileId, int lineStart, int lineEnd) {
            this.repoId = repoId;
            this.fileId = fileId;
            this.lineStart = lineStart;
            this.lineEnd = lineEnd;
        }
    }

    /**
     * 慢 SQL 关联统一入口（ORM 无关，多级兜底）：
     * <ol>
     *   <li>调用方法链定位（首选，任何 ORM 均有效：类名/方法名精确匹配业务代码）</li>
     *   <li>MyBatis Mapper XML 匹配（项目使用 MyBatis 时）</li>
     *   <li>JPA/Hibernate 实体定位：SQL 表名 → @Table(name) / @Entity 驼峰推断（JPA、Hibernate、
     *       Spring Data JDBC 等注解式 ORM 通用）</li>
     * </ol>
     *
     * @return 定位结果；三级均未命中返回 null
     */
    public IssueLocation associateSlowSqlSmart(SourceRepo repo, String sqlText, String methodChainJson) {
        IssueLocation loc = associateByMethodChain(repo, methodChainJson);
        if (loc != null) {
            return loc;
        }
        loc = associateMyBatisXml(repo, sqlText);
        if (loc != null) {
            return loc;
        }
        return associateByTableEntity(repo, sqlText);
    }

    /**
     * JPA / Hibernate / Spring Data JDBC 实体定位：从 SQL 提取表名，
     * 扫描 Java 源码按「@Table(name = "表名")」精确匹配，未命中再按
     * 「@Entity + 类名驼峰转下划线 == 表名」推断（JPA 默认命名策略）。
     */
    public IssueLocation associateByTableEntity(SourceRepo repo, String sqlText) {
        String table = extractTableName(sqlText);
        if (table == null || table.isBlank()) {
            return null;
        }
        String shortTable = table.contains(".") ? table.substring(table.lastIndexOf('.') + 1) : table;
        List<SourceFile> javaFiles = sourceFileMapper.listByRepoContains(repo.getId(), ".java", 5000)
                .stream().filter(f -> f.getRelativePath().endsWith(".java")).toList();
        SourceFile entityFallback = null;
        for (SourceFile sf : javaFiles) {
            String content = readSafely(repo, sf);
            if (content == null) {
                continue;
            }
            // 1) 显式 @Table(name = "t_order")（容忍反引号/单双引号/空白）
            if (content.matches("(?s).*@Table\\s*\\(\\s*name\\s*=\\s*[\"`']" + Pattern.quote(shortTable) + "[\"`'].*")) {
                return new IssueLocation(repo.getId(), sf.getId(), 1, Math.min(30, lineCount(content)));
            }
            // 2) @Entity + 类名推断（JPA 默认命名策略：类名驼峰转下划线 == 表名）
            if (entityFallback == null && content.contains("@Entity")) {
                String className = sf.getFileName().replace(".java", "");
                if (camelToSnake(className).equals(shortTable)) {
                    entityFallback = sf;
                }
            }
        }
        if (entityFallback != null) {
            return new IssueLocation(repo.getId(), entityFallback.getId(), 1,
                    Math.min(30, lineCount(readSafely(repo, entityFallback))));
        }
        return null;
    }

    /** 从 SQL 提取主表名（FROM/JOIN/INTO/UPDATE 后），无则返回 null */
    private String extractTableName(String sql) {
        if (sql == null || sql.isBlank()) {
            return null;
        }
        Matcher m = Pattern.compile("(?:from|join|into|update)\\s+`?([A-Za-z0-9_.]+)`?", Pattern.CASE_INSENSITIVE)
                .matcher(sanitize(sql));
        return m.find() ? m.group(1) : null;
    }

    /** 复用 DbExplainService 同款清理：剥掉驱动 toString 前缀噪音，定位到 SQL 关键字 */
    private String sanitize(String sql) {
        String s = sql.trim();
        Matcher m = Pattern.compile("(select|insert|update|delete|with)\\s", Pattern.CASE_INSENSITIVE).matcher(s);
        if (m.find() && m.start() > 0) {
            s = s.substring(m.start());
        }
        return s.trim();
    }

    /** 驼峰转下划线（JPA 默认命名策略）：OrderInfo → order_info */
    private static String camelToSnake(String name) {
        return name.replaceAll("([a-z\\d])([A-Z])", "$1_$2").toLowerCase();
    }

    private static int lineCount(String content) {
        return content == null ? 1 : content.split("\n", -1).length;
    }

    /**
     * MyBatis 专属关联：提取 SQL 关键词片段，在 Mapper XML 中匹配（仅当项目使用 MyBatis 时命中）。
     */
    public IssueLocation associateMyBatisXml(SourceRepo repo, String sqlText) {
        if (sqlText == null || sqlText.isBlank()) {
            return null;
        }
        String keyFragment = extractSqlFragment(sqlText);
        if (keyFragment == null) {
            return null;
        }
        // 1. 列出 Mapper XML 文件
        List<SourceFile> xmlFiles = sourceFileMapper.listByRepoContains(repo.getId(), ".xml", 5000)
                .stream().filter(f -> f.getRelativePath().endsWith(".xml")).toList();
        for (SourceFile xml : xmlFiles) {
            String content = readSafely(repo, xml);
            if (content == null || !content.contains(keyFragment)) {
                continue;
            }
            // 2. 匹配最近的 statement id（方法名）
            String methodId = findNearestMethodId(content, content.indexOf(keyFragment));
            if (methodId == null) {
                continue;
            }
            // 3. 同目录下找对应 DAO 接口（XML 名去掉 .xml 后缀）
            String baseName = xml.getFileName().replace(".xml", "");
            List<SourceFile> daos = sourceFileMapper.listByName(repo.getId(), baseName + ".java", 10);
            if (!daos.isEmpty()) {
                SourceFile dao = daos.get(0);
                String daoContent = readSafely(repo, dao);
                int[] lines = findMethodLine(daoContent, methodId);
                if (lines != null) {
                    return new IssueLocation(repo.getId(), dao.getId(), lines[0], lines[1]);
                }
            }
            // 4. 兜底：直接定位 XML 中该 statement 所在行
            return new IssueLocation(repo.getId(), xml.getId(), content.indexOf(keyFragment) > 0 ? 1 : 1, 1);
        }
        return null;
    }

    /** 热点方法关联：按类名/方法名精确匹配 */
    public IssueLocation associateHotMethod(SourceRepo repo, String className, String methodName) {
        String simpleName = className == null ? null : className.substring(className.lastIndexOf('.') + 1);
        List<SourceFile> candidates = new ArrayList<>();
        if (simpleName != null && !simpleName.isBlank()) {
            candidates.addAll(sourceFileMapper.listByName(repo.getId(), simpleName + ".java", 20));
        }
        if (candidates.isEmpty() && className != null && !className.isBlank()) {
            // 按类路径包含找（如 com/foo/Bar）
            String pathPrefix = className.replace('.', '/');
            candidates.addAll(sourceFileMapper.listByRepoContains(repo.getId(), pathPrefix, 20));
        }
        for (SourceFile sf : candidates) {
            String content = readSafely(repo, sf);
            if (content == null) {
                continue;
            }
            int[] lines = findMethodLine(content, methodName);
            if (lines != null) {
                return new IssueLocation(repo.getId(), sf.getId(), lines[0], lines[1]);
            }
        }
        return null;
    }

    /**
     * 方法链关联：慢 SQL 等事件携带的调用方法链（根 → 叶，如 Controller → Service → Mapper），
     * 从链尾（最靠近 DB 的方法，即 SQL 执行处）向链首逐个尝试精确定位，
     * 命中即返回（优先定位 SQL 所在的 DAO/Mapper，其次定位业务 Service/Controller）。
     *
     * @param methodChainJson 方法链 JSON 数组，如 ["com.a.OrderController#list","com.a.OrderMapper#select"]
     */
    public IssueLocation associateByMethodChain(SourceRepo repo, String methodChainJson) {
        if (methodChainJson == null || methodChainJson.isBlank()) {
            return null;
        }
        List<String[]> chain = new ArrayList<>();
        try {
            com.fasterxml.jackson.databind.JsonNode root = objectMapper.readTree(methodChainJson);
            if (!root.isArray()) {
                return null;
            }
            for (com.fasterxml.jackson.databind.JsonNode n : root) {
                String m = n.asText("");
                // 兼容两种格式：agent 上报 "包.类.方法"（最后一个 . 前为类）与 "包.类#方法"
                int hash = m.lastIndexOf('#');
                int dot = m.lastIndexOf('.');
                if (hash > 0) {
                    chain.add(new String[]{m.substring(0, hash), m.substring(hash + 1)});
                } else if (dot > 0) {
                    chain.add(new String[]{m.substring(0, dot), m.substring(dot + 1)});
                }
            }
        } catch (Exception e) {
            return null;
        }
        // 链尾优先：SQL 定义在 DAO/Mapper；接口方法声明行同样可被 findMethodLine 匹配
        for (int i = chain.size() - 1; i >= 0; i--) {
            IssueLocation loc = associateHotMethod(repo, chain.get(i)[0], chain.get(i)[1]);
            if (loc != null) {
                return loc;
            }
        }
        return null;
    }

    /** 压测瓶颈 URL 关联：URL 匹配 Controller RequestMapping，定位处理方法 */
    public IssueLocation associateUrl(SourceRepo repo, String url) {
        if (url == null || url.isBlank()) {
            return null;
        }
        String normalized = normalizeUrl(url);
        List<SourceFile> controllers = sourceFileMapper.listByRepoContains(repo.getId(), "controller", 5000)
                .stream().filter(f -> f.getFileName().endsWith(".java")).toList();
        for (SourceFile ctl : controllers) {
            String content = readSafely(repo, ctl);
            if (content == null) {
                continue;
            }
            // 类级 @RequestMapping 前缀
            String classPrefix = findClassMappingPrefix(content);
            int[] lines = findMappingMethodLine(content, normalized, classPrefix);
            if (lines != null) {
                return new IssueLocation(repo.getId(), ctl.getId(), lines[0], lines[1]);
            }
        }
        return null;
    }

    /** 从 SQL 中提取用于模糊匹配的关键片段（表名 或 前 40 字符） */
    private String extractSqlFragment(String sql) {
        String s = sql.trim();
        Matcher m = Pattern.compile("(?:from|into|update|join)\\s+([`\\w.]+)", Pattern.CASE_INSENSITIVE).matcher(s);
        if (m.find()) {
            String table = m.group(1).replace("`", "");
            if (!table.isBlank()) {
                return table;
            }
        }
        return s.length() > 40 ? s.substring(0, 40) : s;
    }

    /** 找到 keywordIndex 之前最近的 statement id */
    private String findNearestMethodId(String xml, int keywordIndex) {
        Matcher m = XML_METHOD_ID.matcher(xml);
        String last = null;
        while (m.find()) {
            if (m.start() > keywordIndex) {
                break;
            }
            last = m.group(2);
        }
        return last;
    }

    /** 在 Java 内容中找到方法定义行区间 */
    private int[] findMethodLine(String content, String methodName) {
        if (content == null || methodName == null || methodName.isBlank()) {
            return null;
        }
        String[] lines = content.split("\n", -1);
        Pattern p = Pattern.compile("\\b" + Pattern.quote(methodName) + "\\s*\\(");
        for (int i = 0; i < lines.length; i++) {
            String line = lines[i];
            // 跳过方法调用（以 . 或 this. 开头）与注解行
            if (line.trim().startsWith("//") || line.contains("." + methodName + "(")) {
                continue;
            }
            if (p.matcher(line).find() && line.contains(")")) {
                int end = Math.min(lines.length, i + 8);
                return new int[]{i + 1, end};
            }
        }
        return null;
    }

    /** 找 Controller 类级 @RequestMapping 前缀 */
    private String findClassMappingPrefix(String content) {
        for (String line : content.split("\n")) {
            Matcher m = REQUEST_MAPPING.matcher(line);
            if (m.find()) {
                String path = m.group(1).trim().replace("\"", "");
                if (path.startsWith("/") && line.contains("@RequestMapping")) {
                    return path;
                }
            }
        }
        return "";
    }

    /** 找匹配 URL 的处理方法（含类级前缀拼接） */
    private int[] findMappingMethodLine(String content, String normalizedUrl, String classPrefix) {
        String[] lines = content.split("\n", -1);
        for (int i = 0; i < lines.length; i++) {
            String line = lines[i];
            if (!line.trim().startsWith("@")) {
                continue;
            }
            Matcher m = REQUEST_MAPPING.matcher(line);
            String path = null;
            if (m.find()) {
                path = m.group(1).trim().replace("\"", "");
            }
            if (path == null || path.isBlank()) {
                continue;
            }
            String full = (path.startsWith("/") ? "" : classPrefix) + path;
            if (normalizeUrl(full).equals(normalizedUrl) || normalizedUrl.contains(normalizeUrl(full))) {
                // 方法定义在注解下一行或下几行
                for (int j = i + 1; j < Math.min(lines.length, i + 10); j++) {
                    if (lines[j].contains("(") && !lines[j].trim().startsWith("@")) {
                        return new int[]{j + 1, Math.min(lines.length, j + 8)};
                    }
                }
            }
        }
        return null;
    }

    private String normalizeUrl(String url) {
        String s = url.trim();
        // 去掉域名与 query
        if (s.contains("://")) {
            s = s.substring(s.indexOf("://") + 3);
            if (s.contains("/")) {
                s = s.substring(s.indexOf('/'));
            } else {
                s = "/";
            }
        }
        int q = s.indexOf('?');
        if (q >= 0) {
            s = s.substring(0, q);
        }
        // 去掉路径参数占位符差异（/user/1 vs /user/{id}）
        return s.replaceAll("/\\{[^}]+\\}", "/{id}");
    }

    private String readSafely(SourceRepo repo, SourceFile sf) {
        try {
            return sourceFileService.readContent(sourceManager.getRepoRoot(repo), sf);
        } catch (IOException e) {
            return null;
        }
    }
}
