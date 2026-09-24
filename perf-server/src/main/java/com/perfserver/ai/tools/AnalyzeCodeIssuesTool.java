package com.perfserver.ai.tools;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.perfserver.entity.PerformanceIssue;
import com.perfserver.entity.SourceRepo;
import com.perfserver.mapper.SourceRepoMapper;
import com.perfserver.service.SourceIssueService;
import org.springframework.stereotype.Component;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 代码问题分析工具：从检测证据（慢 SQL/热点方法/压测 URL）创建性能问题并调用大模型生成多方案。
 *
 * <p><b>repoId 必填</b>：过去默认 {@code repoId=1L}，用户一句"分析这个慢 SQL"
 * 就可能把问题记录误建到仓库 1（通常是无关项目）上。现在缺失/非法一律不落库，
 * 改为返回可用仓库清单让模型向用户确认——代价是多一轮对话，换来的是不会写错库。
 */
@Component
public class AnalyzeCodeIssuesTool extends AbstractAiTool {

    private final SourceIssueService sourceIssueService;
    private final SourceRepoMapper repoMapper;

    public AnalyzeCodeIssuesTool(SourceIssueService sourceIssueService, SourceRepoMapper repoMapper) {
        this.sourceIssueService = sourceIssueService;
        this.repoMapper = repoMapper;
    }

    @Override
    public String name() {
        return "analyze_code_issues";
    }

    @Override
    public String description() {
        return "分析性能问题根因并生成 2-3 个修复方案。参数：source 必填（slow_sql 慢 SQL 定位 / hot_method 热点方法定位 / "
                + "stress 压测 URL 定位）；repoId 源码仓库 ID <b>必填</b>（缺失或不存在时返回可用仓库清单，"
                + "请向用户确认后重试——不要在未确认仓库时创建问题）；慢 SQL 定位需 slowSqlId；热点方法定位需 className 与 "
                + "methodName（及可选 appName）；压测定位需 stressTaskId。注意：本工具会在平台创建一条性能问题记录。"
                + "返回问题 ID、根因与生成的方案列表（含涉及文件/修改点/收益/风险）。";
    }

    @Override
    public String parametersJson() {
        return buildParams(new String[]{"source", "repoId", "slowSqlId", "className", "methodName", "appName", "stressTaskId"},
                new String[]{"source", "repoId"}).toString();
    }

    @Override
    public String execute(String jsonArgs) {
        JsonNode args = parse(jsonArgs);
        String source = str(args, "source", "");
        if (!List.of("slow_sql", "hot_method", "stress").contains(source)) {
            return error("source 仅支持 slow_sql / hot_method / stress");
        }

        // repoId 防呆：缺失 / 非法一律不落库，回清单让模型与用户确认
        Long repoId = resolveRepoId(args);
        if (repoId == null) {
            return error("请先确认分析哪个源码仓库（repoId 必填，缺失或不存在）。"
                    + "可用仓库清单：" + repoOptions());
        }

        try {
            PerformanceIssue issue;
            switch (source) {
                case "slow_sql" -> issue = sourceIssueService.createFromSlowSql(
                        lng(args, "slowSqlId", 0L), repoId);
                case "hot_method" -> issue = sourceIssueService.createFromHotMethod(
                        str(args, "appName", null), str(args, "className", ""), str(args, "methodName", ""), repoId);
                default -> issue = sourceIssueService.createFromStress(
                        lng(args, "stressTaskId", 0L), repoId, str(args, "appName", null));
            }
            int suggestionCount = sourceIssueService.analyze(issue.getId());
            var detail = sourceIssueService.getIssueDetail(issue.getId());
            // 用 LinkedHashMap 而非 Map.of：标题/根因可能为 null（分析未产出根因时），
            // Map.of 遇 null 直接 NPE，会把"分析成功但暂无根因"误判为工具执行失败
            Map<String, Object> data = new LinkedHashMap<>();
            data.put("issueId", issue.getId());
            data.put("title", issue.getTitle() == null ? "" : issue.getTitle());
            data.put("rootCause", detail.getIssue().getRootCause());
            data.put("suggestionCount", suggestionCount);
            data.put("suggestions", detail.getSuggestions());
            return ok(data);
        } catch (Exception e) {
            return error("问题分析失败: " + e.getMessage());
        }
    }

    /**
     * 解析并校验 repoId：返回 null 表示"不可用，需用户确认"。
     *
     * <p>校验顺序：字段缺失 → 数据库不存在。两者都视为需要用户重新指定，
     * 因为模型可能凭上下文猜一个 id，这里的回查是唯一能挡住"猜"的地方。
     */
    private Long resolveRepoId(JsonNode args) {
        if (!args.hasNonNull("repoId")) {
            return null;
        }
        long candidate = args.get("repoId").asLong(0L);
        if (candidate <= 0) {
            return null;
        }
        try {
            SourceRepo repo = repoMapper.findById(candidate);
            return repo == null ? null : repo.getId();
        } catch (Exception e) {
            // 回查失败按"不可用"处理：宁可不建问题，也不要建到不确定的仓库上
            return null;
        }
    }

    /** 可用仓库清单（id + 名称），供模型向用户追问 */
    private ArrayNode repoOptions() {
        ArrayNode arr = mapper.createArrayNode();
        try {
            List<SourceRepo> repos = repoMapper.query(50);
            for (SourceRepo repo : repos) {
                var n = arr.addObject();
                n.put("repoId", repo.getId());
                n.put("repoName", repo.getRepoName());
                n.put("appName", repo.getAppName() == null ? "" : repo.getAppName());
            }
        } catch (Exception ignored) {
            // 清单查不到就返回空数组，错误信息本身已说明"repoId 必填"
        }
        return arr;
    }
}
