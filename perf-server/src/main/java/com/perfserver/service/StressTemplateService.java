package com.perfserver.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.StressTemplate;
import com.perfserver.mapper.StressTemplateMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.util.List;

/**
 * 压测模板服务（P2-3）：沉淀常用场景参数，支持「保存为模板」与「从模板填充」。
 *
 * <p>为什么要有模板：一次性压测场景（如"登录→下单→查询"混合场景 + 断言 + 分档并发）
 * 参数有十几项，每次靠表单重填既慢又容易漏配（少填断言会导致错误率失真）。
 * 定时压测虽有 {@code task_json}，但那是"调度"的附属品，无法给一次性场景复用。
 *
 * <p><b>与压测请求的关系</b>：模板只存参数快照（{@code StressTaskRequest} 子集），
 * 创建任务时仍是走 {@code POST /api/stress/tasks} 走原有校验——模板不做"跳过校验"的后门，
 * 存量非法模板（如历史并发超上限）在真正发起压测时照样被拦。
 */
@Service
public class StressTemplateService {

    private static final Logger log = LoggerFactory.getLogger(StressTemplateService.class);

    private static final int MAX_NAME_LEN = 128;
    private static final int MAX_DESC_LEN = 512;
    /** 单个模板参数 JSON 体积上限：正常场景参数在 KB 级，超过说明被塞了非参数内容 */
    private static final int MAX_TASK_JSON_LEN = 64 * 1024;

    private final StressTemplateMapper templateMapper;
    private final ObjectMapper objectMapper;

    public StressTemplateService(StressTemplateMapper templateMapper, ObjectMapper objectMapper) {
        this.templateMapper = templateMapper;
        this.objectMapper = objectMapper;
    }

    /** 列表（appName 为空查全部；指定应用时含通用模板） */
    public List<StressTemplate> list(String appName, int limit) {
        return templateMapper.query(appName == null ? "" : appName.trim(), Math.max(1, Math.min(limit, 200)));
    }

    public StressTemplate detail(long id) {
        StressTemplate t = templateMapper.findById(id);
        if (t == null) {
            throw new IllegalArgumentException("压测模板不存在: " + id);
        }
        return t;
    }

    /** 新增模板 */
    public StressTemplate create(StressTemplate template, String creator) {
        validate(template, true);
        template.setCreator(creator == null || creator.isBlank() ? "unknown" : creator.trim());
        templateMapper.insert(template);
        return template;
    }

    /** 更新模板（保留创建人与创建时间） */
    public StressTemplate update(long id, StressTemplate template) {
        StressTemplate existing = detail(id);
        validate(template, true);
        existing.setTemplateName(template.getTemplateName());
        existing.setAppName(template.getAppName());
        existing.setTaskJson(template.getTaskJson());
        existing.setDescription(template.getDescription());
        templateMapper.update(existing);
        return existing;
    }

    public void delete(long id) {
        detail(id);
        templateMapper.deleteById(id);
    }

    /**
     * 校验与归一化。
     *
     * <p>{@code requireTaskJson} 为 true 时要求 taskJson 非空且是合法 JSON 对象——
     * 存进去一个坏 JSON 的后果是"从模板填充"时才报错，用户已经找不到是哪个模板有问题。
     */
    void validate(StressTemplate template, boolean requireTaskJson) {
        String name = template.getTemplateName() == null ? "" : template.getTemplateName().trim();
        if (name.isEmpty()) {
            throw new IllegalArgumentException("模板名称不能为空");
        }
        if (name.length() > MAX_NAME_LEN) {
            throw new IllegalArgumentException("模板名称不能超过 " + MAX_NAME_LEN + " 字符");
        }
        template.setTemplateName(name);
        template.setAppName(template.getAppName() == null ? "" : template.getAppName().trim());
        String desc = template.getDescription() == null ? "" : template.getDescription().trim();
        if (desc.length() > MAX_DESC_LEN) {
            throw new IllegalArgumentException("模板说明不能超过 " + MAX_DESC_LEN + " 字符");
        }
        template.setDescription(desc);

        String json = template.getTaskJson() == null ? "" : template.getTaskJson().trim();
        if (json.isEmpty()) {
            if (requireTaskJson) {
                throw new IllegalArgumentException("模板参数（taskJson）不能为空");
            }
            template.setTaskJson("{}");
            return;
        }
        if (json.length() > MAX_TASK_JSON_LEN) {
            throw new IllegalArgumentException("模板参数体积过大（上限 " + (MAX_TASK_JSON_LEN / 1024) + "KB）");
        }
        try {
            JsonNode node = objectMapper.readTree(json);
            if (node == null || !node.isObject()) {
                throw new IllegalArgumentException("模板参数必须是 JSON 对象");
            }
        } catch (IllegalArgumentException e) {
            throw e;
        } catch (Exception e) {
            throw new IllegalArgumentException("模板参数不是合法 JSON：" + e.getMessage());
        }
        template.setTaskJson(json);
    }
}
