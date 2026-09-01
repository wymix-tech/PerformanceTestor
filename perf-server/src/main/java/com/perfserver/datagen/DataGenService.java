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
    private final com.perftestor.datagen.enumv.EnumValueDiscoverer enumDiscoverer =
            new com.perftestor.datagen.enumv.EnumValueDiscoverer();

    public DataGenService(DataGenTaskMapper taskMapper, SemanticHintMapper hintMapper,
                          ObjectMapper objectMapper, DataGenSpecAdapter specAdapter,
                          DefaultDataGenEngine engine, SourceFileProvider sourceFileProvider,
                          com.perfserver.source.EndpointScanner endpointScanner,
                          com.perfserver.mapper.AppInstanceMapper appInstanceMapper,
                          com.perfserver.mapper.SourceRepoMapper sourceRepoMapper) {
        this.taskMapper = taskMapper;
        this.hintMapper = hintMapper;
        this.objectMapper = objectMapper;
        this.specAdapter = specAdapter;
        this.engine = engine;
        this.sourceFileProvider = sourceFileProvider;
        this.endpointScanner = endpointScanner;
        this.appInstanceMapper = appInstanceMapper;
        this.sourceRepoMapper = sourceRepoMapper;
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

        EngineResult result = engine.execute(spec, listener);
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
