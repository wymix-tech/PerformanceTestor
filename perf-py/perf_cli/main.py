"""perf CLI 入口（typer）。

阶段 1 目标命令（Issue #23）：
- perf stress run   读场景 → POST /api/stress/gate → 等待完成 → 输出报告
- perf gate         替代 scripts/perf-gate.sh（退出码 0/1/2 语义对齐）
- perf scenario     场景文件校验

退出码约定：0 = 通过；1 = 门禁未通过；2 = 调用异常（参数/网络）。
"""

from __future__ import annotations

import os
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
ptde_app = typer.Typer(help="智能测试数据引擎 PTDE（B7：分析/画像/规划/数据集生命周期）。", no_args_is_help=True)
app.add_typer(stress_app, name="stress")
app.add_typer(scenario_app, name="scenario")
app.add_typer(datagen_app, name="datagen")
app.add_typer(tools_app, name="tools")
app.add_typer(agent_app, name="agent")
app.add_typer(ptde_app, name="ptde")

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
    seed: int = typer.Option(None, "--seed", help="随机种子（QW1 可复现；覆盖 spec 内 seed 字段）"),
    source_dir: Path = typer.Option(
        None, "--source-dir", exists=True, file_okay=False,
        help="源码目录（.java/.xml；PTDE 路由不进行源码语义扫描，仅 legacy 路径使用）"
    ),
    output: Path = typer.Option(None, "--output", "-o", help="JSON 工件输出路径（默认仅 stdout）"),
) -> None:
    """执行灌数任务：读 spec → 引擎编排（Dry-Run 优先/拓扑排序/批量写入）→ 输出结果。

    B7 接管切换（T7.6）：执行体路由到 PTDE 真库管线（宿主契约/退出码不变；
    仅支持 jdbc:mysql:// 目标库；legacy 口径 max_rows 钳制随接管按 A1 归一移除）。
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
    if seed is not None:
        spec.seed = seed

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
    from tools.observability_tools import wire_observability_tools

    wire_observability_tools(registry)  # P1-2：CLI agent 也接入在线观测工具
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


# ---------------------------------------------------------------- PTDE（B7 · T7.1）


@ptde_app.command("analyze")
def ptde_analyze(
    project_dir: str = typer.Argument(..., help="项目源码目录"),
    output: Path = typer.Option(None, "--output", "-o", help="JSON 工件输出路径"),
) -> None:
    """分析项目源码：SnapshotBuilder 纯源码证据快照（结构/关系/不变量发现输入）。"""
    from perf_cli.ptde_runner import run_analyze

    raise typer.Exit(run_analyze(project_dir, output=output))


@ptde_app.command("profile")
def ptde_profile(
    profile_file: str = typer.Argument(None, help="画像 JSON 文件（inspect 模式）"),
    collect: bool = typer.Option(False, "--collect", help="采集模式（--config 提供 PtdeConfig）"),
    config: Path = typer.Option(None, "--config", help="PtdeConfig JSON 文件（采集模式必需）"),
    output: Path = typer.Option(None, "--output", "-o", help="JSON 工件输出路径"),
) -> None:
    """统计画像：缺省 inspect（文件画像导入）；--collect 走只读采集（须 --config）。"""
    from perf_cli.ptde_runner import run_profile_collect, run_profile_inspect

    if collect:
        raise typer.Exit(run_profile_collect(
            str(config) if config else None, output=output))
    if not profile_file:
        err_console.print("[red]错误：[/red]inspect 模式须提供画像文件路径（采集模式用 --collect --config）")
        raise typer.Exit(EXIT_INVOCATION_ERROR)
    raise typer.Exit(run_profile_inspect(profile_file, output=output))


@ptde_app.command("plan")
def ptde_plan(
    spec_file: Path = typer.Argument(..., exists=True, readable=True, help="DatasetSpec JSON 文件"),
    def_key: str = typer.Option(None, "--def-key", help="规划登记逻辑名（create 模式必需）"),
    version: str = typer.Option("1", "--version", help="Definition 版本"),
    name: str = typer.Option(None, "--name", help="Definition 名称（缺省用 def-key）"),
    preview: bool = typer.Option(False, "--preview", help="预览模式：规模/分布/预算离线规划（不落库）"),
    seed: int = typer.Option(None, "--seed", help="随机种子（预览/构建透传，QW1 可复现）"),
    db: str = typer.Option(None, "--db", help="CLI 落库地址（缺省 PTDE_CLI_DB 或本地 sqlite）"),
    output: Path = typer.Option(None, "--output", "-o", help="JSON 工件输出路径"),
) -> None:
    """数据集规划：--preview 离线预览；否则注册 Definition（def_key+version 唯一）。"""
    from perf_cli.ptde_runner import run_plan_create, run_plan_preview

    if preview:
        raise typer.Exit(run_plan_preview(str(spec_file), seed=seed, output=output))
    if not def_key:
        err_console.print("[red]错误：[/red]create 模式须提供 --def-key（或改用 --preview）")
        raise typer.Exit(EXIT_INVOCATION_ERROR)
    raise typer.Exit(run_plan_create(
        str(spec_file), def_key, version=version, name=name, db_url=db, output=output))


@ptde_app.command("dataset")
def ptde_dataset(
    action: str = typer.Argument(None, help="动作：create/build/status/verify/list/cleanup"),
    dataset_id: str = typer.Argument(None, help="数据集实例 ID（status/build/verify/cleanup）"),
    def_key: str = typer.Option(None, "--def-key", help="create：来源 Definition 逻辑名"),
    version: str = typer.Option("1", "--version", help="create：Definition 版本"),
    environment: str = typer.Option(None, "--environment", help="create：目标环境标识（非生产）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="build：试运行"),
    seed: int = typer.Option(None, "--seed", help="build：随机种子（可复现）"),
    batch: int = typer.Option(None, "--batch", help="build：分片大小"),
    resume: bool = typer.Option(False, "--resume", help="build：恢复 PARTIAL 实例"),
    manifest_id: str = typer.Option(None, "--manifest-id", help="resume：恢复上下文 Manifest ID"),
    status_filter: str = typer.Option(None, "--status", help="list：状态过滤"),
    env_class: str = typer.Option(None, "--env-class", help="cleanup：环境类别（dedicated_test_schema/dedicated_test_db/shared_test）"),
    approvals: Path = typer.Option(None, "--approvals", help="cleanup：授权 JSON 文件"),
    db: str = typer.Option(None, "--db", help="CLI 落库地址（缺省 PTDE_CLI_DB 或本地 sqlite）"),
    output: Path = typer.Option(None, "--output", "-o", help="JSON 工件输出路径"),
) -> None:
    """数据集生命周期：build/status/verify/cleanup 直连落库层与 READY 门禁。

    生产环境不提供构建与清理入口（总方案 §15.1）；构建执行体未接线时
    显式失败（真库环境批次落地）。
    """
    from perf_cli import ptde_runner

    action = (action or "").strip().lower()
    if action == "list":
        raise typer.Exit(ptde_runner.run_dataset_list(
            status=status_filter, db_url=db, output=output))
    if not action:
        err_console.print("[red]错误：[/red]须提供动作（create/build/status/verify/list/cleanup）")
        raise typer.Exit(EXIT_INVOCATION_ERROR)
    if action == "create":
        if not def_key or not environment:
            err_console.print("[red]错误：[/red]create 须提供 --def-key 与 --environment")
            raise typer.Exit(EXIT_INVOCATION_ERROR)
        raise typer.Exit(ptde_runner.run_dataset_create(
            def_key, environment, version=version, db_url=db, output=output))
    if not dataset_id:
        err_console.print(f"[red]错误：[/red]{action} 须提供数据集实例 ID")
        raise typer.Exit(EXIT_INVOCATION_ERROR)
    if action == "build":
        raise typer.Exit(ptde_runner.run_dataset_build(
            dataset_id, dry_run=dry_run, seed=seed, batch=batch, resume=resume,
            manifest_id=manifest_id, db_url=db, output=output))
    if action == "status":
        raise typer.Exit(ptde_runner.run_dataset_status(
            dataset_id, db_url=db, output=output))
    if action == "verify":
        raise typer.Exit(ptde_runner.run_dataset_verify(
            dataset_id, db_url=db, output=output))
    if action == "cleanup":
        if not env_class:
            err_console.print("[red]错误：[/red]cleanup 须提供 --env-class（环境类别须明确，§13.1）")
            raise typer.Exit(EXIT_INVOCATION_ERROR)
        raise typer.Exit(ptde_runner.run_dataset_cleanup(
            dataset_id, env_class=env_class,
            approvals_file=str(approvals) if approvals else None,
            db_url=db, output=output))
    err_console.print(f"[red]错误：[/red]未知动作 {action!r}（可选 create/build/status/verify/list/cleanup）")
    raise typer.Exit(EXIT_INVOCATION_ERROR)


@ptde_app.command("migrate-legacy")
def ptde_migrate_legacy(
    db: str = typer.Option(None, "--db", help="legacy 与 ptde 同库 DSN（缺省 PTDE_CLI_DB 或本地 sqlite）"),
    output: Path = typer.Option(None, "--output", "-o", help="核对报告输出路径"),
) -> None:
    """A4a 存量迁移（T7.8）：legacy 三表 → ptde_*，幂等可重放 + 行数核对报告。"""
    from perf_cli.ptde_runner import run_legacy_migrate

    raise typer.Exit(run_legacy_migrate(db_url=db, output=output))


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址"),
    port: int = typer.Option(8000, "--port", help="监听端口"),
    knowledge_db: str = typer.Option(None, "--knowledge-db",
                                     help="知识库 SQLite 文件路径"
                                          "（缺省 ~/.perftestor/knowledge.sqlite）"),
) -> None:
    """启动 Python server（FastAPI，AI 域 /api/ai）。

    LLM 配置读 PERF_LLM_MODEL / PERF_LLM_BASE_URL / PERF_LLM_API_KEY；
    未配置时可启动（对话端点将返回 LLM 客户端异常的 error 事件）。
    数据库读 PERF_PY_DATABASE_URL（生产 mysql+pymysql://…；缺省内存库仅测试用）；
    PTDE_REALDB_URL 配置后数据集构建/验证按目标库动态接线真库执行体。
    """
    import uvicorn

    from agents.llm import client_from_env
    from server.app import create_app
    from server.knowledge import KnowledgeService, SqliteKnowledgeStore

    llm = client_from_env()
    if llm is None:
        err_console.print("[yellow]PERF_LLM_MODEL 未配置：server 可启动，"
                          "但 AI 对话端点会返回 LLM 配置错误[/yellow]")
    # P0-3：知识库缺省持久化（内存态重启即清空，评审 P0-3 断点）
    resolved_kb = knowledge_db or os.environ.get("PERF_KNOWLEDGE_DB") \
        or str(Path.home() / ".perftestor" / "knowledge.sqlite")
    Path(resolved_kb).parent.mkdir(parents=True, exist_ok=True)
    knowledge = KnowledgeService(SqliteKnowledgeStore(resolved_kb))
    db_url = os.environ.get("PERF_PY_DATABASE_URL") or ":memory:"
    dialect = "mysql" if "mysql" in db_url else ("postgres" if "postgres" in db_url
                                                else "sqlite")
    err_console.print(f"[cyan]数据库方言: {dialect}（PERF_PY_DATABASE_URL"
                      f"{'未设置，退化为内存库' if 'PERF_PY_DATABASE_URL' not in os.environ else ' 已生效'}）[/cyan]")
    app = create_app(llm=llm, knowledge=knowledge)
    uvicorn.run(app, host=host, port=port, log_level="info")


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
