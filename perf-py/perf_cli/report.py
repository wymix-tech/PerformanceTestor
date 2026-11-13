"""结果输出：控制台渲染 + JSON 工件落盘（CI 可回传留档）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.table import Table

from perf_cli.verdict import GateVerdict


def render_verdict(verdict: GateVerdict) -> None:
    from rich.console import Console

    console = Console()
    console.print(f"    任务 #{verdict.task_id} 状态: {verdict.status}")
    if verdict.no_baseline:
        console.print("    基线: 无（本次结果将作为后续基线）")
    elif verdict.baseline_task_id:
        console.print(f"    基线: 任务 #{verdict.baseline_task_id}")
    if verdict.rows:
        table = Table(title="基线对比", show_lines=False)
        for col in ("指标", "基线", "本次", "变化", "结论"):
            table.add_column(col)
        for row in verdict.rows:
            delta = f"{row.delta_pct:+.2f}%" if isinstance(row.delta_pct, (int, float)) else "-"
            verdict_txt = ""
            if row.regressed:
                verdict_txt = "[red]回归[/red]"
            elif row.improved:
                verdict_txt = "[green]改善[/green]"
            table.add_row(row.label, str(row.base), str(row.cur), delta, verdict_txt)
        console.print(table)
    console.print(f"    结论: {verdict.summary}")
    if verdict.passed:
        console.print("==> 性能门禁通过 [green]✅[/green]")
    else:
        console.print("==> 性能门禁未通过 [red]❌[/red]")


def render_task_metrics(report: dict[str, Any]) -> None:
    """渲染 GET /api/stress/tasks/{id}/report 的 {task, result} 指标。"""
    from rich.console import Console

    result = report.get("result") or {}
    if not result:
        return
    console = Console()
    table = Table(title="压测指标")
    for col in ("总请求", "错误率", "QPS", "P50(ms)", "P95(ms)", "P99(ms)", "avg(ms)", "max(ms)"):
        table.add_column(col)
    table.add_row(
        str(result.get("totalRequests", "-")),
        str(result.get("errorRate", "-")),
        str(result.get("qps", "-")),
        str(result.get("p50Ms", "-")),
        str(result.get("p95Ms", "-")),
        str(result.get("p99Ms", "-")),
        str(result.get("avgMs", "-")),
        str(result.get("maxMs", "-")),
    )
    console.print(table)


def build_artifact(verdict: GateVerdict, task_report: dict[str, Any] | None, scenario_name: str | None) -> dict[str, Any]:
    """组装 JSON 工件：verdict + 指标 + 元信息，供 CI 归档。"""
    return {
        "schema": "perf-py.artifact.v1",
        "generatedAt": datetime.now(UTC).isoformat(),
        "scenario": scenario_name,
        "verdict": {k: v for k, v in verdict.raw.items()},
        "taskReport": task_report,
    }


def save_artifact(path: Path | None, artifact: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(artifact, fh, ensure_ascii=False, indent=2, default=str)
