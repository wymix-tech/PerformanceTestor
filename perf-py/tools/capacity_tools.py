"""容量分析工具：分档采样点 → 容量结论（移植自 capacity/，算法逐行对齐 Java）。"""

from __future__ import annotations

import json
from pathlib import Path

from capacity.analyzer import analyze
from engine.result import StagePoint
from tools.registry import Tool

CAPACITY_SCHEMA = {
    "type": "object",
    "properties": {
        "points": {
            "type": "array",
            "description": "分档采样点数组（字段 camelCase 如 p95Ms，或 snake_case p95_ms）",
            "items": {"type": "object"},
        },
        "result_file": {
            "type": "string",
            "description": "可选：压测工件路径（从中读取 stagePoints 作为采样点）",
        },
        "target_p95_ms": {
            "type": "number",
            "description": "目标 P95 延迟阈值（ms），用于插值判定最大可用并发",
        },
    },
    "required": ["target_p95_ms"],
}


def capacity_analyze(args: dict) -> dict:
    """容量评估：maxQps / maxConcurrency / RT 拐点 / R² / 置信区间。"""
    target = args.get("target_p95_ms")
    if target is None:
        return {"error": "缺少 target_p95_ms 参数"}
    raw_points = args.get("points")
    result_file = args.get("result_file")
    if raw_points is None:
        if not result_file:
            return {"error": "需要 points 数组或 result_file 二选一"}
        p = Path(result_file)
        if not p.is_file():
            return {"error": f"文件不存在: {result_file}"}
        raw_points = json.loads(p.read_text(encoding="utf-8")).get("stagePoints") or []
    if not isinstance(raw_points, list) or len(raw_points) < 2:
        return {"error": "分档采样点不足（至少 2 个）"}

    points = [StagePoint.model_validate(x) for x in raw_points]
    result = analyze(points, float(target))
    out = result.to_java_json()
    # 面向 Agent 的快捷结论（判定口径与 Java 相同，不引入新语义）
    out["advice"] = _advice_line(result.max_concurrency, result.max_qps,
                                 result.rt_knee_concurrency)
    return out


def _advice_line(max_conc: int, max_qps: int, knee: float) -> str:
    if max_conc <= 0:
        return "目标 P95 下无可用容量档位（全部超阈值），建议先优化慢路径"
    line = f"目标 P95 内最大可用并发 {max_conc}，对应吞吐上限约 {max_qps} QPS"
    if knee > 0:
        line += f"；RT 二阶导拐点约并发 {knee:g}（超过后延迟加速恶化）"
    return line


CAPACITY_TOOLS = [
    Tool(
        name="capacity_analyze",
        description="对分档压测采样点做容量评估：目标 P95 下的最大可用并发/QPS、"
                    "RT-并发曲线拐点、拟合 R² 与置信区间。",
        parameters=CAPACITY_SCHEMA,
        handler=capacity_analyze,
    ),
]
