package com.perfserver.controller;

import com.perfserver.common.ApiResponse;
import com.perfserver.entity.FixSuggestion;
import com.perfserver.entity.PerformanceIssue;
import com.perfserver.entity.SourceFile;
import com.perfserver.entity.SourceRepo;
import com.perfserver.mapper.SourceFileMapper;
import com.perfserver.service.SourceIssueService;
import com.perfserver.source.SourceFileService;
import com.perfserver.source.SourceManager;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

import java.io.IOException;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 源码与优化：仓库导入（zip/git/本地）、文件浏览、性能问题列表/详情、多方案与修复提示词。
 */
@RestController
@RequestMapping("/api/source")
public class SourceController {

    private final SourceManager sourceManager;
    private final SourceFileService sourceFileService;
    private final SourceFileMapper sourceFileMapper;
    private final SourceIssueService sourceIssueService;
    private final com.perfserver.mapper.FixSuggestionMapper suggestionMapper;
    private final com.perfserver.source.EndpointScanner endpointScanner;

    public SourceController(SourceManager sourceManager, SourceFileService sourceFileService,
                            SourceFileMapper sourceFileMapper, SourceIssueService sourceIssueService,
                            com.perfserver.mapper.FixSuggestionMapper suggestionMapper,
                            com.perfserver.source.EndpointScanner endpointScanner) {
        this.sourceManager = sourceManager;
        this.sourceFileService = sourceFileService;
        this.sourceFileMapper = sourceFileMapper;
        this.sourceIssueService = sourceIssueService;
        this.suggestionMapper = suggestionMapper;
        this.endpointScanner = endpointScanner;
    }

    /** 仓库列表 */
    @GetMapping("/repos")
    public ApiResponse<List<SourceRepo>> repos(@RequestParam(defaultValue = "50") int limit) {
        return ApiResponse.ok(sourceManager.listRepos(limit));
    }

    /** 扫描仓库 HTTP 接口清单（供压测中心选项目自动出接口） */
    @GetMapping("/repos/{repoId}/endpoints")
    public ApiResponse<List<Map<String, Object>>> endpoints(@PathVariable long repoId) {
        return ApiResponse.ok(endpointScanner.scan(repoId));
    }

    /** 重新同步仓库：清空副本 → 重新拉取源码 → 重建索引（同步执行，返回索引文件数） */
    @PostMapping("/repos/{repoId}/resync")
    public ApiResponse<Map<String, Object>> resync(@PathVariable long repoId) {
        int indexed = sourceManager.resync(repoId);
        return ApiResponse.ok(Map.of("repoId", repoId, "indexed", indexed));
    }

    /** 分支列表（git 仓库专用，fetch 远端后枚举） */
    @GetMapping("/repos/{repoId}/branches")
    public ApiResponse<List<String>> branches(@PathVariable long repoId) {
        return ApiResponse.ok(sourceManager.listBranches(repoId));
    }

    /** 远端分支枚举（导入前使用，git ls-remote 直连，支持私有仓库凭据） */
    @PostMapping("/repos/git-branches")
    public ApiResponse<List<String>> remoteBranches(@RequestBody Map<String, String> body) {
        return ApiResponse.ok(sourceManager.listRemoteBranches(
                body.get("gitUrl"), body.get("gitUser"), body.get("gitToken")));
    }

    /** 切换分支：更新配置并重新同步（fetch + checkout + 重建索引） */
    @PostMapping("/repos/{repoId}/branch")
    public ApiResponse<Map<String, Object>> switchBranch(@PathVariable long repoId,
                                                         @RequestBody Map<String, String> body) {
        String branch = body.get("branch");
        int indexed = sourceManager.switchBranch(repoId, branch);
        return ApiResponse.ok(Map.of("repoId", repoId, "branch", branch == null ? "" : branch, "indexed", indexed));
    }

    /** 绑定应用（项目与应用强绑定；空串 = 解绑） */
    @PostMapping("/repos/{repoId}/bind-app")
    public ApiResponse<Map<String, Object>> bindApp(@PathVariable long repoId, @RequestBody Map<String, String> body) {
        String app = sourceManager.bindApp(repoId, body.get("appName"));
        return ApiResponse.ok(Map.of("repoId", repoId, "appName", app));
    }

    /** zip 上传导入（appName 导入即绑定应用） */
    @PostMapping(value = "/repos/zip", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public ApiResponse<SourceRepo> importZip(@RequestParam("repoName") String repoName,
                                             @RequestParam(value = "appName", required = false) String appName,
                                             @RequestParam("file") MultipartFile file) throws IOException {
        if (file == null || file.isEmpty()) {
            throw new IllegalArgumentException("请选择要上传的 zip 文件");
        }
        String saved = sourceManager.saveZip(file.getInputStream(), file.getOriginalFilename());
        return ApiResponse.ok(sourceManager.startImport(repoName, "zip", saved, null, null, null, appName));
    }

    /** git 地址导入（可选分支、私有仓库凭据；导入即绑定应用） */
    @PostMapping("/repos/git")
    public ApiResponse<SourceRepo> importGit(@RequestBody Map<String, String> body) {
        String name = body.get("repoName");
        String url = body.get("gitUrl");
        String branch = body.get("branch");
        String gitUser = body.get("gitUser");
        String gitToken = body.get("gitToken");
        if (url == null || url.isBlank()) {
            throw new IllegalArgumentException("git 地址不能为空");
        }
        return ApiResponse.ok(sourceManager.startImport(name, "git", url, branch, gitUser, gitToken, body.get("appName")));
    }

    /** 本地目录导入（appName 导入即绑定应用） */
    @PostMapping("/repos/local")
    public ApiResponse<SourceRepo> importLocal(@RequestBody Map<String, String> body) {
        String name = body.get("repoName");
        String path = body.get("localPath");
        if (path == null || path.isBlank()) {
            throw new IllegalArgumentException("本地路径不能为空");
        }
        return ApiResponse.ok(sourceManager.startImport(name, "local", path, null, null, null, body.get("appName")));
    }

    /** 仓库文件列表（文件树浏览） */
    @GetMapping("/repos/{repoId}/files")
    public ApiResponse<List<SourceFile>> files(@PathVariable long repoId,
                                               @RequestParam(required = false) String prefix,
                                               @RequestParam(defaultValue = "5000") int limit) {
        sourceManager.getRepo(repoId);
        return ApiResponse.ok(sourceFileMapper.listByRepo(repoId, prefix, Math.max(1, Math.min(limit, 5000))));
    }

    /** 文件内容 */
    @GetMapping("/files/{fileId}/content")
    public ApiResponse<Map<String, Object>> content(@PathVariable long fileId) {
        SourceFile sf = sourceFileMapper.findById(fileId);
        if (sf == null) {
            throw new IllegalArgumentException("文件不存在: " + fileId);
        }
        SourceRepo repo = sourceManager.getRepo(sf.getRepoId());
        Path root = sourceManager.getRepoRoot(repo);
        Map<String, Object> result = new HashMap<>();
        try {
            result.put("relativePath", sf.getRelativePath());
            result.put("language", sf.getLanguage());
            result.put("content", sourceFileService.readContent(root, sf));
        } catch (IOException e) {
            throw new IllegalStateException("读取文件失败: " + e.getMessage());
        }
        return ApiResponse.ok(result);
    }

    /** 问题列表（可按应用过滤） */
    @GetMapping("/issues")
    public ApiResponse<List<PerformanceIssue>> issues(@RequestParam(required = false) String appName,
                                                      @RequestParam(required = false) String issueType,
                                                      @RequestParam(required = false) String status,
                                                      @RequestParam(defaultValue = "100") int limit) {
        return ApiResponse.ok(sourceIssueService.listIssues(appName, issueType, status, limit));
    }

    /** 问题详情（含方案） */
    @GetMapping("/issues/{issueId}")
    public ApiResponse<SourceIssueService.IssueDetail> issueDetail(@PathVariable long issueId) {
        return ApiResponse.ok(sourceIssueService.getIssueDetail(issueId));
    }

    /** 执行大模型分析（生成根因 + 多方案） */
    @PostMapping("/issues/{issueId}/analyze")
    public ApiResponse<Map<String, Object>> analyze(@PathVariable long issueId) {
        int count = sourceIssueService.analyze(issueId);
        Map<String, Object> result = new HashMap<>();
        result.put("generated", count);
        return ApiResponse.ok(result);
    }

    /** 补绑源码仓库并自动定位（压测问题分析前置；按问题类型自动定位） */
    @PostMapping("/issues/{issueId}/bind-repo")
    public ApiResponse<SourceIssueService.IssueDetail> bindIssueRepo(@PathVariable long issueId,
                                                                     @RequestParam long repoId) {
        return ApiResponse.ok(sourceIssueService.bindRepo(issueId, repoId));
    }

    /** 选定方案并生成修复提示词 */
    @PostMapping("/issues/{issueId}/suggestions/{suggestionId}/select")
    public ApiResponse<Map<String, Object>> select(@PathVariable long issueId, @PathVariable long suggestionId) {
        String promptMd = sourceIssueService.selectSuggestion(issueId, suggestionId);
        Map<String, Object> result = new HashMap<>();
        result.put("promptMd", promptMd);
        return ApiResponse.ok(result);
    }

    /** 从慢 SQL 创建问题（关联源码） */
    @PostMapping("/issues/from-slow-sql")
    public ApiResponse<PerformanceIssue> fromSlowSql(@RequestParam long slowSqlId,
                                                     @RequestParam long repoId) {
        return ApiResponse.ok(sourceIssueService.createFromSlowSql(slowSqlId, repoId));
    }

    /** 从热点方法创建问题 */
    @PostMapping("/issues/from-hot-method")
    public ApiResponse<PerformanceIssue> fromHotMethod(@RequestParam String className,
                                                       @RequestParam String methodName,
                                                       @RequestParam long repoId,
                                                       @RequestParam(required = false) String appName) {
        return ApiResponse.ok(sourceIssueService.createFromHotMethod(appName, className, methodName, repoId));
    }

    /** 从压测任务创建问题（可指定归属应用） */
    @PostMapping("/issues/from-stress")
    public ApiResponse<PerformanceIssue> fromStress(@RequestParam long stressTaskId,
                                                    @RequestParam long repoId,
                                                    @RequestParam(required = false) String appName) {
        return ApiResponse.ok(sourceIssueService.createFromStress(stressTaskId, repoId, appName));
    }

    /** 最近慢 SQL（供前端一键创建问题） */
    @GetMapping("/recent-slow-sqls")
    public ApiResponse<List<?>> recentSlowSqls(@RequestParam(required = false) String app,
                                               @RequestParam(defaultValue = "20") int limit) {
        return ApiResponse.ok(sourceIssueService.recentSlowSqls(app, limit));
    }

    /** 修复方案详情（含提示词） */
    @GetMapping("/suggestions/{suggestionId}")
    public ApiResponse<FixSuggestion> suggestion(@PathVariable long suggestionId) {
        FixSuggestion s = suggestionMapper.findById(suggestionId);
        if (s == null) {
            throw new IllegalArgumentException("方案不存在: " + suggestionId);
        }
        return ApiResponse.ok(s);
    }
}
