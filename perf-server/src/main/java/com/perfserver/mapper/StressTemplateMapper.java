package com.perfserver.mapper;

import com.perfserver.entity.StressTemplate;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.util.List;

/**
 * 压测模板 Mapper。
 */
@Mapper
public interface StressTemplateMapper {

    int insert(StressTemplate template);

    /** 更新模板（仅名称/应用/参数/说明；创建人与创建时间不变） */
    int update(StressTemplate template);

    int deleteById(@Param("id") long id);

    StressTemplate findById(@Param("id") long id);

    /**
     * 模板列表：按应用过滤（{@code appName} 为空则查全部）。
     *
     * <p>包含"通用模板"（{@code app_name=''}）——与知识库同一语义：
     * 通用模板对所有项目可见，这类沉淀跨项目复用价值最高。
     */
    List<StressTemplate> query(@Param("appName") String appName, @Param("limit") int limit);
}
