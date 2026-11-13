"""本地引擎 CLI 入口：`perf-engine` 独立命令。

gevent monkey-patch 必须发生在进程最早时刻（任何 socket/ssl 模块导入之前），
故独立于 `perf` 主命令——本模块 import 时第一时间 patch，再进入主逻辑。
"""

# ruff: noqa: E402  —— monkey.patch_all() 必须先于其余 import（有意为之）

from __future__ import annotations

from gevent import monkey

monkey.patch_all()

import sys
from pathlib import Path

import typer
from rich.console import Console

engine_app = typer.Typer(
    name="perf-engine",
    help="本地压测引擎（不经平台 /api，直接发压并产出对齐 StressTaskResult 契约的工件）。",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)

EXIT_OK, EXIT_FAILED, EXIT_ERROR = 0, 1, 2


@engine_app.command()
def run(
    scenario_file: Path = typer.Argument(..., exists=True, readable=True, help="场景 YAML 文件"),
    engine: str = typer.Option("locust", "--engine", "-e", help="引擎类型：locust / jmeter"),
    output: Path = typer.Option(None, "--output", "-o", help="结果工件输出路径（JSON，camelCase 对齐 Java）"),
) -> None:
    """本地执行压测场景并输出 StressTaskResult 契约工件。"""
    from engine.registry import EngineRegistry
    from scenario.loader import ScenarioError, load_scenario

    try:
        scenario = load_scenario(scenario_file)
    except ScenarioError as exc:
        err_console.print(f"[red]场景校验失败[/red]：{exc}")
        raise typer.Exit(EXIT_ERROR) from None

    registry = EngineRegistry()
    if engine == "locust":
        from engine.locust_engine import LocustEngine

        registry.register(LocustEngine())
    elif engine == "jmeter":
        from engine.jmeter_engine import JMeterEngine

        registry.register(JMeterEngine())
    else:
        err_console.print(f"[red]未知引擎：{engine}（可选 locust / jmeter）[/red]")
        raise typer.Exit(EXIT_ERROR)

    console.print(f"==> 本地引擎 [{engine}] 启动（{scenario.summary_line()}）")

    def on_sample(sample) -> None:
        console.print(
            f"    [{sample.sample_time}] QPS={sample.qps} P95={sample.p95_ms}ms 错误={sample.error_count}"
        )

    try:
        result = registry.get(engine).run(scenario, listener=on_sample)
    except FileNotFoundError as exc:
        err_console.print(f"[red]错误：[/red]{exc}")
        raise typer.Exit(EXIT_ERROR) from None

    console.print(f"    完成：总请求={result.total_requests} QPS={result.qps} "
                  f"P50={result.p50_ms} P95={result.p95_ms} P99={result.p99_ms} "
                  f"错误率={result.error_rate}%")
    for w in result.warnings:
        console.print(f"    [yellow]警告：{w}[/yellow]")

    if output is not None:
        import json

        output.parent.mkdir(parents=True, exist_ok=True)
        artifact = {
            "schema": "perf-py.result.v1",
            "scenario": scenario.name,
            "generatedAt": __import__("datetime").datetime.now().isoformat(),
            "result": result.to_java_json(),
        }
        with open(output, "w", encoding="utf-8") as fh:
            json.dump(artifact, fh, ensure_ascii=False, indent=2)
        console.print(f"    结果工件: {output}")

    ok = result.error_rate < 100.0 and result.total_requests > 0
    raise typer.Exit(EXIT_OK if ok else EXIT_FAILED)


def main() -> None:
    engine_app()


if __name__ == "__main__":
    sys.exit(main())
