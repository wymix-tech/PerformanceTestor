package com.perfserver.controller;

import com.perfserver.ai.AiAgentService;
import com.perfserver.ai.AiKnowledgeService;
import com.perfserver.ai.AiTool;
import com.perfserver.ai.PromptBuilder;
import com.perfserver.common.ApiResponse;
import com.perfserver.entity.AiKnowledge;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

/**
 * AI 助手接口：自然语言对话（Function Calling）、工具清单、会话管理。
 */
@RestController
@RequestMapping("/api/ai")
public class AiController {

    private final AiAgentService aiAgentService;
    private final PromptBuilder promptBuilder;
    private final AiKnowledgeService knowledgeService;

    public AiController(AiAgentService aiAgentService, PromptBuilder promptBuilder,
                        AiKnowledgeService knowledgeService) {
        this.aiAgentService = aiAgentService;
        this.promptBuilder = promptBuilder;
        this.knowledgeService = knowledgeService;
    }

    /** 发送消息（sessionId 缺省为 default） */
    @PostMapping("/chat")
    public ApiResponse<Map<String, Object>> chat(@RequestBody Map<String, String> body) {
        String sessionId = body.getOrDefault("sessionId", "default");
        String message = body.getOrDefault("message", "");
        return ApiResponse.ok(aiAgentService.chat(sessionId, message));
    }

    /** 工具清单（前端展示 AI 可用能力） */
    @GetMapping("/tools")
    public ApiResponse<List<Map<String, String>>> tools() {
        List<Map<String, String>> list = promptBuilder.listTools().stream().map(t -> {
            Map<String, String> m = new LinkedHashMap<>();
            m.put("name", t.name());
            m.put("description", t.description());
            return m;
        }).collect(Collectors.toList());
        return ApiResponse.ok(list);
    }

    /** 知识库检索（query 为空时返回最近沉淀的结论） */
    @GetMapping("/knowledge")
    public ApiResponse<List<AiKnowledge>> knowledge(@RequestParam(required = false) String query,
                                                    @RequestParam(required = false) String app,
                                                    @RequestParam(defaultValue = "10") int limit) {
        return ApiResponse.ok(knowledgeService.search(query, app, limit));
    }

    /**
     * 删除单条沉淀结论（前端知识库列表的删除入口；已在检索结果中被模型引用过的结论也可清理）。
     *
     * @return 是否删除成功（id 不存在时返回 false）
     */
    @DeleteMapping("/knowledge/{id}")
    public ApiResponse<Boolean> deleteKnowledge(@PathVariable long id) {
        return ApiResponse.ok(knowledgeService.delete(id));
    }

    /** 清空会话历史 */
    @DeleteMapping("/session/{sessionId}")
    public ApiResponse<String> clear(@PathVariable String sessionId) {
        aiAgentService.clear(sessionId);
        return ApiResponse.ok("cleared");
    }
}
