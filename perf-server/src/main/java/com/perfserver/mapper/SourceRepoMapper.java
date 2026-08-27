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

    SourceRepo findById(@Param("id") long id);

    List<SourceRepo> query(@Param("limit") int limit);
}
