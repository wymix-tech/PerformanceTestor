package com.perftestor.datagen;

import com.perftestor.datagen.model.DataGenSpec;
import com.perftestor.datagen.model.EngineListener;
import com.perftestor.datagen.model.EngineResult;

/**
 * 引擎门面契约：宿主（perf-server 或任意使用方）唯一入口。
 * 引擎无状态、不依赖持久化——进度与语义提示经回调交宿主自行落库。
 *
 * <p>dryRun=true 时完整执行校验路径（元数据读取、规则解析、关联识别、拓扑排序、
 * SQL 预检、外键池预检）但不执行 INSERT，经 onProgress 报告预计行数等信息。</p>
 */
public interface DataGenEngine {

    /**
     * 执行灌数（或试运行）。
     *
     * @param spec     表组规格（含连接信息、规则、可选源码文件、dryRun 标记）
     * @param listener 进度/提示回调（不可为 null）
     * @return 执行结果（成功/失败、行数统计、警告清单）
     */
    EngineResult execute(DataGenSpec spec, EngineListener listener);
}
