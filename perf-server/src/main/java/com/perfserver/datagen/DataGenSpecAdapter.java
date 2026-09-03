package com.perfserver.datagen;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.DataGenTask;
import com.perftestor.datagen.model.DataGenSpec;
import com.perftestor.datagen.model.FieldRule;
import com.perftestor.datagen.model.SourceFile;
import com.perftestor.datagen.model.TableSpec;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * DataGenRequest（宿主 HTTP 模型）→ DataGenSpec（模块引擎模型）适配。
 * 兼容旧单表形态（tables 为空时以 tableName/rowCount 构造单元素表组）。
 *
 * <p>关于 {@code connId}：本适配器只认 {@code dbUrl/dbUser/dbPassword}，<b>不解析 connId</b>——
 * 它是无状态组件，拿不到 {@code DbConnMapper} 与解密服务。connId → 真实凭据的换算
 * 统一由编排层（{@code DataGenService#runFromPlan}、{@code DataGenController#start}）在
 * 调用本适配器之前完成，避免同一份解析逻辑散落多处。</p>
 */
@Component
public class DataGenSpecAdapter {

    private final ObjectMapper objectMapper;

    public DataGenSpecAdapter(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    public DataGenSpec toSpec(DataGenRequest request, List<SourceFile> sourceFiles) throws Exception {
        DataGenSpec spec = new DataGenSpec();
        spec.setJdbcUrl(request.getDbUrl());
        spec.setDbUser(request.getDbUser());
        spec.setDbPassword(request.getDbPassword());
        spec.setOrphanRate(request.getOrphanRate());
        spec.setDryRun(request.isDryRun());
        spec.setSourceFiles(sourceFiles);

        List<TableSpec> tables = new ArrayList<>();
        if (request.getTables() != null && !request.getTables().isEmpty()) {
            for (DataGenRequest.TableSpecDto dto : request.getTables()) {
                TableSpec ts = new TableSpec(dto.getTableName(), dto.getRowCount());
                ts.setFields(parseFields(dto.getFields()));
                ts.setOrphanRate(dto.getOrphanRate());
                tables.add(ts);
            }
        } else {
            TableSpec ts = new TableSpec(request.getTableName(), request.getRowCount());
            ts.setFields(parseFieldsJson(request.getRuleJson()));
            tables.add(ts);
        }
        spec.setTables(tables);
        return spec;
    }

    /** 表组请求 JSON（供任务 tables_json 落库与前端回显） */
    public String toTablesJson(DataGenRequest request) throws Exception {
        if (request.getTables() == null || request.getTables().isEmpty()) {
            return null;
        }
        return objectMapper.writeValueAsString(request.getTables());
    }

    /** fields JSON（{"col":{rule}}）→ 规则 Map */
    private Map<String, FieldRule> parseFields(String fieldsJson) throws Exception {
        Map<String, FieldRule> out = new LinkedHashMap<>();
        if (fieldsJson == null || fieldsJson.isBlank()) {
            return out;
        }
        JsonNode root = objectMapper.readTree(fieldsJson);
        root.fields().forEachRemaining(e -> {
            FieldRule r = toRule(e.getValue());
            if (r != null) {
                out.put(e.getKey().toLowerCase(), r);
            }
        });
        return out;
    }

    /** 旧单表 ruleJson（{"fields":{...}}）→ 规则 Map */
    private Map<String, FieldRule> parseFieldsJson(String ruleJson) throws Exception {
        Map<String, FieldRule> out = new LinkedHashMap<>();
        if (ruleJson == null || ruleJson.isBlank()) {
            return out;
        }
        JsonNode fields = objectMapper.readTree(ruleJson).path("fields");
        if (fields.isObject()) {
            fields.fields().forEachRemaining(e -> {
                FieldRule r = toRule(e.getValue());
                if (r != null) {
                    out.put(e.getKey().toLowerCase(), r);
                }
            });
        }
        return out;
    }

    private FieldRule toRule(JsonNode node) {
        if (node == null || !node.isObject()) {
            return null;
        }
        FieldRule r = new FieldRule();
        r.setType(node.path("type").asText(""));
        JsonNode v = node.path("value");
        if (v.isNumber()) {
            r.setValue(v.numberValue());
        } else if (!v.isMissingNode() && !v.isNull()) {
            r.setValue(v.asText());
        }
        if (node.hasNonNull("min")) {
            r.setMin(node.path("min").asDouble());
        }
        if (node.hasNonNull("max")) {
            r.setMax(node.path("max").asDouble());
        }
        JsonNode values = node.path("values");
        if (values.isArray()) {
            List<Object> vals = new ArrayList<>();
            values.forEach(x -> vals.add(x.isNumber() ? x.numberValue() : x.asText()));
            r.setValues(vals);
        }
        JsonNode weights = node.path("weights");
        if (weights.isArray()) {
            List<Double> ws = new ArrayList<>();
            weights.forEach(x -> ws.add(x.asDouble()));
            r.setWeights(ws);
        }
        if (node.hasNonNull("table")) {
            r.setTable(node.path("table").asText());
        }
        if (node.hasNonNull("column")) {
            r.setColumn(node.path("column").asText());
        }
        if (node.hasNonNull("hotPercent")) {
            r.setHotPercent(node.path("hotPercent").asDouble());
        }
        if (node.hasNonNull("hotShare")) {
            r.setHotShare(node.path("hotShare").asDouble());
        }
        JsonNode ot = node.path("overThreshold");
        if (ot.isObject()) {
            FieldRule.OverThreshold t = new FieldRule.OverThreshold();
            if (ot.hasNonNull("op")) {
                t.setOp(ot.path("op").asText(">"));
            }
            t.setValue(ot.path("value").asDouble());
            t.setShare(ot.path("share").asDouble(30));
            r.setOverThreshold(t);
        }
        if (node.hasNonNull("seqPrefix")) {
            r.setSeqPrefix(node.path("seqPrefix").asText());
        }
        if (node.hasNonNull("seqStart")) {
            // 由 SeqStartResolver 在执行时刷新；方案 JSON 里不固化起始值
            r.setSeqStart(node.path("seqStart").asLong());
        }
        return r;
    }
}

