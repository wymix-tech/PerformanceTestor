"""LlmPlanEnhancer 单元测试（阶段 5 · 步骤 11）：逐语义对齐 Java 源码与
LlmPlanEnhancerSanitizeTest（脱敏边界守卫）。

契约：降级链文案逐字、只合入已有列的 EQ/IN 枚举补充、MAX_MERGED_VALUES=32、
MAX_TABLES=20 截断、redact/shapeOf 脱敏、提示词零字面值泄漏。
"""

import json

from agents.llm import ChatResult, LlmClient
from ptde.analyzer.lang_java._legacy_model import Op, Predicate, SqlAccess
from ptde.planner.llm_enhance import (
    MAX_MERGED_VALUES,
    MAX_TABLES,
    Enhancement,
    _merge_enum_suggestions,
    _redact,
    _sanitized_prompt,
    _shape_of,
    contains_literal,
    llm_enhance,
)
from ptde.planner.workload import EndpointTableFact


def _fact(table="t_order", col="status", op=Op.EQ, values=None, joins=None):
    return EndpointTableFact(
        table=table, access=SqlAccess.READ,
        predicates=[Predicate(column=col, op=op, values=values or ["PAID"])],
        join_tables=joins or [])


class _Scripted(LlmClient):
    """脚本化假客户端：按序返回 content 或抛异常。"""

    def __init__(self, content=None, error: Exception | None = None):
        self.content = content
        self.error = error

    def chat(self, messages, tools):
        if self.error is not None:
            raise self.error
        return ChatResult(content=self.content)


# ------------------------------------------------------------ 降级链


def test_disabled():
    assert llm_enhance([_fact()], False) == Enhancement(False, "未开启 LLM 增强")


def test_no_facts():
    assert llm_enhance([], True, _Scripted("{}")) == \
        Enhancement(False, "无静态分析结论，跳过 LLM 增强")


def test_no_key():
    assert llm_enhance([_fact()], True, None) == \
        Enhancement(False, "未配置大模型 API Key，已降级为纯静态分析结论")


def test_empty_return():
    assert llm_enhance([_fact()], True, _Scripted("  ")) == \
        Enhancement(False, "大模型无有效返回，已降级为纯静态分析结论")


def test_llm_error_degrades_verbatim():
    r = llm_enhance([_fact()], True, _Scripted(error=RuntimeError("boom")))
    assert r.applied is False
    assert r.reason == "大模型调用失败，已降级为纯静态分析结论: boom"


def test_no_adoption_reports_false():
    """解析成功但无可采纳建议 → applied=False（不制造「已应用」假象）。"""
    content = json.dumps({"tables": [{"table": "t_other", "columns": [
        {"column": "status", "values": ["X"]}]}]})
    assert llm_enhance([_fact()], True, _Scripted(content)) == \
        Enhancement(False, "大模型未给出可采纳的补全建议，已沿用纯静态分析结论")


# ------------------------------------------------------------ 合入语义


def test_merge_applies_enum_values():
    """EQ 谓词补充取值：合入 1 列，applied=True + warnings 提示复核。"""
    fact = _fact(values=["PAID"])
    content = json.dumps({"tables": [{"table": "T_ORDER", "columns": [
        {"column": "STATUS", "values": ["CANCELLED", "PAID", "REFUNDED"]}]}]})
    r = llm_enhance([fact], True, _Scripted(content))
    assert r.applied is True
    assert r.reason == "LLM 增强已应用（补充 1 列的取值建议）"
    assert r.warnings == ["本方案含大模型语义补全，请复核表清单后再执行"]
    assert fact.predicates[0].values == ["PAID", "CANCELLED", "REFUNDED"]  # 去重、原值保留


def test_merge_only_eq_in_columns():
    """RANGE 谓词列不接受取值补充。"""
    fact = _fact(op=Op.RANGE, values=[])
    content = json.dumps({"tables": [{"table": "t_order", "columns": [
        {"column": "status", "values": ["X"]}]}]})
    assert _merge_enum_suggestions([fact], json.loads(content)) == 0


def test_merge_unknown_table_ignored():
    fact = _fact()
    content = json.dumps({"tables": [{"table": "nope", "columns": [
        {"column": "status", "values": ["X"]}]}]})
    assert _merge_enum_suggestions([fact], json.loads(content)) == 0


def test_merge_numeric_as_long_and_cap():
    """数值取值按 Java asLong 截断；超出 MAX_MERGED_VALUES 停止合入。"""
    fact = _fact(values=[1])
    values = [{"column": "status", "values": [2, 3.9] + list(range(4, 200))}]
    merged = _merge_enum_suggestions([fact], {"tables": [{"table": "t_order",
                                                          "columns": values}]})
    assert merged == 1
    vs = fact.predicates[0].values
    assert vs[1] == 2 and vs[2] == 3  # asLong 截断
    assert len(vs) == MAX_MERGED_VALUES


def test_merge_top_level_array_accepted():
    fact = _fact()
    content = json.dumps([{"table": "t_order", "columns": [
        {"column": "status", "values": ["X"]}]}])
    assert _merge_enum_suggestions([fact], json.loads(content)) == 1


# ------------------------------------------------------------ 脱敏（SanitizeTest 守卫）


def test_prompt_contains_no_literals():
    """提示词零字面值泄漏：SQL 字面值、连接串、长数字一律打码。"""
    fact = _fact(values=["PAID", "2026-11-16", "secret-token-abc"])
    prompt = _sanitized_prompt([fact])
    assert not contains_literal(prompt, "PAID")
    assert not contains_literal(prompt, "secret-token-abc")
    assert "<int>" in prompt or "<str:len=" in prompt
    shapes = json.loads(prompt)["tables"][0]["predicates"][0]["valueShapes"]
    assert shapes[0] == "<str:len=4>" and shapes[1] == "<date>" and shapes[2] == "<str:len=16>"


def test_redact():
    assert _redact("col = 'secret' AND x = 12345") == "col = '<redacted>' AND x = <num>"
    assert _redact("jdbc:mysql://u:p@h/db") == "<conn>"
    long_text = "x" * 300
    assert _redact(long_text) == "x" * 200 + "..."


def test_shape_of():
    assert _shape_of(None) == "<null>"
    assert _shape_of(5) == "<int>"
    assert _shape_of(5.5) == "<decimal>"
    assert _shape_of("2026-11-16") == "<date>"
    assert _shape_of("2026-11-16T10:00:00") == "<date>"
    assert _shape_of("ab") == "<str:len=2>"


def test_max_tables_truncation():
    """超过 MAX_TABLES=20 张表截断送审。"""
    facts = [_fact(table=f"t_{i:02d}") for i in range(25)]
    prompt = _sanitized_prompt(facts)
    tables = json.loads(prompt)["tables"]
    assert len(tables) == MAX_TABLES


def test_openai_client_available_semantics():
    from agents.llm import OpenAiCompatClient

    assert OpenAiCompatClient("m", api_key="k").available() is True
    assert OpenAiCompatClient("m").available() is False
