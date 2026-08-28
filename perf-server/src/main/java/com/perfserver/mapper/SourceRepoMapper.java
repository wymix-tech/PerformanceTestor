package com.perfserver.mapper;

import com.perfserver.entity.SourceRepo;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.util.List;

/**
 * 源码仓库 Mapper。
 */
@Mapper
public interface SourceRepoMapper {

    int insert(SourceRepo repo);

    /** 更新导入状态 */
    int updateStatus(@Param("id") long id, @Param("status") String status, @Param("errorMsg") String errorMsg);

    /** 索引完成：更新文件数与总大小 */
    int updateIndexed(@Param("id") long id, @Param("fileCount") int fileCount, @Param("totalSize") long totalSize);

    /** 更新分支配置（切换分支） */
    int updateBranch(@Param("id") long id, @Param("branch") String branch);

    /** 更新绑定应用（空 = 解绑） */
    int updateAppName(@Param("id") long id, @Param("appName") String appName);

    /** 按项目名查询（agent 上报准入校验用），不存在返回 null */
    SourceRepo findByName(@Param("repoName") String repoName);

    SourceRepo findById(@Param("id") long id);

    List<SourceRepo> query(@Param("limit") int limit);
}
