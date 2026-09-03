package com.perfserver.datagen;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.perfserver.entity.DataGenTask;
import com.perfserver.entity.SemanticHint;
import com.perfserver.mapper.DataGenTaskMapper;
import com.perfserver.mapper.SemanticHintMapper;
import com.perfserver.source.SourceFileProvider;
import com.perftestor.datagen.DefaultDataGenEngine;
import com.perftestor.datagen.model.DataGenSpec;
import com.perftestor.datagen.model.EngineListener;
import com.perftestor.datagen.model.FieldRule;
import com.perftestor.datagen.model.EngineResult;
import com.perftestor.datagen.model.MagicHint;
import com.perftestor.datagen.model.RelationHint;
import com.perftestor.datagen.model.SchemaMetadata;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.sql.Connection;
import java.sql.DatabaseMetaData;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Properties;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 数据灌入服务（宿主编排层）：任务落库 + 引擎调用 + 进度/语义提示持久化。
 * 生成逻辑全部委托 perf-datagen 模块（联表外键感知/枚举语义化/魔法值感知/Dry-Run）。
 *
 * <p>安全：JDBC URL 仅允许已注册方言的库种（当前 jdbc:mysql:// 与 jdbc:postgresql://）；
 * 表名白名单正则；表组总行数上限 100 万。</p>
 */
@Service
public class DataGenService {

    private static final Logger log = LoggerFactory.getLogger(DataGenService.class);

    private static final int MAX_ROWS = 1_000_000;
    private static final String TABLE_NAME_REGEX = "^[A-Za-z0-9_]+$";
    /** 端点自检抽检数量 */
    private static final int SELF_CHECK_ENDPOINTS = 3;

    private static final ExecutorService DATAGEN_EXECUTOR = Executors.newFixedThreadPool(2, r -> {
        Thread t = new Thread(r, "datagen-runner");
        t.setDaemon(true);
        return t;
    });

    private final DataGenTaskMapper taskMapper;
    private final SemanticHintMapper hintMapper;
    private final ObjectMapper objectMapper;
    private final DataGenSpecAdapter specAdapter;
    private final DefaultDataGenEngine engine;
    private final SourceFileProvider sourceFileProvider;
    private final com.perfserver.source.EndpointScanner endpointScanner;
    private final com.perfserver.mapper.AppInstanceMapper appInstanceMapper;
    private final com.perfserver.mapper.SourceRepoMapper sourceRepoMapper;
    private final com.perfserver.mapper.DataGenPlanMapper planMapper;
    private final com.perfserver.mapper.DbConnMapper dbConnMapper;
    private final com.perfserver.security.CryptoService cryptoService;
    private final com.perfserver.datagen.plan.EndpointDataPlanService planService;
    private final com.perftestor.datagen.enumv.EnumValueDiscoverer enumDiscoverer =
            new com.perftestor.datagen.enumv.EnumValueDiscoverer();

    public DataGenService(DataGenTaskMapper taskMapper, SemanticHintMapper hintMapper,
                          ObjectMapper objectMapper, DataGenSpecAdapter specAdapter,
                          DefaultDataGenEngine engine, SourceFileProvider sourceFileProvider,
                          com.perfserver.source.EndpointScanner endpointScanner,
                          com.perfserver.mapper.AppInstanceMapper appInstanceMapper,
                          com.perfserver.mapper.SourceRepoMapper sourceRepoMapper,
                          com.perfserver.mapper.DataGenPlanMapper planMapper,
                          com.perfserver.mapper.DbConnMapper dbConnMapper,
                          com.perfserver.security.CryptoService cryptoService,
                          com.perfserver.datagen.plan.EndpointDataPlanService planService) {
        this.taskMapper = taskMapper;
        this.hintMapper = hintMapper;
        this.objectMapper = objectMapper;
        this.specAdapter = specAdapter;
        this.engine = engine;
        this.sourceFileProvider = sourceFileProvider;
        this.endpointScanner = endpointScanner;
        this.appInstanceMapper = appInstanceMapper;
        this.sourceRepoMapper = sourceRepoMapper;
        this.planMapper = planMapper;
        this.dbConnMapper = dbConnMapper;
        this.cryptoService = cryptoService;
        this.planService = planService;
    }

    /** 创建任务并异步执行（dryRun=true 时仅校验预检） */
    public DataGenTask createAndRun(DataGenRequest request) {
        validate(request);
        boolean multiTable = request.getTables() != null && !request.getTables().isEmpty();
        int totalRows = multiTable
                ? request.getTables().stream().mapToInt(DataGenRequest.TableSpecDto::getRowCount).sum()
                : request.getRowCount();

        DataGenTask task = new DataGenTask();
        task.setTaskName(request.getTaskName() == null || request.getTaskName().isBlank()
                ? "灌数据-" + (multiTable ? request.getTables().get(0).getTableName() : request.getTableName())
                : request.getTaskName());
        task.setDbUrl(request.getDbUrl());
        task.setTableName(multiTable ? request.getTables().get(0).getTableName() : request.getTableName());
        task.setRowCount(multiTable ? 0 : request.getRowCount());
        task.setTotalRows(totalRows);
        task.setRuleJson(request.getRuleJson());
        task.setStatus("pending");
        task.setInserted(0);
        try {
            task.setTablesJson(specAdapter.toTablesJson(request));
        } catch (Exception ignore) {
            // JSON 序列化失败不阻断建任务
        }
        taskMapper.insert(task);

        final long taskId = task.getId();
        final DataGenRequest req = request;
        DATAGEN_EXECUTOR.submit(() -> {
            long start = System.currentTimeMillis();
            try {
                taskMapper.updateStatus(taskId, "running");
                EngineResult result = runEngine(req, taskId);
                if (result.isSuccess()) {
                    taskMapper.updateFinished(taskId, "finished",
                            (int) result.getInsertedRows(), System.currentTimeMillis() - start, null);
                    log.info("datagen finished, taskId={}, dryRun={}, rows={}",
                            taskId, req.isDryRun(), result.getInsertedRows());
                } else {
                    taskMapper.updateFinished(taskId, "failed", 0,
                            System.currentTimeMillis() - start, result.getErrorMsg());
                }
            } catch (Exception e) {
                log.error("datagen failed, taskId={}", taskId, e);
                taskMapper.updateFinished(taskId, "failed", 0,
                        System.currentTimeMillis() - start, e.getMessage());
            }
        });
        return task;
    }

    /**
     * 执行引擎，遇环依赖时降级重试一次。
     *
     * <p>引擎拓扑排序会抛 {@code IllegalArgumentException}（消息已定位环中表名）。
     * 直接让任务失败对用户体验很差，故剔除环中置信度最低的一条 ref 边后重试一次：
     * 重试<b>复用同一 listener 与同一 dryRun 标记</b>，避免重复输出告警与重复落语义提示。
     * 重试上限 1 次，仍失败则写告警交用户手工调整。</p>
     */
    private EngineResult executeWithCycleFallback(DataGenRequest request, DataGenSpec spec,
                                                  EngineListener listener, List<String> warnings) throws Exception {
        try {
            return engine.execute(spec, listener);
        } catch (IllegalArgumentException e) {
            String msg = e.getMessage() == null ? "" : e.getMessage();
            if (!msg.contains("循环依赖")) {
                throw e;
            }
            com.perfserver.datagen.plan.PlanIntegrityChecker checker =
                    new com.perfserver.datagen.plan.PlanIntegrityChecker(objectMapper);
            List<com.perfserver.datagen.plan.PlanIntegrityChecker.PlanTable> tables =
                    toPlanTables(request.getTables());
            // 置信度必须来自规则 JSON：早前这里传 Map.of()，所有边都按默认 1.0 对待，
            // 「剔除最低置信度边」实际退化为「剔除环中第一条边」
            boolean broke = checker.breakWeakestCycleEdge(tables, msg, checker.edgeConfidence(tables));
            if (!broke) {
                warnings.add("检测到循环依赖且无法自动降级: " + msg);
                throw e;
            }
            warnings.add("检测到循环依赖，已剔除一条最低置信度的关联边后重试；被剔除列的外键值将为随机值");
            // 重建 spec（引用规则已变），复用同一 listener 与 dryRun
            List<com.perftestor.datagen.model.SourceFile> sourceFiles = request.getRepoId() != null
                    ? sourceFileProvider.loadFiles(request.getRepoId()) : List.of();
            applyPlanTables(request, tables);
            return engine.execute(specAdapter.toSpec(request, sourceFiles), listener);
        }
    }

    private static List<com.perfserver.datagen.plan.PlanIntegrityChecker.PlanTable> toPlanTables(
            List<DataGenRequest.TableSpecDto> dtos) {
        List<com.perfserver.datagen.plan.PlanIntegrityChecker.PlanTable> out = new ArrayList<>();
        if (dtos == null) {
            return out;
        }
        for (DataGenRequest.TableSpecDto d : dtos) {
            out.add(new com.perfserver.datagen.plan.PlanIntegrityChecker.PlanTable(
                    d.getTableName(), d.getRowCount(), d.getFields(), null));
        }
        return out;
    }

    private static void applyPlanTables(DataGenRequest request,
                                        List<com.perfserver.datagen.plan.PlanIntegrityChecker.PlanTable> tables) {
        List<DataGenRequest.TableSpecDto> dtos = new ArrayList<>();
        for (com.perfserver.datagen.plan.PlanIntegrityChecker.PlanTable t : tables) {
            DataGenRequest.TableSpecDto dto = new DataGenRequest.TableSpecDto();
            dto.setTableName(t.getTable());
            dto.setRowCount(Math.max(1, t.getRowCount()));
            dto.setFields(t.getFields());
            dtos.add(dto);
        }
        request.setTables(dtos);
    }

    /** 引擎执行 + 进度/语义提示落库 + 详情/自检报告 */
    private EngineResult runEngine(DataGenRequest request, long taskId) throws Exception {
        List<com.perftestor.datagen.model.SourceFile> sourceFiles = request.getRepoId() != null
                ? sourceFileProvider.loadFiles(request.getRepoId()) : List.of();
        DataGenSpec spec = specAdapter.toSpec(request, sourceFiles);

        ObjectNode detail = objectMapper.createObjectNode();
        List<String> warnings = new ArrayList<>();
        EngineListener listener = new EngineListener() {
            @Override
            public void onProgress(long totalRows, long insertedRows, String currentTable) {
                taskMapper.updateProgress(taskId, (int) insertedRows);
            }

            @Override
            public void onHints(List<MagicHint> magicHints, List<RelationHint> relationHints) {
                try {
                    persistHints(request.getRepoId(), magicHints, relationHints);
                } catch (Exception e) {
                    log.warn("persist semantic hints failed: {}", e.getMessage());
                }
            }

            @Override
            public void onWarn(String message) {
                warnings.add(message);
            }
        };

        EngineResult result = executeWithCycleFallback(request, spec, listener, warnings);
        if (sourceFiles.isEmpty() && request.getRepoId() != null) {
            warnings.add("源码不可读，魔法值仅基于数据库元数据（ENUM/CHECK/列注释），精准度下降");
        }

        // 详情：逐表统计 + 警告 + 试运行报告
        ArrayNode stats = detail.putArray("tableStats");
        result.getTableStats().forEach(s -> {
            ObjectNode st = stats.addObject();
            st.put("table", s.getTable());
            st.put("rows", s.getRows());
        });
        ArrayNode warns = detail.putArray("warnings");
        warnings.forEach(warns::add);
        if (result.getErrorMsg() != null) {
            detail.put("errorMsg", result.getErrorMsg());
        }
        if (request.isDryRun()) {
            ObjectNode dry = detail.putObject("dryRunReport");
            dry.put("estimatedMaxTxnMs", result.getEstimatedMaxTxnMs());
            dry.put("totalRows", result.getTotalRows());
            dry.put("passed", result.isSuccess());
        } else if (result.isSuccess()) {
            ObjectNode sc = selfCheck(request);
            if (sc != null) {
                detail.set("selfCheck", sc);
            }
        }
        taskMapper.updateDetail(taskId, detail.toString(), (int) result.getTotalRows());
        return result;
    }

    /** 语义提示落库（魔法值→value / 关联→relation；naming 系 pending 交前端确认；null 归一空串保证幂等键） */
    private void persistHints(Long repoId, List<MagicHint> magicHints, List<RelationHint> relationHints) {
        for (MagicHint mh : magicHints) {
            SemanticHint h = new SemanticHint();
            h.setRepoId(repoId);
            h.setHintType(SemanticHint.TYPE_VALUE);
            h.setTableName(nvl(mh.getTable()));
            h.setColumnName(nvl(mh.getColumn()));
            h.setValuesJson(toJson(mh.getValues()));
            h.setParentTable("");
            h.setParentColumn("");
            h.setSourceRefs(toJson(mh.getSourceRefs()));
            h.setSource(nvl(mh.getKind()));
            h.setStatus(SemanticHint.ST_PENDING);
            try {
                hintMapper.upsert(h);
            } catch (Exception e) {
                log.debug("upsert value hint failed: {}", e.getMessage());
            }
        }
        for (RelationHint rh : relationHints) {
            SemanticHint h = new SemanticHint();
            h.setRepoId(repoId);
            h.setHintType(SemanticHint.TYPE_RELATION);
            h.setTableName(nvl(rh.getChildTable()));
            h.setColumnName(nvl(rh.getChildColumn()));
            h.setParentTable(nvl(rh.getParentTable()));
            h.setParentColumn(nvl(rh.getParentColumn()));
            h.setSourceRefs(toJson(rh.getSourceRefs()));
            h.setSource(nvl(rh.getSource()));
            h.setConfidence(rh.getConfidence());
            h.setStatus(rh.autoApplicable() ? SemanticHint.ST_ADOPTED : SemanticHint.ST_PENDING);
            try {
                hintMapper.upsert(h);
            } catch (Exception e) {
                log.debug("upsert relation hint failed: {}", e.getMessage());
            }
        }
    }

    private static String nvl(String s) {
        return s == null ? "" : s.toLowerCase(Locale.ROOT);
    }

    /** 灌数后端点非空自检：抽 GET 接口实调，非空返回率写入详情 */
    private ObjectNode selfCheck(DataGenRequest request) {
        try {
            var repo = repoById(request.getRepoId());
            if (repo == null || repo.getAppName() == null || repo.getAppName().isBlank()) {
                return null;
            }
            var app = appInstanceMapper.findByAppName(repo.getAppName());
            if (app == null || app.getAppHost() == null || app.getAppPort() == null) {
                return null; // 无应用实例信息，跳过自检
            }
            List<Map<String, Object>> endpoints = endpointScanner.scan(request.getRepoId()).stream()
                    .filter(e -> "GET".equalsIgnoreCase(String.valueOf(e.get("httpMethod"))))
                    .limit(SELF_CHECK_ENDPOINTS)
                    .toList();
            if (endpoints.isEmpty()) {
                return null;
            }
            ObjectNode report = objectMapper.createObjectNode();
            ArrayNode items = report.putArray("endpoints");
            int nonEmpty = 0;
            for (Map<String, Object> ep : endpoints) {
                String path = String.valueOf(ep.get("path"));
                String url = "http://" + app.getAppHost() + ":" + app.getAppPort()
                        + (path.startsWith("/") ? path : "/" + path);
                ObjectNode item = items.addObject();
                item.put("url", url);
                item.put("path", path);
                try {
                    java.net.http.HttpClient client = java.net.http.HttpClient.newHttpClient();
                    java.net.http.HttpRequest req = java.net.http.HttpRequest.newBuilder()
                            .uri(java.net.URI.create(url))
                            .timeout(java.time.Duration.ofSeconds(5))
                            .GET().build();
                    java.net.http.HttpResponse<String> resp =
                            client.send(req, java.net.http.HttpResponse.BodyHandlers.ofString());
                    boolean nonEmptyBody = resp.statusCode() == 200
                            && resp.body() != null && resp.body().length() > 2;
                    item.put("status", resp.statusCode());
                    item.put("nonEmpty", nonEmptyBody);
                    if (nonEmptyBody) {
                        nonEmpty++;
                    }
                } catch (Exception e) {
                    item.put("status", -1);
                    item.put("nonEmpty", false);
                    item.put("error", e.getMessage());
                }
            }
            report.put("nonEmptyRate", endpoints.isEmpty() ? 0 : (double) nonEmpty / endpoints.size());
            if (nonEmpty == 0) {
                report.put("warning", "数据未命中业务谓词或服务不可达，压测结果可能失真");
            }
            return report;
        } catch (Exception e) {
            log.warn("datagen self-check failed: {}", e.getMessage());
            return null;
        }
    }

    private com.perfserver.entity.SourceRepo repoById(Long repoId) {
        return repoId == null ? null : sourceRepoMapper.findById(repoId);
    }

    private String toJson(Object o) {
        try {
            return objectMapper.writeValueAsString(o);
        } catch (Exception e) {
            return "[]";
        }
    }

    /** 读取表结构（跳过系统表） */
    private List<ColumnInfo> readColumns(Connection conn, String tableName) throws Exception {
        List<ColumnInfo> columns = new ArrayList<>();
        DatabaseMetaData meta = conn.getMetaData();
        String catalog = conn.getCatalog();
        try (ResultSet rs = meta.getColumns(catalog, null, tableName, "%")) {
            while (rs.next()) {
                ColumnInfo col = new ColumnInfo();
                col.setName(rs.getString("COLUMN_NAME"));
                col.setTypeName(rs.getString("TYPE_NAME"));
                col.setSize(rs.getInt("COLUMN_SIZE"));
                col.setNullable(rs.getInt("NULLABLE") != DatabaseMetaData.columnNoNulls);
                col.setAutoIncrement("YES".equalsIgnoreCase(rs.getString("IS_AUTOINCREMENT")));
                col.setComment(rs.getString("REMARKS"));
                columns.add(col);
            }
        }
        // 主键列标记
        try (ResultSet rs = meta.getPrimaryKeys(catalog, null, tableName)) {
            while (rs.next()) {
                String pk = rs.getString("COLUMN_NAME");
                for (ColumnInfo col : columns) {
                    if (col.getName().equalsIgnoreCase(pk)) {
                        col.setPrimaryKey(true);
                    }
                }
            }
        }
        return columns;
    }

    /**
     * 按接口数据方案执行灌数。
     *
     * <p>执行时序（顺序不可调换，见评审补充）：</p>
     * <ol>
     *   <li>读方案表清单</li>
     *   <li>{@code PlanIntegrityChecker} 补父表——<b>必须在构建 DataGenSpec 之前</b>，
     *       否则 explicit 边被引擎的 metaMap 过滤掉，ref 池为空且无任何报错</li>
     *   <li>{@code SeqStartResolver} 连库探测并刷新 seqStart（保证方案可重复执行）</li>
     *   <li>解析连接并组装 DataGenRequest</li>
     *   <li>复用 {@link #createAndRun}（引擎内部拓扑排序保证父表先灌）</li>
     * </ol>
     *
     * @param planId       方案 ID
     * @param dryRun       是否仅试运行
     * @param rowOverrides 用户在方案卡上改过的行数（表 → 行数），为空则按落库值执行
     * @return 灌数任务
     */
    public DataGenTask runFromPlan(long planId, boolean dryRun,
                                   Map<String, Integer> rowOverrides) {
        com.perfserver.entity.DataGenPlan plan = planMapper.findById(planId);
        if (plan == null) {
            throw new IllegalArgumentException("方案不存在: " + planId);
        }
        validatePlanConnection(plan);

        // ① 读方案
        List<com.perfserver.datagen.plan.PlanIntegrityChecker.PlanTable> tables =
                planService.parseTables(plan.getTablesJson());
        if (tables.isEmpty()) {
            throw new IllegalArgumentException("方案尚未分析出表清单（可能分析失败或接口未定位）");
        }

        // ② 补父表（早于构建 spec）
        com.perfserver.datagen.plan.PlanIntegrityChecker integrity =
                new com.perfserver.datagen.plan.PlanIntegrityChecker(objectMapper);
        com.perfserver.datagen.plan.PlanIntegrityChecker.CheckResult checked =
                integrity.completeParents(tables);
        List<String> warnings = new ArrayList<>(checked.getWarnings());

        // ③ 刷新 seqStart
        List<com.perfserver.datagen.plan.PlanIntegrityChecker.PlanTable> finalTables = checked.getTables();
        if (plan.getConnId() != null || plan.getDbUrl() != null) {
            refreshSeqStarts(plan, finalTables, warnings);
        }

        // ④ 组装请求：connId 必须在编排层换成真实连接三元组，
        //    否则 connId-only 方案会以 dbUrl=null 撞上 validate() 的方言校验
        ResolvedConn resolved = resolvePlanConn(plan);
        DataGenRequest request = new DataGenRequest();
        request.setTaskName(plan.getPlanName());
        request.setConnId(plan.getConnId());
        request.setDbUrl(resolved.getUrl());
        request.setDbUser(resolved.getUser());
        request.setDbPassword(resolved.getPassword());
        request.setRepoId(plan.getRepoId());
        request.setDryRun(dryRun);
        List<DataGenRequest.TableSpecDto> specs = new ArrayList<>();
        for (com.perfserver.datagen.plan.PlanIntegrityChecker.PlanTable t : finalTables) {
            DataGenRequest.TableSpecDto dto = new DataGenRequest.TableSpecDto();
            dto.setTableName(t.getTable());
            Integer override = rowOverrides == null ? null : rowOverrides.get(t.getTable());
            // 用户改过的行数优先（方案卡可编辑），并同样受上下限保护
            int rows = override != null && override > 0
                    ? Math.min(override, MAX_ROWS) : Math.max(1, t.getRowCount());
            dto.setRowCount(rows);
            dto.setFields(t.getFields());
            specs.add(dto);
        }
        request.setTables(specs);

        // ⑤ 执行（引擎内部拓扑排序保证父先子后）
        DataGenTask task = createAndRun(request);
        // 试运行不回写：updateTaskId 的 SQL 固定置 status='executed'，而 dryRun 并没有真正
        // 灌数——回写会把方案永久标成已执行（前端状态失真），且后续正式灌数时 taskId
        // 被最后一次试运行覆盖，「方案 → 任务」追溯链路指向一次空跑
        if (!dryRun && task != null && task.getId() != null) {
            try {
                planMapper.updateTaskId(planId, task.getId());
            } catch (Exception e) {
                log.warn("update plan taskId failed, planId={}: {}", planId, e.getMessage());
            }
        }
        if (!warnings.isEmpty()) {
            // 必须用「只更新 warnings」的专用 SQL：此处 plan 是方法开头 findById 取出的旧对象，
            // 其 taskId/status 仍是旧值，若走全字段 update 会把刚回写的 task_id/status 冲掉，
            // 导致「方案 → 任务」追溯链路丢失
            try {
                planMapper.updateWarnings(planId, toJson(warnings));
            } catch (Exception e) {
                log.warn("persist plan warnings failed, planId={}: {}", planId, e.getMessage());
            }
        }
        return task;
    }

    /** 方案是否尚未指定连接（分析阶段允许留空，执行前需补上） */
    public boolean planNeedsConnection(long planId) {
        com.perfserver.entity.DataGenPlan plan = planMapper.findById(planId);
        if (plan == null) {
            throw new IllegalArgumentException("方案不存在: " + planId);
        }
        return plan.getConnId() == null && (plan.getDbUrl() == null || plan.getDbUrl().isBlank());
    }

    /**
     * 回填方案连接（connId 与 dbUrl 二选一）。
     * 用于「分析时未指定连接、在数据准备页选定后执行」的流程。
     */
    public void applyPlanConnection(long planId, Map<String, Object> conn) {
        com.perfserver.entity.DataGenPlan plan = planMapper.findById(planId);
        if (plan == null) {
            throw new IllegalArgumentException("方案不存在: " + planId);
        }
        Long connId = toLong(conn.get("connId"));
        String dbUrl = conn.get("dbUrl") == null ? null : String.valueOf(conn.get("dbUrl"));
        boolean hasConn = connId != null;
        boolean hasUrl = dbUrl != null && !dbUrl.isBlank();
        if (hasConn == hasUrl) {
            throw new IllegalArgumentException("数据库连接配置 ID 与 JDBC URL 必须且只能提供其一");
        }
        if (hasConn) {
            if (dbConnMapper == null || dbConnMapper.findById(connId) == null) {
                throw new IllegalArgumentException("数据库连接配置不存在: " + connId);
            }
            plan.setConnId(connId);
            plan.setDbUrl(null);
        } else {
            plan.setConnId(null);
            plan.setDbUrl(dbUrl);
        }
        planMapper.update(plan);
    }

    private static Long toLong(Object v) {
        if (v == null) {
            return null;
        }
        if (v instanceof Number n) {
            return n.longValue();
        }
        String s = String.valueOf(v).trim();
        if (s.isEmpty()) {
            return null;
        }
        try {
            return Long.parseLong(s);
        } catch (NumberFormatException e) {
            return null;
        }
    }

    /** 解析后的可直连三元组（connId 或 dbUrl 二选一解析而来） */
    private static final class ResolvedConn {
        private final String url;
        private final String user;
        private final String password;

        ResolvedConn(String url, String user, String password) {
            this.url = url;
            this.user = user;
            this.password = password;
        }

        String getUrl() {
            return url;
        }

        String getUser() {
            return user;
        }

        String getPassword() {
            return password;
        }
    }

    /**
     * 把方案上的「connId 或 dbUrl」解析成可直连的 JDBC 三元组。
     *
     * <p>为什么必须在编排层做：connId 只是连接配置的引用，引擎与
     * {@link DataGenSpecAdapter} 都只认 {@code jdbcUrl / dbUser / dbPassword}，
     * 而适配器是无状态组件、拿不到 {@code DbConnMapper} 与解密服务。
     * 此前 {@code runFromPlan} 直接把 {@code plan.getDbUrl()}（connId-only 方案下为 null）
     * 塞进 request，最终撞在 {@link #validate} 的 {@code validateDbUrl(null)} 上抛异常——
     * connId 是本功能明确支持的连接方式，这条路径必现失败。</p>
     *
     * @throws IllegalArgumentException 连接配置不存在或不可用时
     */
    private ResolvedConn resolvePlanConn(com.perfserver.entity.DataGenPlan plan) {
        if (plan.getConnId() != null) {
            if (dbConnMapper == null) {
                throw new IllegalArgumentException("数据库连接配置不可用: " + plan.getConnId());
            }
            com.perfserver.entity.DbConn conn = dbConnMapper.findById(plan.getConnId());
            if (conn == null) {
                throw new IllegalArgumentException("数据库连接配置不存在: " + plan.getConnId());
            }
            return new ResolvedConn(conn.getDbUrl(), conn.getUsername(),
                    cryptoService.decrypt(conn.getPassword()));
        }
        return new ResolvedConn(plan.getDbUrl(), null, null);
    }

    /** 连接参数强校验：connId 与 dbUrl 必须且只能其一（DDL 不加 CHECK，统一在服务层校验） */
    private void validatePlanConnection(com.perfserver.entity.DataGenPlan plan) {
        boolean hasConn = plan.getConnId() != null;
        boolean hasUrl = plan.getDbUrl() != null && !plan.getDbUrl().isBlank();
        // 「两者都无」是执行期最常见的情形（分析阶段允许留空，执行时才补），
        // 必须单独给文案：说成「必须且只能提供其一」用户不知道该去哪里补
        if (!hasConn && !hasUrl) {
            throw new IllegalArgumentException("方案尚未指定数据库连接，请先选择连接配置或填写 JDBC URL");
        }
        if (hasConn && hasUrl) {
            throw new IllegalArgumentException("方案的数据库连接配置 ID 与 JDBC URL 只能提供其一");
        }
        if (hasConn && dbConnMapper != null && dbConnMapper.findById(plan.getConnId()) == null) {
            throw new IllegalArgumentException("数据库连接配置不存在: " + plan.getConnId());
        }
    }

    /**
     * 刷新方案中所有 seq 规则的起始值（探测失败回退 1 并写告警）。
     *
     * <p>连接前先做方言校验，与 {@link #listTables} 等既有入口保持一致：非法 URL 若直接交给
     * JDBC 驱动，报错信息是驱动内部的堆栈文本，用户看不出是 URL 填错。</p>
     */
    private void refreshSeqStarts(com.perfserver.entity.DataGenPlan plan,
                                  List<com.perfserver.datagen.plan.PlanIntegrityChecker.PlanTable> tables,
                                  List<String> warnings) {
        ResolvedConn resolved;
        try {
            resolved = resolvePlanConn(plan);
        } catch (IllegalArgumentException e) {
            warnings.add("序号起始值未刷新: " + e.getMessage());
            return;
        }
        String url = resolved.getUrl();
        if (url == null || url.isBlank()) {
            return;
        }
        try {
            validateDbUrl(url);
        } catch (IllegalArgumentException e) {
            warnings.add("序号起始值未刷新: " + e.getMessage());
            return;
        }
        // 没有任何 seq 规则时不建连：多数方案不含唯一编码列，白跑一次连库只会在
        // 目标库不可达时白白耗掉超时时间
        if (tables.stream().noneMatch(this::hasSeqRule)) {
            return;
        }
        if (resolved.getUser() == null || resolved.getUser().isBlank()) {
            // 方案不保存账号密码，dbUrl 形态下只能依赖 URL 内嵌凭据——把这条隐式依赖讲明白
            warnings.add("方案未保存数据库账号，序号起始值探测依赖 JDBC URL 内嵌凭据"
                    + "（URL 未带凭据时探测会失败并回退起始值 1）");
        }
        com.perfserver.datagen.plan.SeqStartResolver resolver =
                new com.perfserver.datagen.plan.SeqStartResolver(objectMapper);
        try (Connection conn = connect(url, resolved.getUser(), resolved.getPassword())) {
            for (com.perfserver.datagen.plan.PlanIntegrityChecker.PlanTable t : tables) {
                if (!hasSeqRule(t)) {
                    continue;
                }
                var refreshed = resolver.refresh(conn, t.getTable(), t.getFields());
                t.setFields(refreshed.getFieldsJson());
                warnings.addAll(refreshed.getWarnings());
            }
        } catch (Exception e) {
            warnings.add("序号起始值刷新失败（若库内已有数据可能撞唯一键）: " + e.getMessage());
            log.warn("refresh seqStart failed, planId={}: {}", plan.getId(), e.getMessage());
        }
    }

    private boolean hasSeqRule(com.perfserver.datagen.plan.PlanIntegrityChecker.PlanTable t) {
        return t.getFields() != null && t.getFields().contains("\"" + FieldRule.TYPE_SEQ + "\"");
    }

    /** 任务列表（时间倒序） */
    public List<DataGenTask> list(int limit) {
        return taskMapper.query(Math.max(1, Math.min(limit, 200)));
    }

    /** 任务详情 */
    public DataGenTask detail(long id) {
        return taskMapper.findById(id);
    }

    /** 目标库表清单（灌数据表单下拉） */
    public List<String> listTables(String dbUrl, String dbUser, String dbPassword) {
        validateDbUrl(dbUrl);
        List<String> tables = new ArrayList<>();
        try (Connection conn = connect(dbUrl, dbUser, dbPassword);
             ResultSet rs = conn.getMetaData().getTables(conn.getCatalog(), null, "%",
                     new String[]{"TABLE"})) {
            while (rs.next()) {
                tables.add(rs.getString("TABLE_NAME"));
            }
        } catch (Exception e) {
            throw new IllegalArgumentException("连接目标库失败: " + e.getMessage());
        }
        return tables;
    }

    /** 目标表列清单（规则配置表单；附枚举建议值与疑似枚举标记） */
    public List<ColumnInfo> listColumns(String dbUrl, String dbUser, String dbPassword, String tableName) {
        validateDbUrl(dbUrl);
        validateTableName(tableName);
        try (Connection conn = connect(dbUrl, dbUser, dbPassword)) {
            List<ColumnInfo> columns = readColumns(conn, tableName);
            for (ColumnInfo col : columns) {
                // 模块枚举发现：内联 ENUM / 注释（CHECK 与 Java 枚举由建议接口/任务回调补充）
                SchemaMetadata.ColumnMeta cm = new SchemaMetadata.ColumnMeta();
                cm.setName(col.getName());
                cm.setTypeName(col.getTypeName());
                cm.setComment(col.getComment());
                List<Object> values = enumDiscoverer.discover(null, null, cm, Map.of());
                if (!values.isEmpty()) {
                    col.setSuggestedValues(values);
                } else {
                    col.setSuspectEnum(com.perftestor.datagen.enumv.EnumValueDiscoverer
                            .suspectEnumColumn(cm));
                }
            }
            return columns;
        } catch (Exception e) {
            throw new IllegalArgumentException("读取表结构失败: " + e.getMessage());
        }
    }

    /** 语义建议查询（魔法值 value / 关联 relation；status 过滤 pending 可查看待确认项） */
    public List<SemanticHint> suggestions(Long repoId, String tableName, String hintType, String status) {
        return hintMapper.query(repoId, tableName, hintType,
                status == null || status.isBlank() ? null : status, 100);
    }

    /** 建议采纳/忽略（adopted / dismissed） */
    public void adopt(long hintId, boolean adopt) {
        SemanticHint hint = hintMapper.findById(hintId);
        if (hint == null) {
            throw new IllegalArgumentException("建议不存在: " + hintId);
        }
        hintMapper.updateStatus(hintId, adopt ? SemanticHint.ST_ADOPTED : SemanticHint.ST_DISMISSED);
    }

    private Connection connect(String dbUrl, String dbUser, String dbPassword) throws Exception {
        Properties props = new Properties();
        props.setProperty("user", dbUser == null ? "" : dbUser);
        props.setProperty("password", dbPassword == null ? "" : dbPassword);
        props.setProperty("connectTimeout", "5000");
        return DriverManager.getConnection(enrichUrl(dbUrl), props);
    }

    /**
     * URL 增强：按已注册方言路由，避免把 MySQL 专有参数拼到 PG 等其它库种上。
     * MySQL 追加 rewriteBatchedStatements（批量写入）+ useInformationSchema（读列注释），
     * PostgreSQL 追加 reWriteBatchedInserts；未匹配方言时原样返回（调用方已前置校验）。
     */
    private String enrichUrl(String url) {
        return engine.getDialects().stream()
                .filter(d -> d.supports(url))
                .findFirst()
                .map(d -> d.enrichJdbcUrl(url))
                .orElse(url);
    }

    /** 参数校验与上限保护（单表与表组两种形态） */
    private void validate(DataGenRequest request) {
        validateDbUrl(request.getDbUrl());
        boolean multiTable = request.getTables() != null && !request.getTables().isEmpty();
        if (multiTable) {
            int total = 0;
            for (DataGenRequest.TableSpecDto ts : request.getTables()) {
                validateTableName(ts.getTableName());
                if (ts.getRowCount() < 0) {
                    throw new IllegalArgumentException("行数非法: " + ts.getTableName());
                }
                total += ts.getRowCount();
            }
            if (total < 1 || total > MAX_ROWS) {
                throw new IllegalArgumentException("表组总行数必须在 1-" + MAX_ROWS + " 之间");
            }
        } else {
            validateTableName(request.getTableName());
            if (request.getRowCount() < 1 || request.getRowCount() > MAX_ROWS) {
                throw new IllegalArgumentException("行数必须在 1-" + MAX_ROWS + " 之间");
            }
        }
    }

    /**
     * JDBC URL 校验：仅允许已注册方言支持的库种（当前 MySQL + PostgreSQL）。
     *
     * <p>与 {@code DefaultDataGenEngine#resolveDialect} 使用同一套 {@code DatabaseDialect#supports}
     * 判定，避免校验通过但引擎路由失败的不一致状态；新增库种只需注册方言实现，此处自动放行。
     */
    private void validateDbUrl(String dbUrl) {
        if (dbUrl == null || dbUrl.isBlank()
                || engine.getDialects().stream().noneMatch(d -> d.supports(dbUrl))) {
            throw new IllegalArgumentException("JDBC URL 仅支持 jdbc:mysql:// 或 jdbc:postgresql:// 前缀");
        }
    }

    private void validateTableName(String tableName) {
        if (tableName == null || !tableName.matches(TABLE_NAME_REGEX)) {
            throw new IllegalArgumentException("表名仅允许字母数字下划线");
        }
    }
}
