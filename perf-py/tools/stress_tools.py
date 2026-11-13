"""压测结果工具：读取本地引擎工件（perf-engine -o 产物）并输出核心指标摘要。

Agent 典型链路：发起压测（平台 gate 或本地 perf-engine）→ 本工具读取指标 →
capacity/slowlog/source 工具关联分析。
"""

from __future__ import annotations

import json
import sys
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
    # perf-engine 工件形态 {schema, scenario, generatedAt, result:{...}}：取内层
    if isinstance(raw, dict) and isinstance(raw.get("result"), dict):
        raw = raw["result"]
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


STRESS_RUN_SCHEMA = {
    "type": "object",
    "properties": {
        "scenario_file": {"type": "string", "description": "压测场景 YAML 路径"},
        "engine": {"type": "string", "description": "引擎：locust（默认）/ jmeter",
                   "default": "locust"},
        "output": {"type": "string", "description": "结果工件输出路径（缺省临时文件）"},
        "timeout_sec": {"type": "integer",
                        "description": "执行超时秒数（默认 900；含 warmup 与 ramp-up）",
                        "default": 900},
    },
    "required": ["scenario_file"],
}


def stress_run(args: dict) -> dict:
    """发起本地压测：perf-engine 子进程执行场景，阻塞至完成并返回指标摘要。

    长耗时工具（read_only=False）：gevent monkey-patch 要求独立进程，
    故以 `python -m perf_cli.engine_entry` 子进程形态执行（同 perf-engine CLI）。

    子进程启动用 os.posix_spawn 而非 subprocess.run：宿主进程可能已被
    locust/gevent monkey-patch（fork 在多线程下会竞态死锁），posix_spawn
    绕开 gevent 的 fork 路径；stdout/stderr 落临时文件，waitpid 轮询实现超时。
    """
    import os
    import signal
    import tempfile
    import time

    scenario = args.get("scenario_file")
    if not scenario:
        return {"error": "缺少 scenario_file 参数"}
    if not Path(scenario).is_file():
        return {"error": f"场景文件不存在: {scenario}"}
    engine = args.get("engine") or "locust"
    output = args.get("output") or tempfile.mkstemp(suffix=".json", prefix="perf-agent-")[1]
    timeout = int(args.get("timeout_sec") or 900)

    # engine_app 为 typer 单命令应用：无 run 子命令，直接跟场景参数
    cmd = [sys.executable, "-m", "perf_cli.engine_entry", scenario,
           "--engine", engine, "--output", output]

    with tempfile.TemporaryFile(mode="w+") as out_f, \
            tempfile.TemporaryFile(mode="w+") as err_f:
        file_actions = [
            (os.POSIX_SPAWN_DUP2, out_f.fileno(), 1),
            (os.POSIX_SPAWN_DUP2, err_f.fileno(), 2),
        ]
        try:
            pid = os.posix_spawn(sys.executable, cmd, dict(os.environ),
                                 file_actions=file_actions)
        except OSError as e:
            return {"error": f"无法启动引擎进程: {e}"}

        deadline = time.monotonic() + timeout
        status = None
        while True:
            wpid, st = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                status = st
                break
            if time.monotonic() > deadline:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                return {"error": f"压测超时（>{timeout}s），任务已终止",
                        "timeoutSec": timeout}
            time.sleep(0.1)

        exit_code = os.waitstatus_to_exitcode(status)
        err_f.seek(0)
        stderr_text = err_f.read()

    body: dict = {
        "exitCode": exit_code,
        "engine": engine,
        "artifactPath": output,
    }
    if stderr_text.strip():
        body["stderrTail"] = stderr_text.strip().splitlines()[-5:]
    if exit_code == 2:
        body["error"] = "引擎调用异常（参数/场景校验失败）"
        body["stderrTail"] = stderr_text.strip().splitlines()[-10:]
        return body
    summary = stress_result_read({"path": output})
    if "error" in summary:
        body["error"] = f"压测完成但工件解析失败: {summary['error']}"
        return body
    body["result"] = summary
    return body


STRESS_TOOLS = [
    Tool(
        name="stress_result_read",
        description="读取压测结果工件 JSON（perf-engine -o 产物或平台 resultJson），"
                    "返回 QPS/P50/P95/P99/错误率等核心指标与分档采样点。",
        parameters=STRESS_RESULT_SCHEMA,
        handler=stress_result_read,
    ),
    Tool(
        name="stress_run",
        description="发起本地压测（阻塞执行）：读场景 YAML → perf-engine 子进程发压 → "
                    "返回核心指标摘要与工件路径。长耗时（分钟级），timeout_sec 控制上限。",
        parameters=STRESS_RUN_SCHEMA,
        handler=stress_run,
        read_only=False,
    ),
]
