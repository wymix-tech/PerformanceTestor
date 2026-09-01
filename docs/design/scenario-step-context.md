# 多接口混合场景编排优化设计：步骤上下文引用（{{p1.header.x}} / {{p2.result.x}}）

> 生成日期：2026-09-01　模块：perf-server（压测引擎）　状态：评审中
> 需求来源：Issue #7「多接口混合场景编排优化」

---

## 一、概要设计

### 1.1 背景与现状

当前 scenario 模式已支持多接口按权重混合压测，步骤间数据传递依赖**显式提取规则**：

```json
{
  "scenario": [
    { "name": "login", "url": "/api/login", "extract": [{ "var": "token", "source": "body", "path": "data.token" }] },
    { "name": "order", "url": "/api/order", "headers": { "Authorization": "Bearer {{token}}" } }
  ]
}
```

痛点：

1. **先声明后使用**——上一步必须显式写 extract 才能取值，配置冗长；
2. **变量池扁平**——所有提取值共用一个命名空间，多步骤同名变量（如两套系统的 token）互相覆盖；
3. **看不到来源**——`{{token}}` 无法表达"来自第几步的什么位置"，排查配置错误困难；
4. **响应头提取不自然**——跨步骤引用响应头（如 traceId、Set-Cookie）必须声明中间变量。

### 1.2 目标

引入**步骤上下文引用**语法，下游步骤可直接引用任意上游步骤的响应头与响应体，无需声明 extract：

- 第 N 步可引用第 1 ~ N-1 步的返回数据（含 header）；**引用作用域覆盖该步骤的请求 URL、请求 body 与请求 header**（即后续步骤的请求 header 模板同样支持 `{{pN.header.x}}` / `{{pN.result.x}}`，与 URL/body 一致）；
- 引用语法：`{{p1.header.X-Token}}`、`{{p2.result.data.token}}`；
- `p1`/`p2`/... 指步骤序号（**默认实现**，从 1 开始）；每步可起名，用 `{{login.result.xxx}}` 亦可；
- `header` = 从响应头取值；`result` = 从响应体取值；
- 响应体为**基本类型**时直接 `{{p1.result}}` 取整体；为 **JSON** 时 `result` 后接点分 key 路径，**支持递归**（任意深度嵌套）。

### 1.3 设计原则

| 原则 | 说明 |
| ---- | ---- |
| 兼容存量 | `{{var}}` 扁平变量池与 extract 规则**全部保留**，新旧语法共存，老任务零改动 |
| 引用即用 | 不声明 extract 也能直接 `{{p2.result.x}}`；extract 与新语法可混用 |
| 失败静默 | 引用未命中保留原占位符（与现行为一致），不中断压测 |
| 零依赖 | 继续用 Jackson 点分路径递归解析，不引入 json-path |
| 并发安全 | 压测多 worker 并发读写上下文，沿用 ConcurrentHashMap 方案 |
| 性能优先 | 热路径上解析缓存化：响应体 JSON 只解析一次；超长响应截断防护 |

### 1.4 总体方案

在 `HttpStressEngine` 的 `Scenario` 运行时上下文中，将现有「扁平变量池」升级为「**扁平变量池 + 步骤响应上下文池**」双池结构：

```
StressTaskRequest(scenario)                    ← 配置层（新增 alias 字段）
        │
        ▼
HttpStressEngine.Scenario                       ← 运行时上下文（每任务一个，worker 共享）
 ├── StepSelector        权重选择（不变）
 ├── Map<String,String>  varPool               ← 原扁平变量池（兼容 {{var}} / extract）
 └── StepContextStore    ctxStore              ← 新增：步骤上下文池
      ├── steps[i].alias                        步骤别名 → 序号映射
      ├── steps[i].responseBody(JsonNode)       最近一次响应体（懒解析 JSON）
      ├── steps[i].responseBodyRaw(String)      原始响应体（非 JSON 或基本类型）
      └── steps[i].responseHeaders              最近一次响应头
        │
        ▼
TemplateRenderer（新增，吸收 ResponseVarExtractor.substitute 逻辑）
 └── 渲染顺序：先 {{pN.xxx}} / {{alias.xxx}} 上下文引用 → 再 {{var}} 扁平变量
```

每个 worker 迭代：选步骤 → 渲染模板（双池）→ 发请求 → 响应写入 `ctxStore`（本步骤槽位）→ 按需 extract 写回 `varPool`。

> **注意**：scenario 是"每迭代随机选一步"的流量模型，`p2` 指的是**步骤定义中的第 2 个接口**最近一次成功返回，而非"本次迭代第 2 次请求"。这一语义与登录态等"会话级数据"的使用方式一致（详见 2.6 语义澄清）。

---

## 二、详细设计

### 2.1 引用语法规范

```
{{ <stepRef> . <source> [ . <path> ] }}
```

| 片段 | 取值 | 说明 |
| ---- | ---- | ---- |
| `stepRef` | `p<N>`（N≥1）或步骤 `alias`/`name` | `p1` = 第 1 步（默认实现）；命名后两种写法等价 |
| `source` | `header` / `result` | 从响应头 / 响应体取值 |
| `path` | 点分路径，可选 | header：响应头名，大小写不敏感；result：JSON key 路径，可递归任意深度 |

合法示例：

```text
{{p1.result}}                 第 1 步响应体整体（基本类型或原始文本）
{{p1.result.data.token}}      递归取 JSON：根 data → token
{{p1.result.list.0.id}}       数组下标（点分数字下标）
{{p2.header.Set-Cookie}}      第 2 步响应头（大小写不敏感）
{{p2.header.X-Request-Id}}    含连字符的头名
{{login.result.user.id}}      步骤别名为 login 的响应体路径
```

非法/未命中示例（渲染时**保留原文**，便于排查）：

```text
{{p0.result.x}}     步骤序号从 1 开始，p0 非法 → 保留原文
{{p3.result.x}}     不存在第 3 步 → 保留原文
{{p1.result.a.b}}   路径未命中 → 保留原文
{{p1.header.x}}     响应头不存在 → 保留原文
```

**同步骤引用**：步骤不可引用自身（`{{p1.*}}` 写在 p1 自己的模板里），渲染时该步骤上下文尚无数据（首轮）或为上一轮旧值（歧义），故规则为：**引用只对渲染方生效，允许引用任何已定义步骤的最新上下文，包括自身槽位的上一轮值**——这能覆盖"刷新 token"类自依赖场景；首轮未命中自然保留原文，无需特判。

### 2.2 优先级与解析流程（TemplateRenderer）

```
渲染 template：
  1. 扫描 {{...}} 占位符
  2. 对每个占位符按顺序尝试解析：
     a. 上下文引用：^([A-Za-z_][\w-]*)\.(header|result)(\..+)?$
        stepRef → 序号（pN 直接算；否则查 alias 表，未命中则不是上下文引用）
        查 StepContextStore → 命中则替换，未命中进入 b（保留语义见下）
     b. 扁平变量：varPool.get(name) → 命中则替换
     c. 都未命中 → 保留 {{...}} 原文
  3. 注意：{{var}} 若恰好形如 {{login.result.x}} 且 login 不是别名 → 走 b 分支，
     保证老任务里叫 "xxx.result.yyy" 的变量名不被劫持（理论冲突，规则确定性兜底）
```

实现为引擎内静态工具类 `TemplateRenderer`，吸收并委托 `ResponseVarExtractor.substitute`（后者保留，标记 `@Deprecated` 供存量单测过渡）。

**渲染作用域**：上下文引用对步骤模板的**三处渲染点统一生效**——`url`、`body`、以及**每个请求 header 的 key 值**（header 名本身不渲染，值参与渲染）。即除第一步外的任意步骤，其请求 header 值（如 `Authorization: Bearer {{p1.result.data.token}}`、`X-Trace-Id: {{p1.header.X-Request-Id}}`）与 URL/body 走完全相同的解析优先级与未命中语义；第一步因尚无任何上游上下文，模板中即便写了 `{{pN.*}}` 也只会保留原文（等同未命中）。

**性能**：占位符扫描沿用现有手写扫描（无正则回溯风险）；上下文引用预编译——任务启动时 `Scenario.of()` 对每个步骤的 url/headers/body 扫描一遍占位符，把"该步骤依赖哪些上下文槽位"预计算成位图，运行期仅做已登记的槽位查找，无占位符的模板直接原样发送（现状已如此，保持）。

### 2.3 StepContextStore（核心新增）

```java
/** 步骤响应上下文池：每任务一个，worker 共享，并发安全 */
final class StepContextStore {
    private final int size;
    /** 别名 → 步骤下标（0-based），无别名步骤不登记 */
    private final Map<String, Integer> aliasIndex;
    /** 每步最近一次响应：下标对齐步骤 */
    private final StepResponse[] slots;   // volatile 语义由 AtomicReference 保证

    static final class StepResponse {
        final Map<String, List<String>> headers;   // 响应头（保持原始大小写，查找忽略大小写）
        final String bodyRaw;                      // 原始响应体（≤ 截断上限）
        final JsonNode bodyJson;                   // 解析后的 JSON 根（非 JSON 为 null，懒解析见下）
        final boolean jsonParsed;
    }
}
```

要点：

1. **只存"最近一次"**：每个步骤槽位只保留最近一次响应，新响应原子替换整个 `StepResponse`（`AtomicReference.set`），读侧永远看到完整一致快照，不存在 header/body 跨版本的撕裂。
2. **懒解析 JSON**：写入槽位时只存 `bodyRaw`；首次被 `{{pN.result.*}}` 引用时才用 Jackson 解析并缓存到 `bodyJson`（`synchronized` 双检，仅首次 paid）。非 JSON 响应（基本类型、纯文本、XML）解析失败置 `jsonParsed=true, bodyJson=null`，`{{pN.result}}` 返回 `bodyRaw` 原文。
3. **截断防护**：写入前按 `MAX_EXTRACT_BODY_CHARS`（现有 64KB 常量）截断，超长响应不拖垮压测机；超长体如需引用，用户应引导被测接口收敛响应体。
4. **内存上界**：内存占用 ≈ 步骤数 ×（响应体 ≤64KB + 头表），与变量池同量级，可控。
5. **错误响应也入池**：与现有 extract 时机一致——即使本次请求失败/断言未过，响应仍写槽位，保证后续步骤引用的是"最新可达数据"；获取不到有效 token 时下游引用未命中保留原文，请求仍发出（压测要压出真实错误，不静默跳过）。
6. **warmup 期也写池**：与现有变量池行为一致，保证正式期引用有效。

### 2.4 配置模型变更（StressTaskRequest）

`ScenarioStep` 仅新增一个字段，向后兼容（Jackson 忽略未知字段本就成立，老 JSON 照常反序列化）：

```java
public static class ScenarioStep {
    private String name;      // 已有：展示名 + 统计维度
    private String alias;     // 新增：引用别名（{{alias.result.x}}）；为空时 name 兼作别名
    // ...其余字段不变
}
```

别名规则：

- `alias` 为空时回退用 `name` 作别名（"每接口可以起名，用对应名字也可以"）；
- 别名合法字符 `[A-Za-z_][A-Za-z0-9_-]*`，启动校验不合法则告警并忽略该别名（`pN` 引用始终可用）；
- 别名冲突（重名）：首个生效，启动日志 WARN；
- 别名与 `pN` 字面冲突：**保留 `pN` 为最高优先**——步骤 alias 不允许叫 `p1`~`p999` 形式，校验拦截。

前端表单无需强改：不填 alias 时一切按 `pN` 工作。

### 2.5 引擎侧改动点（HttpStressEngine）

| 位置 | 改动 |
| ---- | ---- |
| `Scenario.of()` | 增加 `StepContextStore` 构建（别名表校验）；对每步模板做占位符预编译 |
| `Worker.runScenario()` | 响应到达后：`ctxStore.put(stepIdx, resp)`（原子替换）→ 再走现有 extract 流程 |
| `Worker.buildRequest()` | **请求 header 值**渲染从 `substitute(v, pool)` 改为 `TemplateRenderer.render(v, pool, ctxStore)`；url/body 同理——三处渲染点（url / headers / body）统一走双池渲染，后续步骤的请求 header 因此可直接引用上游响应头与响应体 |
| `runSingle()` | 不变（单接口模式无上下文） |

顺序关键点：**先写槽位后 extract**——extract 写 varPool 与槽位写上下文互不依赖，但 extract 结果可能又被下游以 `{{var}}` 引用，保持现状即可。

### 2.6 语义澄清：引用的是"最新值"而非"本次链路值"

scenario 是按权重随机选步的开放模型，同一 worker 相邻两次迭代可能连续命中同一步骤，多 worker 更是完全并发。因此：

- `{{p2.result.x}}` 语义 = **步骤 2 在全局最近一次响应中的 x**，非"与本次 p1 同一链路的 p2 响应"；
- 这与登录态、会话 token 等"全局共享凭据"的真实需求完全吻合（本功能的核心场景）；
- 多 worker 并发下引用值存在瞬时竞态（拿到的 token 可能来自另一 worker 刚刷新的响应）——对凭据类数据这正是期望行为；如需"请求级串联"（如订单创建→详情的强关联 ID），应使用 extract 显式提取到 varPool，由该 worker 迭代内独占，两者定位互补，文档中写明选型指引。

### 2.7 递归取值规则（result.path）

复用并增强 `ResponseAccess.resolvePath`：

```
逐段下钻：
  ObjectNode → 按 key 取
  ArrayNode  → 段为纯数字则按下标取（越界返回 null）
  遇 value 节点且仍有剩余段 → 未命中返回 null
最终节点：
  value 节点 → asText()
  object/array 节点（路径止于中间层，如 {{p1.result.data}}）→ node.toString()（紧凑 JSON 串）
  {{pN.result}}（无 path）→ bodyRaw 原文
```

"递归"即任意深度点分路径，无单层深度限制；解析一次性完成，热路径无重复解析开销（2.3 懒解析缓存）。

### 2.8 校验与可观测

- **启动校验**（`Scenario.of()`）：占位符引用的步骤序号越界、别名不存在 → 日志 WARN（不 fail-fast，压测配置常由 AI 生成，宽松处理更可用），并在任务结果 JSON 附 `warnings` 数组；
- **运行期**：引用未命中保留原文，属用户可感知信号；连续未命中可在分步骤统计中记 `renderMiss` 计数（新增到 `StressStatistician.StepStats`，非必选，列为可选项）；
- **脱敏**：日志绝不打印引用命中的具体值（沿用 `describe()` 只记长度的方式）。

### 2.9 兼容性

| 场景 | 行为 |
| ---- | ---- |
| 老任务（仅 extract + {{var}}） | 不受影响，TemplateRenderer 中 b 分支兜底 |
| 老任务里变量名恰为 `a.b.c` 形式 | 非 `pN.` 前缀且非已注册别名 → 走扁平变量，正确解析 |
| `JmeterStressEngine` | 不支持 scenario（现状），不改 |
| AI Function Calling 工具描述 | 需同步更新 stress 工具的参数 schema 说明，让 AI 直接产出新语法 |
| 前端 | alias 输入项可选；占位符提示文案补充新语法 |

### 2.10 测试计划

单测（`perf-server/src/test/java/com/perfserver/stress/`）：

1. `TemplateRendererTest`：
   - pN 序号引用（header/result/递归路径/数组下标/整体 result）；
   - alias 引用、alias 未注册回退扁平变量、`p0` 非法保留原文；
   - 未命中保留原文；`{{var}}` 与新语法混用；含 `}}` 边界与空占位符；
2. `StepContextStoreTest`：懒解析、非 JSON 响应、截断、原子替换并发读写（多线程压一压）、错误响应入池；
3. `StressScenarioTest` 扩展：两步场景端到端（MockHttpServer 模拟登录返回 token → 下游引用发请求断言请求头携带）。

验收脚本：扩展 `scripts/` 下压测自验，用 perf-demo 的登录/下单接口串联验证。

### 2.11 实施拆分

| 阶段 | 内容 |
| ---- | ---- |
| P1 | `StepContextStore` + `TemplateRenderer` + 引擎接入（pN 引用全量能力）+ 单测 |
| P2 | alias 字段、启动校验、warnings、统计计数（可选） |
| P3 | AI 工具 schema 更新 + 前端表单提示 + 文档归档 |

---

## 三、示例

```json
{
  "engineType": "http",
  "durationSec": 120,
  "concurrency": 20,
  "scenario": [
    {
      "alias": "login",
      "name": "登录",
      "url": "{{baseUrl}}/api/login",
      "method": "POST",
      "headers": { "Content-Type": "application/json" },
      "body": "{\"username\":\"u1\",\"password\":\"p1\"}"
    },
    {
      "alias": "order",
      "name": "下单",
      "weight": 3,
      "url": "{{baseUrl}}/api/order",
      "method": "POST",
      "headers": {
        "Authorization": "Bearer {{p1.result.data.token}}",
        "X-Trace-Id": "{{p1.header.X-Request-Id}}"
      },
      "body": "{\"sku\":\"S001\",\"userId\":\"{{login.result.data.userId}}\"}"
    },
    {
      "alias": "detail",
      "url": "{{baseUrl}}/api/order/{{order.result.data.orderId}}",
      "headers": { "Authorization": "Bearer {{p1.result.data.token}}" }
    }
  ]
}
```

三个接口混合发压；下单与详情直接引用上一步响应体/响应头，零 extract 声明。
