# PerformanceTestor P0/P1 评审缺陷精准修复提示词

> **适用基线**：`main` 分支提交 `5fd0a638`  
> **修复目标**：一次性收口代码评审发现的 **4 处高危缺陷**（时序竞态、DSN 凭据丢失、探针并发/栈破坏、SSH 安全）与 **3 处中危隐患**（反射可访问性、非整型主键导出、存量库迁移 SQL），确保 P0/P1 闭环 100% 达标并稳妥进入 P2。

---

## 一、修复总览与优先级

| 编号 | 级别 | 涉及文件 | 核心问题 |
| --- | --- | --- | --- |
| **Fix-1** | 高危 · 竞态 | `perf-py/server/stress_service.py`<br>`perf-py/server/app.py`<br>`perf-py/server/dataset_service.py` | `resetPolicy="before_and_after"` 在 `submit()` 之后才执行 `before` 清理，且未等待异步 `build()` 完成即开压 |
| **Fix-2** | 高危 · 必败 | `perf-py/server/dataset_pipeline_factory.py`<br>`perf-py/server/app.py` | 回退 `db_conn` 表时丢失 `username` 与解密后的 `password`；`app.py` 用 `PTDE_REALDB_URL` 环境变量拦截了工厂装配 |
| **Fix-3** | 高危 · 并发 | `perf-agent/.../trace/TraceNode.java`<br>`perf-agent/.../trace/TraceContext.java` | `TraceNode.children` 非线程安全导致异步并发 `CME`；嵌套 `detach(prev)` 新建单节点 `Ctx` 破坏外层完整调用栈 |
| **Fix-4** | 高危 · 安全 | `perf-py/server/deploy_api.py`<br>`perf-py/server/deploy_service.py` | `deploy_conn` 密码/私钥明文落库；SSH 远端命令未做 `shlex.quote()` 转义存在命令注入风险 |
| **Fix-5** | 中危 · 兼容 | `perf-agent/.../trace/TraceContext.java` | `GET_HEADER_CACHE` 缺 `setAccessible(true)`，在非 `public` ServletRequest 包装类上染色头静默变 `null` |
| **Fix-6** | 中危 · 健壮 | `perf-py/server/dataset_sample.py` | `_fetch_rows` 强转 `int(bounds[0])` 在 UUID/`VARCHAR` 主键表上抛 `ValueError` |
| **Fix-7** | 中危 · 运维 | `sql/migrate_p0_p1.sql`（新建） | 存量 MySQL 库升级缺少幂等 `ALTER TABLE` 迁移脚本，直接启动会报 `Unknown column` |

---

## 二、逐项精准修复要求与代码指引

### Fix-1：修复 `dataset_reset` 时序竞态与异步 `build()` 未等待

#### 问题位置
- `perf-py/server/stress_service.py`（`create_and_run` 与 `_schedule_dataset_reset`）
- `perf-py/server/app.py`（`_dataset_reset`）

#### 改动要求
1. **`perf-py/server/stress_service.py`**：
   - 将 `_schedule_dataset_reset` 拆分为两个明确阶段：
     - `_run_before_reset(task_id, binding)`：**必须在 `self._runner.submit(task_id, payload)` 之前同步执行**（当 `resetPolicy == "before_and_after"` 时）。若 `before` 重置抛异常，直接将任务标为 `failed` 或向上抛出拒绝本次发压（绝不在空库/坏库上盲目发压）。
     - `_schedule_after_reset(task_id, binding)`：保留在 `self._runner.submit(task_id, payload)` 之后，后台线程轮询任务终局（`finished`/`failed`/`cancelled`）再触发 `after`。
2. **`perf-py/server/app.py` & `dataset_service.py`**：
   - 在 `DatasetLifecycleService` 增加同步等待辅助方法 `wait_until_settled(instance_id: str, timeout_sec: float = 600.0) -> str`：获取 `self._tasks.get(row.id)` 的 `Future` 并 `future.result(timeout=timeout_sec)`，返回最终状态。
   - **关键检查**：检查 `dataset_service.py` 的 `cleanup()` 后实例状态（`CLEANED`）与 `build()` 的前置状态校验（当前 `build()` 第 307 行要求 `row.status == "PLANNED"`）！若 `cleanup()` 后直接调 `build()` 会因状态非 `PLANNED` 报错，请在 `DatasetLifecycleService` 增加合规的状态重置或按既有状态机重建新实例/允许从 `CLEANED` 回到可构建态（须同步核对 `ptde/models/dataset.py` 的 `ALLOWED_TRANSITIONS`）。
   - 在 `app.py` 的 `_dataset_reset(instance_id, phase)` 中：`cleanup(...)` → `build(...)` → **`wait_until_settled(instance_id)`**，若终态不是 `"READY"` 则显式抛出异常。

#### 回归单测
- 在 `perf-py/tests/test_stress_ops.py` 新增用例：记录事件顺序列表 `events`，验证 `resetPolicy="before_and_after"` 时严格满足 `events == ["before_reset_done", "runner_submit", "after_reset_done"]`。

---

### Fix-2：修复 `dataset_pipeline_factory.py` 回退 `db_conn` 丢失凭据 & `app.py` 装配条件

#### 问题位置
- `perf-py/server/dataset_pipeline_factory.py:42-83`
- `perf-py/server/app.py:155-165`

#### 改动要求
1. **`perf-py/server/dataset_pipeline_factory.py`**：
   - 修改 `jdbc_url_to_pymysql` 签名与实现，支持拼入用户名与密码（URL 编码防密码含 `@`/`/`/`:`/`#` 破坏 DSN 解析）：
     ```python
     from urllib.parse import quote, urlsplit, urlunsplit
     from server.crypto import decrypt

     def jdbc_url_to_pymysql(url: str, username: str | None = None,
                             password: str | None = None) -> str:
         prefix = "jdbc:mysql://"
         converted = "mysql+pymysql://" + url[len(prefix):] if url.startswith(prefix) else url
         if not username:
             return converted
         parts = urlsplit(converted)
         if parts.username:  # URL 自身已带凭据则不覆盖
             return converted
         user_q = quote(username, safe="")
         auth = f"{user_q}:{quote(password, safe='')}" if password else user_q
         netloc = f"{auth}@{parts.hostname}" + (f":{parts.port}" if parts.port else "")
         return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
     ```
   - 在 `resolve_target_dsn()` 遍历 `DbConnModel` 时，传入 `row.username` 和 `decrypt(row.password) if row.password else None`：
     ```python
     dsn = jdbc_url_to_pymysql(
         row.db_url,
         username=row.username,
         password=decrypt(row.password) if row.password else None,
     )
     ```
   - **安全注意点**：错误信息与日志中严禁打印含明文密码的完整 `dsn`。
2. **`perf-py/server/app.py`**：
   - 修改第 159–164 行：去掉 `and os.environ.get("PTDE_REALDB_URL")` 的外层限制！只要未显式注入 `dataset_pipeline` 和 `dataset_pipeline_factory`，且非内存测试库（`":memory:" not in resolved_db_url` 或由单测决定），均默认装配 `realdb_pipeline_factory(app.state.dataset_repo)`。
   - **契约注意点**：检查 `tests/test_server_dataset_api.py` 等既有单测是否依赖 `:memory:` 库下默认挂载 `UnwiredPipeline` 返回「数据集执行体未接线」——若依赖，保留 `":memory:" not in resolved_db_url or bool(os.environ.get("PTDE_REALDB_URL"))` 条件，即可同时保证生产 MySQL 下只配 `db_conn` 也能自动生效，且不破坏内存库单测。

#### 回归单测
- 在 `perf-py/tests/test_p0_closeout.py` 新增用例：清空 `PTDE_REALDB_URL`，向 `db_conn` 插入带加密密码（含特殊字符如 `p@ss/123`）的 `jdbc:mysql://127.0.0.1:3306/demo` 记录，断言 `resolve_target_dsn()` 返回的 DSN 正确包含 URL 编码后的用户名与解密密码。

---

### Fix-3：修复 `TraceContext` 异步并发修改与嵌套 `detach` 破栈（+ Fix-5 反射可访问性）

#### 问题位置
- `perf-agent/src/main/java/com/perfagent/trace/TraceNode.java:24`
- `perf-agent/src/main/java/com/perfagent/trace/TraceContext.java:48-58, 290-370`

#### 改动要求
1. **`TraceNode.java`**：
   - 将 `public final List<TraceNode> children = new ArrayList<>();` 改为 `public final List<TraceNode> children = new java.util.concurrent.CopyOnWriteArrayList<>();`（异步子线程与 `MetricsReporter` 遍历线程序列化并发安全，且大多数节点子节点数很少，读多写少开销极低）。
2. **`TraceContext.java`（保持 `attach` / `detach` 方法签名不变，100% 兼容 `TraceContextAsyncTest`）**：
   - 在 `Snapshot` 中新增包级私有字段 `final Ctx previousCtx;`（记录 `attach` 之前线程原本的 `Ctx` 引用，原样保留完整调用栈、`filterOwned` 与 `nodeCount`）。
   - 修改 `attach(Snapshot snapshot)`：
     ```java
     public static Snapshot attach(Snapshot snapshot) {
         Ctx existing = CTX.get();
         if (snapshot == null || existing != null) {
             // 已有上下文不覆盖：把原 Ctx 引用暂存进返回值，供 detach 原样归还（不动原栈）
             return existing == null ? null : Snapshot.ofExisting(existing);
         }
         Ctx ctx = new Ctx(false);
         ctx.sampled = snapshot.sampled;
         ctx.traceId = snapshot.traceId;
         ctx.stressTaskId = snapshot.stressTaskId;
         ctx.rootStartNanos = System.nanoTime();
         if (snapshot.sampled && snapshot.parentNode != null) {
             ctx.stack.push(snapshot.parentNode);
         }
         CTX.set(ctx);
         return null; // 表示 attach 前无上下文，detach(null) 时直接 CTX.remove()
     }
     ```
   - 修改 `detach(Snapshot prev)`：
     ```java
     public static void detach(Snapshot prev) {
         if (prev == null) {
             CTX.remove();
             return;
         }
         if (prev.previousCtx != null) {
             CTX.set(prev.previousCtx); // 原样恢复外层 Ctx（不丢失原有调用栈与 filterOwned）
             return;
         }
         // 兼容外部直接传入手动 capture() 快照的旧调用
         Ctx ctx = new Ctx(false);
         ctx.sampled = prev.sampled;
         ctx.traceId = prev.traceId;
         ctx.stressTaskId = prev.stressTaskId;
         ctx.rootStartNanos = System.nanoTime();
         if (prev.sampled && prev.parentNode != null) {
             ctx.stack.push(prev.parentNode);
         }
         CTX.set(ctx);
     }
     ```
3. **`TraceContext.java`（Fix-5：`GET_HEADER_CACHE` 反射可访问性）**：
   - 在 `GET_HEADER_CACHE.computeValue(Class<?> type)` 中：优先查找 `type.getMethod("getHeader", String.class)` 后调用 `m.setAccessible(true)`（若受模块限制则遍历 `type.getInterfaces()` 查找 `public` 接口上的 `getHeader(String)`），避免在非 `public` ServletRequest 实现类上调用时报 `IllegalAccessException` 被静默吞掉。

#### 回归单测
- 在 `TraceContextAsyncTest.java` 新增两例：
  1. **嵌套栈保护**：外层线程已有 2 层调用栈且 `filterOwned=true`，调用 `attach(foreign)` 后再 `detach(prev)`，断言外层 2 层调用栈完整保留且 `currentMethodChain().size() == 2`；
  2. **非 public Request 包装类**：定义包级私有内部类实现 `public String getHeader(String name)`，断言 `extractStressTaskId(req)` 能正常提取 `X-Perf-Stress-Task-Id`。

---

### Fix-4：修复 `deploy_service.py` 凭据明文落库与 SSH 命令注入

#### 问题位置
- `perf-py/server/deploy_api.py:32-47`
- `perf-py/server/deploy_service.py:25-48, 125-190`

#### 改动要求
1. **凭据加密存储与取用点解密**：
   - `perf-py/server/deploy_api.py` 的 `add_conn`：入库前对非空 `password` 和 `private_key` 调用 `server.crypto.encrypt()`（与 `dbconn_service.py` 完全一致）。
   - `perf-py/server/deploy_service.py`：
     - `ParamikoRunner._client(conn)` 取用凭据时调用 `decrypt(conn.password)` / `decrypt(conn.private_key)`（注意：`test_conn(config)` 构造的未落库临时对象传的是明文，建议 `decrypt` 本身若已支持明文透传则直接复用，否则仅在 `find_by_id()` 从库读取的记录上解密，避免双重解密或明文报错——请先核对 `server/crypto.py` 的 `decrypt()` 对未加密前缀的行为）。
2. **SSH 远端命令全部经 `shlex.quote()` 转义**：
   - `deploy_service.py` 引入 `import shlex`。
   - 对 `app_port` 做强校验：`port = int(app_port)` 且 `1 <= port <= 65535`，否则抛 `DeployError`。
   - 所有拼接到远端 shell 的变量均使用 `shlex.quote(...)`：
     - `mkdir -p {shlex.quote(work)}`
     - `test -f {shlex.quote(remote_agent)} && echo ok`
     - `launch` 命令中的 `work`、`-javaagent:{remote_agent}=reportUrl={report_url},appName={name},appPort={port}`、`remote_jar`、`{work}/app.log` 全部经 `shlex.quote()` 包裹。
     - `status()` 中的 `pgrep` 匹配串、`tail -n 50 {shlex.quote(f"{work}/app.log")}` 同样转义。

#### 回归单测
- 新增单测验证：
  1. `POST /api/deploy/conns` 落库后数据库原列 `password` / `private_key` 非明文；
  2. `work_dir` 或 `app_name` 含 `; id` / 空格时，传给 `FakeRunner.exec()` 的命令中参数已被单引号安全转义，不发生命令注入。

---

### Fix-6：`dataset_sample.py` 兼容非整型主键（UUID / VARCHAR）

#### 问题位置
- `perf-py/server/dataset_sample.py:60-85`

#### 改动要求
- 在 `_fetch_rows()` 中，尝试 `lo, hi = int(bounds[0]), int(bounds[1])`；若捕获 `(TypeError, ValueError)`（说明主键为 UUID / 字符串 / 复合非数值列），优雅降级为：
  ```python
  cols = ", ".join(f"`{n}`" for n in names)
  cur.execute(
      f"SELECT {cols} FROM `{table}` ORDER BY `{key_col}` ASC LIMIT %s",
      [limit],
  )
  return [list(r) for r in cur.fetchall()]
  ```
- 对数值主键的返回排序保持按数值顺序（`key=lambda r: r[pk_idx]` 而非 `str(r[0])` 字典序）。

---

### Fix-7：补充存量 MySQL 库幂等升级脚本 `sql/migrate_p0_p1.sql`

#### 改动要求
- 新建 `sql/migrate_p0_p1.sql`：使用 MySQL 8 兼容的幂等存储过程（检查 `information_schema.COLUMNS` 与 `information_schema.STATISTICS`，不存在才执行 `ALTER TABLE`），补齐 P0/P1 新增的列与索引：
  - `stress_task.request_json MEDIUMTEXT NULL`
  - `trace_chain.stress_task_id BIGINT NULL` + `INDEX idx_trace_stress_task (stress_task_id, start_time)`
  - `slow_sql.stress_task_id BIGINT NULL` + `INDEX idx_slow_sql_stress_task (stress_task_id, execute_time)`
  - `slow_command.stress_task_id BIGINT NULL` + `INDEX idx_slow_cmd_stress_task (stress_task_id, execute_time)`
  - `jvm_metric.extra_metrics TEXT NULL`
  - 四张时序表的 `(app_name, <time_col>, id)` 游标分页复合索引。
- 保证脚本**可重复执行（幂等）**，不会在已建列/索引的库上报错。

---

## 三、提交前验证命令

```bash
# 1. Java 探针全量单测
mvn -pl perf-agent test

# 2. Python 全量单测 + lint
cd perf-py && uv run pytest && uv run ruff check .

# 3. 压测引擎专项测试
cd perf-py && PERF_PY_ENGINES=1 uv run pytest tests/test_locust_engine.py tests/test_jmeter_engine.py
```

完成后请输出：改动文件清单、各测试命令实际通过数，以及 `dataset_service.py` 中 `CLEANED → build()` 状态机流转的确认说明。
