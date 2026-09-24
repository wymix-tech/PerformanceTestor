package com.perfserver.source;

import com.perfserver.entity.SourceRepo;
import com.perfserver.mapper.StressTaskMapper;
import com.perfserver.service.DataCleanupService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 项目生命周期编排（P2-1 删除项目）：把"删项目"涉及的三件事收口到一处，
 * 保证顺序与一致性——关联历史数据清理 → 源码索引/记录/磁盘目录 → 缓存失效。
 *
 * <p><b>为什么要有这一层</b>：删除项目横跨 {@code DataCleanupService}（按 appName 清历史数据）
 * 与 {@link SourceManager}（删仓库自身），且必须带前置校验（二次确认、进行中压测拦截）。
 * 若把编排写进 Controller，磁盘与数据库操作会脱离事务边界，失败时留下半删状态。
 *
 * <p><b>事务边界</b>：数据库部分在同一事务内完成（清理 + 删索引 + 删记录），
 * 磁盘目录删除放在数据库事务之后（不可回滚，且失败只告警不回滚——记录已删，
 * 目录残留由运维清理，比"回滚后记录与目录都不一致"更可控）。
 */
@Service
public class ProjectLifecycleService {

    private static final Logger log = LoggerFactory.getLogger(ProjectLifecycleService.class);

    private final SourceManager sourceManager;
    private final DataCleanupService dataCleanupService;
    private final StressTaskMapper stressTaskMapper;

    public ProjectLifecycleService(SourceManager sourceManager, DataCleanupService dataCleanupService,
                                   StressTaskMapper stressTaskMapper) {
        this.sourceManager = sourceManager;
        this.dataCleanupService = dataCleanupService;
        this.stressTaskMapper = stressTaskMapper;
    }

    /** 删除结果：各环节删除量，供前端展示与审计 */
    public static class DeleteResult {
        public long repoId;
        public String repoName;
        public String appName;
        /** 是否连带清理了该应用的历史数据（仓库未绑定应用时为 false） */
        public boolean cleanedData;
        /** 磁盘源码目录是否已删除 */
        public boolean dirDeleted;
        /** 删除的源码文件索引条数 */
        public int sourceFiles;
        /** 历史数据各表删除量（未清理时为空表） */
        public Map<String, Integer> deleted;

        public long getRepoId() {
            return repoId;
        }

        public String getRepoName() {
            return repoName;
        }

        public String getAppName() {
            return appName;
        }

        public boolean isCleanedData() {
            return cleanedData;
        }

        public boolean isDirDeleted() {
            return dirDeleted;
        }

        public int getSourceFiles() {
            return sourceFiles;
        }

        public Map<String, Integer> getDeleted() {
            return deleted;
        }
    }

    /**
     * 删除项目（含关联历史数据）。
     *
     * <p>前置校验（任一不满足即抛 {@link IllegalArgumentException}/{@link IllegalStateException}）：
     * <ol>
     *   <li>项目存在；</li>
     *   <li>请求体回显的 repoName 与项目名精确一致（二次确认，防误删）；</li>
     *   <li>该项目绑定的应用没有进行中的压测任务（否则压测线程仍在写数据，删了会留下孤儿写入）。</li>
     * </ol>
     *
     * @param repoId         项目 ID
     * @param confirmRepoName 请求体回显的项目名（必须与项目名完全一致）
     * @param cleanData      是否连带清理该项目应用的历史数据（默认 true）
     */
    public DeleteResult deleteProject(long repoId, String confirmRepoName, boolean cleanData) {
        SourceRepo repo = sourceManager.getRepo(repoId);

        // 二次确认：项目名精确匹配。不做大小写/空白宽容——确认的意义就是"用户明确知道删的是哪个"
        String expected = repo.getRepoName() == null ? "" : repo.getRepoName();
        String provided = confirmRepoName == null ? "" : confirmRepoName.trim();
        if (!expected.equals(provided)) {
            throw new IllegalArgumentException(
                    "删除项目需二次确认：请求体 repoName 必须与项目名完全一致（当前项目名：" + expected + "）");
        }

        String appName = repo.getAppName() == null ? "" : repo.getAppName().trim();
        if (!appName.isEmpty()) {
            int running = stressTaskMapper.countRunningByApp(appName);
            if (running > 0) {
                throw new IllegalStateException(
                        "项目「" + expected + "」绑定的应用 " + appName + " 有 " + running
                                + " 个压测任务进行中，请等待完成或先终止后再删除");
            }
        }

        return doDelete(repo, appName, cleanData);
    }

    /**
     * 删除主体。
     *
     * <p><b>为什么不加 {@code @Transactional}</b>：本方法是同类内部调用，Spring AOP 的
     * 代理不会拦截自调用——标注了也不生效，反而给出"有事务保护"的错觉。
     * 数据库三步（清理历史数据 / 删索引 / 删记录）中，{@code DataCleanupService} 自身
     * 已带 {@code @Transactional}，索引与仓库记录的删除各自是单语句原子操作；
     * 磁盘目录删除刻意放在数据库之后，避免"事务回滚但目录已删"。
     */
    DeleteResult doDelete(SourceRepo repo, String appName, boolean cleanData) {
        DeleteResult result = new DeleteResult();
        result.repoId = repo.getId();
        result.repoName = repo.getRepoName();
        result.appName = appName;
        result.deleted = new LinkedHashMap<>();

        // 1. 关联历史数据（按应用名）：未绑定应用时无法定位，跳过并如实上报
        if (cleanData && !appName.isEmpty()) {
            result.deleted = dataCleanupService.cleanupByAppName(appName, false);
            result.cleanedData = true;
        }

        // 2. 源码索引 + 仓库记录（磁盘目录由 SourceManager 内部处理）
        result.sourceFiles = sourceManager.deleteRepo(repo.getId());
        result.dirDeleted = true;
        return result;
    }

    /** 项目删除前的占用概览（前端确认弹窗展示：有多少数据会被删） */
    public Map<String, Object> deletePreview(long repoId) {
        SourceRepo repo = sourceManager.getRepo(repoId);
        String appName = repo.getAppName() == null ? "" : repo.getAppName().trim();
        Map<String, Object> preview = new LinkedHashMap<>();
        preview.put("repoId", repoId);
        preview.put("repoName", repo.getRepoName());
        preview.put("appName", appName);
        preview.put("fileCount", repo.getFileCount() == null ? 0 : repo.getFileCount());
        preview.put("totalSize", repo.getTotalSize() == null ? 0 : repo.getTotalSize());
        preview.put("boundApp", !appName.isEmpty());
        preview.put("runningStress", appName.isEmpty() ? 0 : stressTaskMapper.countRunningByApp(appName));
        return preview;
    }

    /** 待删除项目名提示（错误信息/日志用，避免在别处重复取） */
    public List<String> bindableAppNames(List<SourceRepo> repos) {
        return repos.stream()
                .map(SourceRepo::getAppName)
                .filter(a -> a != null && !a.isBlank())
                .distinct()
                .toList();
    }
}
