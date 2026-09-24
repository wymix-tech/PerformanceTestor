package com.perfserver.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.StressTemplate;
import com.perfserver.mapper.StressTemplateMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * 压测模板（P2-3）：名称/参数校验、创建人注入、更新保留创建人。
 *
 * <p>校验必须拦在"保存"这一步：存进去坏 JSON 的代价是"从模板填充"时才报错，
 * 用户已找不到是哪个模板有问题。
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class StressTemplateServiceTest {

    @Mock
    private StressTemplateMapper mapper;

    private StressTemplateService service;

    @BeforeEach
    void setUp() {
        service = new StressTemplateService(mapper, new ObjectMapper());
        lenient().when(mapper.query(anyString(), anyInt())).thenReturn(List.of());
        lenient().when(mapper.update(any())).thenReturn(1);
    }

    private static StressTemplate template(String name, String json) {
        StressTemplate t = new StressTemplate();
        t.setTemplateName(name);
        t.setTaskJson(json);
        return t;
    }

    /** 正常保存：创建人注入 + 名称/说明 trim */
    @Test
    void createInjectsCreatorAndTrims() {
        StressTemplate t = template("  混合场景  ", "{\"targetUrl\":\"http://x\"}");
        t.setDescription("  登录→下单  ");
        t.setAppName("  perf-demo  ");

        StressTemplate saved = service.create(t, "  admin  ");

        assertEquals("混合场景", saved.getTemplateName());
        assertEquals("登录→下单", saved.getDescription());
        assertEquals("perf-demo", saved.getAppName());
        assertEquals("admin", saved.getCreator());
        verify(mapper).insert(saved);
    }

    /** 创建人缺失：回落 unknown（列表页展示需要非空） */
    @Test
    void missingCreatorFallsBackToUnknown() {
        assertEquals("unknown", service.create(template("t", "{}"), null).getCreator());
        assertEquals("unknown", service.create(template("t2", "{}"), "  ").getCreator());
    }

    /** 名称为空：拒绝 */
    @Test
    void blankNameRejected() {
        assertThrows(IllegalArgumentException.class, () -> service.create(template("", "{}"), "a"));
        assertThrows(IllegalArgumentException.class, () -> service.create(template("   ", "{}"), "a"));
        assertThrows(IllegalArgumentException.class, () -> service.create(template(null, "{}"), "a"));
        verify(mapper, never()).insert(any());
    }

    /** 名称超长：拒绝 */
    @Test
    void tooLongNameRejected() {
        assertThrows(IllegalArgumentException.class, () -> service.create(template("x".repeat(129), "{}"), "a"));
    }

    /** 空参数：拒绝 */
    @Test
    void blankTaskJsonRejected() {
        assertThrows(IllegalArgumentException.class, () -> service.create(template("t", ""), "a"));
        assertThrows(IllegalArgumentException.class, () -> service.create(template("t", null), "a"));
    }

    /** 非法 JSON：拒绝（不能把坏 JSON 存进来） */
    @Test
    void invalidJsonRejected() {
        assertThrows(IllegalArgumentException.class, () -> service.create(template("t", "{not json"), "a"));
        verify(mapper, never()).insert(any());
    }

    /** JSON 非对象（数组/标量）：拒绝（模板参数必须是对象） */
    @Test
    void nonObjectJsonRejected() {
        assertThrows(IllegalArgumentException.class, () -> service.create(template("t", "[1,2,3]"), "a"));
        assertThrows(IllegalArgumentException.class, () -> service.create(template("t", "\"str\""), "a"));
    }

    /** 参数体积超限：拒绝 */
    @Test
    void oversizedJsonRejected() {
        String big = "{\"x\":\"" + "a".repeat(70 * 1024) + "\"}";
        assertThrows(IllegalArgumentException.class, () -> service.create(template("t", big), "a"));
    }

    /** 说明超长：拒绝 */
    @Test
    void tooLongDescriptionRejected() {
        StressTemplate t = template("t", "{}");
        t.setDescription("d".repeat(513));
        assertThrows(IllegalArgumentException.class, () -> service.create(t, "a"));
    }

    /** 更新：保留创建人与创建时间，只改可编辑字段 */
    @Test
    void updateKeepsCreator() {
        StressTemplate existing = template("old", "{}");
        existing.setId(3L);
        existing.setCreator("alice");
        when(mapper.findById(3L)).thenReturn(existing);

        StressTemplate patch = template("new", "{\"concurrency\":10}");
        patch.setDescription("新说明");
        patch.setAppName("app1");

        StressTemplate updated = service.update(3L, patch);

        assertEquals("new", updated.getTemplateName());
        assertEquals("{\"concurrency\":10}", updated.getTaskJson());
        assertEquals("alice", updated.getCreator(), "创建人不应被覆盖");
        verify(mapper).update(existing);
    }

    /** 更新不存在的模板：抛错 */
    @Test
    void updateMissingRejected() {
        when(mapper.findById(9L)).thenReturn(null);
        assertThrows(IllegalArgumentException.class, () -> service.update(9L, template("t", "{}")));
    }

    /** 删除：先校验存在 */
    @Test
    void deleteChecksExistence() {
        StressTemplate existing = template("t", "{}");
        existing.setId(4L);
        when(mapper.findById(4L)).thenReturn(existing);

        service.delete(4L);
        verify(mapper).deleteById(4L);

        when(mapper.findById(5L)).thenReturn(null);
        assertThrows(IllegalArgumentException.class, () -> service.delete(5L));
    }

    /** 列表：limit 做上下限夹取 */
    @Test
    void listClampsLimit() {
        service.list("app", 0);
        verify(mapper).query(eq("app"), eq(1));
        service.list("app", 9999);
        verify(mapper).query(eq("app"), eq(200));
    }

    /** 列表：appName 为 null 时按空串查（查全部） */
    @Test
    void listNullAppNameBecomesEmpty() {
        service.list(null, 50);
        verify(mapper).query(eq(""), eq(50));
    }

    /** 详情不存在：抛错 */
    @Test
    void detailMissingRejected() {
        when(mapper.findById(7L)).thenReturn(null);
        assertThrows(IllegalArgumentException.class, () -> service.detail(7L));
    }

    /** 已存在的模板可正常返回详情 */
    @Test
    void detailReturnsTemplate() {
        StressTemplate t = template("t", "{}");
        t.setId(7L);
        when(mapper.findById(7L)).thenReturn(t);
        assertEquals(7L, service.detail(7L).getId());
    }

    /** 正常 JSON 对象原样保留（不做字段裁剪） */
    @Test
    void validJsonPreservedAsIs() {
        String json = "{\"targetUrl\":\"http://x\",\"concurrency\":50,\"scenario\":[{\"url\":\"http://y\"}]}";
        StressTemplate saved = service.create(template("t", json), "a");
        assertEquals(json, saved.getTaskJson());
    }

    /** appName 为 null：归一化为空串（列 NOT NULL） */
    @Test
    void nullAppNameBecomesEmptyString() {
        StressTemplate t = template("t", "{}");
        t.setAppName(null);
        assertEquals("", service.create(t, "a").getAppName());
    }
}
