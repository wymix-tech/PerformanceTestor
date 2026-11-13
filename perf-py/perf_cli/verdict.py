"""GateVerdict 解析：POST /api/stress/gate 返回的 data 结构（对齐 StressGateService）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 任务终态：压测本体 finished/failed；门禁层面另有 timeout/missing/interrupted
TERMINAL_STATUSES = {"finished", "failed", "timeout", "missing", "interrupted"}


@dataclass
class DiffRow:
    """基线对比行（label/base/cur/deltaPct/improved/regressed）。"""

    label: str
    base: Any
    cur: Any
    delta_pct: float | None
    improved: bool
    regressed: bool


@dataclass
class GateVerdict:
    task_id: int | None
    task_name: str
    status: str
    passed: bool
    no_baseline: bool
    regressed: bool
    baseline_task_id: int | None
    rows: list[DiffRow] = field(default_factory=list)
    summary: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def executed_ok(self) -> bool:
        """压测本体是否成功完成（与「门禁是否通过」区分）。"""
        return self.status == "finished"


def parse_verdict(data: dict[str, Any]) -> GateVerdict:
    rows: list[DiffRow] = []
    for row in data.get("rows") or []:
        if isinstance(row, dict):
            rows.append(
                DiffRow(
                    label=str(row.get("label", "")),
                    base=row.get("base"),
                    cur=row.get("cur"),
                    delta_pct=row.get("deltaPct"),
                    improved=bool(row.get("improved")),
                    regressed=bool(row.get("regressed")),
                )
            )
    baseline = data.get("baselineTaskId")
    return GateVerdict(
        task_id=data.get("taskId"),
        task_name=str(data.get("taskName", "")),
        status=str(data.get("status", "")),
        passed=bool(data.get("passed")),
        no_baseline=bool(data.get("noBaseline")),
        regressed=bool(data.get("regressed")),
        baseline_task_id=int(baseline) if baseline else None,
        rows=rows,
        summary=str(data.get("summary", "")),
        raw=data,
    )
