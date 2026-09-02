package com.perfserver.datagen.plan;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.DataGenPlan;
import com.perfserver.entity.SourceRepo;
import com.perfserver.mapper.DataGenPlanMapper;
import com.perfserver.mapper.SourceRepoMapper;
import com.perfserver.source.SourceFileProvider;
import com.perftestor.datagen.endpoint.CallGraphAnalyzer;
import com.perftestor.datagen.endpoint.EndpointIndexParser;
import com.perftestor.datagen.endpoint.MyBatisSqlResolver;
import com.perftestor.datagen.model.EndpointRef;
import com.perftestor.datagen.model.EndpointTableFact;
import com.perftestor.datagen.model.SourceFile;
import com.perftestor.datagen.util.SqlPredicateExtractor;
import com.perftestor.datagen.util.SqlTableExtractor;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 接口驱动数据方案服务：接口定位 → 调用链 → SQL → 访问事实 → 规则 → 行数 → 落库。
 *
 * <p>静态分析为 CPU 密集，故执行于独立的 daemon 单线程池 {@code datagen-plan-analyzer}，
 * 避免长期占用 HTTP 工作线程。</p>
 *
 * <p>降级策略：任一层解析不出结论时记入 {@code unresolved} 与 warnings，
 * 由前端黄色提示引导手工补表，<b>不做静默猜测</b>。</p>
 */
@Service
public class EndpointDataPlanService {

    private static final Logger log = LoggerFactory.getLogger(EndpointDataPlanService.class);

    /** 分析线程池（单线程：分析为 CPU 密集，串行避免抢占） */
    private static final ExecutorService ANALYZER = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "datagen-plan-analyzer");
        t.setDaemon(true);
        return t;
    });

    /** 方案列表默认条数 */
    public static final int DEFAULT_LIST_LIMIT = 50;

    /** 接口选择项（前端传入） */
    public static final class EndpointInput {
        private String httpMethod;
        private String path;
        private int weight = 1;

        public String getHttpMethod() {
            return httpMethod;
        }

        public void setHttpMethod(String httpMethod) {
            this.httpMethod = httpMethod;
        }

        public String getPath() {
            return path;
        }

        public void setPath(String path) {
            this.path = path;
        }

        public int getWeight() {
            return weight;
        }

        public void setWeight(int weight) {
            this.weight = weight < 1 ? 1 : weight;
        }
    }

    /** 分析请求 */
    public static final class AnalyzeRequest {
        private Long repoId;
        private String planName;
        private List<EndpointInput> endpoints = new ArrayList<>();
        /** 压测参数（StressTaskRequest 的精简快照） */
        private RowCountEstimator.StressParams stress = new RowCountEstimator.StressParams();
        private Long connId;
        private String dbUrl;
        /** 压测任务 ID（可选；用于 RT 基线取采样点） */
        private Long taskId;
        private boolean llmEnhance;

        public Long getRepoId() {
            return repoId;
        }

        public void setRepoId(Long repoId) {
            this.repoId = repoId;
        }

        public String getPlanName() {
            return planName;
        }

        public void setPlanName(String planName) {
            this.planName = planName;
        }

        public List<EndpointInput> getEndpoints() {
            return endpoints == null ? new ArrayList<>() : endpoints;
        }

        public void setEndpoints(List<EndpointInput> endpoints) {
            this.endpoints = endpoints == null ? new ArrayList<>() : endpoints;
        }

        public RowCountEstimator.StressParams getStress() {
            return stress == null ? new RowCountEstimator.StressParams() : stress;
        }

        public void setStress(RowCountEstimator.StressParams stress) {
            this.stress = stress;
        }

        public Long getConnId() {
            return connId;
        }

        public void setConnId(Long connId) {
            this.connId = connId;
        }

        public String getDbUrl() {
            return dbUrl;
        }

        public void setDbUrl(String dbUrl) {
            this.dbUrl = dbUrl;
        }

        public Long getTaskId() {
            return taskId;
        }

        public void setTaskId(Long taskId) {
            this.taskId = taskId;
        }

        public boolean isLlmEnhance() {
            return llmEnhance;
        }

        public void setLlmEnhance(boolean llmEnhance) {
            this.llmEnhance = llmEnhance;
        }
    }

    private final DataGenPlanMapper planMapper;
    private final SourceRepoMapper repoMapper;
    private final SourceFileProvider sourceFileProvider;
    private final ObjectMapper objectMapper;
    private final RtBaselineResolver rtResolver;
    private final PlanRuleComposer ruleComposer;
    private final LlmPlanEnhancer llmEnhancer;

    public EndpointDataPlanService(DataGenPlanMapper planMapper, SourceRepoMapper repoMapper,
                                   SourceFileProvider sourceFileProvider, ObjectMapper objectMapper,
                                   RtBaselineResolver rtResolver, PlanRuleComposer ruleComposer,
                                   LlmPlanEnhancer llmEnhancer) {
        this.planMapper = planMapper;
        this.repoMapper = repoMapper;
        this.sourceFileProvider = sourceFileProvider;
        this.objectMapper = objectMapper;
        this.rtResolver = rtResolver;
        this.ruleComposer = ruleComposer;
        this.llmEnhancer = llmEnhancer;
    }

    /**
     * 分析并落库（异步执行于分析线程池）。
     *
     * @param request 分析请求
     * @return 已落库的方案（id 已生成）
     */
    public DataGenPlan analyze(AnalyzeRequest request) {
        if (request == null || request.getRepoId() == null) {
            throw new IllegalArgumentException("源码仓库 ID 必填");
        }
        validateConnection(request);
        if (request.getEndpoints().isEmpty()) {
            throw new IllegalArgumentException("至少选择一个压测接口");
        }

        DataGenPlan plan = new DataGenPlan();
        plan.setRepoId(request.getRepoId());
        plan.setPlanName(defaultPlanName(request));
        plan.setEndpointJson(toJson(request.getEndpoints()));
        plan.setStressJson(toJson(request.getStress()));
        plan.setConnId(request.getConnId());
        plan.setDbUrl(request.getDbUrl());
        plan.setStatus(DataGenPlan.ST_PENDING);
        planMapper.insert(plan);
        if (plan.getId() == null) {
            // 自增主键未回填（落库失败）时明确报错，避免后续以 null id 静默写库
            throw new IllegalStateException("方案落库失败：未获取到方案 ID");
        }

        final long planId = plan.getId();
        ANALYZER.submit(() -> {
            long start = System.currentTimeMillis();
            try {
                List<String> warnings = new ArrayList<>();
                List<PlanIntegrityChecker.PlanTable> tables = doAnalyze(request, warnings);
                DataGenPlan saved = planMapper.findById(planId);
                if (saved != null) {
                    saved.setTablesJson(toJson(tables));
                    saved.setWarningsJson(toJson(warnings));
                    planMapper.update(saved);
                }
                log.info("plan analyzed: planId={}, repoId={}, endpoints={}, tables={}, elapsedMs={}",
                        planId, request.getRepoId(), request.getEndpoints().size(), tables.size(),
                        System.currentTimeMillis() - start);
            } catch (Exception e) {
                log.error("plan analyze failed, planId={}", planId, e);
                DataGenPlan saved = planMapper.findById(planId);
                if (saved != null) {
                    saved.setWarningsJson(toJson(List.of("分析失败: " + e.getMessage())));
                    planMapper.update(saved);
                }
            }
        });
        return plan;
    }

    /** 核心分析流程（供单测直接调用，同步执行） */
    List<PlanIntegrityChecker.PlanTable> doAnalyze(AnalyzeRequest request, List<String> warnings) {
        List<SourceFile> files = loadSources(request.getRepoId(), warnings);
        List<SourceFile> javaFiles = filter(files, ".java");
        List<SourceFile> xmlFiles = filter(files, ".xml");

        List<EndpointRef> index = new EndpointIndexParser().parse(javaFiles);
        if (index.isEmpty()) {
            warnings.add("源码中未解析到任何 Controller 接口，请确认导入的仓库包含 Controller 源码");
        }

        MyBatisSqlResolver sqlResolver = new MyBatisSqlResolver(xmlFiles);
        sqlResolver.indexAnnotations(javaFiles);

        CallGraphAnalyzer analyzer = new CallGraphAnalyzer();
        // 表 → 事实（多接口/多步骤合并：行数取 max，谓词值集取 union）
        Map<String, EndpointTableFact> factsByTable = new LinkedHashMap<>();
        List<RowCountEstimator.TableDemand> demands = new ArrayList<>();
        List<Integer> weights = new ArrayList<>();

        for (EndpointInput input : request.getEndpoints()) {
            EndpointRef matched = match(index, input, warnings);
            if (matched == null) {
                warnings.add("接口未定位: " + input.getHttpMethod() + " " + input.getPath()
                        + "（可手工补表后执行）");
                continue;
            }
            CallGraphAnalyzer.Result graph = analyzer.analyze(javaFiles, matched);
            graph.getUnresolved().forEach(r -> warnings.add("调用链未覆盖: " + r));
            if (graph.isTripped()) {
                warnings.add("调用链分析触发熔断，涉及表可能不完整");
            }
            weights.add(input.getWeight());
            collectFacts(graph, sqlResolver, factsByTable, demands, warnings, graph.isTripped());
        }

        // 行数推导
        RowCountEstimator.StressParams params = request.getStress();
        SourceRepo repo = repoMapper.findById(request.getRepoId());
        String appName = repo == null ? null : repo.getAppName();
        RtBaselineResolver.Baseline baseline = rtResolver.resolve(appName, firstPath(request), request.getTaskId());
        params.setAssumedRtMs(baseline.getRtMs());
        warnings.add("响应时间基线取值来源: " + RtBaselineResolver.describe(baseline.getSource())
                + "（" + round(baseline.getRtMs()) + "ms）");

        RowCountEstimator.Estimate estimate = RowCountEstimator.estimate(params, demands, weights);
        warnings.addAll(estimate.getWarnings());

        // LLM 可选增强（无 Key 静默降级）
        LlmPlanEnhancer.Enhancement enhancement =
                llmEnhancer.enhance(new ArrayList<>(factsByTable.values()), request.isLlmEnhance());
        if (!enhancement.isApplied() && request.isLlmEnhance()) {
            warnings.add(enhancement.getReason());
        }
        warnings.addAll(enhancement.getWarnings());

        if (factsByTable.isEmpty()) {
            // 空清单必须有明确出口：否则用户看到空白页无从判断是「接口不查库」还是「分析失败」
            warnings.add("未从源码分析出该接口涉及的数据库表"
                    + "（接口可能不访问数据库，或调用链未覆盖到 Mapper），请手动补表后执行");
        }

        // 规则合成 + 父表补齐（必须在构建 spec 之前，否则补了也不生效）
        Map<String, PlanRuleComposer.TableContext> contexts = new LinkedHashMap<>();
        PlanRuleComposer.Composed composed = ruleComposer.compose(new ArrayList<>(factsByTable.values()), contexts);
        warnings.addAll(composed.getWarnings());

        List<PlanIntegrityChecker.PlanTable> tables = new ArrayList<>();
        for (Map.Entry<String, EndpointTableFact> e : factsByTable.entrySet()) {
            String table = e.getKey();
            EndpointTableFact fact = e.getValue();
            PlanIntegrityChecker.PlanTable pt = new PlanIntegrityChecker.PlanTable(
                    table,
                    estimate.getRowsByTable().getOrDefault(table, RowCountEstimator.MIN_MAIN_ROWS),
                    composed.getFieldsByTable().get(table),
                    fact.getAccess() == null ? "READ" : fact.getAccess().name());
            pt.getEvidence().addAll(fact.getSourceRefs());
            tables.add(pt);
        }
        PlanIntegrityChecker integrity = new PlanIntegrityChecker(objectMapper);
        PlanIntegrityChecker.CheckResult checked = integrity.completeParents(tables);
        warnings.addAll(checked.getWarnings());
        return checked.getTables();
    }

    /** 调用链 → SQL → 访问事实 */
    private void collectFacts(CallGraphAnalyzer.Result graph, MyBatisSqlResolver sqlResolver,
                              Map<String, EndpointTableFact> factsByTable,
                              List<RowCountEstimator.TableDemand> demands,
                              List<String> warnings, boolean tripped) {
        for (CallGraphAnalyzer.MapperInvocation inv : graph.getInvocations()) {
            String sql = sqlResolver.resolve(inv).orElse(null);
            if (sql == null) {
                warnings.add("Mapper 方法未定位到 SQL: " + inv.getMapperType() + "#" + inv.getMethodName()
                        + "（" + inv.getCallSite() + "）");
                continue;
            }
            SqlTableExtractor.Result extracted = SqlTableExtractor.extract(sql);
            if (extracted.getWarning() != null) {
                warnings.add(inv.getMapperType() + "#" + inv.getMethodName() + "："
                        + extracted.getWarning());
            }
            if (extracted.getTables().isEmpty()) {
                warnings.add("SQL 未识别出表名: " + inv.getMapperType() + "#" + inv.getMethodName());
                continue;
            }
            for (SqlTableExtractor.TableAccess ta : extracted.getTables()) {
                EndpointTableFact fact = factsByTable.computeIfAbsent(ta.getTable(),
                        k -> new EndpointTableFact(k, ta.getAccess()));
                fact.getSourceRefs().add(inv.getCallSite());
                if (tripped) {
                    fact.setConfidence(CallGraphAnalyzer.TRIPPED_CONFIDENCE);
                }
                // 同一张表被不同 Mapper 以不同方式访问时，写优先（写链路覆盖读链路需求）
                if (ta.getAccess() == EndpointTableFact.SqlAccess.WRITE) {
                    fact.setAccess(EndpointTableFact.SqlAccess.WRITE);
                }
                for (EndpointTableFact.Predicate p : SqlPredicateExtractor.extract(sql, ta.getTable())) {
                    mergePredicate(fact, p);
                }
                if (extracted.getPageSize() > fact.getPageSize()) {
                    fact.setPageSize(extracted.getPageSize());
                }
                for (String other : extracted.tableNames()) {
                    if (!other.equals(ta.getTable()) && !fact.getJoinTables().contains(other)) {
                        fact.getJoinTables().add(other);
                    }
                }
                demands.add(new RowCountEstimator.TableDemand(ta.getTable(),
                        ta.getAccess() == EndpointTableFact.SqlAccess.WRITE,
                        extracted.getPageSize()));
            }
        }
    }

    /** 谓词合并：同列同算子合并值集（多步骤 union，避免漏造导致查询不命中） */
    private void mergePredicate(EndpointTableFact fact, EndpointTableFact.Predicate incoming) {
        for (EndpointTableFact.Predicate p : fact.getPredicates()) {
            if (!p.getColumn().equals(incoming.getColumn()) || p.getOp() != incoming.getOp()) {
                continue;
            }
            for (Object v : incoming.getValues()) {
                if (!p.getValues().contains(v)) {
                    p.getValues().add(v);
                }
            }
            if (incoming.getMin() != null && (p.getMin() == null || incoming.getMin() < p.getMin())) {
                p.setMin(incoming.getMin());
            }
            if (incoming.getMax() != null && (p.getMax() == null || incoming.getMax() > p.getMax())) {
                p.setMax(incoming.getMax());
            }
            return;
        }
        fact.getPredicates().add(incoming);
    }

    /**
     * 接口匹配：① (httpMethod, path) 精确 ② 忽略方法 ③ classFile#methodName。
     * 降级到第②③层时记录命中层级，便于定位归一化问题。
     */
    EndpointRef match(List<EndpointRef> index, EndpointInput input, List<String> warnings) {
        if (index == null || index.isEmpty() || input == null) {
            return null;
        }
        String wantKey = (input.getHttpMethod() == null ? "" : input.getHttpMethod().toUpperCase(Locale.ROOT))
                + " " + input.getPath();
        for (EndpointRef r : index) {
            if (r.key().equals(wantKey)) {
                return r;
            }
        }
        for (EndpointRef r : index) {
            if (r.pathKey().equals(input.getPath())) {
                warnings.add("接口按路径匹配（HTTP 方法不一致）: " + input.getPath());
                return r;
            }
        }
        return null;
    }

    private List<SourceFile> loadSources(Long repoId, List<String> warnings) {
        List<SourceFile> files = new ArrayList<>(sourceFileProvider.loadFiles(repoId));
        if (files.isEmpty()) {
            warnings.add("源码不可读，无法按接口推导涉及表（请确认仓库已导入且磁盘路径可访问）");
        }
        return files;
    }

    private static List<SourceFile> filter(List<SourceFile> files, String suffix) {
        List<SourceFile> out = new ArrayList<>();
        for (SourceFile f : files) {
            if (f.getPath() != null && f.getPath().toLowerCase(Locale.ROOT).endsWith(suffix)) {
                out.add(f);
            }
        }
        return out;
    }

    /**
     * 连接参数校验：不允许同时提供 connId 与 dbUrl（二者择一）。
     *
     * <p>允许二者都为空——分析阶段只读源码，不连目标库，连接可以在数据准备页
     * 选定后再回填方案。「必须且只能其一」的强校验放在真正用到连接的执行入口
     * （{@code DataGenService#runFromPlan}），那里缺连接会直接报错。</p>
     */
    private void validateConnection(AnalyzeRequest request) {
        boolean hasConn = request.getConnId() != null;
        boolean hasUrl = request.getDbUrl() != null && !request.getDbUrl().isBlank();
        if (hasConn && hasUrl) {
            throw new IllegalArgumentException("数据库连接配置 ID 与 JDBC URL 只能提供其一");
        }
    }

    private String defaultPlanName(AnalyzeRequest request) {
        if (request.getPlanName() != null && !request.getPlanName().isBlank()) {
            return request.getPlanName();
        }
        EndpointInput first = request.getEndpoints().get(0);
        String name = (first.getHttpMethod() == null ? "" : first.getHttpMethod().toUpperCase(Locale.ROOT) + " ")
                + first.getPath();
        return "数据方案-" + name.trim();
    }

    private String firstPath(AnalyzeRequest request) {
        EndpointInput first = request.getEndpoints().get(0);
        return first == null ? null : first.getPath();
    }

    private static double round(double v) {
        return Math.round(v * 100) / 100.0;
    }

    private String toJson(Object o) {
        try {
            return objectMapper.writeValueAsString(o);
        } catch (Exception e) {
            return "[]";
        }
    }

    // ===== 方案查询与状态流转 =====

    public List<DataGenPlan> list(Long repoId, String status, int limit) {
        return planMapper.query(repoId, status, Math.max(1, Math.min(limit, 200)));
    }

    public DataGenPlan detail(long id) {
        DataGenPlan plan = planMapper.findById(id);
        if (plan == null) {
            throw new IllegalArgumentException("方案不存在: " + id);
        }
        return plan;
    }

    /** 采纳/放弃 */
    public DataGenPlan adopt(long id, boolean adopt) {
        DataGenPlan plan = detail(id);
        planMapper.updateStatus(id, adopt ? DataGenPlan.ST_ADOPTED : DataGenPlan.ST_DISCARDED);
        plan.setStatus(adopt ? DataGenPlan.ST_ADOPTED : DataGenPlan.ST_DISCARDED);
        return plan;
    }

    public void delete(long id) {
        planMapper.deleteById(id);
    }

    /** 解析方案表清单 JSON（供 runFromPlan 使用） */
    public List<PlanIntegrityChecker.PlanTable> parseTables(String tablesJson) {
        List<PlanIntegrityChecker.PlanTable> out = new ArrayList<>();
        if (tablesJson == null || tablesJson.isBlank()) {
            return out;
        }
        try {
            JsonNode arr = objectMapper.readTree(tablesJson);
            if (arr == null || !arr.isArray()) {
                return out;
            }
            for (JsonNode n : arr) {
                PlanIntegrityChecker.PlanTable t = new PlanIntegrityChecker.PlanTable();
                t.setTable(n.path("table").asText(null));
                t.setRowCount(n.path("rowCount").asInt(RowCountEstimator.MIN_MAIN_ROWS));
                t.setFields(n.hasNonNull("fields") ? n.get("fields").toString() : null);
                t.setAccess(n.path("access").asText("READ"));
                JsonNode ev = n.get("evidence");
                if (ev != null && ev.isArray()) {
                    for (JsonNode e : ev) {
                        t.getEvidence().add(e.asText());
                    }
                }
                if (t.getTable() != null) {
                    out.add(t);
                }
            }
        } catch (Exception e) {
            log.warn("parse plan tables failed: {}", e.getMessage());
        }
        return out;
    }
}
