package com.perfserver.mapper;

import com.perfserver.entity.SourceFile;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.util.List;

/**
 * 源码文件索引 Mapper。
 */
@Mapper
public interface SourceFileMapper {

    int batchInsert(List<SourceFile> list);

    /** 删除仓库全部文件索引（重新同步前清空） */
    int deleteByRepo(@Param("repoId") long repoId);

    /** 按仓库与相对路径精确查询 */
    SourceFile findByRepoAndPath(@Param("repoId") long repoId, @Param("relativePath") String relativePath);

    /** 仓库文件列表（支持前缀过滤，文件树浏览） */
    List<SourceFile> listByRepo(@Param("repoId") long repoId, @Param("prefix") String prefix,
                                @Param("limit") int limit);

    /** 按文件名精确查询（issue 关联定位用） */
    List<SourceFile> listByName(@Param("repoId") long repoId, @Param("fileName") String fileName,
                                @Param("limit") int limit);

    /** 按路径关键字包含匹配（issue 关联定位用，兼容带顶层目录的相对路径） */
    List<SourceFile> listByRepoContains(@Param("repoId") long repoId, @Param("keyword") String keyword,
                                        @Param("limit") int limit);

    SourceFile findById(@Param("id") long id);
}
