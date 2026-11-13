package com.perfserver.ai;

import com.perfserver.common.Texts;
import com.perfserver.entity.AiKnowledge;
import com.perfserver.mapper.AiKnowledgeMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * AI 知识库服务：分析结论沉淀与中文全文检索。
 *
 * <p>沉淀时机：AI 会话给出最终结论后（由 {@link AiAgentService} 调用 {@link #save}）；
 * 检索入口：大模型通过 {@code search_knowledge} 工具调用。
 *
 * <p>检索降级：FULLTEXT 在某些部署（如未建索引、词长过短）下可能抛异常，
 * 此时降级为最近结论列表，保证对话链路不被中断。
 * 注意：只有"检索异常"才降级；"检索无命中"如实返回空列表，不做 recent 回落——
 * 把无关的近期结论当"参考经验"回填会污染模型判读（见 {@link #search}）。
 *
 * <p>沉淀去重：同一问题反复对话会产出近似结论，入库前与最近 {@value #DEDUP_LOOKBACK}
 * 条同应用结论做相似度比对，超过 {@value #DEDUP_SIMILARITY} 视为重复并跳过——
 * 否则检索结果会被重复项淹没，模型参考价值随使用时长反而下降。
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
    /** 去重回看条数：与最近 N 条同应用结论比对 */
    static final int DEDUP_LOOKBACK = 50;
    /** 重复判定阈值（字符 bigram Jaccard 相似度），≥ 该值视为同一结论 */
    static final double DEDUP_SIMILARITY = 0.9;
    /** 参与相似度计算的正文长度上限（结论可能很长，截断以控制比对开销） */
    private static final int DEDUP_SAMPLE_CHARS = 1000;

    private final AiKnowledgeMapper mapper;

    public AiKnowledgeService(AiKnowledgeMapper mapper) {
        this.mapper = mapper;
    }

    /**
     * 沉淀一条分析结论。
     *
     * @return 是否入库（内容过短、与已有结论重复或异常时返回 false）
     */
    public boolean save(String appName, String title, String content, String tags) {
        if (content == null || content.trim().length() < MIN_CONTENT_CHARS) {
            return false;
        }
        try {
            String normalized = content.trim();
            if (isDuplicate(appName, normalized)) {
                log.info("skip duplicated ai knowledge, app={}, title={}", appName, title);
                return false;
            }
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
     * 重复判定：与最近 {@value #DEDUP_LOOKBACK} 条同应用结论逐一比对相似度。
     *
     * <p>比对失败（检索异常）时返回 false——宁可写入一条重复结论，也不能阻塞沉淀链路。
     */
    boolean isDuplicate(String appName, String content) {
        List<AiKnowledge> recent;
        try {
            recent = mapper.recent(appName == null ? "" : appName, DEDUP_LOOKBACK);
        } catch (Exception e) {
            log.warn("load recent knowledge for dedup failed: {}", e.getMessage());
            return false;
        }
        if (recent == null) {
            return false;
        }
        for (AiKnowledge item : recent) {
            if (item.getContent() == null) {
                continue;
            }
            if (similarity(content, item.getContent()) >= DEDUP_SIMILARITY) {
                return true;
            }
        }
        return false;
    }

    /**
     * 字符 bigram Jaccard 相似度：中文无空格分词，二元字组足以刻画"近似重复"，
     * 且对语序微调、少量增删不敏感（正是同一问题多次对话的产物形态）。
     */
    static double similarity(String a, String b) {
        Set<String> ga = bigrams(a);
        Set<String> gb = bigrams(b);
        if (ga.isEmpty() || gb.isEmpty()) {
            return 0;
        }
        int intersection = 0;
        for (String g : ga) {
            if (gb.contains(g)) {
                intersection++;
            }
        }
        return (double) intersection / (ga.size() + gb.size() - intersection);
    }

    private static Set<String> bigrams(String text) {
        String s = text.replaceAll("\\s+", "");
        if (s.length() > DEDUP_SAMPLE_CHARS) {
            s = s.substring(0, DEDUP_SAMPLE_CHARS);
        }
        Set<String> grams = new HashSet<>();
        for (int i = 0; i + 2 <= s.length(); i++) {
            grams.add(s.substring(i, i + 2));
        }
        if (grams.isEmpty() && !s.isEmpty()) {
            grams.add(s);
        }
        return grams;
    }

    /**
     * 全文检索。
     *
     * <p><b>无命中即返回空</b>：早期实现会在"查不到"时回落 {@code recent(...)}，
     * 把与检索词无关的近期结论当成"参考经验"喂给模型——模型随后把它们当成
     * 同问题的历史结论复用，结论口径被污染，且与系统提示词"工具返回空 = 暂无数据"
     * 自相矛盾。故只保留"检索异常"这一种降级（FULLTEXT 不可用时退化为最近结论列表），
     * 语义上的"无命中"如实返回空列表。
     *
     * @param query   检索词（空白时返回最近结论——这是"列最近"而非"检索无命中"，语义不同）
     * @param appName 应用名过滤（空表示不过滤；非空时含 {@code app_name=''} 的全局结论）
     * @param limit   条数上限（受 {@link #MAX_LIMIT} 约束）
     */
    public List<AiKnowledge> search(String query, String appName, int limit) {
        int bounded = Math.max(1, Math.min(limit <= 0 ? 5 : limit, MAX_LIMIT));
        try {
            if (query == null || query.isBlank()) {
                return mapper.recent(appName, bounded);
            }
            List<AiKnowledge> hits = mapper.search(query.trim(), appName, bounded);
            // 无命中返回空：交给模型按"暂无历史经验"处理，不做无关键词相关的回落
            return hits == null ? new ArrayList<>() : hits;
        } catch (Exception e) {
            // 异常降级：FULLTEXT 未建索引/兼容库时不能中断对话链路
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
