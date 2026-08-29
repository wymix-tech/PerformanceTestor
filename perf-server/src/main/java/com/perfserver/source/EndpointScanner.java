package com.perfserver.source;

import com.perfserver.entity.SourceFile;
import com.perfserver.entity.SourceRepo;
import com.perfserver.mapper.SourceFileMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 源码 HTTP 接口扫描器：解析源码仓库中的 Spring MVC Controller，
 * 提取接口清单（HTTP 方法 + 完整路径 + 所在类/方法/行号），
 * 供压测中心"选项目 → 自动出接口"流程使用，用户无需手工填 URL。
 *
 * <p>支持注解（Spring MVC / Spring Boot 通用）：
 * 类级 @RequestMapping 前缀 + 方法级 @GetMapping/@PostMapping/@PutMapping/
 * @DeleteMapping/@PatchMapping/@RequestMapping；兼容含 consumes 等多属性的写法。
 */
@Component
public class EndpointScanner {

    private static final Logger log = LoggerFactory.getLogger(EndpointScanner.class);

    private final SourceFileMapper sourceFileMapper;
    private final SourceManager sourceManager;

    /** 类级 @RequestMapping：必须紧邻 class 声明（中间仅允许其他注解），避免误吸方法级注解 */
    private static final Pattern CLASS_MAPPING = Pattern.compile(
            "@RequestMapping\\s*\\(([^)]*)\\)\\s*(?:@\\w+(?:\\([^)]*\\))?\\s*)*(?:public\\s+)?(?:final\\s+|abstract\\s+)*class\\s+\\w+",
            Pattern.CASE_INSENSITIVE);
    /** 方法级注解：@GetMapping("/x") / @PostMapping(...) / @RequestMapping(value="/x", method=...) */
    private static final Pattern METHOD_MAPPING = Pattern.compile(
            "@(Get|Post|Put|Delete|Patch|Request)Mapping\\b([^\\n]*)");
    /** 注解属性中的路径字符串 */
    private static final Pattern PATH_ATTR =
            Pattern.compile("(?:value|path)\\s*=\\s*\\{?\\s*\"([^\"]*)\"|\\(\\s*\"([^\"]*)\"");

    /** 方法名（用于展示来源） */
    private static final Pattern METHOD_DEF =
            Pattern.compile("(?:public|protected|private)\\s+[\\w.<>\\[\\],\\s]+?\\s+(\\w+)\\s*\\(");

    /** 是否 Controller 类 */
    private static final Pattern CONTROLLER_ANNO =
            Pattern.compile("@(Rest)?Controller\\b");

    public EndpointScanner(SourceFileMapper sourceFileMapper, SourceManager sourceManager) {
        this.sourceFileMapper = sourceFileMapper;
        this.sourceManager = sourceManager;
    }

    /**
     * 扫描仓库全部 HTTP 接口。
     *
     * @return 接口列表：{httpMethod, path, className, methodName, line}
     */
    public List<Map<String, Object>> scan(long repoId) {
        SourceRepo repo = sourceManager.getRepo(repoId);
        List<Map<String, Object>> endpoints = new ArrayList<>();
        List<SourceFile> files = sourceFileMapper.listByRepoContains(repo.getId(), ".java", 5000);
        for (SourceFile sf : files) {
            if (!sf.getRelativePath().endsWith(".java")) {
                continue;
            }
            String content = readSafely(repo, sf);
            if (content == null || !CONTROLLER_ANNO.matcher(content).find()) {
                continue;
            }
            scanFile(sf, content, endpoints);
        }
        return endpoints;
    }

    /** 单文件解析：类前缀 + 方法级注解逐个匹配 */
    private void scanFile(SourceFile sf, String content, List<Map<String, Object>> out) {
        String className = sf.getFileName().replace(".java", "");
        String classPrefix = "";
        int classAnnoEndLine = -1;
        Matcher classM = CLASS_MAPPING.matcher(content);
        if (classM.find()) {
            classPrefix = normalize(extractPath(classM.group(1)));
            // 类级注解匹配段的行范围（含中间注解），方法级扫描时跳过，避免类级 @RequestMapping 被重复解析
            classAnnoEndLine = (int) (content.substring(0, classM.start()).chars().filter(c -> c == '\n').count()) + 1;
        }

        String[] lines = content.split("\n", -1);

        for (int i = 0; i < lines.length; i++) {
            String line = lines[i].trim();
            if (i < classAnnoEndLine) {
                continue; // 类声明前的类级注解段
            }
            Matcher mm = METHOD_MAPPING.matcher(line);
            if (!mm.find()) {
                continue;
            }
            String anno = mm.group(1);           // Get/Post/.../Request
            String httpMethod;
            if ("Request".equals(anno)) {
                // @RequestMapping(method = RequestMethod.POST) 显式指定时优先
                Matcher rm = Pattern.compile("RequestMethod\\.(GET|POST|PUT|DELETE|PATCH)", Pattern.CASE_INSENSITIVE)
                        .matcher(mm.group(2));
                httpMethod = rm.find() ? rm.group(1).toUpperCase() : "GET";
            } else {
                httpMethod = anno.toUpperCase();
            }
            String path = extractPath(mm.group(2));

            // 向下找最近的方法定义行（最多 12 行），确定 methodName
            // 注意：只跳过"以 @ 开头"的注解行；方法定义行本身可能含 @RequestParam 等参数注解，不能整行跳过
            String methodName = "";
            for (int j = i + 1; j < Math.min(i + 13, lines.length); j++) {
                String candidate = lines[j].trim();
                if (candidate.isEmpty()) {
                    continue;
                }
                if (candidate.startsWith("@") || candidate.startsWith("*") || candidate.startsWith("/*")) {
                    continue;
                }
                Matcher md = METHOD_DEF.matcher(candidate);
                if (md.find()) {
                    methodName = md.group(1);
                    break;
                }
                // 已到下一个方法级注解或类结束仍未命中，停止
                if (METHOD_MAPPING.matcher(candidate).find() || candidate.startsWith("}")) {
                    break;
                }
            }

            String fullPath = classPrefix + normalize(path);
            Map<String, Object> ep = new java.util.LinkedHashMap<>();
            ep.put("httpMethod", httpMethod);
            ep.put("path", fullPath);
            ep.put("className", className);
            ep.put("classFile", sf.getRelativePath());
            ep.put("methodName", methodName);
            ep.put("line", i + 1);
            out.add(ep);
        }
    }

    /** 从注解参数串提取路径：@GetMapping("/a")、@RequestMapping(value="/a", ...)、无参为空 */
    private String extractPath(String annoArgs) {
        if (annoArgs == null || annoArgs.isBlank()) {
            return "";
        }
        Matcher m = PATH_ATTR.matcher(annoArgs);
        if (m.find()) {
            return m.group(1) != null ? m.group(1) : m.group(2);
        }
        // 形如 ("/a") 已被第二分支覆盖；纯字符串参数
        m = Pattern.compile("\"([^\"]*)\"").matcher(annoArgs);
        return m.find() ? m.group(1) : "";
    }

    /** 路径归一化：确保以 / 开头、不以 / 结尾 */
    private String normalize(String p) {
        if (p == null || p.isBlank()) {
            return "";
        }
        String s = p.trim();
        if (!s.startsWith("/")) {
            s = "/" + s;
        }
        while (s.endsWith("/") && s.length() > 1) {
            s = s.substring(0, s.length() - 1);
        }
        return s;
    }

    private String readSafely(SourceRepo repo, SourceFile sf) {
        try {
            Path root = sourceManager.getRepoRoot(repo);
            Path path = root.resolve(sf.getRelativePath()).normalize();
            if (!path.startsWith(root.normalize())) {
                return null;
            }
            return Files.readString(path);
        } catch (IOException e) {
            // 单文件读取失败跳过（不影响其余文件扫描）
            log.warn("read file failed during endpoint scan: {}", sf.getRelativePath(), e);
            return null;
        }
    }
}
