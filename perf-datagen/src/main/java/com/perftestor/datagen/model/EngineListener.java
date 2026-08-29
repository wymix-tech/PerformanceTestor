package com.perftestor.datagen.model;

/**
 * 进度与语义提示回调契约。
 * 引擎在执行过程中回调宿主；宿主据此更新任务进度（data_gen_task）与语义提示库（semantic_hint）。
 */
public interface EngineListener {

    /**
     * 进度回调：按批（每完成一个事务批次）触发。
     *
     * @param totalRows    本任务总目标行数（表组求和）
     * @param insertedRows 已插入（或试运行预估已处理）行数
     * @param currentTable 当前处理的表名
     */
    void onProgress(long totalRows, long insertedRows, String currentTable);

    /**
     * 语义提示回调：引擎识别的魔法值候选与表间关联建议（含低置信命名推断），
     * 由宿主落库供前端确认采纳。
     */
    void onHints(java.util.List<MagicHint> magicHints, java.util.List<RelationHint> relationHints);

    /** 非阻断警告（空表兜底提示、refSkew 高倾斜提示、源码不可读降级等），默认空实现 */
    default void onWarn(String message) {
    }
}
