package com.perfserver.mapper;

import com.perfserver.entity.JvmAlertRule;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.time.LocalDateTime;
import java.util.List;

/**
 * JVM 告警规则 Mapper：规则增删改查与告警时间回写。
 */
@Mapper
public interface JvmAlertRuleMapper {

    /** 规则列表（按应用名、指标键排序，便于前端分组展示） */
    List<JvmAlertRule> list(@Param("appName") String appName, @Param("limit") int limit);

    /** 按主键查询 */
    JvmAlertRule findById(@Param("id") long id);

    /** 新增规则（回填自增主键） */
    int insert(JvmAlertRule rule);

    /** 更新规则（阈值/窗口/冷却/启用/通知地址） */
    int update(JvmAlertRule rule);

    /** 删除规则 */
    int delete(@Param("id") long id);

    /** 仅查询启用中的规则（检测任务用） */
    List<JvmAlertRule> listEnabled();

    /** 回写上次告警时间（冷却期判定依据） */
    int updateLastAlertTime(@Param("id") long id, @Param("time") LocalDateTime time);
}
