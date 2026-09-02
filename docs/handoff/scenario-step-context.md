# 步骤上下文引用（scenario-step-context）— 工作交接文档

> 交接日期：2026-09-01
> 前任执行：AI 编码助手（CodeBuddy / Hy4）
> 交接范围：Issue #7「多接口混合场景编排优化」P1 + P2 + P3 全量
> 设计文档：`docs/design/scenario-step-context.md`（已置为「已实现」，含实现记录与偏差说明）
> 计划文档：`~/.local/share/code-server/.../plans/25e9ebc238b94e0bbf8878c587c02eb5`

---

## ✅ 0. 接手必读

### 0.1 状态：已验证通过，但代码**未提交**

**2026-09-01 补充**：后续已装好工具链并跑通全部测试，**本节初稿的「未编译验证」警告已作废**。
实测结果见 §0.2。

工作区有 **20 个文件的未提交改动**（14 改 + 6 新增）。

```bash
cd /workspace && git status --short
#  M docs/design/scenario-step-context.md
#  M perf-server/.../ai/tools/AbstractAiTool.java
#  M perf-server/.../ai/tools/StressTool.java
#  M perf-server/.../stress/HttpStressEngine.java
#  M perf-server/.../stress/ResponseAccess.java
#  M perf-server/.../stress/ResponseVarExtractor.java
#  M perf-server/.../stress/StressStatistician.java
#  M perf-server/.../stress/StressTaskRequest.java
#  M perf-server/.../stress/StressTaskResult.java
#  M perf-server/.../stress/ResponseAccessTest.java       (test)
#  M perf-server/.../stress/StressScenarioTest.java       (test)
#  M perf-server/.../stress/StressStatisticianTest.java   (test)
#  M perf-web/src/components/stress/StressForm.vue
#  M perf-web/src/components/stress/StressReport.vue
# ?? perf-demo/.../controller/DemoScenarioController.java
# ?? perf-server/.../stress/StepContextStore.java
# ?? perf-server/.../stress/TemplateRenderer.java
# ?? perf-server/.../stress/StepContextStoreTest.java     (test)
# ?? perf-server/.../stress/TemplateRendererTest.java     (test)
# ?? scripts/verify-scenario-context.sh
```

### 0.2 实测结果（2026-09-01）

环境：JDK 17.0.20.1（`/opt/java`）+ Maven 3.9.6（`/opt/maven`）+ 阿里云镜像 + Node 22。
若容器重建，按 `docs/handoff/environment-setup.md` 重装（约 10 分钟）。

```bash
export JAVA_HOME=/opt/java/jdk-17.0.20.1+1
export PATH=$JAVA_HOME/bin:/opt/maven/apache-maven-3.9.6/bin:$PATH

mvn -B test                    # 后端 277 个测试全过，4 个模块 SUCCESS
cd perf-web && npx vue-tsc -b  # 前端类型检查零错误
cd perf-web && npx vitest run  # 前端 21 个测试全过
```

| 项 | 结果 |
| --- | --- |
| 后端全量 | **278 passed / 0 failed**（2026-09-02 复核：原 277 + 本轮新增分档用例 1）；perf-datagen、perf-agent、perf-server、perf-demo 全部 SUCCESS |
| 新增用例 | 28 个（`TemplateRendererTest` 15 + `StepContextStoreTest` 12 + `StressScenarioTest` 净增 1，另扩展 `ResponseAccessTest` / `StressStatisticianTest`）；2026-09-02 再增 1 个（`stagedScenarioDeduplicatesWarningsAndMergesStepStats`） |
| 前端类型检查 | `vue-tsc -b` 退出码 0，无输出 |
| 前端测试 | **23 passed**（2026-09-02：新增 `StressReport.spec.ts` 2 个，覆盖分步骤表格与老报告兜底） |
| ~~**未验证**~~ | ~~`scripts/verify-scenario-context.sh` 尚未运行~~ → **已跑通（2026-09-02）：12/12 PASS**，详见 §0.4 |

若出现**存量用例失败**，优先怀疑本次改动，别急着改老用例——兼容性是本次的硬约束。

### 0.4 端到端自验（2026-09-02 首次跑通）

```bash
docker start perf-mysql          # 容器已建好，库 performance_testor（21 张表）
mvn -B install -DskipTests       # 必须先整体安装，否则 perf-server 单独 spring-boot:run
                                 # 会报 perf-datagen 依赖解析失败
setsid nohup mvn -pl perf-demo spring-boot:run > /tmp/demo.log 2>&1 < /dev/null & disown
setsid nohup mvn -pl perf-server spring-boot:run > /tmp/server.log 2>&1 < /dev/null & disown
BASE=http://127.0.0.1:8080 DEMO=http://127.0.0.1:8081 ./scripts/verify-scenario-context.sh
```

**结果：PASS=12 FAIL=0**。三步场景（零 extract 声明）116504 次请求，下单 / 详情步骤**错误率均 0%**，
正式期 `renderMiss == 0`、`warnings` 为空；越界引用任务照常完成，产出 1 条 warning，
`renderMiss == totalRequests`（16285），100% 401 符合"占位符原样发出"的预期。

**自验脚本本轮修了 3 个 bug**（此前从未运行过，故一直没暴露）：

| 症状 | 原因 | 修法 |
| --- | --- | --- |
| 全部接口 401 | 平台默认开启 JWT 鉴权，`/api/**` 需登录，脚本无 token | 新增 §0 登录步骤取 token，后续请求带 `Authorization` |
| `KeyError: 'result'` | 报告接口返回 `ApiResponse` 包装，result 在 **`data.result`** 下（前端由 axios 拦截器解包，裸 curl 得多取一层） | 加 `result_json()` 统一取层，6 处解析全部改走它 |
| `renderMiss = -1`（按名查不到步骤） | 步骤只填 `alias` 未填 `name`；**`stepName` 只取 `name`**，`alias` 仅用于上下文引用，未填 name 时回落成 `step-N` | 给两步骤补 `name` |

> ⚠️ 起服务前先确认 8080 是否被**陈旧进程**占用——本次发现一个跑了 46 分钟的
> `perf-server.jar`（非当前代码），它会静默接管请求，导致验证的是旧逻辑。

### 0.3 最可能的编译问题（人工复核时的存疑点）

| 风险点 | 位置 | 说明 |
| --- | --- | --- |
| `javadoc` 花括号嵌套 | `TemplateRenderer.java`、`StepContextStore.java` | 类注释里出现过 `{@code {{...}}}`，已改为纯中文描述规避；若开 javadoc 严格模式仍报错，删注释即可 |
| `Map.get()` 返回 `Integer` 拆箱 | `StepContextStoreTest` | 已改 `.intValue()`，但其余断言请复核 |
| `arr()` 与重载歧义 | `AbstractAiTool` | 新增 `buildParams(ObjectNode, String[])` 与 `buildParams(String[], String[])`，空数组字面量可能命中错误重载 |
| Vue 模板插值 | `StressForm.vue` | 模板里**禁止直写 `{{ }}`**（会被 Vue 编译器当插值解析报错），新常量沿用 `VAR_PH` 手法以字符串注入，已加校验拦截 |

---

## 1. 背景：这个功能是干什么的

### 1.1 领域概念（新 session 无记忆，请先读这段）

`perf-server` 的压测引擎支持三种模式（见 `StressTaskRequest` 类注释）：

1. **单接口模式**：给 `targetUrl`，对该地址持续发压；
2. **多接口混合场景（scenario）**：给 `scenario` 步骤数组，按 `weight` 权重配比随机选步发压；
3. **JMeter 模式**：外部脚本，**不支持 scenario，本次完全未动**。

scenario 模式下，**每个 worker 线程独立按权重随机选步骤执行**，多 worker 共享一个「变量池」。

### 1.2 改动前的问题

步骤间传值**只有一条路径**：上一步显式声明 `extract` 提取变量到扁平变量池，下游用 `{{var}}` 引用。
这带来四个痛点：

1. **先声明后使用**：想用一步响应里的某个字段，必须先在那一步写一条 `extract` 规则，配置冗长；
2. **变量池扁平**：多步骤的同名变量（两套系统的 token）互相覆盖；
3. **看不到来源**：`{{token}}` 无法表达「来自第几步的什么位置」；
4. **响应头提取不自然**：跨步骤引用 `X-Request-Id` / `Set-Cookie` 也要先声明成中间变量。

### 1.3 改动后

新增**步骤上下文引用**——下游步骤**无需任何 extract 声明**，直接引用上游响应：

```json
{
  "scenario": [
    { "alias": "login", "url": "{{baseUrl}}/api/login", "method": "POST" },
    { "alias": "order", "weight": 3, "url": "{{baseUrl}}/api/order", "method": "POST",
      "headers": { "Authorization": "Bearer {{p1.result.data.token}}",
                   "X-Trace-Id": "{{p1.header.X-Request-Id}}" },
      "body": "{\"userId\":\"{{login.result.data.userId}}\"}" },
    { "alias": "detail", "url": "{{baseUrl}}/api/order/{{order.result.data.orderId}}",
      "headers": { "Authorization": "Bearer {{p1.result.data.token}}" } }
  ]
}
```

**语法**：`{{ <stepRef> . <source> [ . <path> ] }}`

| 片段 | 取值 |
| --- | --- |
| `stepRef` | `p<N>`（序号，从 1 开始）或步骤 `alias`（留空时回退用 `name`） |
| `source` | `header`（响应头，大小写不敏感、支持连字符）/ `result`（响应体） |
| `path` | 点分路径递归任意深度，支持数组下标 `list.0.id`；`{{p1.result}}` 取响应体整体 |

**未命中一律保留占位符原文**（如 `{{p0.result.x}}`、`{{p9.result.x}}`、路径不存在、头不存在），
**不中断压测**，并计入该步骤的 `renderMiss` 计数。

---

## 2. 核心实现

### 2.1 总体结构：双池

改动前 `Scenario`（`HttpStressEngine` 内部类）只有「扁平变量池」；改动后是**双池**：

```
StressTaskRequest(scenario，含新增 alias 字段)
        ▼
HttpStressEngine.Scenario（每档一个，worker 共享）
 ├── StepSelector        权重选择（未改动）
 ├── pool                扁平变量池：兼容存量 {{var}} / extract
 └── ctxStore            步骤上下文池（新增）：每步一个槽位，存最近一次响应
      ├── headers        响应头
      ├── bodyRaw        截断后的原始响应体
      └── bodyJson       懒解析缓存（首次被引用才解析）
```

**渲染顺序**（`TemplateRenderer`）：先上下文引用 → 再扁平变量 → 都未命中保留原文。

### 2.2 新增类一：`StepContextStore`（299 行）

`perf-server/src/main/java/com/perfserver/stress/StepContextStore.java`，**包级可见**。

| 成员 | 行 | 职责 |
| --- | --- | --- |
| `AtomicReferenceArray<StepResponse> slots` | 58 | 每步一个槽位，`put` 时**整体原子替换** |
| `static int parseStepRef(String)` | 86 | **pN 解析的唯一实现**（见 3.2） |
| `static Map buildAliasIndex(steps, warnings)` | 129 | 启动期构建别名表，问题写入 warnings |
| `int indexOf(String stepRef)` | 194 | 槽位解析：pN → 序号，否则查别名表，都不命中返回 -1 |
| `void put(int, headers, body)` | ~215 | 原子替换槽位；**仅在真实收到响应时调用** |
| `String resolve(int, source, path)` | 235 | 按 source + path 取值，未命中返回 null |
| `synchronized (this)` 双检懒解析 | 289 | 锁粒度是**单个槽位**，仅首次引用付费解析 JSON |

**三条不变量（改代码时别破坏）**：

1. **错误响应照常入池**（设计 2.3.5）：`status>=400` 或断言未过的响应也要入池，
   保证下游引用「最新可达数据」——压测要压出真实错误，不能静默跳过；
2. **网络异常不入池**：`catch` 分支没有响应对象，**不得**用空快照覆盖旧的可达数据；
3. **无人引用的槽位不写池**：由预编译的 `referenced[]` 决定（`HttpStressEngine:517`），省一次快照分配。

### 2.3 新增类二：`TemplateRenderer`（207 行）

`perf-server/src/main/java/com/perfserver/stress/TemplateRenderer.java`，`public final`。

```java
public static String render(String template, Map<String,String> pool,
                            StepContextStore ctx, long[] missCounter);
```

- 吸收了 `ResponseVarExtractor.substitute` 的**手写扫描**（无正则回溯风险），
  `template.indexOf("{{") < 0` 的快速返回原样保留；
- 上下文形状判定用静态 `Pattern`，但**先 `indexOf('.') < 0` 快速排除**——
  绝大多数存量 `{{var}}` 不含点号，不进 matcher；
- `static String[] parseContextRef(String)` 是**形状判定的唯一实现**，
  运行期渲染与启动期预编译（`scanContextRefs`）共用，防两处正则漂移；
- `missCounter` 由调用方复用 `long[1]`，**只统计上下文引用未命中**，不统计 `{{var}}` 未命中
  （否则存量配置会被灌满无意义计数）。

### 2.4 引擎改动：`HttpStressEngine`

| 行 | 改动 |
| --- | --- |
| 65 / 86 / 101 / 120 | `allWarnings` 收集：单档分支与分档分支分别收集，`LinkedHashSet` 去重后 `setWarnings` |
| 144 | `StepStats.of(...)` 新增第 6 参 `renderMiss` |
| 282 | `outcome.warnings = scenario == null ? List.of() : scenario.warnings()` |
| 459 / 528-529 | **renderMiss 取零语义**（见 3.1） |
| 488-489 | `needBody` / `needHeaders` 拆成两个预编译标志 |
| 517-518 | 有响应且槽位被引用 → `ctxStore.put`（**必须在 extract 之前**） |
| 621 / 645 | `Scenario.of()`：别名表 + 上下文池 + 依赖预编译 + warnings |
| 664 | `collectContextRefs`：扫描全部步骤模板（url / 请求头值 / body）产出三个布尔数组 |

**预编译的三个布尔数组**：

| 数组 | 含义 |
| --- | --- |
| `captureBody[i]` | 步骤 i 的响应体需要驻留（被 `result` 引用 / 有 extract / 有 body\|json 断言） |
| `captureHeaders[i]` | 步骤 i 的响应头需要驻留（被 `header` 引用 / 有 extract / 有断言） |
| `referenced[i]` | 步骤 i 的槽位被**任一**步骤模板引用（决定要不要 `put`） |

> ⚠️ **成本澄清（易误判）**：capture 标志只控制「是否**驻留**」，不控制「是否**拷贝**」——
> 只要该步骤声明了断言，`resp.headers().map()` 的拷贝照常发生（断言传参需要）。
> 所以 `captureHeaders=false` **不等于**零开销。

### 2.5 其余改动一览

| 文件 | 改动 |
| --- | --- |
| `ResponseAccess.java` | `resolvePath` 增强：**仅当 `current.isArray()`** 且段为纯数字时按下标取。旧实现在 ArrayNode 上 `get("0")` 恒返回 null，属纯新增能力。该方法被 `ResponseAssertor`（json 断言）共用，下标判断**必须**限定在 ArrayNode 分支内 |
| `ResponseVarExtractor.java` | `substitute` 标 `@Deprecated` 并委托 `TemplateRenderer`（签名不变，存量 6 个 substitute 用例必须全绿）；`extract`/`createPool`/`describe` 未动 |
| `StressTaskRequest.java` | `ScenarioStep` 新增 `alias` 字段 + getter/setter；类注释补充两条传值路径说明 |
| `StressStatistician.java` | `StepCounter` 加 `LongAdder renderMiss`；新增 `record(cost, error, stepName, miss)` 重载（三参重载保留）；`StepStats` 加 `renderMiss` 字段并在 `stepStats()` / `of()` 透传 |
| `StressTaskResult.java` | 新增 `List<String> warnings`，随 `objectMapper.writeValueAsString(result)` 自动落库 |
| `AbstractAiTool.java` | **纯新增**：`buildParams(ObjectNode, String[])` 重载、`prop()` / `arrayProp()` schema 助手、`arr()` 数组读取助手。现有 2 参 `buildParams` 与 `str`/`integer`/`lng` **未动**，子类零改动 |
| `StressTool.java` | `stress_test` 工具新增 scenario 步骤数组参数（url 必填，缺失返回 `error()`）；`description` 与 `parametersJson` 补充新语法说明与 hint 引导 |
| `StressForm.vue` | 步骤网格 名称3/方法2/权重2/URL5 → **名称2/别名2/方法2/权重2/URL4**；新增 `CONTEXT_PH`/`HEADER_PH`/`ALIAS_PH` 常量（**沿用 `VAR_PH` 手法，模板里禁止直写 `{{ }}`**）；`stepAliasErrors` 校验并纳入 `canSubmit`；`buildScenario()` 输出 alias |
| `StressReport.vue` | 指标卡下方新增**琥珀色警示卡片**，渲染 `report.result?.warnings`（老报告无该字段，`?? []` 兜底） |
| `DemoScenarioController.java` | perf-demo 演示接口：登录（返回 `data.token`/`data.userId` + `X-Request-Id` 头）/ 下单（校验 `Bearer tk-`）/ 详情（路径 orderId） |
| `verify-scenario-context.sh` | 端到端自验：三步场景断言错误率、renderMiss、warnings；含越界引用降级验证 |

---

## 3. 四个关键设计决策（Issue #7 评审意见闭环）

这些是**踩过坑后的结论**，改动时若违反会引入回归。

### 3.1 renderMiss 必须「取零」而非「累加」（评审 P1）

**问题**：`stats.record(...)` 只在 `warmupDone && !stop` 时调用。若 miss 持续累加在复用的
`long[1]` 里，warmup 期产生的未命中会**残留**并被正式期第一次 `record` 一并提交，计数虚高。

**实现**（`HttpStressEngine:459, 528-529`）：

```java
renderMiss[0] = 0;                    // 迭代开头清零
... 渲染 url / headers / body（累计到 renderMiss[0]）...
long missCount = renderMiss[0];       // 取走
renderMiss[0] = 0;                    // 无论 counted 真假都清零
if (counted) {
    stats.record(costMs, error, scenario.stepName(stepIdx), missCount);
}
```

**测试锁定**：`StressScenarioTest.warmupMissesDoNotLeakIntoFormalStats`（2026-09-01 补）。

> ⚠️ **踩坑记录（务必读，否则可能白改）**：初版用
> `runsContextReferenceWithoutExtractDeclaration` 断言「warmup 后 renderMiss == 0」，
> 但<b>变异测试证明该断言是空的</b>——把两处清零全部关掉，它依然通过。
> 原因有两点：
> 1. 未命中的占位符若落在 **URL** 里（如 `?x={{p9.result.a}}`），`URI.create` 会抛异常，
>    走 `buildRequest` 的 catch 分支，**那个分支自身也会清零**，掩盖了泄漏；
> 2. 该场景的未命中只在 warmup 最初几毫秒发生（约 12 次），且 `(a)`/`(b)`/`(c)` 三处清零
>    互为冗余，关掉任意一处都不影响结果。
>
> **正确做法**：让某步骤<b>必然</b>未命中（引用越界的 `p9`）、把占位符放在**请求头**里
> （避开 URI 异常分支）、开启 warmup，然后断言 `renderMiss == totalRequests`
> ——用「等于总请求数」而非「等于 0」，与迭代数无关，是确定性断言。
> 实测：正常 3222 == 3222 通过；关掉清零后 7514637 != 3222 失败。

**当前实现有三处冗余清零**（任缺其一都不会产生 P1 缺陷，勿误删）：

- `(a)` `runScenario()` 迭代开头 `renderMiss[0] = 0`；
- `(b)` 渲染后取走再清零 `missCount = renderMiss[0]; renderMiss[0] = 0;`；
- `(c)` `buildRequest` 抛异常的 catch 分支（URL 非法时）同样取走并清零。

### 3.2 pN 解析规则必须单一实现（评审 P2）

别名登记要**拦截** `pN` 形式，运行期 `indexOf` 要把 `pN` **解析**成槽位——两处若各写一份判断
（比如一方用 `startsWith("p")`），`p01` / `p1x` / `p999999999` 的归属就会错位。

**实现**：`StepContextStore.parseStepRef` 是唯一入口（`StepContextStore:86`），两侧共用。

| 输入 | 结果 | 理由 |
| --- | --- | --- |
| `p1` / `P2` | 1 / 2 | 前缀大小写不敏感 |
| `p0` | -1 | 序号从 1 开始 |
| `p01` | -1 | **前导零非法**——避免同一槽位多种写法 |
| `p1x` / `p` / `1p` | -1 | 不匹配正则，**禁止用 `startsWith("p")` 判断** |
| `p99999999999999999999` | -1 | 数字段溢出（先 `Long.parseLong` 再范围判断） |

### 3.3 「未命中回落扁平变量」的动态语义要固化（评审 P3）

设计 2.2 规定：上下文引用未命中时**回落**扁平变量池。这意味着同一变量池 key 的语义
**随运行时状态切换**：

- 已注册别名 + **槽位为空**（首轮未到达）→ 回落扁平变量；
- 已注册别名 + **槽位有值** → 上下文优先。

这是设计既定行为，但**极易被后人"优化"掉**（改成直接保留原文，或反过来劫持扁平变量），
故 `TemplateRendererTest.registeredAliasFallsBackToFlatVarWhenSlotEmpty` 专门固化该边界。

### 3.4 warnings 传递链路（评审 P4）

计划初版漏了载体：`Scenario.of()` 在 `runStage` 内构建，`run()` 拿不到。

```
Scenario.of() 收集 warnings
    → Scenario.warnings()
    → runStage: outcome.warnings = scenario.warnings()
    → run(): allWarnings 收集（分档跨档累加）→ LinkedHashSet 去重 → result.setWarnings()
    → StressTaskService: objectMapper.writeValueAsString(result) 落库
    → /api/stress/tasks/{id}/report 透出 → StressReport.vue 渲染
```

**只报确定性问题**：仅 `pN` 越界、别名非法/重名。**不**对「疑似别名拼写错误」报警——
它与扁平点分变量在语法上不可区分，报警会误伤存量配置。

---

## 4. 防劫持：本次改动最重要的兼容性保证

**判据是「stepRef 能否解析到合法槽位」，而不是「字符串长得像不像上下文引用」。**

所以存量配置里叫 `xxx.result.yyy` 的扁平变量，只要 `xxx` 不是 `pN` 也未登记为别名，
就永远走扁平变量分支，**不会被新语法劫持**。

配套的两条硬约束：

1. `ResponseAccess.resolvePath` 的数组下标判断**只**在 `current.isArray()` 分支内生效，
   ObjectNode 的 key 恰为数字串（如 `{"0":"a"}`）**仍按 key 取**——
   已用 `ResponseAccessTest.resolvePathKeepsNumericKeysOnObjectNodes` 锁定；
2. `ResponseVarExtractor.substitute` 标 `@Deprecated` 但**签名与行为不变**（委托到
   `TemplateRenderer` 的无上下文池分支），存量 6 个 substitute 用例必须全绿。

---

## 5. 已知风险 / 未验证项

按优先级排列，**接手后请逐项确认**：

| # | 风险 | 说明 |
| --- | --- | --- |
| 1 | ~~**全流程未编译**~~ | **已解决（2026-09-01）**：工具链已装，后端 277 + 前端 21 全过，见 §0.2 |
| 2 | ~~**URL 未命中占位符的降级行为改变**~~ | **已确认（2026-09-02）**：旧代码会把 worker 打死，现改为捕获、计一次错误并跳过本次迭代（打一次 WARN）。**会改变存量同类配置的行为**，已写入设计文档偏差说明第 4 条。**原缺口**：该降级路径此前无任何断言锁定，已在 `outOfRangeStepReferenceProducesWarningAndMiss` 补 `errorCount == totalRequests`（URL 含未命中占位符必然构造失败）与 `renderMiss == totalRequests` 两条；变异测试（把降级分支的 `error` 改成 `false`）确认能捕获：`expected 8269 but was 0` |
| 3 | `p01` 判为非法 | 属对设计的细化（设计文档未明确规定），已写入偏差说明第 3 条 |
| 4 | 别名回退日志降噪 | `alias` 为空回退 `name` 时，若 `name` 不合法（如中文名「登录」）**仅 DEBUG 不 WARN**；只有**显式填写**的 alias 出问题才告警。已写入偏差说明第 1 条 |
| 5 | ~~`StressScenarioTest` 时序敏感~~ | **已实测（2026-09-02）**：连跑 5 轮全部通过（后 3 轮含新增用例，7 tests/轮），未复现偶发失败。风险仍保留在案——慢机器上若偶发失败，**先确认是时序还是逻辑问题**，别直接放宽断言。**另注**：单模块跑测试需带 `-am -Dsurefire.failIfNoSpecifiedTests=false`，否则 `perf-datagen` 未安装会报依赖解析失败 / 无匹配用例 |
| 5.1 | **测试强度已用变异测试验证**（2026-09-01） | 用注入开关做了 5 轮变异：① 关掉 `ctxStore.put` → 端到端用例失败（`实际命中 0 次`），证明引用生效性断言**非空转**；②~⑤ 分别关掉三处清零 → 只有新加的 `warmupMissesDoNotLeakIntoFormalStats` 能捕获（详见 §3.1 踩坑记录）。**结论：端到端与 P1 两条关键断言均已确认有牙齿** |
| 6 | ~~分档模式 warnings 去重~~ | **已补端到端用例（2026-09-02）**：新增 `stagedScenarioDeduplicatesWarningsAndMergesStepStats`，两档场景断言 ① `warnings.size() == 1`（跨档去重）② 分步 `totalRequests` 跨档合并 ③ `renderMiss` 跨档相加 ④ 分步之和 == 总请求数。**注意**：未命中须放在请求头而非 URL——URL 未命中走降级分支会额外计错误，破坏第 ④ 条口径 |
| 7 | `BodyHandlers.ofString()` 未改 | 改用 `discarding()` 是收益明确的独立优化，但需改 `HttpResponse<?>` 泛型处理，本轮**刻意未做** |

---

## 6. 后续可做（未纳入本轮）

1. **`BodyHandlers.discarding()` 优化**：未被引用且无 extract/断言的步骤，连响应体都不必物化
   （需改 `HttpResponse<?>` 泛型处理，是独立的小重构）；
2. ~~**前端步骤列表展示 `renderMiss`**~~ —— **已实现（2026-09-02）**：`StressReport.vue` 新增分步骤统计表格。⚠️ **原描述有误**：此前前端**根本没有分步骤表格**（全项目搜不到 `stepStats`），不是"表格缺一列"，故本轮属整块新增：列含 步骤 / 请求数 / QPS / 错误数 / 错误率 / 平均RT / 最大RT，外加 **引用未命中** 列（仅在存在未命中时展示，避免多数场景白占列宽）。**注意后端 `StepStats` 无 P95 字段**（只有 avg/max），前端勿臆造该列
3. **前端上下文引用自动补全**：步骤下拉里列出可用的 `{{pN.result.*}}` 候选（需解析上游响应样例）；
4. **`{{pN.status}}`**：目前只支持 `header` / `result`，状态码引用是自然的下一步扩展；
5. **`{{pN.result}}` 的 64KB 截断提示**：超长响应被截断时，用户可能拿到不完整的 JSON。

---

## 7. 快速上手：手工验证一遍

若想直观确认功能生效（无需跑测试）：

```bash
# 起服务（后台，务必用 setsid 否则命令返回时进程被回收）
setsid nohup mvn -pl perf-demo spring-boot:run > /tmp/demo.log 2>&1 < /dev/null & disown
setsid nohup mvn -pl perf-server spring-boot:run > /tmp/server.log 2>&1 < /dev/null & disown

# 自验（登录 → 下单 → 详情，零 extract 声明）
BASE=http://127.0.0.1:8080 DEMO=http://127.0.0.1:8081 ./scripts/verify-scenario-context.sh
```

**判定要点**：

- 下单 / 详情步骤**错误率 0%** → 说明下游确实拿到了上游的 token
  （引用失败时 placeholder 原样发出，会表现为 100% 401）；
- `renderMiss == 0` → warmup 期未命中未污染正式期统计；
- `warnings` 为空 → 别名 `login` / `order` / `detail` 均合法且不重名。

第二个用例故意用 `{{p9.result.data.token}}`（只有 2 步），验证：
**照常完成 + 产出 warning + renderMiss >= 1**，即降级不中断压测。

---

## 8. 文件索引（按接手排查顺序）

| 顺序 | 文件 | 看什么 |
| --- | --- | --- |
| 1 | `docs/design/scenario-step-context.md` | 需求与设计（文首「实现记录」含偏差说明与评审闭环表） |
| 2 | `perf-server/.../stress/TemplateRenderer.java` | 双池渲染与防劫持分支（207 行，最核心） |
| 3 | `perf-server/.../stress/StepContextStore.java` | 上下文池与 pN 解析（299 行） |
| 4 | `perf-server/.../stress/HttpStressEngine.java` | 引擎接入（重点看 `Scenario.of()` 621 与 `runScenario()` 455-540） |
| 5 | `perf-server/.../stress/TemplateRendererTest.java` | 15 个用例，覆盖 P2/P3 边界 |
| 6 | `perf-server/.../stress/StepContextStoreTest.java` | 12 个用例，含并发与懒解析 |
| 7 | `perf-server/.../stress/StressScenarioTest.java` | 端到端（含「warmup 期 miss 不计入」口径） |
