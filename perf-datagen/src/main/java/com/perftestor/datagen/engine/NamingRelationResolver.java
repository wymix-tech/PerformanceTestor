package com.perftestor.datagen.engine;

import com.perftestor.datagen.model.RelationHint;
import com.perftestor.datagen.model.SchemaMetadata;

import java.sql.Connection;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/**
 * 命名约定关联推断（第四级兜底）：子表列 {@code <parent>_id}（单复数归一）匹配父表主键 + 类型兼容校验。
 * 空表兜底：父表 0 行 → 无条件采纳（0.3 / NAMING_EMPTY_TABLE_FALLBACK）。
 * 抽样：父表 <100 行全表、>100 抽 1000 条；超时 500ms 降级纯命名（0.5）。
 */
public class NamingRelationResolver {

    static final int SAMPLE_SIZE = 1000;
    static final long SAMPLE_TIMEOUT_MS = 500;
    static final int FULL_SCAN_THRESHOLD = 100;
    static final double CONTAIN_RATE_THRESHOLD = 0.95;

    private final ExecutorService sampler = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "datagen-naming-sampler");
        t.setDaemon(true);
        return t;
    });

    public List<RelationHint> resolve(SchemaMetadata child, Map<String, SchemaMetadata> parents,
                                      Connection conn, com.perftestor.datagen.spi.DatabaseDialect dialect) {
        List<RelationHint> hints = new ArrayList<>();
        if (child == null || parents.isEmpty()) {
            return hints;
        }
        String pk = child.getPrimaryKeys().isEmpty() ? "id" : child.getPrimaryKeys().iterator().next();
        for (SchemaMetadata.ColumnMeta col : child.getColumns()) {
            if (col.isPrimaryKey() || col.isAutoIncrement() || !RuleEngine.isIntegerFamily(col.getTypeName())) {
                continue;
            }
            String parentTable = matchParentTable(col.getName(), pk, parents.keySet());
            if (parentTable == null) {
                continue;
            }
            SchemaMetadata parent = parents.get(parentTable);
            if (parent == null || parent.getPrimaryKeys().isEmpty()) {
                continue;
            }
            String parentPk = parent.getPrimaryKeys().iterator().next();
            SchemaMetadata.ColumnMeta parentPkCol = parent.findColumn(parentPk);
            if (parentPkCol == null || !RuleEngine.isIntegerFamily(parentPkCol.getTypeName())) {
                continue;
            }
            if (conn == null || dialect == null) {
                hints.add(new RelationHint(child.getTableName(), col.getName(),
                        parentTable, parentPk, RelationHint.SRC_NAMING, 0.5));
                continue;
            }
            hints.add(verifyBySampling(child, col.getName(), parent, parentPk, conn, dialect));
        }
        return hints;
    }

    RelationHint verifyBySampling(SchemaMetadata child, String childColumn, SchemaMetadata parent,
                                  String parentPk, Connection conn,
                                  com.perftestor.datagen.spi.DatabaseDialect dialect) {
        if (parent.getRowCount() == 0) {
            return RelationHint.namingFallback(child.getTableName(), childColumn,
                    parent.getTableName(), parentPk);
        }
        List<Long> parentIds = sampleParentIds(parent.getTableName(), parentPk,
                parent.getRowCount(), conn, dialect);
        if (parentIds.isEmpty()) {
            return new RelationHint(child.getTableName(), childColumn, parent.getTableName(), parentPk,
                    RelationHint.SRC_NAMING, 0.5);
        }
        double containRate = childContainRate(child.getTableName(), childColumn, parentIds, conn, dialect);
        if (containRate < 0) {
            return new RelationHint(child.getTableName(), childColumn, parent.getTableName(), parentPk,
                    RelationHint.SRC_NAMING, 0.5);
        }
        double confidence = containRate >= CONTAIN_RATE_THRESHOLD ? 0.85 : 0.4;
        RelationHint h = new RelationHint(child.getTableName(), childColumn, parent.getTableName(), parentPk,
                RelationHint.SRC_NAMING, confidence);
        h.getSourceRefs().add(String.format("sample(containRate=%.2f)", containRate));
        return h;
    }

    private List<Long> sampleParentIds(String table, String pk, long rowCount, Connection conn,
                                       com.perftestor.datagen.spi.DatabaseDialect dialect) {
        Future<List<Long>> future = sampler.submit(() -> {
            List<Long> ids = new ArrayList<>();
            String sql = "SELECT " + dialect.quoteIdentifier(pk) + " FROM " + dialect.quoteIdentifier(table);
            if (rowCount > FULL_SCAN_THRESHOLD) {
                sql += " ORDER BY " + dialect.quoteIdentifier(pk) + " LIMIT " + SAMPLE_SIZE;
            }
            try (java.sql.Statement st = conn.createStatement();
                 java.sql.ResultSet rs = st.executeQuery(sql)) {
                while (rs.next() && ids.size() < SAMPLE_SIZE) {
                    ids.add(rs.getLong(1));
                }
            }
            return ids;
        });
        try {
            return future.get(SAMPLE_TIMEOUT_MS, TimeUnit.MILLISECONDS);
        } catch (TimeoutException e) {
            future.cancel(true);
            return List.of();
        } catch (Exception e) {
            return List.of();
        }
    }

    private double childContainRate(String table, String fkColumn, List<Long> parentIds,
                                    Connection conn, com.perftestor.datagen.spi.DatabaseDialect dialect) {
        try {
            java.util.Set<Long> pool = new java.util.HashSet<>(parentIds);
            String sql = "SELECT " + dialect.quoteIdentifier(fkColumn) + " FROM "
                    + dialect.quoteIdentifier(table) + " LIMIT " + SAMPLE_SIZE;
            int total = 0, hit = 0;
            try (java.sql.Statement st = conn.createStatement();
                 java.sql.ResultSet rs = st.executeQuery(sql)) {
                while (rs.next()) {
                    long v = rs.getLong(1);
                    if (!rs.wasNull()) {
                        total++;
                        if (pool.contains(v)) {
                            hit++;
                        }
                    }
                }
            }
            return total == 0 ? -1 : (double) hit / total;
        } catch (Exception e) {
            return -1;
        }
    }

    static String matchParentTable(String columnName, String childPk, Collection<String> tableNames) {
        String col = columnName.toLowerCase(Locale.ROOT);
        String suffix = "_" + childPk.toLowerCase(Locale.ROOT);
        if (!col.endsWith(suffix)) {
            return null;
        }
        String stem = col.substring(0, col.length() - suffix.length());
        if (stem.isEmpty()) {
            return null;
        }
        for (String t : tableNames) {
            if (t.equals(stem) || t.equals(singular(stem)) || t.equals(plural(stem))) {
                return t;
            }
        }
        return null;
    }

    static String singular(String s) {
        if (s.endsWith("ies") && s.length() > 3) {
            return s.substring(0, s.length() - 3) + "y";
        }
        if (s.endsWith("ses") && s.length() > 3) {
            return s.substring(0, s.length() - 2);
        }
        if (s.endsWith("s") && !s.endsWith("ss") && s.length() > 1) {
            return s.substring(0, s.length() - 1);
        }
        return s;
    }

    static String plural(String s) {
        if (s.endsWith("y") && s.length() > 1 && "bcdfgjklmnpqrstvwz".indexOf(s.charAt(s.length() - 2)) >= 0) {
            return s.substring(0, s.length() - 1) + "ies";
        }
        if (s.endsWith("s") || s.endsWith("x") || s.endsWith("ch") || s.endsWith("sh")) {
            return s + "es";
        }
        return s + "s";
    }
}
