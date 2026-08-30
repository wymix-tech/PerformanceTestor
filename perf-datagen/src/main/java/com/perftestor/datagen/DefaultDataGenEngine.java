package com.perftestor.datagen;

import com.perftestor.datagen.engine.IdPool;
import com.perftestor.datagen.engine.RelationResolver;
import com.perftestor.datagen.engine.RuleEngine;
import com.perftestor.datagen.engine.TopoSorter;
import com.perftestor.datagen.enumv.EnumValueDiscoverer;
import com.perftestor.datagen.model.DataGenSpec;
import com.perftestor.datagen.model.EngineListener;
import com.perftestor.datagen.model.EngineResult;
import com.perftestor.datagen.model.FieldRule;
import com.perftestor.datagen.model.MagicHint;
import com.perftestor.datagen.model.RelationHint;
import com.perftestor.datagen.model.SchemaMetadata;
import com.perftestor.datagen.model.SourceFile;
import com.perftestor.datagen.model.TableSpec;
import com.perftestor.datagen.spi.BatchExecutor;
import com.perftestor.datagen.spi.DatabaseDialect;
import com.perftestor.datagen.spi.OrmSourceParser;
import com.perftestor.datagen.util.SqlLiteralExtractor;

import java.sql.Connection;
import java.sql.DriverManager;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Properties;
import java.util.concurrent.ThreadLocalRandom;

/**
 * 引擎默认实现：编排 关联识别 → 拓扑排序 → Dry-Run 校验/正式写入。
 * Dry-Run 优先：完整执行元数据读取、规则解析、关联识别、拓扑排序、SQL 预检与
 * 外键池预检，但不 INSERT；报告预计行数/事务时长估算/缺失警告。
 */
public class DefaultDataGenEngine implements DataGenEngine {

    /** 自引用表最大引用深度（parent_id 树层级上限，防爆栈） */
    static final int SELF_REF_MAX_DEPTH = 5;
    /** 父 ID 池填充批量游标步长 */
    private static final int POOL_FETCH_BATCH = 50_000;
    /** 高倾斜警告阈值：单热行承载子行占比 */
    static final double HOT_ROW_SHARE_WARN = 50.0;

    private final DatabaseDialect dialect;
    private final BatchExecutor executor;
    private final OrmSourceParser ormParser;
    private final EnumValueDiscoverer enumDiscoverer = new EnumValueDiscoverer();

    public DefaultDataGenEngine(DatabaseDialect dialect, BatchExecutor executor, OrmSourceParser ormParser) {
        this.dialect = dialect;
        this.executor = executor;
        this.ormParser = ormParser;
    }

    @Override
    public EngineResult execute(DataGenSpec spec, EngineListener listener) {
        EngineResult result = new EngineResult();
        result.setDryRun(spec.isDryRun());
        result.setTotalRows(spec.getTables().stream().mapToLong(TableSpec::getRowCount).sum());
        try {
            validate(spec);
            try (Connection conn = connect(spec)) {
                // 1. 元数据读取（缺失表立即失败）
                Map<String, SchemaMetadata> metas = new LinkedHashMap<>();
                for (TableSpec ts : spec.getTables()) {
                    SchemaMetadata m = dialect.fetchMetadata(conn, ts.getTableName());
                    if (m.getColumns().isEmpty()) {
                        throw new IllegalArgumentException("表不存在或无可见列: " + ts.getTableName());
                    }
                    metas.put(m.getTableName(), m);
                }

                // 2. 源码语义扫描（可选；失败静默降级）
                List<MagicHint> magicHints = new ArrayList<>();
                Map<String, List<String>> javaEnums = new HashMap<>();
                scanSource(spec, metas.keySet(), magicHints, javaEnums);
                List<RelationHint> ormHints = new ArrayList<>();
                if (ormParser != null && spec.getSourceFiles() != null && !spec.getSourceFiles().isEmpty()) {
                    for (RelationHint h : ormParser.scan(spec.getSourceFiles(), metas.keySet()).getRelationHints()) {
                        if (metas.containsKey(h.getChildTable()) && metas.containsKey(h.getParentTable())) {
                            ormHints.add(h);
                        }
                    }
                }

                // 3. 关联识别（四级合并，orm 语义参数化传递）与枚举建议
                //    解析器为 execute 内局部对象：引擎无状态，同一实例可并发执行多任务
                RelationResolver relationResolver = new RelationResolver();
                RelationResolver.RelationResolution rel =
                        relationResolver.resolve(metas.values(), spec, conn, dialect, ormHints);
                if (!rel.getPendingSuggestions().isEmpty()) {
                    listener.onWarn("存在基于命名约定推断的关联（低置信度），请在建议面板确认后采纳");
                }
                listener.onHints(magicHints, mergeHints(rel, magicHints));

                // 4. 依赖图 + 拓扑排序（环检测）
                Map<String, java.util.Set<String>> deps = buildDependencies(metas.keySet(), rel.getAutoEdges());
                List<String> order = TopoSorter.sort(metas.keySet(), deps);

                // 5. 枚举建议注入（未配置 enum 规则的列，用发现值生成建议提示）
                collectEnumSuggestions(conn, spec, metas, javaEnums, result);

                // 6. Dry-Run：估算后返回；正式：执行写入
                long estMs = estimateMaxTxnMs(spec);
                result.setEstimatedMaxTxnMs(estMs);
                if (spec.isDryRun()) {
                    listener.onProgress(result.getTotalRows(), 0, order.isEmpty() ? "" : order.get(0));
                    for (TableSpec ts : spec.getTables()) {
                        result.getTableStats().add(new EngineResult.TableStat(
                                ts.getTableName(), ts.getRowCount()));
                    }
                    result.setSuccess(true);
                    return result;
                }
                long inserted = insertAll(spec, conn, metas, rel, order, listener, result);
                result.setInsertedRows(inserted);
                result.setSuccess(true);
                return result;
            }
        } catch (Exception e) {
            result.setSuccess(false);
            result.setErrorMsg(e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage());
            return result;
        }
    }

    // ===== 写入编排（正式执行） =====

    private long insertAll(DataGenSpec spec, Connection conn, Map<String, SchemaMetadata> metas,
                           RelationResolver.RelationResolution rel, List<String> order,
                           EngineListener listener, EngineResult result) throws Exception {
        long totalInserted = 0;
        long totalRows = result.getTotalRows();

        // 边索引：子表 → (子列 → 父表.父列)
        Map<String, Map<String, RelationHint>> edgesByTable = new HashMap<>();
        for (RelationHint e : rel.getAutoEdges()) {
            edgesByTable.computeIfAbsent(e.getChildTable(), k -> new LinkedHashMap<>())
                    .put(e.getChildColumn(), e);
        }

        // 父 ID 池：表 → 列 → 池（自引用与组间引用共用）
        Map<String, Map<String, IdPool>> pools = new HashMap<>();

        for (String tableName : order) {
            TableSpec ts = spec.getTables().stream()
                    .filter(t -> t.getTableName().toLowerCase(Locale.ROOT).equals(tableName))
                    .findFirst().orElseThrow();
            SchemaMetadata meta = metas.get(tableName);
            Map<String, RelationHint> tableEdges = edgesByTable.getOrDefault(tableName, Map.of());

            // 先填充依赖父表的 ID 池
            for (RelationHint edge : tableEdges.values()) {
                ensurePool(spec, conn, metas, pools, edge.getParentTable(), edge.getParentColumn());
            }

            long inserted = insertTable(spec, conn, meta, ts, tableEdges, pools, listener, totalInserted, totalRows);
            totalInserted += inserted;
            result.getTableStats().add(new EngineResult.TableStat(tableName, inserted));

            // 自引用表：本表主键也进入池（后续行引用已插行）
            if (tableEdges.values().stream().anyMatch(e -> e.getParentTable().equals(tableName))) {
                String pk = meta.getPrimaryKeys().isEmpty() ? "id" : meta.getPrimaryKeys().iterator().next();
                ensurePool(spec, conn, metas, pools, tableName, pk);
            }
        }
        return totalInserted;
    }

    /** 确保父表主键池已填充（分段游标拉取，上限 50 万；幂等） */
    private void ensurePool(DataGenSpec spec, Connection conn, Map<String, SchemaMetadata> metas,
                            Map<String, Map<String, IdPool>> pools, String table, String column) throws Exception {
        Map<String, IdPool> byCol = pools.computeIfAbsent(table, k -> new LinkedHashMap<>());
        if (byCol.containsKey(column)) {
            return;
        }
        IdPool pool = new IdPool();
        SchemaMetadata meta = metas.get(table);
        boolean autoIncrementPk = meta != null && meta.findColumn(column) != null
                && meta.findColumn(column).isAutoIncrement();
        if (autoIncrementPk) {
            // 自增主键：游标分段拉取（id > lastId LIMIT batch）
            String sql = "SELECT " + dialect.quoteIdentifier(column) + " FROM "
                    + dialect.quoteIdentifier(table) + " WHERE " + dialect.quoteIdentifier(column)
                    + " > ? ORDER BY " + dialect.quoteIdentifier(column) + " LIMIT " + POOL_FETCH_BATCH;
            long lastId = 0;
            try (java.sql.PreparedStatement ps = conn.prepareStatement(sql)) {
                while (pool.size() < IdPool.MAX_POOL_SIZE) {
                    ps.setLong(1, lastId);
                    try (java.sql.ResultSet rs = ps.executeQuery()) {
                        int n = 0;
                        while (rs.next()) {
                            long id = rs.getLong(1);
                            pool.add(id);
                            lastId = Math.max(lastId, id);
                            n++;
                        }
                        if (n < POOL_FETCH_BATCH) {
                            break;
                        }
                    }
                }
            }
        } else if (meta != null) {
            // 非自增主键：直接全量拉取（上限内）
            String sql = "SELECT " + dialect.quoteIdentifier(column) + " FROM " + dialect.quoteIdentifier(table);
            try (java.sql.Statement st = conn.createStatement();
                 java.sql.ResultSet rs = st.executeQuery(sql)) {
                while (rs.next() && pool.size() < IdPool.MAX_POOL_SIZE) {
                    pool.add(rs.getLong(1));
                }
            }
        }
        byCol.put(column, pool);
    }

    /** 单表生成与批量插入（每表独立事务；ref/refSkew 采样 + orphanRate 独立池后置替换） */
    private long insertTable(DataGenSpec spec, Connection conn, SchemaMetadata meta, TableSpec ts,
                             Map<String, RelationHint> tableEdges, Map<String, Map<String, IdPool>> pools,
                             EngineListener listener, long insertedBefore, long totalRows) throws Exception {
        List<String> insertColumns = new ArrayList<>();
        List<SchemaMetadata.ColumnMeta> cols = new ArrayList<>();
        for (SchemaMetadata.ColumnMeta c : meta.getColumns()) {
            if (!c.isAutoIncrement()) {
                insertColumns.add(c.getName());
                cols.add(c);
            }
        }
        if (insertColumns.isEmpty()) {
            return 0;
        }
        String sql = dialect.buildInsertSql(meta.getTableName(), insertColumns);
        double orphanRate = ts.getOrphanRate() != null ? ts.getOrphanRate()
                : (spec.getOrphanRate() == null ? 0 : spec.getOrphanRate());
        String pk = meta.getPrimaryKeys().isEmpty() ? "id" : meta.getPrimaryKeys().iterator().next();
        boolean selfRef = tableEdges.values().stream()
                .anyMatch(e -> e.getParentTable().equals(meta.getTableName()));

        // refSkew 权重预热
        Map<String, IdPool> poolByCol = pools.get(meta.getTableName()) == null
                ? Map.of() : pools.get(meta.getTableName());
        for (RelationHint edge : tableEdges.values()) {
            FieldRule fr = ts.getFields().get(edge.getChildColumn());
            if (fr != null && FieldRule.TYPE_REF_SKEW.equals(fr.getType())) {
                Map<String, IdPool> parentPools = pools.get(edge.getParentTable());
                if (parentPools != null) {
                    IdPool pp = parentPools.get(edge.getParentColumn());
                    if (pp != null) {
                        int hp = fr.getHotPercent() == null ? 20 : fr.getHotPercent().intValue();
                        pp.buildSkewWeights(hp, fr.getHotShare() == null ? 80 : fr.getHotShare().intValue());
                        // 高倾斜警告（单热行承载占比估算）
                        double perHot = 100.0 * (fr.getHotShare() == null ? 80 : fr.getHotShare())
                                / Math.max(1, hp);
                        if (perHot > HOT_ROW_SHARE_WARN && !spec.isDryRun()) {
                            listener.onWarn(String.format(
                                    "表 %s 列 %s 热点倾斜极高（单热行约承载 %.0f%% 子行），"
                                            + "建议降低 BATCH_SIZE 或为子表分区",
                                    ts.getTableName(), edge.getChildColumn(), perHot));
                        }
                    }
                }
            }
        }

        long inserted = 0;
        int batchSize = executor.getBatchSize();
        conn.setAutoCommit(false);
        try {
            List<Object[]> batch = new ArrayList<>(batchSize);
            for (int row = 0; row < ts.getRowCount(); row++) {
                batch.add(buildRow(conn, meta, ts, insertColumns, cols, tableEdges, pools, pk, selfRef, orphanRate));
                if (batch.size() >= batchSize) {
                    executor.executeBatch(conn, sql, batch);
                    inserted += batch.size();
                    batch.clear();
                    conn.commit();
                    listener.onProgress(totalRows, insertedBefore + inserted, meta.getTableName());
                }
            }
            if (!batch.isEmpty()) {
                executor.executeBatch(conn, sql, batch);
                inserted += batch.size();
                conn.commit();
                listener.onProgress(totalRows, insertedBefore + inserted, meta.getTableName());
            }
        } catch (Exception e) {
            conn.rollback();
            throw e;
        } finally {
            conn.setAutoCommit(true);
        }
        return inserted;
    }

    /** 构造一行参数值：规则引擎 + ref/refSkew 池采样 + orphanRate 后置替换 */
    private Object[] buildRow(Connection conn, SchemaMetadata meta, TableSpec ts,
                              List<String> insertColumns, List<SchemaMetadata.ColumnMeta> cols,
                              Map<String, RelationHint> tableEdges, Map<String, Map<String, IdPool>> pools,
                              String pk, boolean selfRef, double orphanRate) {
        ThreadLocalRandom r = ThreadLocalRandom.current();
        Object[] row = new Object[insertColumns.size()];
        boolean orphan = r.nextDouble() < orphanRate;

        for (int i = 0; i < insertColumns.size(); i++) {
            String colName = insertColumns.get(i);
            SchemaMetadata.ColumnMeta col = cols.get(i);
            FieldRule fr = ts.getFields().get(colName);
            Object value;

            RelationHint edge = tableEdges.get(colName);
            boolean isRefRule = fr != null && (FieldRule.TYPE_REF.equals(fr.getType())
                    || FieldRule.TYPE_REF_SKEW.equals(fr.getType()));
            if (edge != null || isRefRule) {
                String parentTable = edge != null ? edge.getParentTable()
                        : fr.getTable().toLowerCase(Locale.ROOT);
                String parentColumn = edge != null ? edge.getParentColumn()
                        : (fr.getColumn() == null ? "id" : fr.getColumn());
                IdPool pool = pools.get(parentTable) == null ? null : pools.get(parentTable).get(parentColumn);
                value = sampleRef(pool, fr, orphan, col);
            } else {
                value = fr != null ? RuleEngine.applyRule(fr, col) : RuleEngine.generateDefault(col);
                if (orphan && colName.endsWith("_" + pk) && !col.isNullable()) {
                    value = null; // 不可空列的孤儿策略由池采样无效 ID 处理（见 sampleRef），此处仅可空列置 NULL
                }
            }
            row[i] = value;
        }
        return row;
    }

    /** ref/refSkew 采样；orphan=true 时按可空性置 NULL 或无效 ID（独立于倾斜池，后置替换） */
    private Object sampleRef(IdPool pool, FieldRule fr, boolean orphan, SchemaMetadata.ColumnMeta col) {
        boolean skewed = fr != null && FieldRule.TYPE_REF_SKEW.equals(fr.getType());
        if (orphan) {
            if (col.isNullable() && ThreadLocalRandom.current().nextBoolean()) {
                return null;
            }
            return pool == null ? 0L : pool.sampleInvalidId();
        }
        if (pool == null || pool.isEmpty()) {
            // 无池可用（父表组外/空表兜底未采纳）：退化随机数，交由 dry-run 警告提示
            return RuleEngine.generateDefault(col);
        }
        return skewed ? pool.sampleSkewed() : pool.sampleUniform();
    }

    private void validate(DataGenSpec spec) {
        if (!dialect.supports(spec.getJdbcUrl())) {
            throw new IllegalArgumentException("不支持的 JDBC URL（本模块仅配置了对应方言）");
        }
        if (spec.getTables() == null || spec.getTables().isEmpty()) {
            throw new IllegalArgumentException("表组为空");
        }
        long total = spec.getTables().stream().mapToLong(TableSpec::getRowCount).sum();
        if (total < 1 || total > spec.getMaxRows()) {
            throw new IllegalArgumentException("表组总行数必须在 1-" + spec.getMaxRows() + " 之间");
        }
        for (TableSpec ts : spec.getTables()) {
            if (ts.getTableName() == null || !ts.getTableName().matches("[A-Za-z0-9_]+")) {
                throw new IllegalArgumentException("表名仅允许字母数字下划线: " + ts.getTableName());
            }
            if (ts.getRowCount() < 0) {
                throw new IllegalArgumentException("行数非法: " + ts.getTableName());
            }
        }
    }

    private Connection connect(DataGenSpec spec) throws Exception {
        Properties props = new Properties();
        props.setProperty("user", spec.getDbUser() == null ? "" : spec.getDbUser());
        props.setProperty("password", spec.getDbPassword() == null ? "" : spec.getDbPassword());
        props.setProperty("connectTimeout", "5000");
        props.setProperty("socketTimeout", "60000");
        return DriverManager.getConnection(dialect.enrichJdbcUrl(spec.getJdbcUrl()), props);
    }

    private void scanSource(DataGenSpec spec, java.util.Set<String> tables,
                            List<MagicHint> outMagic, Map<String, List<String>> outEnums) {
        List<SourceFile> files = spec.getSourceFiles();
        if (files == null || files.isEmpty()) {
            return;
        }
        try {
            OrmSourceParser.ScanResult sr = ormParser.scan(files, tables);
            outMagic.addAll(sr.getMagicHints());
            outEnums.putAll(sr.getJavaEnums());
        } catch (Exception e) {
            // 源码解析失败静默降级：仅依赖 DB 元数据
        }
    }

    private List<RelationHint> mergeHints(RelationResolver.RelationResolution rel, List<MagicHint> magic) {
        List<RelationHint> all = new ArrayList<>(rel.getAutoEdges());
        all.addAll(rel.getPendingSuggestions());
        return all;
    }

    private Map<String, java.util.Set<String>> buildDependencies(java.util.Set<String> tables,
                                                                 List<RelationHint> edges) {
        Map<String, java.util.Set<String>> deps = new HashMap<>();
        for (String t : tables) {
            deps.put(t, new java.util.LinkedHashSet<>());
        }
        for (RelationHint e : edges) {
            if (deps.containsKey(e.getChildTable()) && deps.containsKey(e.getParentTable())
                    && !e.getChildTable().equals(e.getParentTable())) {
                deps.get(e.getChildTable()).add(e.getParentTable());
            }
        }
        return deps;
    }

    private void collectEnumSuggestions(Connection conn, DataGenSpec spec,
                                        Map<String, SchemaMetadata> metas,
                                        Map<String, List<String>> javaEnums, EngineResult result) {
        for (TableSpec ts : spec.getTables()) {
            SchemaMetadata m = metas.get(ts.getTableName().toLowerCase(Locale.ROOT));
            if (m == null) {
                continue;
            }
            for (SchemaMetadata.ColumnMeta col : m.getColumns()) {
                if (ts.getFields().get(col.getName()) != null) {
                    continue; // 已配置规则
                }
                List<Object> values = enumDiscoverer.discover(conn, dialect, col, javaEnums);
                if (values.isEmpty() && EnumValueDiscoverer.suspectEnumColumn(col)) {
                    result.getWarnings().add("列 " + m.getTableName() + "." + col.getName()
                            + " 疑似枚举列（列名特征），建议人工配置 enum 规则");
                }
            }
        }
    }

    private long estimateMaxTxnMs(DataGenSpec spec) {
        int batchSize = executor.getBatchSize();
        long maxRows = spec.getTables().stream().mapToLong(TableSpec::getRowCount).max().orElse(0);
        // 经验估算：每批 5ms（rewriteBatchedStatements 多行插入），不含索引开销
        return (maxRows / Math.max(1, batchSize)) * 5L;
    }

}
