"""Agent 编排层等价测试（阶段 4 步骤 2）：ReAct 循环 / 角色裁剪 / 流水线传递 /
stress_run 工具 / CLI。全部用假 LLM 客户端驱动，无网络依赖（stress_run 真实链路
仅指向本进程内 HTTP 服务）。
"""

import http.server
import json
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agents.llm import ChatResult, LlmClient, ToolCall
from agents.pipeline import run_pipeline
from agents.react import Agent, AgentReport
from agents.roles import ROLE_ADVISORY, ROLE_ANALYSIS, ROLE_STRESS, ROLES
from perf_cli.config import EXIT_GATE_FAILED, EXIT_INVOCATION_ERROR, EXIT_OK
from perf_cli.main import app
from tools.observability_tools import wire_observability_tools
from tools.registry import build_registry, call_tool

runner = CliRunner()


def _full_registry() -> dict:
    """完整注册表：域工具 + P1-2 在线观测工具（CLI agent 同款装配）。"""
    return wire_observability_tools(build_registry())


class FakeLlm(LlmClient):
    """脚本化假 LLM：按调用次数回放预设响应，记录收到的消息。"""

    def __init__(self, script: list[ChatResult]) -> None:
        self.script = list(script)
        self.calls: list[tuple[list[dict], list[dict]]] = []

    def chat(self, messages, tools):
        # 捕获快照：messages 为调用方持有的可变列表，直接存引用会被后续轮次污染
        self.calls.append(([dict(m) for m in messages], tools))
        return self.script.pop(0)


def _tool_call(i, name, args) -> ToolCall:
    return ToolCall(id=f"call-{i}", name=name, arguments=args)


def _final(text) -> ChatResult:
    return ChatResult(content=text)


# ===== ReAct 循环 =====


def test_react_tool_then_final():
    llm = FakeLlm([
        ChatResult(tool_calls=[_tool_call(1, "capacity_analyze",
                                          {"points": [{"concurrency": 10, "qps": 100, "p95Ms": 10},
                                                      {"concurrency": 20, "qps": 150, "p95Ms": 15}],
                                           "target_p95_ms": 50})]),
        _final("容量结论：目标内最大并发 20"),
    ])
    report = Agent("analysis", "s", llm, build_registry()).run("评估容量")
    assert report.final_answer == "容量结论：目标内最大并发 20"
    assert report.stopped_reason == "final"
    assert len(report.steps) == 1
    assert report.steps[0]["tool"] == "capacity_analyze"
    assert "error" not in report.steps[0]["result"]
    # tool 结果以 tool 角色消息回传（tool_call_id 配对）
    msgs = llm.calls[1][0]
    assert msgs[-1]["role"] == "tool" and msgs[-1]["tool_call_id"] == "call-1"


def test_react_max_steps_guard():
    class InfiniteLlm(LlmClient):
        def chat(self, messages, tools):
            return ChatResult(tool_calls=[_tool_call("x", "slowlog_analyze", {"content": "x"})])

    report = Agent("analysis", "s", InfiniteLlm(), build_registry(),
                   max_steps=3).run("任务")
    assert report.stopped_reason == "max_steps"
    assert report.final_answer is None
    assert len(report.steps) == 3


def test_agent_tool_subset_enforced():
    llm = FakeLlm([ChatResult(tool_calls=[_tool_call(1, "stress_run",
                                                    {"scenario_file": "x.yaml"})]),
                   _final("ok")])
    report = Agent("analysis", "s", llm, build_registry(),
                   tool_names=["capacity_analyze"]).run("任务")
    assert report.steps[0]["result"]["error"].startswith("未知工具"), "角色白名单外的工具被拒绝"


def test_agent_tool_subset_missing_raises():
    with pytest.raises(ValueError):
        Agent("x", "s", FakeLlm([]), build_registry(), tool_names=["no_such"])


def test_react_context_injected_into_user_message():
    llm = FakeLlm([_final("ok")])
    Agent("advisory", "s", llm, build_registry()).run("任务", context="上游结论")
    user_msg = llm.calls[0][0][1]
    assert "任务" in user_msg["content"] and "上游结论" in user_msg["content"]


def test_report_context_block():
    r = AgentReport(agent="analysis", task="t", final_answer="根因 X",
                    steps=[{"tool": "capacity_analyze", "result": {"maxQps": 6158}}])
    block = r.context_block()
    assert "根因 X" in block and "capacity_analyze" in block
    d = r.to_dict()
    assert d["finalAnswer"] == "根因 X" and d["stoppedReason"] == "final"


# ===== 角色定义 =====


def test_roles_tool_subsets():
    assert set(ROLE_STRESS.tool_names) == {"stress_run", "stress_result_read", "datagen_dry_run"}
    assert set(ROLE_ANALYSIS.tool_names) == {
        "capacity_analyze", "slowlog_analyze", "source_scan", "stress_result_read",
        # P1-2 在线观测查询工具
        "jvm_metrics_query", "slow_sql_query", "trace_query", "hotspot_query",
        "db_explain_run",
        # P2-1 火焰图热点路径（Fix-4）
        "profile_flamegraph_summary",
    }
    assert ROLE_ADVISORY.tool_names == (), "修复建议 Agent 纯推理"


# ===== 三角色流水线 =====


def test_pipeline_three_agents_serial():
    """假 LLM 全脚本：压测读结果 → 分析容量 → 修复建议（无工具纯推理）。"""
    script = [
        # stress 角色：直接读工件
        ChatResult(tool_calls=[_tool_call(1, "stress_result_read", {"path": "/tmp/none.json"})]),
        ChatResult(tool_calls=[_tool_call(2, "stress_result_read", {"path": "nope"})]),
        _final("压测完成：QPS 100，P95 200ms"),
        # analysis 角色：容量分析（直接内联分档点）
        ChatResult(tool_calls=[_tool_call(1, "capacity_analyze",
                                          {"points": [{"concurrency": 10, "qps": 100, "p95Ms": 120},
                                                      {"concurrency": 20, "qps": 120, "p95Ms": 300}],
                                           "target_p95_ms": 150})]),
        _final("证据链：P95 超阈值，容量上限低"),
        # advisory 角色：无工具直接给建议
        _final("根因 1（待验证）：索引缺失；修复提示词：..."),
    ]
    llm = FakeLlm(script)
    out = run_pipeline("对 /api/order/list 做压测并归因", llm, _full_registry())
    assert out["finalAdvice"].startswith("根因 1")
    assert not out["degraded"]
    assert [r["agent"] for r in out["reports"]] == [r.name for r in ROLES]
    # 上下游传递：分析 Agent 收到压测 Agent 结论；建议 Agent 收到分析结论
    analysis_user = llm.calls[3][0][1]["content"]
    assert "压测完成" in analysis_user, "上游上下文注入"
    advisory_user = llm.calls[5][0][1]["content"]
    assert "证据链" in advisory_user


def test_pipeline_none_answer_counts_as_final():
    class SilentLlm(LlmClient):
        def chat(self, messages, tools):
            return ChatResult(content=None)

    out = run_pipeline("任务", SilentLlm(), _full_registry())
    assert out["degraded"] is False, "空回答按 final 处理不截断"
    assert out["finalAdvice"] is None


# ===== stress_run 工具 =====


def test_stress_run_missing_scenario():
    out = call_tool(build_registry(), "stress_run", {})
    assert "缺少 scenario_file" in out["error"]


def test_stress_run_scenario_not_found():
    out = call_tool(build_registry(), "stress_run", {"scenario_file": "/tmp/nope.yaml"})
    assert "不存在" in out["error"]


def test_stress_run_invocation_error(tmp_path):
    """场景校验失败 → 引擎 exit 2 → 工具返回 error 字段。"""
    bad = tmp_path / "bad.yaml"
    bad.write_text("labels: []\n", encoding="utf-8")  # 缺 request/steps → 校验失败
    out = call_tool(build_registry(), "stress_run", {"scenario_file": str(bad)})
    assert out.get("exitCode") == 2 and "error" in out


def test_stress_result_read_unwraps_perf_engine_artifact(tmp_path):
    from engine.result import Sample, StressTaskResult

    r = StressTaskResult(task_id=0, engine_type="locust", qps=99.5,
                         samples=[Sample(sample_time=1, qps=99.5)])
    artifact = {"schema": "perf-py.result.v1", "scenario": "s",
                "generatedAt": "2026-11-13T00:00:00", "result": r.to_java_json()}
    f = tmp_path / "artifact.json"
    f.write_text(json.dumps(artifact), encoding="utf-8")
    out = call_tool(build_registry(), "stress_result_read", {"path": str(f)})
    assert "error" not in out
    assert out["qps"] == pytest.approx(99.5), "工件外壳自动解包"


# ===== CLI perf agent run =====


def test_agent_cli_no_llm_config_exit_2(monkeypatch):
    monkeypatch.delenv("PERF_LLM_MODEL", raising=False)
    result = runner.invoke(app, ["agent", "run", "任务"])
    assert result.exit_code == EXIT_INVOCATION_ERROR


def test_agent_cli_pipeline_with_fake_llm(monkeypatch, tmp_path):
    """LLM 指向本进程内假服务不现实——直接 monkeypatch run_pipeline 验证接线与退出码。"""

    fake = {"task": "t", "reports": [], "finalAdvice": "结论 A", "degraded": False}
    monkeypatch.setattr("agents.pipeline.run_pipeline", lambda *a, **kw: fake)
    monkeypatch.setenv("PERF_LLM_MODEL", "test-model")
    out_file = tmp_path / "report.json"
    result = runner.invoke(app, ["agent", "run", "任务", "--output", str(out_file)])
    assert result.exit_code == EXIT_OK
    assert json.loads(out_file.read_text(encoding="utf-8"))["finalAdvice"] == "结论 A"


def test_agent_cli_truncated_exit_1(monkeypatch):
    monkeypatch.setenv("PERF_LLM_MODEL", "test-model")
    monkeypatch.setattr("agents.pipeline.run_pipeline",
                        lambda *a, **kw: {"reports": [], "finalAdvice": None,
                                          "degraded": True})
    result = runner.invoke(app, ["agent", "run", "任务"])
    assert result.exit_code == EXIT_GATE_FAILED


def test_agent_cli_unknown_role_exit_2(monkeypatch):
    monkeypatch.setenv("PERF_LLM_MODEL", "test-model")
    result = runner.invoke(app, ["agent", "run", "任务", "--role", "nope"])
    assert result.exit_code == EXIT_INVOCATION_ERROR


# ===== stress_run 真实子进程链路（本进程内 HTTP 目标，1 秒级） =====


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # noqa: ANN002
        pass


def test_stress_run_e2e(tmp_path):
    import sys

    assert "gevent" not in sys.modules, \
        "父进程必须保持 gevent 未 patch（conftest 闸门保证），否则子进程 fork 会竞态死锁"
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        scenario = tmp_path / "s.yaml"
        scenario.write_text(
            f"name: agent-smoke\napp: agent-smoke\nrequest:\n  url: http://127.0.0.1:{port}/\n"
            "  method: GET\nload:\n  concurrency: 2\n  duration_sec: 1\n  warmup_sec: 0\n",
            encoding="utf-8")
        out = call_tool(build_registry(), "stress_run",
                        {"scenario_file": str(scenario), "timeout_sec": 120})
        if "error" in out and "locust" in (out.get("error") or "") + "".join(out.get("stderrTail") or []):
            pytest.skip("locust 未安装（uv sync --extra locust）")
        assert "error" not in out, out.get("stderrTail")
        assert out["exitCode"] == 0
        r = out["result"]
        assert r["totalRequests"] > 0 and r["engineType"] == "locust"
        assert Path(out["artifactPath"]).is_file()
    finally:
        srv.shutdown()
