"""LLM 可选增强（阶段 5 · 步骤 11 段落，自 datagen/plan_pipeline.py 收编终态）。

脱敏边界（硬性要求，由测试守卫对齐 Java LlmPlanEnhancerSanitizeTest）：
- 允许发送：表名、列名、列类型、读写类型、谓词算子形状（= / IN(n=3) / BETWEEN）、
  unresolved 原因（经 redact）、索引类型（唯一/普通）
- 值一律打码：字面值替换为类型占位（<int> / <str:len=8> / <date>）
- 禁止发送：SQL 全文、源码片段、连接串与凭据、真实数据样本

降级链（静默，绝不阻断分析）：未开启 → 无 Key → 无有效返回 → 解析失败
→ 无可采纳建议 → 调用异常，原因写入 Enhancement.reason。

模型居所：facts 由 server.datagen_plan_service 以 lang_java 解析器产物
组装（``ptde.analyzer.lang_java._legacy_model`` 的 Op/Predicate），故
Op 取自同一居所（枚举必须同类可比）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from ptde.analyzer.lang_java._legacy_model import Op
from ptde.planner.workload import EndpointTableFact

log = logging.getLogger(__name__)

LLM_NO_KEY_REASON = "未配置大模型 API Key，已降级为纯静态分析结论"

MAX_TABLES = 20            # 送审的最大表数（控制成本与提示词长度）
MAX_MERGED_VALUES = 32     # 单次合入的取值上限（防超长列表撑爆值集）

LLM_SYSTEM_PROMPT = ("你是一个测试数据规划助手。根据给定的数据库结构元信息，补充数据准备建议。"
                     "只输出 JSON，不要解释。所有值均已脱敏为类型占位符，不要尝试猜测真实业务值。")


@dataclass
class Enhancement:
    applied: bool
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)


def _llm_available(llm) -> bool:
    """LLM 可用性：None → False；实现带 available() 探测（Java available() 对齐）。"""
    if llm is None:
        return False
    available = getattr(llm, "available", None)
    return available() if callable(available) else True


def llm_enhance(facts: list[EndpointTableFact], enabled: bool, llm=None) -> Enhancement:
    """LLM 可选增强（LlmPlanEnhancer.enhance 移植）：静态分析为主，大模型语义补全。

    只合入「已有列的枚举取值补充」，不接受新表/新列/新关联——表识别坚持
    「宁缺勿滥」，对错误的表造数据会污染业务表；补充取值只影响数据分布，风险可控。
    降级链静默（原因写入 reason），绝不阻断分析。
    """
    if not enabled:
        return Enhancement(False, "未开启 LLM 增强")
    if not facts:
        return Enhancement(False, "无静态分析结论，跳过 LLM 增强")
    if not _llm_available(llm):
        return Enhancement(False, LLM_NO_KEY_REASON)
    try:
        prompt = _sanitized_prompt(facts)
        result = llm.chat([{"role": "system", "content": LLM_SYSTEM_PROMPT},
                           {"role": "user", "content": prompt}], [])
        raw = result.content
        if raw is None or not raw.strip():
            return Enhancement(False, "大模型无有效返回，已降级为纯静态分析结论")
        parsed = json.loads(raw)
        if parsed is None:
            return Enhancement(False, "大模型返回无法解析，已降级为纯静态分析结论")
        merged = _merge_enum_suggestions(facts, parsed)
        if merged == 0:
            # 解析成功但没采纳任何建议时不能报 applied=True——
            # 那会让前端展示「增强已应用」的假象，而实际什么都没变
            return Enhancement(False, "大模型未给出可采纳的补全建议，已沿用纯静态分析结论")
        out = Enhancement(True, f"LLM 增强已应用（补充 {merged} 列的取值建议）")
        out.warnings.append("本方案含大模型语义补全，请复核表清单后再执行")
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("llm plan enhance failed: %s", exc)
        return Enhancement(False, f"大模型调用失败，已降级为纯静态分析结论: {exc}")


def _merge_enum_suggestions(facts: list[EndpointTableFact], parsed) -> int:
    """把 LLM 建议合入事实，返回实际采纳的列数（对齐 mergeEnumSuggestions）。

    期望结构：{"tables":[{"table":"t","columns":[{"column":"c","values":[...]}]}]}
    （也接受顶层数组）。只补充已存在谓词的列（列的存在性由静态分析保证）。
    """
    if isinstance(parsed, list):
        tables = parsed
    elif isinstance(parsed, dict):
        tables = parsed.get("tables")
    else:
        tables = None
    if not isinstance(tables, list):
        return 0
    by_table = {f.table.lower(): f for f in facts if f is not None and f.table}
    merged = 0
    for t in tables:
        if not isinstance(t, dict):
            continue
        table_name = str(t.get("table") or "")
        fact = by_table.get(table_name.strip().lower())
        if fact is None:
            log.debug("llm suggested unknown table, ignored: %s", table_name)
            continue
        columns = t.get("columns")
        if not isinstance(columns, list):
            continue
        for c in columns:
            if not isinstance(c, dict):
                continue
            col = str(c.get("column") or "")
            values_node = c.get("values")
            if not col or not isinstance(values_node, list) or not values_node:
                continue
            # 只补充已存在谓词的列（EQ/IN；列的存在性由静态分析保证）
            target = None
            for p in fact.predicates:
                if p.column is not None and p.column.lower() == col.lower() \
                        and p.op in (Op.EQ, Op.IN):
                    target = p
                    break
            if target is None:
                continue
            before = len(target.values)
            for v in values_node:
                if len(target.values) >= MAX_MERGED_VALUES:
                    break
                if isinstance(v, str):
                    _add_if_absent(target.values, v)
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    _add_if_absent(target.values, int(v))  # Java asLong 截断语义
            if len(target.values) > before:
                merged += 1
    return merged


def _add_if_absent(values: list, v) -> None:
    for o in values:
        if o is not None and str(o) == str(v):
            return
    values.append(v)


def _sanitized_prompt(facts: list[EndpointTableFact]) -> str:
    """构造脱敏后的提示词：只发结构元信息，所有字面值替换为类型占位。"""
    tables_out: list[dict] = []
    for f in facts[:MAX_TABLES]:
        t: dict = {"table": f.table,
                   "access": f.access.name if f.access else "READ",
                   "confidence": f.confidence}
        # unresolvedReason 可能来自 SQL 片段，必须先脱敏再外发（禁止泄漏 SQL 全文与字面值）
        unresolved = getattr(f, "unresolved_reason", None)
        if unresolved and unresolved.strip():
            t["unresolvedReason"] = _redact(unresolved)
        preds: list[dict] = []
        for p in f.predicates:
            pn: dict = {"column": p.column,
                        "op": p.op.name if p.op else "",
                        "valueCount": len(p.values or [])}
            pn["valueShapes"] = [_shape_of(v) for v in (p.values or [])[:3]]
            if p.op == Op.RANGE:
                pn["hasBounds"] = p.min is not None or p.max is not None
            preds.append(pn)
        t["predicates"] = preds
        t["joinTables"] = list(f.join_tables or [])
        tables_out.append(t)
    return json.dumps({"tables": tables_out}, ensure_ascii=False, separators=(",", ":"))


def _redact(text: str) -> str:
    """自由文本脱敏：剥离单引号字符串、数字串与连接串（对齐 redact）。"""
    s = re.sub(r"'[^']*'", "'<redacted>'", text)
    s = re.sub(r"\d{3,}", "<num>", s)
    s = re.sub(r"(?i)\b(jdbc:[a-z]+://[^\s]+)", "<conn>", s)
    return s if len(s) <= 200 else s[:200] + "..."


def _shape_of(v) -> str:
    """值的类型占位：只暴露类型与长度，不暴露内容（对齐 shapeOf）。"""
    if v is None:
        return "<null>"
    if isinstance(v, bool):
        return "<int>"
    if isinstance(v, (int, float)):
        return "<decimal>" if "." in str(v) else "<int>"
    s = str(v)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", s):
        return "<date>"
    if re.fullmatch(r"-?\d+", s):
        return "<int>"
    if re.fullmatch(r"-?\d+\.\d+", s):
        return "<decimal>"
    return f"<str:len={len(s)}>"


def contains_literal(prompt: str | None, literal: str | None) -> bool:
    """断言用：提示词中是否残留原始字面值（测试守卫，对齐 containsLiteral）。"""
    return (prompt is not None and literal is not None and literal.strip() != ""
            and literal.lower() in prompt.lower())
