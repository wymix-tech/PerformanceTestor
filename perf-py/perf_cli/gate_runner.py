"""门禁/压测执行器：stress run 与 gate 共用主流程。

流程：参数组装（场景文件优先，否则 GATE_* 环境变量）→ POST /api/stress/gate
（同步等待压测完成）→ 拉取任务报告 → 渲染 → JSON 工件落盘 → 退出码。

退出码语义对齐 scripts/perf-gate.sh：
- 0 = 门禁通过
- 1 = 未通过（回归 / 超时 / 压测失败 / 无基线且 failOnNoBaseline）
- 2 = 调用异常（参数错误 / 网络 / 认证失败）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from perf_cli.client import ApiError, PerfClient
from perf_cli.config import (
    EXIT_GATE_FAILED,
    EXIT_INVOCATION_ERROR,
    EXIT_OK,
    GateConfig,
    load_gate_config,
    load_server_config,
)
from perf_cli.report import build_artifact, render_task_metrics, render_verdict, save_artifact
from perf_cli.verdict import parse_verdict
from scenario.loader import ScenarioError, load_scenario
from scenario.model import Scenario


def payload_from_env_gate(gate_cfg: GateConfig) -> dict[str, Any]:
    """GATE_* 环境变量 → StressTaskRequest 请求体（对齐 perf-gate.sh 生成的 JSON）。"""
    return {
        "targetUrl": gate_cfg.target_url,
        "appName": gate_cfg.app_name,
        "method": gate_cfg.method,
        "body": gate_cfg.body,
        "concurrency": gate_cfg.concurrency,
        "durationSec": gate_cfg.duration_sec,
        "warmupSec": gate_cfg.warmup_sec,
        "rampUpSec": gate_cfg.ramp_up_sec,
        "qpsLimit": gate_cfg.qps_limit,
    }


def _gate_params(scenario: Scenario | None, gate_cfg: GateConfig | None) -> dict[str, Any]:
    if scenario is not None:
        return {
            "baseline_task_id": scenario.gate.baseline_task_id,
            "timeout_sec": scenario.gate.timeout_sec,
            "fail_on_no_baseline": scenario.gate.fail_on_no_baseline,
            "webhook": scenario.gate.webhook,
        }
    assert gate_cfg is not None
    return {
        "baseline_task_id": gate_cfg.baseline_task_id,
        "timeout_sec": gate_cfg.timeout_sec,
        "fail_on_no_baseline": gate_cfg.fail_on_no_baseline,
        "webhook": gate_cfg.webhook,
    }


def execute_gate(
    *,
    scenario_file: Path | None,
    profile: str | None,
    output: Path | None,
    console: Console,
    err_console: Console,
    transport: Any | None = None,  # httpx.BaseTransport，测试注入用
) -> None:
    def fail_invocation(message: str) -> None:
        err_console.print(f"[red]错误：[/red]{message}")
        raise typer.Exit(EXIT_INVOCATION_ERROR)

    # 1) 参数组装
    try:
        server_cfg = load_server_config(profile)
        scenario = load_scenario(scenario_file) if scenario_file else None
        gate_cfg = None if scenario else load_gate_config(profile)
    except ScenarioError as exc:
        err_console.print(f"[red]场景校验失败[/red]（{len(exc.errors)} 项）：")
        for err in exc.errors:
            err_console.print(f"  - {err}")
        raise typer.Exit(EXIT_INVOCATION_ERROR) from None
    except FileNotFoundError as exc:
        fail_invocation(str(exc))
        return

    if server_cfg.token_missing:
        fail_invocation("未配置 PERF_API_TOKEN（需与平台 perf.auth.api-token 一致）")
        return

    if scenario is not None:
        payload = scenario.to_request_payload()
        console.print(f"==> 触发压测（场景: {scenario.name}）")
    else:
        assert gate_cfg
        payload = payload_from_env_gate(gate_cfg)
        console.print("==> 触发性能门禁")
    console.print(
        f"    目标: {payload.get('targetUrl', payload.get('scenario', '<混合场景>'))}"
        f" / 并发: {payload.get('concurrency')}"
        f" / 时长: {payload.get('durationSec')}s"
        f" / 预热: {payload.get('warmupSec')}s"
    )

    # 2) 调用平台（同步等待压测完成；服务端失败时 HTTP 仍为 200，须看 data.passed）
    verdict_data: dict[str, Any]
    task_report: dict[str, Any] | None = None
    try:
        with PerfClient(
            server_url=server_cfg.server_url,
            api_token=server_cfg.api_token,
            timeout=server_cfg.request_timeout,
            transport=transport,
        ) as client:
            verdict_data = client.run_gate(payload, **_gate_params(scenario, gate_cfg))
            task_id = verdict_data.get("taskId")
            if task_id:
                try:
                    task_report = client.get_task_report(int(task_id))
                except ApiError as exc:  # 报告拉取失败不改变门禁结论，仅提示
                    err_console.print(f"[yellow]警告：任务报告拉取失败：{exc}[/yellow]")
    except ApiError as exc:
        fail_invocation(f"调用门禁接口失败：{exc}")
        return

    # 3) 渲染与落盘
    verdict = parse_verdict(verdict_data)
    render_verdict(verdict)
    if task_report:
        render_task_metrics(task_report)
    save_artifact(output, build_artifact(verdict, task_report, scenario.name if scenario else None))

    # 4) 退出码
    if verdict.passed:
        raise typer.Exit(EXIT_OK)
    err_console.print("==> 性能门禁未通过 ❌")
    raise typer.Exit(EXIT_GATE_FAILED)
