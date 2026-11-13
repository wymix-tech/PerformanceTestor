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
    """校验场景文件（阶段 1 步骤 2 实现完整校验逻辑）。"""
    _fail("scenario validate 将在阶段 1 步骤 2 实现（场景模型 + YAML 加载）")


@stress_app.command("run")
def stress_run(
    scenario_file: Path = typer.Argument(..., exists=True, readable=True, help="场景 YAML 文件"),
    profile: str = typer.Option(None, "--profile", "-p", help="profile 名称（profiles/<name>.toml）"),
    output: Path = typer.Option(None, "--output", "-o", help="报告输出路径（默认 stdout）"),
) -> None:
    """执行压测场景（阶段 1 步骤 3 实现：调用 /api/stress/gate 并等待完成）。"""
    _fail("stress run 将在阶段 1 步骤 3 实现（场景 → /api/stress/gate → 报告）")


@app.command()
def gate(
    profile: str = typer.Option(None, "--profile", "-p", help="profile 名称（profiles/<name>.toml）"),
    scenario_file: Path = typer.Option(
        None, "--scenario", "-s", exists=True, readable=True, help="从场景文件读取门禁参数（覆盖环境变量默认值）"
    ),
) -> None:
    """性能回归门禁（阶段 1 步骤 3 实现，替代 scripts/perf-gate.sh）。"""
    _fail("gate 将在阶段 1 步骤 3 实现（退出码 0/1/2 对齐 perf-gate.sh）")


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
