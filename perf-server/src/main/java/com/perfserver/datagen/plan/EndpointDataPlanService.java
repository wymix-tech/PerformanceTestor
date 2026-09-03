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
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

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

    /** 分析排队上限（超出即拒绝，避免请求无限堆积在内存里） */
    public static final int ANALYZE_QUEUE_CAPACITY = 16;

    /**
     * 分析线程池（单线程：分析为 CPU 密集，串行避免抢占）。
     *
     * <p>必须用有界队列 + 显式拒绝策略，不能用 {@code Executors.newSingleThreadExecutor()}——
     * 后者默认配 {@code LinkedBlockingQueue} 无界队列，任务积压时没有拒绝出口，
     * 请求会一直堆在内存里；有界队列下超额任务立即被 {@code AbortPolicy} 拒绝，
     * 由 {@link #analyze} 落告警并置 failed，用户能马上得到「排队已满」的反馈。</p>
     */
    private static final ExecutorService ANALYZER = new ThreadPoolExecutor(
            1, 1, 0L, TimeUnit.MILLISECONDS,
            new ArrayBlockingQueue<>(ANALYZE_QUEUE_CAPACITY),
            r -> {
                Thread t = new Thread(r, "datagen-plan-analyzer");
                t.setDaemon(true);
                return t;
            },
            new ThreadPoolExecutor.AbortPolicy());

    /** 方案列表默认条数 */
    public static final int DEFAULT_LIST_LIMIT = 50;

    /** 分析失败告警的固定前缀（前端据此高亮，与「降级告警」区分开） */
    public static final String ANALYZE_FAILED_PREFIX = "分析失败: ";

    /** 接口选择项（前端传入） */
    public static final class EndpointInput {
        private String httpMethod;
        private String path;
        private int weight = 1;
        /** 端点扫描产出的 Controller 名（simpleName），用于路径不匹配时的兜底匹配 */
        private String className;
        /** 端点扫描产出的方法名，用于路径不匹配时的兜底匹配 */
        private String methodName;

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

        public String getClassName() {
            return className;
        }

        public void setClassName(String className) {
            this.className = className;
        }

        public String getMethodName() {
            return methodName;
        }

        public void setMethodName(String methodName) {
            this.methodName = methodName;
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
        try {
            ANALYZER.submit(() -> {
                long start = System.currentTimeMillis();
                try {
                    List<String> warnings = new ArrayList<>();
                    List<PlanIntegrityChecker.PlanTable> tables = doAnalyze(request, warnings);
                    // 走专用 SQL 而非全字段 update：分析期间用户可以采纳/放弃方案，
                    // 全字段 update 会用分析启动时的旧 status 把用户的操作冲掉
                    planMapper.updateAnalysis(planId, toJson(tables), toJson(warnings));
                    log.info("plan analyzed: planId={}, repoId={}, endpoints={}, tables={}, elapsedMs={}",
                            planId, request.getRepoId(), request.getEndpoints().size(), tables.size(),
                            System.currentTimeMillis() - start);
                } catch (Exception e) {
                    log.error("plan analyze failed, planId={}", planId, e);
                    markAnalyzeFailed(planId, e.getMessage());
                }
            });
        } catch (RejectedExecutionException e) {
            // 排队已满：方案已落库但没有分析线程会再碰它，必须把结论写清楚，
            // 否则它会永远停在 pending，前端只能干等到轮询超时
            log.warn("plan analyze rejected, queue full, planId={}", planId);
            markAnalyzeFailed(planId, "分析排队已满（上限 " + ANALYZE_QUEUE_CAPACITY + "），请稍后重试");
        }
        return plan;
    }

    /**
     * 分析失败落库：置 failed 并写原因（与执行期异常共用）。
     *
     * <p>同样走专用 SQL 而非全字段 update，理由见 {@code updateAnalysis}。</p>
     */
    private void markAnalyzeFailed(long planId, String reason) {
        planMapper.updateWarnings(planId, toJson(List.of(ANALYZE_FAILED_PREFIX + reason)));
        // 必须置 failed：只写 warnings 而状态仍为 pending 时，前端无法区分
        // 「排队/分析中」与「分析失败」，只能干等到轮询超时
        planMapper.updateStatus(planId, DataGenPlan.ST_FAILED);
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

        int endpointIndex = 0;
        for (EndpointInput input : request.getEndpoints()) {
            EndpointRef matched = match(index, input, warnings);
            if (matched == null) {
                warnings.add("接口未定位: " + input.getHttpMethod() + " " + input.getPath()
                        + "（可手工补表后执行）");
                // 必须同时占位：weights 是「按接口下标的紧凑列表」，
                // 未定位的接口若只跳过不占位，后续接口的 demand 会取到前一个接口的权重
                weights.add(input.getWeight());
                endpointIndex++;
                continue;
            }
            CallGraphAnalyzer.Result graph = analyzer.analyze(javaFiles, matched);
            graph.getUnresolved().forEach(r -> warnings.add("调用链未覆盖: " + r));
            if (graph.isTripped()) {
                warnings.add("调用链分析触发熔断，涉及表可能不完整");
            }
            weights.add(input.getWeight());
            collectFacts(graph, sqlResolver, factsByTable, demands, warnings, graph.isTripped(),
                    endpointIndex, isDetailEndpoint(input.getPath()));
            endpointIndex++;
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
        } else {
            // 外键命名兜底：需全部事实（列名与表清单）齐备后才能做
            resolveFkByNaming(factsByTable);
        }

        // 规则合成 + 父表补齐（必须在构建 spec 之前，否则补了也不生效）
        Map<String, PlanRuleComposer.TableContext> contexts = new LinkedHashMap<>();
        for (Map.Entry<String, EndpointTableFact> e : factsByTable.entrySet()) {
            PlanRuleComposer.TableContext ctx = new PlanRuleComposer.TableContext();
            ctx.setDetailLookup(e.getValue().isDetailLookup());
            ctx.getJoinRefs().addAll(e.getValue().getJoinRefs());
            contexts.put(e.getKey(), ctx);
        }
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

    /** 详情类接口判定：路径末段为路径变量（如 /api/order/{id}） */
    static boolean isDetailEndpoint(String path) {
        if (path == null || path.isBlank()) {
            return false;
        }
        String[] segs = path.split("/");
        String last = segs.length == 0 ? "" : segs[segs.length - 1].trim();
        return last.startsWith("{") && last.endsWith("}");
    }

    /** 调用链 → SQL → 访问事实 */
    private void collectFacts(CallGraphAnalyzer.Result graph, MyBatisSqlResolver sqlResolver,
                              Map<String, EndpointTableFact> factsByTable,
                              List<RowCountEstimator.TableDemand> demands,
                              List<String> warnings, boolean tripped,
                              int endpointIndex, boolean detail) {
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
            Map<String, java.util.Set<String>> columnsByTable = SqlTableExtractor.extractColumnsByTable(sql);
            List<SqlTableExtractor.JoinEdge> joinEdges = SqlTableExtractor.extractJoinEdges(sql);

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
                // 详情类接口：该表按 id 反复查单行，生成的数据应做热点倾斜
                if (detail) {
                    fact.setDetailLookup(true);
                }
                // 累积列名：N+1 形态下外键列可能只在单条 SQL 里出现，需跨 SQL 汇总后再判外键
                java.util.Set<String> cols = columnsByTable.get(ta.getTable());
                if (cols != null) {
                    for (String col : cols) {
                        if (!fact.getColumns().contains(col)) {
                            fact.getColumns().add(col);
                        }
                    }
                }
                collectJoinRefs(fact, ta.getTable(), joinEdges);
                demands.add(new RowCountEstimator.TableDemand(ta.getTable(),
                        ta.getAccess() == EndpointTableFact.SqlAccess.WRITE,
                        extracted.getPageSize(), endpointIndex));
            }
        }
    }

    /**
     * 收集外键引用边（驱动 ref/refSkew 规则，并让父表补齐逻辑有据可依）。
     *
     * <p>只处理 <b>JOIN ON</b> 这类方向明确的证据（置信度 0.9/0.7）。
     * 命名猜测（{@code user_id → perf_user}，覆盖 N+1 这类无 JOIN 的写法）需要跨 SQL 的
     * 列名与全量表清单，放在 {@link #resolveFkByNaming} 里、在所有事实收集完成后统一做。</p>
     */
    private void collectJoinRefs(EndpointTableFact fact, String table,
                                 List<SqlTableExtractor.JoinEdge> joinEdges) {
        for (SqlTableExtractor.JoinEdge edge : joinEdges) {
            if (!table.equals(edge.getChildTable())) {
                continue;
            }
            fact.getJoinRefs().add(new EndpointTableFact.JoinRef(
                    edge.getChildColumn(), edge.getParentTable(), edge.getParentColumn(),
                    edge.getConfidence()));
        }
    }

    /**
     * 外键命名兜底：本表出现形如 {@code xxx_id} 的列，且 {@code xxx} 对应表也在方案里时，
     * 生成一条 ref 边（置信度 0.6）。覆盖 N+1 这类「主表与子表在不同 SQL 中」的写法。
     *
     * <p>仅在没有任何 JOIN ON 证据时才做，避免低置信结论覆盖高置信结论。</p>
     */
    private void resolveFkByNaming(Map<String, EndpointTableFact> factsByTable) {
        for (Map.Entry<String, EndpointTableFact> e : factsByTable.entrySet()) {
            EndpointTableFact fact = e.getValue();
            if (!fact.getJoinRefs().isEmpty() || fact.getColumns().isEmpty()) {
                continue;
            }
            for (Map.Entry<String, EndpointTableFact> other : factsByTable.entrySet()) {
                if (other.getKey().equals(e.getKey())) {
                    continue;
                }
                for (String col : fact.getColumns()) {
                    if (SqlTableExtractor.isFkColumn(col, other.getKey())) {
                        fact.getJoinRefs().add(new EndpointTableFact.JoinRef(col, other.getKey(), "id", 0.6));
                        break;
                    }
                }
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
     * 接口匹配，三级降级：
     * <ol>
     *   <li>{@code (httpMethod, path)} 精确</li>
     *   <li>忽略 HTTP 方法，仅按 {@code path}</li>
     *   <li>{@code classFile#methodName}——前端可带 {@code className}/{@code methodName}
     *       （来自端点扫描结果），用于路径无法匹配（路径变量差异、多值 path）时的兜底</li>
     * </ol>
     *
     * <p>降级到第②③层时记录命中层级，便于定位归一化问题。</p>
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
        // ③ 兜底：按 类名#方法名 匹配（路径变量差异、多值 path 等场景）
        if (notBlank(input.getClassName()) && notBlank(input.getMethodName())) {
            String wantMember = input.getClassName() + "#" + input.getMethodName();
            for (EndpointRef r : index) {
                if (wantMember.equals(r.classMemberKey())) {
                    warnings.add("接口按 类#方法 匹配（路径不一致）: " + wantMember);
                    return r;
                }
            }
        }
        return null;
    }

    private static boolean notBlank(String s) {
        return s != null && !s.isBlank();
    }

    /**
     * 加载分析用源码。
     *
     * <p>按接口分析必须走 {@link SourceFileProvider#ANALYSIS_LIMIT} 而非灌数扫描的 200 上限——
     * 调用链要跨文件解析类型，抽样上限在大仓库上会直接漏掉整条链路。
     * 加载被截断时显式告警，避免「分析不出表」被误判为「接口不查库」。</p>
     */
    private List<SourceFile> loadSources(Long repoId, List<String> warnings) {
        List<SourceFile> files = new ArrayList<>(
                sourceFileProvider.loadFiles(repoId, SourceFileProvider.ANALYSIS_LIMIT));
        if (files.isEmpty()) {
            warnings.add("源码不可读，无法按接口推导涉及表（请确认仓库已导入且磁盘路径可访问）");
            return files;
        }
        int indexed = sourceFileProvider.countIndexed(repoId);
        if (indexed > files.size()) {
            warnings.add(String.format(Locale.ROOT,
                    "源码文件 %d 个超过分析上限 %d 个，仅分析了其中 %d 个，涉及表可能不完整",
                    indexed, SourceFileProvider.ANALYSIS_LIMIT, files.size()));
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
                // 必须用 asText()：TextNode.toString() 会返回带引号转义的 JSON 字面量，
                // 下游 readTree 会解析成 TextNode 而非 ObjectNode，导致规则被静默丢弃
                JsonNode fieldsNode = n.get("fields");
                if (fieldsNode != null && !fieldsNode.isNull()) {
                    t.setFields(fieldsNode.isTextual() ? fieldsNode.asText() : fieldsNode.toString());
                }
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
