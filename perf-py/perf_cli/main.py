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
    EXIT_GATE_FAILED,
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
datagen_app = typer.Typer(help="灌数引擎（perf-datagen Python 化）。", no_args_is_help=True)
tools_app = typer.Typer(help="AI Agent 工具（阶段 4：Function Calling / MCP 双形态）。", no_args_is_help=True)
agent_app = typer.Typer(help="AI Agent 编排（阶段 4：ReAct 单体 / 三角色流水线）。", no_args_is_help=True)
app.add_typer(stress_app, name="stress")
app.add_typer(scenario_app, name="scenario")
app.add_typer(datagen_app, name="datagen")
app.add_typer(tools_app, name="tools")
app.add_typer(agent_app, name="agent")

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


@datagen_app.command("run")
def datagen_run(
    spec_file: Path = typer.Argument(..., exists=True, readable=True, help="灌数规格 JSON 文件"),
    dry_run: bool = typer.Option(False, "--dry-run", help="强制试运行（覆盖 spec 内 dryRun 字段）"),
    source_dir: Path = typer.Option(
        None, "--source-dir", exists=True, file_okay=False, help="源码目录（.java/.xml，供语义扫描与 ORM 解析）"
    ),
    output: Path = typer.Option(None, "--output", "-o", help="JSON 工件输出路径（默认仅 stdout）"),
) -> None:
    """执行灌数任务：读 spec → 引擎编排（Dry-Run 优先/拓扑排序/批量写入）→ 输出结果。

    退出码：0 = 执行成功；1 = 引擎失败（含方言路由失败/表缺失/Dry-Run 校验未过）；
    2 = 调用异常（spec 文件不存在 / JSON 解析失败）。
    """
    import json

    from perf_cli.datagen_runner import (
        ConsoleListener,
        build_default_engine,
        load_spec,
        result_to_camel_json,
    )

    try:
        spec = load_spec(spec_file, source_dir)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        err_console.print(f"[red]spec 加载失败：[/red]{exc}")
        raise typer.Exit(EXIT_INVOCATION_ERROR) from None
    if dry_run:
        spec.dry_run = True

    engine = build_default_engine()
    listener = ConsoleListener()
    result = engine.execute(spec, listener)

    text = result_to_camel_json(result)
    if output:
        output.write_text(text + "\n", encoding="utf-8")
    else:
        console.print(text)

    if not result.success:
        err_console.print(f"[red]灌数失败：[/red]{result.error_msg or '未知错误'}")
        raise typer.Exit(EXIT_GATE_FAILED) from None
    console.print(f"[green]✓ 灌数{'试运行' if result.dry_run else '执行'}成功[/green] "
                  f"插入 {result.inserted_rows}/{result.total_rows} 行")


@tools_app.command("list")
def tools_list() -> None:
    """列出全部 Agent 工具（名称 / 描述 / 只读标记）。"""
    import json

    from tools.registry import build_registry

    registry = build_registry()
    console.print(f"共 {len(registry)} 个工具：")
    for name in sorted(registry):
        t = registry[name]
        ro = "只读" if t.read_only else "写入"
        console.print(f"  {name}  [{ro}]  {t.description}")
        console.print(f"    参数: {json.dumps(t.input_schema(), ensure_ascii=False)}")


@tools_app.command("call")
def tools_call(
    name: str = typer.Argument(..., help="工具名（perf tools list 查看）"),
    args_json: str = typer.Option("{}", "--args", "-a", help="工具参数 JSON"),
) -> None:
    """直接调用一个 Agent 工具（结果以 JSON 输出）。"""
    import json

    from tools.registry import build_registry, call_tool

    try:
        args = json.loads(args_json)
    except json.JSONDecodeError as exc:
        err_console.print(f"[red]--args JSON 解析失败：[/red]{exc}")
        raise typer.Exit(EXIT_INVOCATION_ERROR) from None
    result = call_tool(build_registry(), name, args)
    console.print(json.dumps(result, ensure_ascii=False, indent=2))
    raise typer.Exit(EXIT_GATE_FAILED if "error" in result else EXIT_OK)


@app.command()
def mcp() -> None:
    """启动 MCP server（stdio 传输）：IDE / Agent 以子进程方式接入。

    协议：newline-delimited JSON-RPC 2.0（initialize / tools/list / tools/call）。
    """
    from perf_cli.mcp_server import serve

    serve()
    raise typer.Exit(EXIT_OK)


@agent_app.command("run")
def agent_run(
    task: str = typer.Argument(..., help="任务描述（目标端点/场景文件/关注点）"),
    role: str = typer.Option(
        "pipeline", "--role", "-r",
        help="角色：pipeline（压测→分析→修复建议三角色）/ stress / analysis / advisory",
    ),
    max_steps: int = typer.Option(10, "--max-steps", help="单 Agent 最大工具调用步数"),
    model: str = typer.Option(None, "--model", help="模型名（缺省读 PERF_LLM_MODEL）"),
    base_url: str = typer.Option(None, "--base-url", help="OpenAI 兼容网关（缺省读 PERF_LLM_BASE_URL）"),
    api_key: str = typer.Option(None, "--api-key", help="API Key（缺省读 PERF_LLM_API_KEY）"),
    output: Path = typer.Option(None, "--output", "-o", help="JSON 工件输出路径"),
) -> None:
    """运行 AI Agent：ReAct 循环或三角色流水线，报告与结论以 JSON 输出。

    退出码：0 = 得出结论；1 = 截断/无结论（max-steps 或角色未产出）；
    2 = 调用异常（LLM 配置缺失 / openai 未安装）。
    """
    import json

    from agents.llm import OpenAiCompatClient, client_from_env
    from agents.pipeline import run_pipeline
    from agents.react import Agent
    from agents.roles import ROLES
    from tools.registry import build_registry

    if model or base_url or api_key:
        llm = OpenAiCompatClient(model=model or "", base_url=base_url or None,
                                 api_key=api_key or None)
    else:
        llm = client_from_env()
    if llm is None:
        err_console.print("[red]LLM 未配置：[/red]设置 PERF_LLM_MODEL / PERF_LLM_BASE_URL / "
                          "PERF_LLM_API_KEY 或使用 --model/--base-url/--api-key")
        raise typer.Exit(EXIT_INVOCATION_ERROR) from None

    registry = build_registry()
    try:
        if role == "pipeline":
            result = run_pipeline(task, llm, registry, max_steps)
        else:
            role_def = next((r for r in ROLES if r.name == role), None)
            if role_def is None:
                err_console.print(f"[red]未知角色：[/red]{role}（可选 pipeline/"
                                  f"{'/'.join(r.name for r in ROLES)}）")
                raise typer.Exit(EXIT_INVOCATION_ERROR) from None
            agent = Agent(name=role_def.name, system_prompt=role_def.prompt(), llm=llm,
                          registry=registry, tool_names=list(role_def.tool_names),
                          max_steps=max_steps)
            result = agent.run(task).to_dict()
    except RuntimeError as exc:
        err_console.print(f"[red]Agent 执行失败：[/red]{exc}")
        raise typer.Exit(EXIT_INVOCATION_ERROR) from None

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if output:
        output.write_text(text + "\n", encoding="utf-8")
    else:
        console.print(text)

    final = result.get("finalAdvice") if role == "pipeline" else result.get("finalAnswer")
    if not final:
        err_console.print("[yellow]Agent 未得出结论（达到 max-steps 截断）[/yellow]")
        raise typer.Exit(EXIT_GATE_FAILED) from None
    raise typer.Exit(EXIT_OK)


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
