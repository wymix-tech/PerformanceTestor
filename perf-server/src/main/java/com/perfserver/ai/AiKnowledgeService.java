package com.perfserver.ai;

import com.perfserver.common.Texts;
import com.perfserver.entity.AiKnowledge;
import com.perfserver.mapper.AiKnowledgeMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;

/**
 * AI 知识库服务：分析结论沉淀与中文全文检索。
 *
 * <p>沉淀时机：AI 会话给出最终结论后（由 {@link AiAgentService} 调用 {@link #save}）；
 * 检索入口：大模型通过 {@code search_knowledge} 工具调用。
 *
 * <p>检索降级：FULLTEXT 在某些部署（如未建索引、词长过短）下可能抛异常，
 * 此时降级为最近结论列表，保证对话链路不被中断。
 */
@Service
public class AiKnowledgeService {

    private static final Logger log = LoggerFactory.getLogger(AiKnowledgeService.class);

    /** 结论正文过短（无信息量）时不入库，避免污染知识库 */
    static final int MIN_CONTENT_CHARS = 40;
    /** 检索最大返回条数（防止上下文膨胀） */
    static final int MAX_LIMIT = 20;
    /** 检索结果中每条正文回填上下文的长度上限 */
    static final int SNIPPET_CHARS = 400;

    private final AiKnowledgeMapper mapper;

    public AiKnowledgeService(AiKnowledgeMapper mapper) {
        this.mapper = mapper;
    }

    /**
     * 沉淀一条分析结论。
     *
     * @return 是否入库（内容过短或异常时返回 false）
     */
    public boolean save(String appName, String title, String content, String tags) {
        if (content == null || content.trim().length() < MIN_CONTENT_CHARS) {
            return false;
        }
        try {
            AiKnowledge item = new AiKnowledge();
            item.setAppName(appName == null ? "" : appName);
            item.setTitle(Texts.truncate(title == null || title.isBlank() ? firstLine(content) : title.trim(), 120));
            item.setContent(content.trim());
            item.setTags(tags == null ? "" : tags);
            item.setSource("ai");
            item.setCreateTime(LocalDateTime.now());
            mapper.insert(item);
            return true;
        } catch (Exception e) {
            log.warn("save ai knowledge failed: {}", e.getMessage());
            return false;
        }
    }

    /**
     * 全文检索（降级到最近结论）。
     *
     * @param query   检索词（空白时返回最近结论）
     * @param appName 应用名过滤（空表示不过滤）
     * @param limit   条数上限（受 {@link #MAX_LIMIT} 约束）
     */
    public List<AiKnowledge> search(String query, String appName, int limit) {
        int bounded = Math.max(1, Math.min(limit <= 0 ? 5 : limit, MAX_LIMIT));
        try {
            if (query == null || query.isBlank()) {
                return mapper.recent(appName, bounded);
            }
            List<AiKnowledge> hits = mapper.search(query.trim(), appName, bounded);
            if (hits != null && !hits.isEmpty()) {
                return hits;
            }
            // 无命中：返回最近结论作为参考，避免模型因"查不到"而误判无历史经验
            return mapper.recent(appName, Math.min(bounded, 5));
        } catch (Exception e) {
            log.warn("search ai knowledge failed, fallback to recent: {}", e.getMessage());
            try {
                return mapper.recent(appName, bounded);
            } catch (Exception inner) {
                log.warn("list recent ai knowledge failed: {}", inner.getMessage());
                return new ArrayList<>();
            }
        }
    }

    /** 删除单条结论 */
    public boolean delete(long id) {
        try {
            return mapper.deleteById(id) > 0;
        } catch (Exception e) {
            log.warn("delete ai knowledge {} failed: {}", id, e.getMessage());
            return false;
        }
    }

    /** 取正文首行作为标题兜底 */
    private static String firstLine(String content) {
        int nl = content.indexOf('\n');
        String line = nl > 0 ? content.substring(0, nl) : content;
        return line.trim();
    }

    /** 检索结果正文截断（供工具回填上下文，防膨胀） */
    public static String snippet(String content) {
        return Texts.truncate(content, SNIPPET_CHARS);
    }
}
