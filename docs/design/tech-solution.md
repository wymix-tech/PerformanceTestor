# PerformanceTestor 技术方案

> 生成日期：2026-08-27　模块：全局　状态：已定稿

## 1. 技术栈

| 模块 | 技术 |
| ---- | ---- |
| 监控探针 perf-agent | Java 17 + ByteBuddy（Advice 内联）+ JMX + HTTP 批量上报 |
| 后端 perf-server | Spring Boot 3.x + MyBatis + MySQL 8 + Java HttpClient + OpenAI 兼容 API |
| 前端 perf-web | Vue3 + TypeScript + Vite + tdesign + ECharts |
| 示例应用 perf-demo | Spring Boot 3.x + MyBatis（含 N+1 等可分析问题代码） |
| 构建启动 | Maven 多模块 / npm，脚本支持单机演示与多机生产两种模式 |

## 2. 模块与目录结构

```
/workspace/                        # PerformanceTestor 项目根目录
├── perf-agent/                    # 监控探针（Java 17 + ByteBuddy）
├── perf-server/                   # 后端服务（Spring Boot 3.x + MyBatis）
├── perf-web/                      # 前端（Vue3 + TS + tdesign + ECharts）
├── perf-demo/                     # 示例 Spring Boot 应用（含 N+1 问题示例代码）
├── sql/init.sql                   # 全量建表脚本（分区表 + 新增五张表 + 演示数据）
├── sql/init_empty.sql              # 全量建表脚本（无演示数据，一键初始化空库）
├── scripts/                       # build.sh / start-all.sh(单机) / start-server.sh / start-agent.sh(多机) / link-codebuddy.sh
└── docs/                          # 中间产出物统一归档目录（全部纳入 git 管理）
    ├── README.md
    ├── design/                    # 设计文档/架构图/技术方案/数据库设计说明
    ├── reports/                   # 性能检测报告（压测报告/JVM 分析/容量评估）
    ├── issues/                    # 性能问题分析结果与修复提示词归档
    └── artifacts/                 # 其他中间产物（SQL 脚本导出、数据字典、联调记录等）
```

## 3. 关键设计决策

### 3.1 探针埋点
- ByteBuddy AgentBuilder + Advice 内联，拦截 `@Controller/@RestController/@Service/@Repository` 标注类方法；
- ThreadLocal 维护调用栈构建 TraceNode 树，记录每层耗时与占比；
- 采样率可配（默认 10%）；
- JDBC 拦截 Statement / PreparedStatement execute 系列，SQL 文本与耗时记录，超阈值（默认 500ms）标记慢 SQL。

### 3.2 数据上报（分离部署可靠性）
- 本地环形队列 + 单线程 flush（1s / 500 条）；
- 失败指数退避重试、超时丢弃；
- 上报地址为远程平台地址（http://platform-host:8080/api/agent/report），与平台断网不影响被测应用。

### 3.3 压测引擎（SPI）
```java
public interface StressEngine {
    String type();                       // "http" / "jmeter"
    StressTaskResult run(StressTaskRequest request);
}
```
- 默认 HttpStressEngine：有界线程池 + 周期采样，累加器 + 固定桶直方图，内存 O(1)；
- 预留 JMeter 适配器骨架（实现同接口即可接入）。

### 3.4 容量评估与扩容建议
- CapacityAnalyzer：读取压测多档并发采样（QPS、P95/P99 RT），QPS-RT 曲线分段线性插值，求目标 RT 阈值（默认 P95<200ms）下最大可承受并发与 QPS；
- ScaleAdviceEngine：规则引擎触发条件（CPU>80% 持续、RT P99 超阈值、QPS 接近容量上限、线程池活跃近满、Full GC 频繁、错误率超限），输出扩容方式（水平/垂直）与预估实例数；
- JvmConfigAdvisor：基于堆使用曲线、GC 次数与停顿，给出 -Xmx/-Xms/-Xss、GC 收集器（G1/ZGC）与关键参数建议；
- 大模型对规则结果做可解释性说明。

### 3.5 源码管理与问题分析
- SourceManager：zip 解压（防路径穿越：解压路径强制位于目标目录内）、git clone、本地目录导入；单文件 ≤1MB、总量 ≤200MB、文件数 ≤5000；
- IssueAssociator 三级定位：慢 SQL 模糊匹配 Mapper XML/注解定位 DAO；热点方法按类名/方法名精确匹配；压测瓶颈按 URL 关联 Controller 沿调用链定位 Service；
- PerformanceAnalyzer：组装上下文（检测证据 + 源码片段 + 表结构 + 链路占比）调用大模型，JSON Schema 约束输出 2-3 方案；
- FixPromptGenerator：将选定方案组装为 Markdown（目标文件、问题摘要、检测证据、源码引用、建议修改、验证方式、注意事项）。

### 3.6 AI 编排
- OpenAI 兼容 API Function Calling 多轮循环；
- 工具清单：`stress_test` / `jvm_metrics` / `trace` / `slow_sql` / `data_gen` / `analyze_code_issues` / `generate_fix_prompt` / `capacity_advice` / `generate_report`；
- 无 API Key 降级规则模板模式；
- 大模型请求信号量限流 + 超时重试。

### 3.7 存储设计
- JVM 指标与压测采样按天分区表；
- 调用链存 JSON 树；
- 容量评估结果存压测任务 JSON 字段；
- 新增 source_repo / source_file / performance_issue / fix_suggestion / db_conn 五张表（详见 database-design.md）。

### 3.8 数据库连接管理
- db_conn 表维护连接配置（JDBC URL / 用户名密码 / 绑定应用 / 默认连接），仅允许 jdbc:mysql://；
- 慢 SQL 分析时按 host / appName 匹配连接并试跑 EXPLAIN，给出执行计划证据；
- 灌数据任务复用连接配置，无需前端重复填写；
- agent 上报自动按 host / appName 关联匹配连接。

## 4. 关键代码结构

```java
// CapacityResult：容量评估结果，随压测报告落库并供 AI 工具返回
public class CapacityResult {
    private int maxConcurrency;        // 目标 RT 阈值下可承受最大并发
    private int maxQps;                // 对应最大 QPS
    private double targetP95Ms;        // 目标 P95 阈值（如 200）
    private List<Point> curve;         // 并发-QPS-RT 采样点
    private List<ScaleAdvice> advices; // 扩容建议（触发条件/方式/预估实例数）
    private JvmAdvice jvmAdvice;       // JVM 配置建议（堆/GC/参数）
}
```

```java
// AiTool：AI 工具统一签名，容量建议/分析/提示词工具按此注册
public interface AiTool {
    String name();                     // 如 capacity_advice / analyze_code_issues
    String description();
    String execute(String jsonArgs);   // 执行并返回 JSON 结果
}
```

## 5. 性能与可靠性

- 埋点统计与上报完全异步，业务线程零阻塞；上报重试退避 + 过期丢弃；
- 压测统计 O(1) 内存；容量插值基于采样点集合，计算 O(n)；
- 大模型请求信号量限流 + 超时重试；源码按需懒加载；
- 高风险操作（压测/灌数据/源码导入）统一参数校验：URL 白名单、并发/行数/文件大小上限。

## 6. 配置集中管理

- Agent 配置（上报地址/应用名/采样率/慢 SQL 阈值）经 `-javaagent` 参数或 `agent.properties` 传入，默认开箱即用；
- `application.yml` 集中配置平台地址、MySQL、AI、容量阈值；
- scripts 提供 build.sh / start-all.sh（单机演示）/ start-server.sh / start-agent.sh（多机分离部署）；
- 数据库初始化脚本幂等，含演示数据与示例问题记录。
