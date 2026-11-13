"""压测结果工具：读取本地引擎工件（perf-engine -o 产物）并输出核心指标摘要。

Agent 典型链路：发起压测（平台 gate 或本地 perf-engine）→ 本工具读取指标 →
capacity/slowlog/source 工具关联分析。
"""

from __future__ import annotations

import json
from pathlib import Path

from engine.result import StressTaskResult
from tools.registry import Tool

STRESS_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "压测结果工件路径（perf-engine -o 输出的 JSON，camelCase 契约）",
        },
    },
    "required": ["path"],
}


def stress_result_read(args: dict) -> dict:
    """读取压测结果工件，返回核心指标 + 分档点（供容量/对比分析直接消费）。"""
    path = args.get("path")
    if not path:
        return {"error": "缺少 path 参数"}
    p = Path(path)
    if not p.is_file():
        return {"error": f"文件不存在: {path}"}
    raw = json.loads(p.read_text(encoding="utf-8"))
    # camelCase 契约 + snake_case 双兼容（populate_by_name）
    r = StressTaskResult.model_validate(raw)
    return {
        "taskId": r.task_id,
        "engineType": r.engine_type,
        "targetUrl": r.target_url,
        "concurrency": r.concurrency,
        "durationSec": r.duration_sec,
        "totalRequests": r.total_requests,
        "errorCount": r.error_count,
        "errorRate": r.error_rate,
        "qps": r.qps,
        "p50Ms": r.p50_ms,
        "p95Ms": r.p95_ms,
        "p99Ms": r.p99_ms,
        "maxMs": r.max_ms,
        "avgMs": r.avg_ms,
        "warnings": r.warnings,
        "stagePoints": [sp.model_dump(by_alias=True) for sp in r.stage_points],
        "stepStats": [ss.model_dump(by_alias=True) for ss in r.step_stats],
    }


STRESS_TOOLS = [
    Tool(
        name="stress_result_read",
        description="读取压测结果工件 JSON（perf-engine -o 产物或平台 resultJson），"
                    "返回 QPS/P50/P95/P99/错误率等核心指标与分档采样点。",
        parameters=STRESS_RESULT_SCHEMA,
        handler=stress_result_read,
    ),
]
