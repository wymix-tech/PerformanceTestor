package com.perfserver.mapper;

import com.perfserver.entity.AppInstance;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.util.List;

/**
 * 被测应用实例 Mapper：agent 上报心跳 upsert + 应用列表查询。
 */
@Mapper
public interface AppInstanceMapper {

    /** 按 app_name upsert（不存在则插入，存在则刷新心跳） */
    int upsertByAppName(AppInstance instance);

    /** 查询全部应用（在线优先） */
    List<AppInstance> findAll();

    /** 按 app_name 精确查询 */
    AppInstance findByAppName(@Param("appName") String appName);
}
