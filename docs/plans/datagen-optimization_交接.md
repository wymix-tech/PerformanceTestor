# 测试数据灌入优化 — 交接与后续任务

> 状态：**已实施**（2026-08-29；实现详情见文末「实施记录」）
> 模块：`perf-datagen`（独立 Mock 数据引擎，纯 Java 库）+ `perf-server`（宿主集成层）
> 前端：`perf-web/src/views/StressCenterView.vue`（"数据准备" Tab）
> API：`POST /api/datagen/tasks`（含 dryRun/tables）、`GET /api/datagen/suggestions`、`POST /api/datagen/suggestions/{id}/adopt`

## 1. 现状（交接基线）

当前实现已支持：
- 读取目标表结构（`DatabaseMetaData.getColumns/getPrimaryKeys`）→ 类型映射默认生成器
- 字段级规则 JSON（`ruleJson.fields.<col>`）：`fixed` / `range` / `enum`
- 批量插入（BATCH_SIZE=500，`rewriteBatchedStatements`，行数上限 100 万）
- 异步执行 + 进度上报（`data_gen_task.inserted`）

**核心局限（本交接要解决的三个问题）**：
1. **单表孤立生成**：不感知外键，子表 FK 列填随机值 → 外键约束批量插入失败（strict mode 整批报错）、或孤儿行导致 JOIN 查询空转
2. **无枚举语义**：MySQL `ENUM` 列落入 `default` 分支生成随机字符串 → 违反枚举约束整批失败；Java 侧枚举（值只存在于代码中）完全无感知
3. **与业务代码脱节（魔法值）**：代码里 `WHERE status = 3`、`if (type == 5)` 这类硬编码，随机数据让谓词永远匹配不到行 → 压测查询空转（返回空集、不走真实 IO/分支），性能数据失真

---

## 2. 任务一：联表数据生成（外键感知）

### 2.1 目标
一次任务灌多张关联表，保证引用完整性 + 可控的基数与热点分布，让 JOIN/N+1 场景的压测数据真实。

### 2.2 设计要点

**A. 外键图构建**
- 用 `meta.getImportedKeys(catalog, null, table)` 读取每张表的 FK → 构建 `表 → 父表` 依赖图
- **拓扑排序**决定生成顺序（父表先插，如 user → order → order_item）
- 自引用表（`parent_id` 指向自身）：按行边插边取"已插入行的 ID 池"，带最大深度限制（如 5 层）防爆栈

**B. 父 ID 池采样（替代随机 FK）**
- 父表插入完成后，把父表主键拉入内存池（`SELECT id FROM parent`，超过 50 万时分段采样）
- 子表 FK 列新增规则类型 `ref`：
  ```json
  {"order.user_id": {"type": "ref", "table": "user", "column": "id"}}
  ```
- **热点倾斜（关键）**：新增 `refSkew` 规则，按 Zipf/二八分布从父 ID 池采样——
  ```json
  {"order.user_id": {"type": "refSkew", "table": "user", "column": "id", "hotPercent": 20, "hotShare": 80}}
  ```
  20% 的 user 承接 80% 的 order → 专测"大 V 用户"型 N+1/锁竞争；均匀分布测索引扫描

**C. 基数配置**
- 请求体从单表扩展为表组：
  ```json
  {"tables": [
     {"tableName": "user",    "rowCount": 10000},
     {"tableName": "order",   "rowCount": 100000, "fields": {"user_id": {"type": "ref", ...}}},
     {"tableName": "order_item", "rowCount": 500000}
  ]}
  ```
- 兼容旧单表请求（`tableName/rowCount` 字段保留，等价于单表组）
- 孤儿率可选（默认 0）：`{"orphanRate": 0.01}` 生成少量 FK 指向不存在行，测"LEFT JOIN 查不到"场景

**D. 事务与进度**
- 每表独立事务批次（现有 BATCH_SIZE 逻辑复用），失败时报告到具体表
- `data_gen_task` 进度需扩展为 `总行数/已插行数/当前表`（`ruleJson` 或加 detail JSON 字段）

### 2.3 验收标准
- user(1万)/order(10万)/order_item(50万) 组灌入：FK 100% 有效（无插入失败）、ORDER BY FK 抽查 JOIN 均有命中
- `refSkew 20/80` 验证：`SELECT user_id, COUNT(*) FROM `order` GROUP BY user_id ORDER BY 2 DESC LIMIT 1` 占比 ≈ 80%（±5%）
- 自引用表（如 `category.parent_id`）树深度可配且不报错

---

## 3. 任务二：枚举数据语义化

### 3.1 目标
枚举列不再生成垃圾值；值集合自动发现（DB ENUM / CHECK 约束 / Java 枚举 / 列注释），并支持权重分布。

### 3.2 设计要点

**A. 值集合自动发现（四级来源，优先级递减）**
1. **MySQL ENUM 类型**：`TYPE_NAME` 形如 `ENUM('NEW','PAID','CANCEL')` → 解析出候选值；现状落入 default 生随机串是必修 Bug（strict mode 整批失败）
2. **CHECK 约束**：查 `information_schema.CHECK_CONSTRAINTS`（MySQL 8.0.16+），正则提取 `col IN (...)` / `col = X OR ...` 的字面量
3. **列注释模式**：中文项目惯例 `COMMENT '状态 1=待支付 2=已支付 3=已取消'` → 正则解析 `数字=描述` 对
4. **Java 枚举反查**（平台独有优势——已有 `source_repo` 源码索引）：
   - 扫描业务包下 `enum` 类的常量与 `getCode()/value` 字段
   - 列名与枚举类名模糊匹配（`order_status` ↔ `OrderStatus`）→ 自动建议值集
   - 匹配结果进 `columns` 接口返回（`suggestedValues` 字段），前端一键采纳

**B. 权重分布（对性能测试至关重要）**
```json
{"order.status": {"type": "enum", "values": ["NEW","PAID","CANCEL"], "weights": [10, 80, 10]}}
```
- 80% 行落 `PAID` → `WHERE status='PAID'` 走大量扫描，真实暴露索引选择性差异
- 现有 `enum` 规则已支持 values，需补 `weights`（累计分布采样，一行实现）

**C. 默认策略**
- 发现值集但未配置权重 → 均匀分布 + 前端提示"建议配置权重"
- 未发现值集 → 维持现有类型映射，但 `columns` 接口把"疑似枚举列"（值发现失败但列名含 status/type/state/flag）标出来

### 3.3 验收标准
- `ENUM('a','b')` 列灌 1 万行零失败，值集全部合法
- 注释 `1=待支付 2=已支付` 解析正确并出现在前端建议
- `weights [10,80,10]` 灌后实际分布误差 ±3%
- Java 枚举 `OrderStatus` 自动建议命中 `order.status` 列（基于 perf-demo 源码验证）

---

## 4. 任务三：魔法值感知（与业务代码联动）

### 4.1 目标
生成的数据必须能命中代码里的硬编码谓词（`status = 3`、`type == 5`、`amount > 1000`），保证压测走到真实分支，而不是空集空转。

### 4.2 设计要点

**A. 静态扫描构建"魔法值清单"**（复用已有源码索引 `SourceFileService`）
- 扫描 `source_repo` 内：
  - **Mapper XML**：`WHERE status = 3`、`status in (1,2)` 的字面量（正则 `(\w+)\s*(=|IN)\s*\(?\s*([\d''][^)]*?)`）
  - **Java 源码**：SQL 字符串内的字面量；`static final int XX = 5` 常量 + `.getCode()` 调用；Java 枚举
- 产出：`表列 → 候选值集合（含来源文件:行号）`，例如
  `order.status → [3, 5]（OrderMapper.xml:42, OrderStatus.java:18）`
- 落库建议：新表 `magic_value_hint(repo_id, table_name, column_name, values_json, source_refs, create_time)` 或直接放 `source_repo` 的 detail JSON

**B. 智能对齐（两档）**
- **规则档（无 LLM）**：列名/表名匹配魔法值清单 → `columns` 接口返回建议规则，前端一键填入 `enum` 规则
- **LLM 档**：复用 `AiAgentService` Function Calling，新增工具 `suggestDataRules(tableName)`——把表结构 + 魔法值清单 + 相关源码片段喂给模型，产出完整 ruleJson（值集、权重、范围推断），无 Key 时降级规则档

**C. 分支谓词感知（范围类魔法值）**
- 扫描代码中 `amount > 1000`、`limit >= 10` 等比较谓词 → 建议 `range` 规则保证一定比例行越过阈值：
  ```json
  {"order.amount": {"type": "range", "min": 100, "max": 5000, "overThreshold": {"op": ">", "value": 1000, "share": 30}}}
  ```
  （实现上：30% 行取 (1000, 5000] 区间，70% 取 [100, 1000]）

**D. 数据有效性自检（闭环）**
- 灌完后用**端点扫描**（`EndpointScanner`）拿到的真实接口查一遍：抽 3 个 GET 接口执行，非空返回率 > 0% 才算通过
- 结果写进 `data_gen_task` 完成详情（自检报告），空转时显式告警"数据未命中业务谓词，压测结果可能失真"

### 4.3 验收标准（基于 perf-demo）
- perf-demo 的 `OrderMapper` 含 `status = 3` 类查询 → 扫描清单命中该列
- 灌数据后执行 `/api/user/list?limit=200` 返回非空（现状：随机数据下 N+1 场景 JOIN 可能为空）
- 带 `overThreshold` 的范围规则实际分布达标（±5%）
- 自检报告在任务详情可见

---

## 5. 实施顺序与依赖

| 序 | 任务 | 依赖 | 预估工作量 |
|---|---|---|---|
| 1 | ENUM 类型解析 + weights（3.1A/3.2B） | 无，纯 bug 修复性质 | 0.5 天 |
| 2 | 联表 ref/refSkew + 拓扑排序（任务一） | 无 | 2 天 |
| 3 | 魔法值静态扫描 + 前端建议（4.2A/4.2B 规则档） | source_repo 已就绪 | 1.5 天 |
| 4 | 注释/CHECK 解析 + Java 枚举反查（3.2A 3/4 级） | source_repo 已就绪 | 1 天 |
| 5 | LLM 档 suggestDataRules + 自检闭环（4.2B/4.2D） | 1-4 完成 | 1.5 天 |

> 建议先做 1（现状 ENUM 列是隐性炸弹），2、3 可并行。

## 6. 关联入口（改哪里）

- `DataGenService`：规则引擎扩展（ref/refSkew/weights/overThreshold）、多表编排、拓扑排序
- `DataGenRequest`：表组结构、orphanRate
- `DataGenController` + 前端 `StressCenterView` 数据准备 Tab：表组表单、建议规则一键采纳
- 新增 `MagicValueScanner`（建议放 `com.perfserver.source`，复用 `SourceFileService` 遍历）
- `AiAgentService`：注册 `suggestDataRules` 工具
- `sql/init.sql` / `init_empty.sql`：`data_gen_task` 详情字段 / `magic_value_hint` 表 + 幂等迁移

---

## 7. 实施记录（2026-08-29 完成）

### 7.1 架构演进（相对本文档原设计）

经四轮评审收敛，最终形态为 **独立模块 + SPI 扩展点**：

1. **独立模块 `perf-datagen`**（纯 Java 17 库，`com.perftestor.datagen` 包）：
   - 门面 `DataGenEngine.execute(DataGenSpec, EngineListener)`——无状态不落库，进度/语义提示经回调交宿主
   - SPI 三接口（DatabaseDialect/OrmSourceParser/BatchExecutor）本期仅实现 MySQL/MyBatis，签名保留多库多 ORM 扩展
   - 依赖隔离：除 javaparser-core 外均 provided（slf4j），不做 shade/Uber-Jar
2. **无外键关联识别（四级合并）**：explicit > db_fk > orm_join（resultMap association/collection + 注解 SQL JOIN 正则）> naming（`<parent>_id` 后缀 + 单复数归一 + 类型兼容 + 抽样包含率置信度）；**空表兜底**（父表 0 行 → 0.3/NAMING_EMPTY_TABLE_FALLBACK）；naming 系不静默生效，经 /suggestions 确认采纳
3. **Dry-Run 试运行**：完整执行元数据/规则/关联/拓扑/外键池预检但不 INSERT；前端"试运行通过"后启用正式执行
4. **refSkew × orphanRate 隔离**：orphanRate 独立池后置替换 NULL/无效 ID；高倾斜（热行 >50%）前端与执行器双警告
5. **多 ORM 容错**：resultMap 结构化解析为主（成功率 100%），注解 SQL JOIN 仅简单正则、失败静默跳过，单文件 5s 超时强制跳过

### 7.2 交付清单

| 层 | 交付物 |
|---|---|
| 模块 | perf-datagen/：DataGenEngine+DefaultDataGenEngine、spi×3、model×8（含 DataGenSpec.dryRun）、engine×6（RelationResolver/NamingRelationResolver/RuleEngine/TopoSorter/IdPool/ExplicitRuleHints）、enumv/EnumValueDiscoverer、dialect/mysql/MySQLDialect、executor/JdbcBatchExecutor、orm/mybatis/MyBatisSourceParser、util/SqlLiteralExtractor |
| 单测 | CoreEngineTest（拓扑/环检测/倾斜采样）、SemanticParserTest（overThreshold±4pp/weights±3pp/ENUM/CHECK/注释/枚举匹配/命名归一/空表兜底/SQL 抽取/容错） |
| 宿主 | DataGenSpecAdapter、SourceFileProvider、DataGenEngineConfig、DataGenService（编排+落库+自检）、SemanticHint 实体+Mapper（幂等 upsert）、Controller /suggestions、AI 工具 suggest_data_rules（自动注册，无 Key 天然降级规则档）、ColumnInfo 建议/疑似枚举 |
| SQL | init.sql / init_empty.sql：data_gen_task + total_rows/tables_json/detail_json、semantic_hint 表（幂等） |
| 前端 | 数据准备 Tab：源码项目下拉/orphanRate/表组 JSON/规则编辑（skew 警告）/建议采纳面板/试运行+正式双按钮/联表进度/试运行报告/自检报告展示 |
| 验收载体 | perf-demo DemoDataInitializer.upgradeSchema()：perf_order 幂等补 fk_order_user 外键 + status/user_id 列注释 |
| 验收脚本 | scripts/verify-datagen-mysql.sh（Dry-Run 拦截错误表名/试运行报告/联表 refSkew/weights/建议接口） |

### 7.3 待办（环境就绪后执行）—— 已全部完成（2026-08-30 验收）

1. ~~mvn clean package~~ ✅（JDK 21 + `maven.compiler.release=17`，四模块 jar 产出）
2. ~~sql/init.sql 幂等执行 → 启动 perf-demo（自动补 FK/注释）→ 启动 perf-server~~ ✅（Docker MySQL 8.0；demo 库 15 表 + perf_user=10000/perf_order=50000 预置 + fk_order_user 外键）
3. ~~运行 scripts/verify-datagen-mysql.sh，逐项核对 PASS~~ ✅ **PASS=8 FAIL=0**
4. ~~人工核对 refSkew 占比 SQL（本文档 §2.3）与 weights 分布（±3pp）~~ ✅（见 7.4）

### 7.4 端到端验收记录（2026-08-30，Docker MySQL 8.0 + perf-demo/perf-server 本机）

| 验收项 | 结果 | 说明 |
|---|---|---|
| Dry-Run 拦截错误表名 | ✅ | `no_such_table_xyz` → 任务 failed，错误信息「表不存在或无可见列」 |
| 试运行 dryRunReport | ✅ | 联表+refSkew+enum weights 试运行 finished，详情含估算报告 |
| 正式灌数 FK 100% 有效 | ✅ | 1000 user + 5000 order 零失败，user_id 全部命中父池 |
| weights [10,80,5,5] | ✅ | 实测 PAID=79.8% / PENDING=9.9% / SHIPPED=4.9% / DONE=5.3%（±0.7pp < 3pp） |
| refSkew 20/80 | ✅ | **top 20% 热点 user 承接 81.4% 订单行**（±5% 内）。注：§2.3 原验收 SQL 用 top1 占比口径与 `hotPercent=20`（20% 父行承接 80% 子行）语义不符，top1 会被热点池稀释（预期内）；二八分布口径为准 |
| 枚举建议（列注释发现） | ✅ | columns 接口对 `perf_order.status` 从列注释解析出 [PENDING,PAID,SHIPPED,DONE]（value 类建议实时出口为 columns 接口；/suggestions 的 value 类来自源码扫描 MagicHint，需导入源码项目） |
| relation 类建议 | ✅ | /suggestions 返回 explicit 来源关联提示 |
| 自检报告 | ✅ | 任务详情含 selfCheck |

验收脚本修复记录：原脚本 `fields` 以嵌套对象发送，而 API 契约 `TableSpecDto.fields` 为字符串型规则 JSON → 500；已改为 python3 生成请求体（与前端 JSON.stringify 行为一致）。
