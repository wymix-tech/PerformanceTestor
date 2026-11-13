"""perf CLI 入口（typer）。

阶段 1 目标命令（Issue #23）：
- perf stress run   读场景 → POST /api/stress/gate → 等待完成 → 输出报告
- perf gate         替代 scripts/perf-gate.sh（退出码 0/1/2 语义对齐）
- perf scenario     场景文件校验

退出码约定：0 = 通过；1 = 门禁未通过；2 = 调用异常（参数/网络）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console

from perf_cli import __version__
from perf_cli.config import (
    EXIT_INVOCATION_ERROR,
    EXIT_OK,
)

app = typer.Typer(
    name="perf",
    help="PerformanceTestor Python CLI：可重复压测脚本（场景即代码）。",
    no_args_is_help=True,
    add_completion=False,
)
stress_app = typer.Typer(help="压测执行相关命令。", no_args_is_help=True)
scenario_app = typer.Typer(help="场景文件管理（校验/列举）。", no_args_is_help=True)
app.add_typer(stress_app, name="stress")
app.add_typer(scenario_app, name="scenario")

console = Console()
err_console = Console(stderr=True)


def _fail(message: str) -> None:
    err_console.print(f"[red]错误：[/red]{message}")
    raise typer.Exit(EXIT_INVOCATION_ERROR)


@app.callback()
def main_callback() -> None:
    """全局选项（当前版本无）。"""


@app.command()
def version() -> None:
    """打印 CLI 版本。"""
    console.print(f"perf-py {__version__}")


@scenario_app.command("validate")
def scenario_validate(
    scenario_file: Path = typer.Argument(..., exists=True, readable=True, help="场景 YAML 文件"),
) -> None:
    """校验场景文件：结构 / 负载参数 / 断言规则，全部通过才退出码 0。"""
    from scenario.loader import ScenarioError
    from scenario.loader import validate_scenario as _validate

    try:
        s = _validate(scenario_file)
    except ScenarioError as exc:
        err_console.print(f"[red]场景校验失败[/red]（{len(exc.errors)} 项）：")
        for err in exc.errors:
            err_console.print(f"  - {err}")
        raise typer.Exit(EXIT_INVOCATION_ERROR) from None

    console.print(f"[green]✓ 场景校验通过[/green] {s.summary_line()}")
    if s.labels:
        console.print(f"  标签: {', '.join(s.labels)}")
    raise typer.Exit(EXIT_OK)


@stress_app.command("run")
def stress_run(
    scenario_file: Path = typer.Argument(..., exists=True, readable=True, help="场景 YAML 文件"),
    profile: str = typer.Option(None, "--profile", "-p", help="profile 名称（profiles/<name>.toml）"),
    output: Path = typer.Option(None, "--output", "-o", help="JSON 工件输出路径（默认仅 stdout）"),
) -> None:
    """执行压测场景：读场景 → POST /api/stress/gate → 等待完成 → 输出报告。

    退出码：0 = 任务完成且门禁通过；1 = 任务失败/超时/门禁未通过；2 = 调用异常。
    """
    from perf_cli.gate_runner import execute_gate

    execute_gate(
        scenario_file=scenario_file,
        profile=profile,
        output=output,
        console=console,
        err_console=err_console,
    )


@app.command()
def gate(
    profile: str = typer.Option(None, "--profile", "-p", help="profile 名称（profiles/<name>.toml）"),
    scenario_file: Path = typer.Option(
        None, "--scenario", "-s", exists=True, readable=True, help="从场景文件读取参数（缺省时使用 GATE_* 环境变量）"
    ),
    output: Path = typer.Option(None, "--output", "-o", help="JSON 工件输出路径（默认仅 stdout）"),
) -> None:
    """性能回归门禁：替代 scripts/perf-gate.sh。

    参数来源：--scenario 场景文件（优先）或 GATE_* 环境变量（与 shell 脚本变量一致）。
    退出码：0 = 通过；1 = 未通过（回归/超时/失败）；2 = 调用异常（参数/网络）。
    """
    from perf_cli.gate_runner import execute_gate

    execute_gate(
        scenario_file=scenario_file,
        profile=profile,
        output=output,
        console=console,
        err_console=err_console,
    )


def run_cli() -> int:
    """独立运行入口：供 `perf` console script 使用。"""
    try:
        app()
    except SystemExit as exc:  # typer 通过 SystemExit 传递退出码
        code = int(exc.code) if exc.code is not None else EXIT_OK
        return code if isinstance(code, int) else EXIT_OK
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(run_cli())
