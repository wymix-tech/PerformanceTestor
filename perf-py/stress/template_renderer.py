"""双池模板渲染器：移植自 TemplateRenderer.java + StepContextStore.java。

双池解析顺序：
1. 上下文引用（形状 stepRef.(header|result)(.path)?）→ 槽位取值
2. 扁平变量池
3. 都未命中 → 保留占位符原文（不静默变空串，便于排查配置错误）

防劫持：判据是「stepRef 能否解析到合法槽位」——存量扁平变量叫 xxx.result.yyy
时只要 xxx 不是 pN 也未登记为别名，就走扁平变量，不被新语法劫持。

未命中计数：只统计「确定的上下文引用但取不到值」。
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from typing import Any

from stress.response_access import header_value, parse_json, resolve_path, value_to_text

# 上下文引用形状：stepRef . (header|result) [ . path ]
CTX_REF = re.compile(r"^([A-Za-z_][\w-]*)\.(header|result)(?:\.(.+))?$")
# 步骤序号引用：p（大小写均可）+ 数字；前导零（p01）与 p0 一律非法
STEP_REF = re.compile(r"^[pP](\d+)$")
# 别名合法字符：字母/下划线开头，后接字母、数字、下划线、连字符
ALIAS = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def parse_context_ref(name: str | None) -> tuple[str, str, str | None] | None:
    """解析占位符名的上下文引用形状。命中返回 (stepRef, source, path)；否则 None。"""
    if not name or name.find(".") <= 0:
        return None
    m = CTX_REF.match(name)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


def parse_step_ref(ref: str | None) -> int:
    """解析 pN 形式的步骤序号（1-based）。

    边界：p1→1；P2→2（前缀大小写不敏感）；p0/p01→-1（序号从 1 起，前导零非法）；
    p1x/p/1p→-1；数字溢出→-1。这是「某字符串是否为步骤序号引用」的唯一判定入口。
    """
    if not ref:
        return -1
    m = STEP_REF.match(ref)
    if not m:
        return -1
    digits = m.group(1)
    if len(digits) > 1 and digits[0] == "0":
        return -1
    try:
        n = int(digits)
    except ValueError:
        return -1
    return n if 1 <= n <= 2147483647 else -1


def is_valid_alias(alias: str | None) -> bool:
    return alias is not None and ALIAS.match(alias) is not None


def build_alias_index(steps: list, warnings: list[str]) -> dict[str, int]:
    """构建「别名 → 步骤下标」表（启动期一次性）。

    规则（对齐 StepContextStore.buildAliasIndex）：alias 为空回退 name；
    别名不合法 / 形如 pN / 重名 → 显式声明的写 warnings，回退自 name 的非法别名
    只静默跳过（日志降噪语义）。
    """
    index: dict[str, int] = {}
    for i, step in enumerate(steps):
        explicit = _blank_to_null(getattr(step, "alias", None))
        name = _blank_to_null(getattr(step, "name", None))
        alias = explicit if explicit is not None else name
        explicit_declared = explicit is not None
        if not explicit_declared and name is None:
            continue
        if not is_valid_alias(alias):
            if explicit_declared:
                warnings.append(
                    f"步骤 {i + 1} 的别名「{alias}」不合法（需匹配 [A-Za-z_][A-Za-z0-9_-]*），"
                    f"已忽略；仍可用 p{i + 1} 引用"
                )
            continue
        if parse_step_ref(alias) > 0:
            # 与 pN 字面冲突：pN 永远优先，别名不得占用
            if explicit_declared:
                warnings.append(f"步骤 {i + 1} 的别名「{alias}」形如步骤序号引用（pN），已忽略；序号引用优先级更高")
            continue
        if alias in index:
            if explicit_declared:
                warnings.append(f"步骤 {i + 1} 的别名「{alias}」与步骤 {index[alias] + 1} 重名，已忽略（首个生效）")
            continue
        index[alias] = i
    return index


def _blank_to_null(s: str | None) -> str | None:
    return s.strip() if s and s.strip() else None


class StepContextStore:
    """步骤响应上下文池：每步骤一个槽位，只保留最近一次响应。

    并发模型（对齐 Java）：整体原子替换（写时整体 set 换掉快照，读侧永不撕裂）；
    JSON 懒解析 + 槽位级锁（仅首次付费）。
    """

    def __init__(self, size: int, alias_index: dict[str, int], max_body_chars: int) -> None:
        self._size = size
        self._alias_index = alias_index
        self._max_body_chars = max_body_chars
        self._slots: list[tuple[dict[str, list[str]] | None, str] | None] = [None] * size
        self._json_cache: list[Any | None] = [None] * size
        self._json_parsed = [False] * size
        self._slot_locks = [threading.Lock() for _ in range(size)]

    @property
    def size(self) -> int:
        return self._size

    def index_of(self, step_ref: str | None) -> int:
        """stepRef → 槽位下标；均不命中返回 -1（此时占位符不是上下文引用）。"""
        if not step_ref:
            return -1
        ordinal = parse_step_ref(step_ref)
        if ordinal > 0:
            return ordinal - 1 if ordinal <= self._size else -1
        return self._alias_index.get(step_ref, -1)

    def put(self, step_idx: int, headers: dict[str, list[str]] | None, body: str | None) -> None:
        """写入某步骤最近一次响应（原子整体替换）。

        调用契约：仅在真正收到响应时调用；HTTP 错误与断言未过的响应照常入池
        （保证下游引用「最新可达数据」）；网络异常（无响应对象）不得调用。
        """
        if step_idx < 0 or step_idx >= self._size:
            return
        raw = "" if not body else (body[: self._max_body_chars] if len(body) > self._max_body_chars else body)
        with self._slot_locks[step_idx]:
            self._slots[step_idx] = (headers, raw)
            self._json_parsed[step_idx] = False
            self._json_cache[step_idx] = None

    def _json_of(self, step_idx: int) -> Any | None:
        """槽位 JSON 懒解析（槽位级双检锁；非 JSON 缓存 None 避免重复解析失败）。"""
        if self._json_parsed[step_idx]:
            return self._json_cache[step_idx]
        with self._slot_locks[step_idx]:
            if not self._json_parsed[step_idx]:
                slot = self._slots[step_idx]
                self._json_cache[step_idx] = parse_json(slot[1], self._max_body_chars) if slot else None
                self._json_parsed[step_idx] = True
        return self._json_cache[step_idx]

    def resolve(self, step_idx: int, source: str, path: str | None) -> str | None:
        """按 source 与 path 从指定槽位取值；无数据或未命中返回 None。"""
        if step_idx < 0 or step_idx >= self._size:
            return None
        slot = self._slots[step_idx]
        if slot is None:
            return None
        if source.lower() == "header":
            return header_value(slot[0], path)
        p = (path or "").strip()
        if not p:
            return slot[1] if slot[1] else None
        node = self._json_of(step_idx)
        if node is None:
            return None
        target = resolve_path(node, p)
        if target is None:
            return None
        return value_to_text(target)


def render(
    template: str | None,
    pool: dict[str, str] | None,
    ctx: StepContextStore | None = None,
    miss_counter: list[int] | None = None,
) -> str:
    """渲染模板：上下文引用 → 扁平变量 → 保留原文。无占位符时原样返回。"""
    if template is None or template == "" or template.find("{{") < 0:
        return template if template is not None else ""
    if ctx is None and not pool:
        return template
    out: list[str] = []
    i = 0
    while i < len(template):
        start = template.find("{{", i)
        if start < 0:
            out.append(template[i:])
            break
        end = template.find("}}", start + 2)
        if end < 0:
            out.append(template[i:])
            break
        out.append(template[i:start])
        name = template[start + 2 : end].strip()
        value = _resolve_placeholder(name, pool, ctx, miss_counter)
        # 未命中保留原文（含 {{ }}），不静默变空串
        out.append(value if value is not None else template[start : end + 2])
        i = end + 2
    return "".join(out)


def _resolve_placeholder(
    name: str, pool: dict[str, str] | None, ctx: StepContextStore | None, miss_counter: list[int] | None
) -> str | None:
    if ctx is not None:
        ref = parse_context_ref(name)
        if ref is not None:
            step_ref, source, path = ref
            ordinal = parse_step_ref(step_ref)
            if ordinal > 0:
                # pN 形式：确定是上下文引用。越界或取值失败都计入未命中
                if ordinal > ctx.size:
                    _count_miss(miss_counter)
                else:
                    v = ctx.resolve(ordinal - 1, source, path)
                    if v is not None:
                        return v
                    _count_miss(miss_counter)
            else:
                idx = ctx.index_of(step_ref)
                if idx >= 0:
                    # 已登记别名：确定是上下文引用
                    v = ctx.resolve(idx, source, path)
                    if v is not None:
                        return v
                    _count_miss(miss_counter)
                # 未登记 → 不是上下文引用，视为扁平变量名（防劫持），不计未命中
    return pool.get(name) if pool else None


def _count_miss(miss_counter: list[int] | None) -> None:
    if miss_counter:
        miss_counter[0] += 1


def scan_context_refs(
    template: str | None, ctx: StepContextStore | None, sink: Callable[[int, bool], None],
    out_of_range: Callable[[str], None] | None = None,
) -> None:
    """启动期扫描模板中的上下文引用（冷路径）。

    只对「能解析到槽位」或「pN 越界」的引用回调——形状像但 stepRef 未登记的
    存量扁平变量不回调。
    """
    if not template or ctx is None or template.find("{{") < 0:
        return
    i = 0
    while i < len(template):
        start = template.find("{{", i)
        if start < 0:
            return
        end = template.find("}}", start + 2)
        if end < 0:
            return
        ref = parse_context_ref(template[start + 2 : end].strip())
        if ref is not None:
            ordinal = parse_step_ref(ref[0])
            if ordinal > 0:
                if ordinal > ctx.size:
                    if out_of_range:
                        out_of_range(ref[0])
                else:
                    sink(ordinal - 1, ref[1].lower() == "header")
            else:
                idx = ctx.index_of(ref[0])
                if idx >= 0:
                    sink(idx, ref[1].lower() == "header")
        i = end + 2


def compact_json(node: Any) -> str:
    """JSON 紧凑文本（诊断辅助）。"""
    return json.dumps(node, separators=(",", ":"), ensure_ascii=False)
