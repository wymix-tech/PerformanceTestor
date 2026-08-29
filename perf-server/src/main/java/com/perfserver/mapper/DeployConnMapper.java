package com.perfserver.mapper;

import com.perfserver.entity.DeployConn;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.util.List;

/** 被测服务器 SSH 连接配置 Mapper。 */
@Mapper
public interface DeployConnMapper {

    int insert(DeployConn conn);

    List<DeployConn> list(@Param("limit") int limit);

    DeployConn findById(@Param("id") long id);

    int delete(@Param("id") long id);
}

