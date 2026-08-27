package com.perfserver.mapper;

import com.perfserver.entity.DataGenTask;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.util.List;

/**
 * 灌数据任务 Mapper。
 */
@Mapper
public interface DataGenTaskMapper {

    int insert(DataGenTask task);

    int updateStatus(@Param("id") long id, @Param("status") String status);

    /** 进度更新（已插入行数） */
    int updateProgress(@Param("id") long id, @Param("inserted") int inserted);

    /** 完成/失败收尾 */
    int updateFinished(@Param("id") long id, @Param("status") String status,
                       @Param("inserted") int inserted, @Param("costMs") long costMs,
                       @Param("errorMsg") String errorMsg);

    DataGenTask findById(@Param("id") long id);

    List<DataGenTask> query(@Param("limit") int limit);
}
