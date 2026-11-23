"""P1-2 AI 观测查询工具测试：jvm_metrics_query / slow_sql_query / trace_query /
hotspot_query / db_explain_run 五个只读工具 + ROLE_ANALYSIS 接入。

验收标准：
- 传 stressTaskId 后自动解析压测时间窗并串联调用（FakeLlm 脚本化验证）；
- 无染色数据时降级为时间窗查询并标注 matchedBy=time_window_fallback；
- 非法 SQL（非 SELECT/WITH、分号注入）被 DbExplainService 拦截返回 {"error": ...}；
- 工具层强制聚合摘要 + Top N 上限 + SQL 文本截断（防撑爆上下文）。
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from agents.llm import ChatResult, LlmClient, ToolCall
from agents.roles import ROLE_ANALYSIS
from server.collector_repo import (
    CollectorRepository,
    DbConnModel,
    HotspotMethodModel,
    JvmMetricModel,
    SlowSqlModel,
    TraceChainModel,
)
from server.crypto import encrypt
from server.db import make_engine
from server.db_explain import DbExplainService
from server.stress_repo import Base, StressRepository, StressTaskModel
from tools.observability_tools import build_observability_tools
from tools.registry import Tool, call_tool

NOW = datetime(2026, 11, 20, 12, 0, 0)


def _factory() -> sessionmaker:
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    Base.metadata.create_all(factory.kw["bind"])
    return factory


@pytest.fixture()
def repos():
    factory = _factory()
    return (CollectorRepository(factory), StressRepository(factory),
            DbExplainService(), factory)


def _seed_task(repo: StressRepository, app: str = "demo-app") -> int:
    with repo._sessions() as s:
        task = StressTaskModel(
            task_name="t1", target_url="http://demo/api", app_name=app,
            status="finished", create_time=NOW - timedelta(minutes=5),
            finish_time=NOW)
        s.add(task)
        s.commit()
        return task.id


def _tools(repos):
    collector_repo, stress_repo, db_explain, _ = repos
    return {t.name: t for t in build_observability_tools(collector_repo, stress_repo, db_explain)}


def _call(tools, name, args):
    return call_tool(tools, name, args)


# ---------------------------------------------------------- jvm_metrics_query


def test_jvm_metrics_query_by_stress_task_aggregates_summary(repos):
    collector_repo, stress_repo, _, factory = repos
    task_id = _seed_task(stress_repo)
    t0 = NOW - timedelta(minutes=5)
    with factory() as s:
        s.add_all([
            JvmMetricModel(app_name="demo-app", collect_time=t0, heap_used_mb=200,
                           heap_max_mb=1024, young_gc_count=10, young_gc_ms=100,
                           full_gc_count=1, full_gc_ms=200, thread_count=50,
                           cpu_usage=30, system_cpu=40),
            JvmMetricModel(app_name="demo-app", collect_time=NOW, heap_used_mb=800,
                           heap_max_mb=1024, young_gc_count=20, young_gc_ms=300,
                           full_gc_count=3, full_gc_ms=900, thread_count=120,
                           cpu_usage=80, system_cpu=90),
        ])
        s.commit()
    out = _call(_tools(repos), "jvm_metrics_query", {"stressTaskId": task_id})
    assert out["matchedBy"] == "stress_task"
    assert out["app"] == "demo-app"
    assert out["heap"]["peakUsedMb"] == 800.0
    assert out["heap"]["peakUsagePct"] == 78.1  # 800/1024
    assert out["gc"]["fullGcCountDelta"] == 2
    assert out["gc"]["fullGcMsDelta"] == 700
    assert out["threads"]["peakCount"] == 120
    assert out["cpu"]["jvmPeakPct"] == 80.0
    # 聚合摘要不携带原始时序
    assert "items" not in out


def test_jvm_metrics_query_by_app_window(repos):
    collector_repo, stress_repo, _, factory = repos
    with factory() as s:
        s.add(JvmMetricModel(app_name="demo-app", collect_time=datetime.now(),
                             heap_used_mb=300, heap_max_mb=1024, young_gc_count=0,
                             young_gc_ms=0, full_gc_count=0, full_gc_ms=0,
                             thread_count=40, cpu_usage=10, system_cpu=20))
        s.commit()
    out = _call(_tools(repos), "jvm_metrics_query", {"app": "demo-app", "minutes": 30})
    assert out["matchedBy"] == "app_window"
    assert out["heap"]["peakUsedMb"] == 300.0


def test_jvm_metrics_query_requires_args(repos):
    out = _call(_tools(repos), "jvm_metrics_query", {})
    assert "error" in out


# ---------------------------------------------------------- slow_sql_query


def test_slow_sql_query_top_n_and_truncation(repos):
    collector_repo, stress_repo, _, factory = repos
    task_id = _seed_task(stress_repo)
    t0 = NOW - timedelta(minutes=1)
    long_sql = "SELECT " + "x" * 800
    with factory() as s:
        s.add_all([
            SlowSqlModel(app_name="demo-app", sql_text=long_sql, cost_ms=100,
                         source="jdbc", db_host="", execute_time=t0, stress_task_id=task_id),
            SlowSqlModel(app_name="demo-app", sql_text="SELECT * FROM t_order", cost_ms=900,
                         source="jdbc", db_host="127.0.0.1:3306/demo", execute_time=t0,
                         stress_task_id=task_id),
        ])
        s.commit()
    out = _call(_tools(repos), "slow_sql_query", {"stressTaskId": task_id})
    assert out["matchedBy"] == "stress_task"
    assert [i["costMs"] for i in out["items"]] == [900, 100]  # costMs 降序
    assert len(out["items"][1]["sqlText"]) == 500  # 超长 SQL 截断至 500 字符


def test_slow_sql_query_falls_back_to_time_window(repos):
    """旧探针未染色：无 stress_task_id 命中时按任务时间窗降级并标注 matchedBy。"""
    collector_repo, stress_repo, _, factory = repos
    task_id = _seed_task(stress_repo)
    with factory() as s:
        s.add(SlowSqlModel(app_name="demo-app", sql_text="legacy sql", cost_ms=800,
                           source="jdbc", db_host="", execute_time=NOW - timedelta(seconds=3),
                           stress_task_id=None))
        s.commit()
    out = _call(_tools(repos), "slow_sql_query", {"stressTaskId": task_id})
    assert out["matchedBy"] == "time_window_fallback"
    assert [i["sqlText"] for i in out["items"]] == ["legacy sql"]


# ---------------------------------------------------------- trace_query / hotspot_query


def test_trace_query_returns_summary_without_tree(repos):
    collector_repo, stress_repo, _, factory = repos
    task_id = _seed_task(stress_repo)
    with factory() as s:
        s.add_all([
            TraceChainModel(trace_id="t1", app_name="demo-app", root_method="A#m1",
                            trace_json='{"big": true}', total_ms=50,
                            start_time=NOW - timedelta(seconds=5), stress_task_id=task_id),
            TraceChainModel(trace_id="t2", app_name="demo-app", root_method="B#m2",
                            trace_json="{}", total_ms=500,
                            start_time=NOW - timedelta(seconds=3), stress_task_id=task_id),
        ])
        s.commit()
    out = _call(_tools(repos), "trace_query", {"stressTaskId": task_id})
    assert [i["traceId"] for i in out["items"]] == ["t2", "t1"]  # totalMs 降序
    assert set(out["items"][0]) == {"traceId", "rootMethod", "totalMs",
                                    "startTime", "stressTaskId"}
    assert "traceJson" not in out["items"][0]


def test_hotspot_query_by_task_window(repos):
    collector_repo, stress_repo, _, factory = repos
    task_id = _seed_task(stress_repo)
    with factory() as s:
        s.add_all([
            HotspotMethodModel(app_name="demo-app", method_name="hot.method",
                               samples=100, sample_time=NOW - timedelta(seconds=10)),
            HotspotMethodModel(app_name="demo-app", method_name="cold.method",
                               samples=5, sample_time=NOW - timedelta(hours=2)),
        ])
        s.commit()
    out = _call(_tools(repos), "hotspot_query", {"stressTaskId": task_id})
    names = [r["methodName"] for r in out["items"]]
    assert names == ["hot.method"]  # 时间窗外的冷方法不混入


# ---------------------------------------------------------- db_explain_run


class _FakeDbExplain:
    """替身：仅记录 analyze_sql 收到的（解密后）密码。"""

    def __init__(self):
        self.captured = {}

    def analyze_sql(self, db_url, username, password, sql_text):
        self.captured = {"dbUrl": db_url, "username": username,
                         "password": password, "sql": sql_text}
        return {"explainRows": [{"id": 1, "type": "ALL"}], "indexes": {"t_order": "PRIMARY"},
                "tableRows": {"t_order": 50000}, "explainTables": ["t_order"]}

    def format_summary(self, explain):
        return "EXPLAIN 执行计划：ALL"

    def url_host(self, db_url):
        return "127.0.0.1:3306/demo"


def _seed_conn(repo: CollectorRepository, factory, **over):
    row = DbConnModel(conn_name=over.get("conn_name", "conn-1"),
                      host=over.get("host", "127.0.0.1:3306/demo"),
                      app_name=over.get("app_name", "demo-app"),
                      db_url=over.get("db_url", "jdbc:mysql://127.0.0.1:3306/demo"),
                      username="root", password=encrypt("plain-secret"),
                      is_default=over.get("is_default", 0))
    with factory() as s:
        s.add(row)
        s.commit()
        return row.id


def test_db_explain_run_decrypts_password_at_use_point(repos):
    collector_repo, stress_repo, _, factory = repos
    _seed_conn(collector_repo, factory)
    fake = _FakeDbExplain()
    tools = {t.name: t for t in build_observability_tools(collector_repo, stress_repo, fake)}
    out = _call(tools, "db_explain_run", {"sql": "SELECT * FROM t_order WHERE status = 'PAID'"})
    assert "summary" in out
    assert fake.captured["password"] == "plain-secret"  # 取用点解密
    assert fake.captured["sql"] == "SELECT * FROM t_order WHERE status = 'PAID'"
    # 密码明文不进返回值
    assert "plain-secret" not in str(out)


def test_db_explain_run_rejects_non_select_and_injection(repos):
    collector_repo, stress_repo, db_explain, factory = repos
    _seed_conn(collector_repo, factory)
    tools = {t.name: t for t in build_observability_tools(collector_repo, stress_repo, db_explain)}
    out = _call(tools, "db_explain_run", {"sql": "DROP TABLE t_order"})
    assert "error" in out
    out = _call(tools, "db_explain_run",
                {"sql": "SELECT 1; DROP TABLE t_order"})
    assert "error" in out
    assert "分号" in out["error"]


def test_db_explain_run_host_matching_priority(repos):
    collector_repo, stress_repo, _, factory = repos
    _seed_conn(collector_repo, factory, conn_name="app-conn", app_name="demo-app",
               host="10.0.0.1:3306/a")
    _seed_conn(collector_repo, factory, conn_name="host-conn", app_name="other",
               host="10.0.0.2:3306/demo", is_default=0)
    captured = {}
    fake = _FakeDbExplain()
    orig = fake.analyze_sql

    def spy(db_url, username, password, sql_text):
        captured["dbUrl"] = db_url
        return orig(db_url, username, password, sql_text)

    fake.analyze_sql = spy
    tools = {t.name: t for t in build_observability_tools(collector_repo, stress_repo, fake)}
    out = _call(tools, "db_explain_run",
                {"sql": "SELECT 1", "dbHost": "10.0.0.2:3306/demo"})
    assert "summary" in out
    assert out["connName"] == "host-conn"  # host 双向包含优先于应用绑定


# ---------------------------------------------------------- 角色接入与 Agent 串联


def test_role_analysis_includes_observability_tools():
    for name in ("jvm_metrics_query", "slow_sql_query", "trace_query",
                 "hotspot_query", "db_explain_run"):
        assert name in ROLE_ANALYSIS.tool_names
    assert "stressTaskId" in ROLE_ANALYSIS.system_prompt


class ScriptedLlm(LlmClient):
    """脚本化假 LLM：依次发起工具调用，验证 Agent 自动串联观测工具。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat(self, messages, tools):
        self.calls.append([t["function"]["name"] for t in (tools or [])])
        return self.script.pop(0)


def test_agent_chains_observability_tools_by_stress_task_id(repos):
    collector_repo, stress_repo, _, factory = repos
    task_id = _seed_task(stress_repo)
    _seed_conn(collector_repo, factory)
    with factory() as s:
        s.add(SlowSqlModel(app_name="demo-app", sql_text="SELECT * FROM t_order",
                           cost_ms=900, source="jdbc", db_host="127.0.0.1:3306/demo",
                           execute_time=NOW - timedelta(seconds=30), stress_task_id=task_id))
        s.commit()
    tools = {t.name: t for t in build_observability_tools(collector_repo, stress_repo,
                                                          _FakeDbExplain())}
    # ROLE_ANALYSIS 白名单还引用另外 4 个域工具：补桩满足 Agent 完备性校验
    for n in ("capacity_analyze", "slowlog_analyze", "source_scan", "stress_result_read"):
        tools[n] = Tool(name=n, description="stub", parameters={},
                        handler=lambda args: {"stub": True})
    llm = ScriptedLlm([
        ChatResult(tool_calls=[ToolCall(id="1", name="jvm_metrics_query",
                                        arguments={"stressTaskId": task_id}),
                               ToolCall(id="2", name="slow_sql_query",
                                        arguments={"stressTaskId": task_id})]),
        ChatResult(tool_calls=[ToolCall(id="3", name="db_explain_run",
                                        arguments={"sql": "SELECT * FROM t_order",
                                                   "app": "demo-app"})]),
        ChatResult(content="根因：t_order 全表扫描"),
    ])
    from agents.react import Agent

    report = Agent("analysis", ROLE_ANALYSIS.prompt(), llm, tools,
                   tool_names=list(ROLE_ANALYSIS.tool_names)).run(
        f"分析压测任务 {task_id} 的性能瓶颈")
    assert report.final_answer == "根因：t_order 全表扫描"
    assert [s["tool"] for s in report.steps] == [
        "jvm_metrics_query", "slow_sql_query", "db_explain_run"]
    jvm = report.steps[0]["result"]
    sql = report.steps[1]["result"]
    explain = report.steps[2]["result"]
    assert jvm["matchedBy"] == "stress_task"
    assert sql["matchedBy"] == "stress_task"
    assert "summary" in explain


# ---------------------------------------------------------- app 装配与契约


def test_create_app_injects_observability_tools():
    from fastapi.testclient import TestClient

    from server.app import create_app

    factory = _factory()
    client = TestClient(create_app(
        stress_repo=StressRepository(factory),
        collector_repo=CollectorRepository(factory),
        auth_enabled=False))
    registry = client.app.state.tools_registry
    for name in ("jvm_metrics_query", "slow_sql_query", "trace_query",
                 "hotspot_query", "db_explain_run"):
        assert name in registry
    resp = client.get("/api/ai/tools").json()["data"]
    names = {t["name"] for t in resp}
    assert {"jvm_metrics_query", "db_explain_run"} <= names
    assert all(set(t) == {"name", "description"} for t in resp)


def test_create_app_setdefault_keeps_stub_tools():
    """测试注入的桩工具不被覆盖（registry.setdefault 语义）。"""
    from fastapi.testclient import TestClient

    from server.app import create_app

    factory = _factory()
    stub = Tool(name="slow_sql_query", description="stub", parameters={},
                handler=lambda args: {"stub": True})
    client = TestClient(create_app(
        stress_repo=StressRepository(factory),
        collector_repo=CollectorRepository(factory),
        registry={"slow_sql_query": stub},
        auth_enabled=False))
    assert client.app.state.tools_registry["slow_sql_query"] is stub
