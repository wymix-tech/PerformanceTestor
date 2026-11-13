"""Agent 工具层等价测试（阶段 4 步骤 1）：registry 分发 / 各域 handler /
OpenAI schema / MCP 协议行为。
"""

import json
import sqlite3  # noqa: F401  （预留：datagen 工具真实库路径走冒烟脚本）

import pytest

from tools.registry import Tool, build_registry, call_tool, openai_tools_schema

# ===== registry =====


def test_registry_unique_and_complete():
    reg = build_registry()
    names = set(reg)
    assert names == {"stress_result_read", "capacity_analyze", "slowlog_analyze",
                     "source_scan", "datagen_dry_run", "datagen_run"}, "工具名唯一且齐备"
    assert reg["datagen_run"].read_only is False, "写库工具必须标非只读"
    assert all(reg[n].read_only for n in names if n != "datagen_run")


def test_call_tool_unknown_name():
    reg = build_registry()
    out = call_tool(reg, "no_such_tool", {})
    assert out["error"].startswith("未知工具")
    assert "capacity_analyze" in out["available"]


def test_call_tool_handler_exception_captured():
    def boom(_args):
        raise RuntimeError("x")

    t = Tool(name="boom", description="d", parameters={}, handler=boom)
    out = call_tool({"boom": t}, "boom", {})
    assert out["error"] == "RuntimeError: x"


def test_openai_tools_schema_shape():
    schema = openai_tools_schema(build_registry())
    names = [s["function"]["name"] for s in schema]
    assert names == sorted(names), "按名称稳定排序"
    for s in schema:
        assert s["type"] == "function"
        fn = s["function"]
        assert fn["name"] and fn["description"]
        assert fn["parameters"]["type"] == "object"
        assert fn["parameters"]["additionalProperties"] is False


# ===== stress_result_read =====


def test_stress_result_read_roundtrip(tmp_path):
    from engine.result import StagePoint, StressTaskResult

    r = StressTaskResult(task_id=7, engine_type="locust", target_url="http://x/api",
                         qps=123.4, p95_ms=55.6, p99_ms=88.1, error_rate=0.01,
                         stage_points=[StagePoint(concurrency=10, qps=99, p95_ms=40)])
    f = tmp_path / "result.json"
    f.write_text(json.dumps(r.to_java_json()), encoding="utf-8")
    out = call_tool(build_registry(), "stress_result_read", {"path": str(f)})
    assert out["qps"] == pytest.approx(123.4)
    assert out["p95Ms"] == pytest.approx(55.6)
    assert out["stagePoints"][0]["p95Ms"] == 40, "分档点输出 camelCase"
    assert out["taskId"] == 7


def test_stress_result_read_missing_file(tmp_path):
    out = call_tool(build_registry(), "stress_result_read",
                    {"path": str(tmp_path / "nope.json")})
    assert "error" in out


# ===== capacity_analyze =====


def _stage(conc, qps, p95):
    return {"concurrency": conc, "qps": qps, "p95Ms": p95}


def test_capacity_analyze_with_points():
    points = [_stage(10, 180, 100), _stage(20, 350, 140), _stage(30, 380, 400)]
    out = call_tool(build_registry(), "capacity_analyze",
                    {"points": points, "target_p95_ms": 200})
    assert "error" not in out
    assert out["maxQps"] > 0
    assert out["advice"], "面向 Agent 的快捷结论"


def test_capacity_analyze_from_result_file(tmp_path):
    f = tmp_path / "r.json"
    f.write_text(json.dumps({"stagePoints": [_stage(10, 180, 100), _stage(20, 350, 140)]}),
                 encoding="utf-8")
    out = call_tool(build_registry(), "capacity_analyze",
                    {"result_file": str(f), "target_p95_ms": 150})
    assert "error" not in out


def test_capacity_analyze_insufficient_points():
    out = call_tool(build_registry(), "capacity_analyze",
                    {"points": [_stage(10, 1, 1)], "target_p95_ms": 100})
    assert "不足" in out["error"]


def test_capacity_analyze_missing_input():
    out = call_tool(build_registry(), "capacity_analyze", {"target_p95_ms": 100})
    assert "二选一" in out["error"]


# ===== slowlog_analyze =====

_SLOWLOG = """# Time: 2026-11-13T10:00:00
# User@Host: perf[perf] @  [10.0.0.8]  Id: 100
# Query_time: 2.5  Lock_time: 0.1  Rows_sent: 10  Rows_examined: 100000
SET timestamp=1763008800;
SELECT * FROM perf_order WHERE user_id = 1;
# Time: 2026-11-13T10:00:10
# User@Host: perf[perf] @  [10.0.0.8]  Id: 101
# Query_time: 0.8  Lock_time: 0.0  Rows_sent: 1  Rows_examined: 500
SET timestamp=1763008810;
SELECT * FROM perf_user WHERE id = 2;
"""


def test_slowlog_analyze_content():
    out = call_tool(build_registry(), "slowlog_analyze", {"content": _SLOWLOG})
    assert out["total"] == 2
    assert out["top"][0]["queryTimeMs"] == pytest.approx(2500), "按耗时降序"
    assert out["top"][0]["rowsExamined"] == 100000
    assert "SELECT * FROM perf_order" in out["top"][0]["sql"]
    assert out["summary"]["totalRowsExamined"] == 100500


def test_slowlog_analyze_top_n_and_file(tmp_path):
    f = tmp_path / "slow.log"
    f.write_text(_SLOWLOG, encoding="utf-8")
    out = call_tool(build_registry(), "slowlog_analyze", {"path": str(f), "top_n": 1})
    assert len(out["top"]) == 1


def test_slowlog_analyze_no_input():
    out = call_tool(build_registry(), "slowlog_analyze", {})
    assert "二选一" in out["error"]


def test_slowlog_analyze_empty():
    out = call_tool(build_registry(), "slowlog_analyze", {"content": "no entries here"})
    assert out["total"] == 0


# ===== source_scan =====

_JAVA = """
package com.perfdemo.mapper;
public interface UserMapper {
    @Select("SELECT o.*, u.name FROM perf_order o JOIN perf_user u ON o.user_id = u.id WHERE o.status = 'PAID'")
    java.util.List<java.util.Map<String, Object>> listPaid();
}
"""


def test_source_scan(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    (d / "UserMapper.java").write_text(_JAVA, encoding="utf-8")
    out = call_tool(build_registry(), "source_scan",
                    {"source_dir": str(d), "tables": ["perf_order", "perf_user"]})
    assert "error" not in out
    assert out["scannedFiles"] == 1
    assert out["relationHints"], "JOIN 关联被发现"
    hints = {(h["childTable"], h["parentTable"]) for h in out["relationHints"]}
    assert ("perf_order", "perf_user") in hints
    assert out["magicHints"], "SQL 字面量 PAID 被发现"


def test_source_scan_requires_tables(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    out = call_tool(build_registry(), "source_scan", {"source_dir": str(d)})
    assert "tables" in out["error"]


# ===== datagen 工具（错误路径；真实库走冒烟脚本） =====


def test_datagen_tools_missing_spec(tmp_path):
    reg = build_registry()
    for name in ("datagen_dry_run", "datagen_run"):
        out = call_tool(reg, name, {"spec_file": str(tmp_path / "nope.json")})
        assert "error" in out


def test_datagen_dry_run_unsupported_url(tmp_path):
    f = tmp_path / "spec.json"
    f.write_text(json.dumps({"dbUrl": "jdbc:oracle://h/db", "dryRun": True,
                             "tables": [{"tableName": "t", "rowCount": 1}]}), encoding="utf-8")
    out = call_tool(build_registry(), "datagen_dry_run", {"spec_file": str(f)})
    assert out["success"] is False, "引擎失败经 result 返回而非 error 键"
    assert "未注册匹配方言" in out["errorMsg"]
