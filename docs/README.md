# PerformanceTestor — 性能检测智能体平台

面向 Java 应用的**性能检测智能体平台**：平台（perf-server、MySQL、源码存储）与被测应用**分离部署**在不同服务器，避免压测与监控互相干扰。通过 Java Agent 无侵入采集 JVM 指标、业务方法调用链耗时与慢 SQL；提供自研压测引擎与自动灌数据能力；支持 AI 自然语言对话自动执行检测任务；采集项目源代码，将检测结果关联到源码位置，由大模型分析根因并给出多种修改方案，选定方案后生成修复提示词供 AI 辅助编码工具直接修复。

## 模块总览

```
/workspace/
├── perf-agent/     # 监控探针（Java 17 + ByteBuddy）：方法埋点 / JDBC 拦截 / JMX 采集 / HTTP 批量上报
├── perf-datagen/   # Mock 数据生成引擎（独立纯 Java 库，SPI 可插拔）：联表外键感知 / 无外键语义关联识别 /
│                   #   枚举语义化 / 魔法值感知 / Dry-Run 试运行；MySQL + PostgreSQL 双方言可用，
│                   #   ORM 侧实现 MyBatis，多 ORM 预留扩展点
├── perf-server/    # 后端平台（Spring Boot 3.x + MyBatis + MySQL）：采集存储 / 压测引擎 / 灌数据 /
│                   #   容量评估 / 扩容建议 / 数据库连接管理 / 源码管理 / AI Function Calling 编排
├── perf-web/       # 前端（Vue3 + TS + Vite + ECharts）：七屏可视化
├── perf-demo/      # 示例被测应用（Spring Boot 3.x + MyBatis）：含 N+1 / 慢 SQL 等可分析问题代码
├── sql/init.sql    # 平台库全量建表脚本（幂等，含演示数据）
├── sql/init_empty.sql # 平台库全量建表脚本（幂等，无演示数据，用于一键初始化空库）
├── scripts/        # build.sh / start-all.sh(单机) / start-server.sh / start-agent.sh(多机) /
│                   #   link-codebuddy.sh / verify-datagen-mysql.sh / verify-datagen-postgres.sh
│                   #   (灌数引擎端到端验收) / perf-gate.sh(性能回归门禁 CI 调用)
└── docs/           # 本文档 + design / reports / issues / artifacts / plans 归档
```

## 快速开始（单机演示模式）

前置条件：JDK 17+、Maven、Node 18+、MySQL 8（root/root123，可改环境变量）。

```bash
# 1. 全量构建（后端 + 前端）
cd /workspace && scripts/build.sh

# 2. 一键启动（MySQL + perf-server + perf-demo(挂 agent) + perf-web）
scripts/start-all.sh
#    Web 后台: http://localhost:5173
#    被测应用: http://localhost:8081/api/user/list?limit=200   （N+1 热点接口）

# 3. 也可以在已有 MySQL 上手动启动：
#    mvn -q -pl perf-server,perf-demo package -DskipTests
#    java -jar perf-server/target/perf-server.jar &
#    java -javaagent:perf-agent/target/perf-agent.jar=reportUrl=http://127.0.0.1:8080/api/agent/report,appName=perf-demo,sampleRate=1.0 \
#         -jar perf-demo/target/perf-demo.jar &
#    (cd perf-web && npm run dev)
```

单机模式下平台库 `performance_testor` 与被测库 `demo` 隔离在同一 MySQL 实例，仅用于演示。

## 多机分离部署

平台服务器与被测服务器分离，仅需网络白名单互通两处：**平台 → 被测**（发起压测、灌数据），**被测 → 平台**（agent 上报）。

> agent 上报时自动注册应用实例（来源 IP + `appPort` 配置的端口 + 应用版本号）。压测中心选定项目后，**服务地址自动预填** `http://{appHost}:{appPort}`（用户手动修改过后不再覆盖），配合接口扫描实现"选项目 → 选接口 → 直接压测"。
>
> **版本号来源优先级**：agent 参数显式配置 `appVersion=x.y.z` > 自动读被测 jar `META-INF/MANIFEST.MF` 的 `Implementation-Version`（被测应用 pom 给 maven-jar-plugin 加 `<addDefaultImplementationEntries>true</addDefaultImplementationEntries>` 即可写入）> 空（平台保留已有值）。版本随 agent 上报维护，创建压测任务时自动快照到任务，报告页支持不同版本间的性能对比。
>
> **探针低开销设计**（对被测应用影响最小化）：
> - 埋点范围为**业务包全量**：显式 `includePackages` 配置 > 被测 jar `Start-Class`/`Main-Class` 包名自动推导（推荐零配置）> Spring 注解兜底；覆盖 Controller/Service/Repository/Component/**工具类/静态方法**等全部业务代码，天然排除 JDK 与第三方依赖；
> - **POJO / 数据类不埋点**：entity/dto/vo/model 等包名启发 + 结构判断（全部方法均为 getter/setter/equals 等数据风格）+ 枚举/record 三层排除；
> - 方法级防噪音：构造器/Object 基类方法/getter/setter/桥接合成/main 不埋；
> - 业务线程零转换：调用链以原生树 O(1) 入队，树转 Map/JSON 序列化全部在 reporter 线程完成；
> - 单 ThreadLocal 上下文：方法热路径仅 1 次 ThreadLocal.get；未采样链路零分配；
> - 采样率（比例）+ 每秒令牌桶（绝对量，默认 50/s，`maxTracesPerSec` 可调）双重控制，洪峰下 trace 量与业务流量解耦；
> - trace 队列条数 + 字节预算（默认 8MB，`traceBufferBytes` 可调，下限 1MB）双重限制防大树内存洪水；
> - JDBC 惰性 SQL：enter 仅取参数引用，超阈值后才做 toString 兜底与截断；
> - 每队列独立锁；平台不可达指数退避至 60s，队列满丢最旧并计数，`dropped` 随下次上报带出（平台记录日志）。

```
[平台服务器]  perf-server:8080 + MySQL + 源码存储 + OpenAI 兼容 API
    ↑ agent 上报（被测→平台）        ↓ 压测/灌数据（平台→被测）
[被测服务器]  perf-demo:8081 + MySQL(demo库) + -javaagent 探针
```

### 平台服务器

```bash
scripts/start-server.sh    # 端口 8080；源码上传目录默认 ./data/source
```

### 被测服务器

```bash
# REPORT_URL 必须改为平台服务器真实地址，如 http://<平台IP>:8080/api/agent/report
REPORT_URL=http://<平台IP>:8080/api/agent/report scripts/start-agent.sh
```

网络要求：被测服务器可访问平台 `8080` 端口（上报）；平台服务器可访问被测服务器 `8081` 端口（压测）与被测库 `3306` 端口（灌数据）。agent 上报采用本地队列 + 1s 批量 flush，失败指数退避重试、超时丢弃，网络抖动不影响被测业务。

## perf-demo 示例应用（可分析问题点）

| 问题类型 | 位置 | 表现 |
| -------- | ---- | ---- |
| N+1 查询（最典型） | `UserService.listUsersWithOrderCount`（循环内逐条 `countOrders`） | `GET /api/user/list?limit=N`，N 个用户产生 N+1 次 SQL |
| 慢 SQL：无索引全表扫描 | `OrderMapper.selectByStatus`（`status` 列刻意无索引） | `GET /api/order/list?status=PAID` |
| 慢 SQL：大表 GROUP BY | `OrderMapper.selectStats`（5 万行聚合） | `GET /api/order/stats` |
| 慢 SQL：无覆盖索引排序 | `OrderMapper.selectRecent`（`created_at` filesort） | `GET /api/order/recent` |

启动时自动建表并灌入 1 万用户 / 5 万订单（`DemoDataInitializer`，幂等），并自动补充 `perf_order.user_id` 外键约束与 `status` 列枚举注释（作为灌数引擎的验收载体）。压测 `GET /api/user/list?limit=200`（20 并发）可稳定复现：QPS ≈ 300、P95 ≈ 135ms、错误率 > 0.5%，同时产生 N+1 热点调用链与慢 SQL 上报。

## 数据准备引擎（perf-datagen 独立模块）

Mock 数据生成能力沉淀为**独立纯 Java 库**（`com.perftestor.datagen`，可脱离平台复用），宿主通过唯一门面调用，进度与语义提示经回调交还：

- **联表生成**：表组拓扑排序（环检测定位表名）、父 ID 池插入后回查采样（>50 万分段）、`ref`/`refSkew` 热点倾斜（累计权重二分 O(log n)）、`orphanRate` 独立池后置替换、自引用深度限制；
- **无外键关联识别（四级合并）**：显式规则 > DB 声明外键 > MyBatis 源码语义（resultMap association/collection + 注解 SQL JOIN 正则）> 命名约定推断（`父表_id` + 单复数归一 + 类型兼容 + 数据抽样置信度）；空表兜底（父表 0 行 → 低置信建议交前端确认）；低置信关联不静默生效，经建议接口采纳；
- **枚举语义化**：MySQL ENUM → CHECK 约束 → 列注释「PENDING=待支付…」→ Java 枚举 四级发现 + weights 权重分布；
- **魔法值感知**：扫描 MyBatis XML/注解 SQL/静态常量/Java 枚举，产出「表列→候选值（文件：行号）」，范围谓词生成 `overThreshold` 规则；源码不可读自动降级纯 DB 元数据并黄色警告；
- **Dry-Run 试运行**：完整执行元数据/规则/关联/拓扑/外键池预检但不 INSERT，前端试运行通过后启用正式执行；
- **SPI 可插拔**：`DatabaseDialect` / `OrmSourceParser` / `BatchExecutor` 三扩展点本期实现 MySQL + PostgreSQL 双方言与 MyBatis ORM，扩展 Oracle/JPA 仅需新增实现类；单元测试通过 H2（MySQL 模式）子类化方言驱动全流程，突变测试杀死率 92%（覆盖内）。
- **PostgreSQL 方言**：URL 前缀 `jdbc:postgresql:`，自动追加 `reWriteBatchedInserts=true`（多行 VALUES 合并）与 `stringtype=unspecified`（参数按目标列类型推断，避免字符串写入原生枚举列报类型不匹配）；元数据走 JDBC `DatabaseMetaData` + `information_schema`；枚举发现查 `pg_enum`，降级走 CHECK 约束（`pg_get_constraintdef` 文本解析）；`SERIAL/BIGSERIAL` 的 `nextval(...)` 默认值识别为自增并跳过插值；标识符用双引号（PG 未加引号标识符折成小写）。

规则 JSON 契约（前端编辑器 / 引擎 / AI 工具三方一致）：

```json
{"order.user_id": {"type": "refSkew", "table": "user", "column": "id", "hotPercent": 20, "hotShare": 80},
 "order.status":  {"type": "enum", "values": ["PENDING","PAID"], "weights": [20, 80]},
 "order.amount":  {"type": "range", "min": 100, "max": 5000, "overThreshold": {"op": ">", "value": 1000, "share": 30}}}
```

端到端验收：
- `scripts/verify-datagen-mysql.sh` —— MySQL：Dry-Run 拦截错误表名 / 联表 refSkew / weights / 语义建议接口。
- `scripts/verify-datagen-postgres.sh` —— PostgreSQL：同上用例，并额外校验原生枚举（`CREATE TYPE ... AS ENUM`）写入、BIGSERIAL 自增列跳过、枚举建议值发现。

## 七屏 Web 后台

1. **总览 Dashboard**：关键指标卡 + JVM 堆趋势 + 最近压测/慢 SQL/性能问题 + AI 快捷入口
2. **项目管理**：zip/git/本地三种方式导入源码并绑定应用（agent 上报名称一致时数据自动归属）；git 分支枚举/切换与重新同步；一键清理项目历史数据（源码与项目保留）
3. **项目监控**：JVM 堆 / 非堆 / GC / 线程 / CPU 四类图表，5s 轮询可暂停，按项目与时间范围筛选
4. **调用链分析**（含慢 SQL Tab）：调用链列表 + 调用树 + 火焰图 + 慢 SQL 明细
5. **压测中心**（含数据准备与容量建议 Tab）：压测表单（选项目自动带出服务地址与接口清单）+ 实时 QPS 曲线 + 容量评估（目标 RT 下最大并发/QPS）+ 扩容建议 + JVM 配置建议 + 数据准备（表组联表配置 / 规则编辑器 / Dry-Run 试运行双按钮 / 语义建议一键采纳 / 多表进度与自检报告）
6. **性能优化**：数据库连接配置（EXPLAIN 分析用）→ 性能问题列表（慢 SQL/热点方法/压测瓶颈一键创建）→ 大模型多方案对比 → 修复提示词（复制/下载）
7. **AI 助手**：对话流 + 工具调用卡片 + 无 Key 降级规则模式演示

## AI 修复闭环演示（无 Key 降级模式）

未配置 `ai.api-key` 时平台以**降级规则模板**演示完整闭环（基于真实库数据）：

1. 在 **压测中心** 对 `http://<被测>:8081/api/user/list?limit=200` 发起压测（如 20 并发 × 10s）；
2. 在 **项目管理** 导入被测项目源码（zip 或 git，并绑定应用名）；
3. 在 **AI 助手** 输入：
   - 「分析最近慢 SQL 的根因并给出修复方案」
   - 「当前性能能承受多少并发、何时扩容」
   - 「生成一份性能检测报告」
   - 「列出待处理的性能问题」
4. 在 **性能优化** 的问题详情中查看多方案对比，选定方案后生成**修复提示词**，复制到 AI 辅助编码工具即可落地修复。

配置 `ai.api-key` 与 `ai.base-url`（OpenAI 兼容 API）后，AI 助手升级为 Function Calling 多轮编排：工具清单 `stress_test / jvm_metrics / trace / slow_sql / data_gen / suggest_data_rules / analyze_code_issues / generate_fix_prompt / capacity_advice / generate_report`，模型自主选工具执行并汇总报告。其中 `suggest_data_rules` 综合表结构、语义提示库（魔法值/关联）与枚举建议产出完整 ruleJson，无 API Key 时自动降级为规则档。

## 关键 API

```
# 采集与上报
POST /api/agent/report             # agent 批量上报（JVM/调用链/慢SQL；项目不存在自动丢弃）
GET  /api/apps                     # 应用实例列表
GET  /api/jvm/metrics              # JVM 指标查询
GET  /api/traces                   # 调用链查询（含 traceJson 调用树）
GET  /api/traces/{id}              # 调用链详情
GET  /api/slow-sqls                # 慢 SQL 查询
POST /api/slowlog/parse            # MySQL 慢日志文本解析导入

# 数据库连接管理（慢 SQL 分析 EXPLAIN / 灌数据复用）
GET  /api/dbconns                  # 连接配置列表
POST /api/dbconns                  # 保存连接
POST /api/dbconns/{id}/default     # 设为默认连接
POST /api/dbconns/test             # 测试连接（可携带 SQL 试跑 EXPLAIN）

# 压测与数据准备
POST /api/stress/tasks             # 启动压测（引擎 http/jmeter）
GET  /api/stress/tasks/{id}/report # 压测报告
POST /api/datagen/tasks            # 启动灌数据（dryRun=true 试运行；tables 表组联表；repoId 源码分析）
GET  /api/datagen/tasks/{id}       # 任务详情（进度/逐表统计/试运行报告/端点自检报告）
POST /api/datagen/tables           # 读取目标库表清单
POST /api/datagen/columns          # 列结构（含枚举建议值/疑似枚举标记）
GET  /api/datagen/suggestions      # 语义建议查询（魔法值 value / 关联 relation；status=pending 待确认）
POST /api/datagen/suggestions/{id}/adopt  # 建议采纳/忽略（采纳后作为生成规则依据）

# 源码管理与性能优化
POST /api/source/repos/zip         # 源码导入：zip 上传
POST /api/source/repos/git         # 源码导入：git 克隆
POST /api/source/repos/local       # 源码导入：本地目录
GET  /api/source/repos             # 源码仓库列表
POST /api/source/repos/{id}/resync         # 重新同步源码并重建索引
POST /api/source/repos/{id}/cleanup-data   # 清理项目历史数据
POST /api/source/issues/{id}/analyze       # 大模型根因分析（生成多方案）
POST /api/source/issues/{id}/suggestions/{sid}/select  # 选定方案生成修复提示词

# AI 与容量
POST /api/ai/chat                  # AI 对话（降级/Function Calling）
POST /api/capacity/analyze         # 容量评估（目标 RT 下最大并发/QPS）
GET  /api/capacity/advice          # 容量与扩容建议
```

## 归档约定

开发过程中的全部中间产出物归档于 `docs/`（纳入 git 管理，不写入 `.gitignore`）：

| 文档 | 说明 | 状态 |
| ---- | ---- | ---- |
| [design/requirements.md](design/requirements.md) | 需求文档：7 大功能模块与边界说明 | 已定稿 |
| [design/architecture.md](design/architecture.md) | 整体架构：平台与被测应用分离部署拓扑 | 已定稿 |
| [design/tech-solution.md](design/tech-solution.md) | 技术方案：技术栈、关键设计决策、代码结构 | 已定稿 |
| [design/database-design.md](design/database-design.md) | 数据库设计说明：核心表、新增五张表、分区策略 | 已定稿 |
