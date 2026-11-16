"""stress 写路径服务（阶段 5 · 步骤 6）：POST /tasks + /gate 背后的任务创建与执行。

对齐 Java StressTaskService / StressGateService：
- createAndRun：校验 → 建任务（pending）→ 异步执行（Java 固定 4 线程池）；
- runInternal：running → 引擎执行（采样实时落 stress_sample）→ 结果 JSON 落库；
  异常 → failed + finish_time=NOW()；
- runGate：超时计算（下限=预热+时长+爬坡+各档+60s 余量，硬上限 1800s）→
  创建任务 → 轮询等待 → 基线比对 → GateVerdict（HTTP 恒 200，passed 由脚本判定）。

执行模型差异（有意为之）：Java 引擎在 JVM 进程内跑线程；Python 侧 locust 需要
gevent monkey-patch，不能与 uvicorn 异步运行时共存——默认 SubprocessRunner 在
独立 worker 子进程执行（`python -m server.stress_worker`），测试注入 InlineRunner。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.result import Sample, StagePoint, StressTaskResult
from server.baseline_compare import diff as baseline_diff
from server.notify import send as notify_send
from server.stress_repo import StressRepository
from server.stress_request import (
    EXECUTE_ENGINE_OF_HTTP,
    HTTP_ENGINE,
    StageData,
    StressTaskRequestData,
    parse_stress_request,
    stage_scenario,
    to_scenario,
    validate_stress_request,
)

log = logging.getLogger(__name__)

# 对齐 StressGateService 默认配置（perf.verify.timeout-sec / perf.gate.max-timeout-sec
# / perf.gate.timeout-overhead-sec）
DEFAULT_GATE_TIMEOUT_SEC = 300
MAX_GATE_TIMEOUT_SEC = 1800
GATE_TIMEOUT_OVERHEAD_SEC = 60


def engine_available(engine_type: str) -> bool:
    """引擎可用性（validateCommon 前置校验）。jmeter 探测可执行文件；http→locust 探测安装。"""
    t = engine_type if engine_type and engine_type.strip() else HTTP_ENGINE
    if t == "jmeter":
        from engine.jmeter_engine import JMeterEngine

        return bool(JMeterEngine()._jmeter_bin)
    import importlib.util

    return importlib.util.find_spec("locust") is not None


def _default_engine(request: StressTaskRequestData):
    """对齐 resolveEngine：场景模式强制回退 http（→locust）；http→locust 等价承担。"""
    t = request.engine_type if request.engine_type and request.engine_type.strip() else HTTP_ENGINE
    if request.is_scenario_mode and t != HTTP_ENGINE:
        log.warning("压测引擎「%s」不支持多接口场景，自动回退 http 引擎执行（步骤数 %s）",
                    t, len(request.scenario or []))
        t = HTTP_ENGINE
    if t == "jmeter":
        from engine.jmeter_engine import JMeterEngine

        return JMeterEngine()
    from engine.locust_engine import LocustEngine

    return LocustEngine()


# ---------------------------------------------------------------- 引擎执行


def _fallback_name(request: StressTaskRequestData) -> str:
    """对齐 createAndRun 的任务名兜底：场景模式取摘要，单接口取 URL。"""
    if request.is_scenario_mode:
        return f"场景压测（{len(request.scenario or [])} 步）"
    return request.target_url or ""


def _persist_sample(repo: StressRepository, task_id: int, sample: Sample) -> None:
    """对齐 persistSample：epoch ms → LocalDateTime；失败仅告警不影响压测。"""
    try:
        repo.insert_sample(
            task_id=task_id,
            sample_time=datetime.fromtimestamp(sample.sample_time / 1000.0),
            concurrency=sample.concurrency,
            qps=sample.qps,
            p50_ms=sample.p50_ms,
            p95_ms=sample.p95_ms,
            p99_ms=sample.p99_ms,
            error_count=sample.error_count,
        )
    except Exception:  # noqa: BLE001 — 对齐 Java：采样落库失败仅日志
        log.warning("persist stress sample failed, taskId=%s", task_id)


def _run_stages(request: StressTaskRequestData, engine, repo: StressRepository,
                task_id: int) -> StressTaskResult:
    """分档模式：对齐 HttpStressEngine.run 分档分支——仅首档 warmup，逐档产 StagePoint；
    汇总 total/errorRate，qps/p50/p95/p99 取最后一档（max/avg 不填，Java 同款留 0）。"""
    stages = request.resolve_stages() or []
    points: list[StagePoint] = []
    samples: list[Sample] = []
    warnings: list[str] = []
    total = 0
    errors = 0
    last: StressTaskResult | None = None
    for i, stage in enumerate(stages):
        sub = stage_scenario(request, stage, with_warmup=(i == 0))
        r = engine.run(sub, listener=lambda s, _t=task_id: _persist_sample(repo, _t, s))
        points.append(StagePoint(
            concurrency=stage.concurrency, qps=r.qps, p50_ms=r.p50_ms,
            p95_ms=r.p95_ms, p99_ms=r.p99_ms, error_rate=r.error_rate,
            total_requests=r.total_requests))
        total += r.total_requests
        errors += r.error_count
        samples.extend(r.samples)
        warnings.extend(r.warnings)
        last = r
    error_rate = 0 if total == 0 else errors * 100.0 / total
    return StressTaskResult(
        task_id=task_id,
        engine_type=last.engine_type if last else EXECUTE_ENGINE_OF_HTTP,
        target_url=request.target_url or "",
        concurrency=request.concurrency,
        duration_sec=request.duration_sec,
        warmup_sec=request.warmup_sec,
        ramp_up_sec=request.ramp_up_sec,
        total_requests=total,
        error_count=errors,
        error_rate=error_rate,
        qps=points[-1].qps if points else 0.0,
        p50_ms=points[-1].p50_ms if points else 0.0,
        p95_ms=points[-1].p95_ms if points else 0.0,
        p99_ms=points[-1].p99_ms if points else 0.0,
        samples=samples,
        stage_points=points,
        warnings=list(dict.fromkeys(warnings)),  # 对齐 LinkedHashSet 保序去重
    )


def run_task(task_id: int, payload: dict, session_factory, engine=None) -> None:
    """对齐 runInternal：running → 引擎执行 → 结果落库 / 失败落库。

    供 InlineRunner（测试）与 stress_worker 子进程（生产）共用。
    engine 可注入测试桩；缺省按请求 engineType 解析（场景模式回退 http→locust）。
    """
    request = parse_stress_request(payload)
    repo = StressRepository(session_factory)
    try:
        repo.update_status(task_id, "running")
        engine = engine or _default_engine(request)
        if request.is_scenario_mode or request.resolve_stages() is None:
            scenario = to_scenario(request)
            result = engine.run(
                scenario, listener=lambda s: _persist_sample(repo, task_id, s))
            result.task_id = task_id
            # 单档模式：Java 会填 stagePoints=[本档点]（容量曲线起点），对齐补齐
            if not result.stage_points:
                result.stage_points = [StagePoint(
                    concurrency=result.concurrency, qps=result.qps,
                    p50_ms=result.p50_ms, p95_ms=result.p95_ms, p99_ms=result.p99_ms,
                    error_rate=result.error_rate, total_requests=result.total_requests)]
        else:
            result = _run_stages(request, engine, repo, task_id)
        result_json = json.dumps(result.to_java_json(), ensure_ascii=False,
                                 separators=(",", ":"))
        repo.update_finished(task_id, result_json, datetime.now())
        log.info("stress task finished, id=%s, total=%s, qps=%s",
                 task_id, result.total_requests, result.qps)
    except Exception as exc:  # noqa: BLE001 — 对齐 updateFailed 兜底
        log.error("stress task failed, id=%s", task_id, exc_info=exc)
        repo.update_failed(task_id, str(exc) or exc.__class__.__name__)


# ---------------------------------------------------------------- 执行器


class SubprocessRunner:
    """生产执行器：独立 worker 子进程执行（gevent monkey-patch 隔离在子进程内）。"""

    def __init__(self, db_url: str, python: str | None = None):
        self._db_url = db_url
        self._python = python or sys.executable
        self._package_root = Path(__file__).resolve().parent.parent

    def submit(self, task_id: int, payload: dict) -> None:
        if ":memory:" in (self._db_url or ""):
            raise RuntimeError(
                "内存库不支持子进程执行压测：请配置 PERF_PY_DATABASE_URL 指向共享数据库")
        fd, path = tempfile.mkstemp(prefix="perf-py-task-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        env = dict(os.environ)
        env["PERF_PY_DATABASE_URL"] = self._db_url
        try:
            subprocess.Popen(  # noqa: S603 — 固定模块入口，无外部输入拼接
                [self._python, "-m", "server.stress_worker", path, str(task_id)],
                cwd=str(self._package_root), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            os.unlink(path)
            raise


class InlineRunner:
    """测试/内嵌执行器：后台线程进程内执行（须注入共享 session_factory）。"""

    def __init__(self, session_factory, engine=None):
        self._factory = session_factory
        self._engine = engine

    def submit(self, task_id: int, payload: dict) -> None:
        threading.Thread(target=run_task, daemon=True,
                         args=(task_id, payload, self._factory, self._engine)).start()


# ---------------------------------------------------------------- 任务服务


class StressTaskService:
    """对齐 StressTaskService + StressGateService 的写路径门面。"""

    def __init__(self, repo: StressRepository, runner: Any,
                 engine_availability: Callable[[str], bool] | None = None):
        self._repo = repo
        self._runner = runner
        self._availability = engine_availability or engine_available

    # -------------------------------------------------------- POST /tasks

    def create_and_run(self, payload: dict) -> dict:
        """对齐 createAndRun：校验 → 建任务 → 提交执行 → 返回任务 wire 形态。

        返回 dict 与 Java 实体 Jackson 序列化一致；createTime/finishTime/resultJson/
        errorMsg 均为 null（MyBatis 插入后实体未回填，DB 默认值仅入库）。
        """
        request = parse_stress_request(payload)
        self._validate(request)

        task_name = request.task_name if request.task_name and request.task_name.strip() \
            else _fallback_name(request)
        target_url = _fallback_name(request) if request.is_scenario_mode \
            else (request.target_url or "")
        app_name = request.app_name if request.app_name and request.app_name.strip() else ""
        # 版本快照：发起压测时记录应用当前版本（agent 心跳维护），只解析一次
        app_version = self._repo.find_app_version(app_name)
        task_id = self._repo.insert_task(
            task_name=task_name,
            engine_type=request.engine_type,
            target_url=target_url,
            app_name=app_name,
            app_version=app_version,
            method=request.method,
            concurrency=request.concurrency,
            duration_sec=request.duration_sec,
            qps_limit=request.qps_limit,
        )
        self._runner.submit(task_id, payload)
        return {
            "id": task_id,
            "taskName": task_name,
            "engineType": request.engine_type,
            "targetUrl": target_url,
            "method": request.method,
            "appName": app_name,
            "appVersion": app_version,
            "concurrency": request.concurrency,
            "durationSec": request.duration_sec,
            "qpsLimit": request.qps_limit,
            "status": "pending",
            "resultJson": None,
            "errorMsg": None,
            "createTime": None,
            "finishTime": None,
        }

    def _validate(self, request: StressTaskRequestData) -> None:
        """对齐 validateCommon 的引擎可用性校验（其余文案在 stress_request 层）。"""
        validate_stress_request(request)
        engine_type = request.engine_type if request.engine_type and request.engine_type.strip() \
            else HTTP_ENGINE
        if not self._availability(engine_type):
            if engine_type == "jmeter":
                raise ValueError(
                    "压测引擎「jmeter」不可用：JMeter 需配置 perf.stress.jmeter-home"
                    "（或环境变量 JMETER_HOME）指向安装目录，或将 jmeter 加入 PATH；"
                    "也可改用默认 http 引擎")
            raise ValueError(
                f"压测引擎「{engine_type}」不可用：locust 未安装"
                "（uv sync --extra locust），或改用已安装的引擎")

    # -------------------------------------------------------- POST /gate

    def run_gate(self, payload: dict, baseline_task_id: int, timeout_sec: int,
                 fail_on_no_baseline: bool, webhook: str | None) -> dict:
        """对齐 StressGateService.runGate：创建压测 → 等待完成 → 基线比对 → 结论。"""
        request = parse_stress_request(payload)
        timeout = timeout_sec if timeout_sec > 0 else DEFAULT_GATE_TIMEOUT_SEC
        min_timeout = self._estimate_min_timeout(request)
        if timeout < min_timeout:
            log.warning("门禁超时 %ss 小于预估最短耗时 %ss，自动放宽", timeout, min_timeout)
            timeout = min_timeout
        if timeout > MAX_GATE_TIMEOUT_SEC:
            log.warning("门禁超时 %ss 超过硬上限 %ss，按上限等待", timeout, MAX_GATE_TIMEOUT_SEC)
            timeout = MAX_GATE_TIMEOUT_SEC

        task = self.create_and_run(payload)
        verdict: dict[str, Any] = {
            "taskId": task["id"], "taskName": task["taskName"], "status": "",
            "passed": False, "noBaseline": False, "regressed": False,
            "baselineTaskId": 0, "rows": [], "summary": "",
        }
        status = self._await_finish(task["id"], timeout)
        verdict["status"] = status
        if status != "finished":
            verdict["summary"] = f"压测未在 {timeout}s 内成功完成（状态：{status}）"
            self._notify(webhook, verdict)
            return verdict

        d = baseline_diff(self._repo, task["id"], baseline_task_id)
        verdict["rows"] = d["rows"]
        verdict["noBaseline"] = d["noBaseline"]
        if d["baseline"] is not None and d["baseline"].id is not None:
            verdict["baselineTaskId"] = d["baseline"].id
        verdict["regressed"] = d["regressed"]

        if d["noBaseline"]:
            verdict["passed"] = not fail_on_no_baseline
            verdict["summary"] = "无可用基线，按 fail-on-no-baseline 策略判定为失败" \
                if fail_on_no_baseline else \
                "无可用基线（首次执行或历史任务缺失），本次结果已记录为后续基线，门禁放行"
        elif d["regressed"]:
            verdict["passed"] = False
            verdict["summary"] = (f"性能回归：与基线任务 #{verdict['baselineTaskId']} 相比，"
                                  + self._describe_regressions(d["rows"]))
        else:
            verdict["passed"] = True
            verdict["summary"] = (f"性能达标：与基线任务 #{verdict['baselineTaskId']} 相比无回归")
        self._notify(webhook, verdict)
        return verdict

    def _await_finish(self, task_id: int, timeout_sec: int) -> str:
        """对齐 awaitFinish：轮询任务状态（100ms-1s 分片睡眠）。"""
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            t = self._repo.find_by_id(task_id)
            if t is None:
                return "missing"
            status = t.status or ""
            if status in ("finished", "failed"):
                return status
            time.sleep(min(1.0, max(0.1, deadline - time.monotonic())))
        return "timeout"

    @staticmethod
    def _estimate_min_timeout(request: StressTaskRequestData) -> int:
        """对齐 estimateMinTimeout：注意 Java 用原始 stages（简写 stageConcurrencies 不计）。"""
        base = request.duration_sec + request.warmup_sec + request.ramp_up_sec
        for s in request.stages or []:
            base += s.duration_sec
        return base + GATE_TIMEOUT_OVERHEAD_SEC

    @staticmethod
    def _describe_regressions(rows: list[dict]) -> str:
        """对齐 describeRegressions：回归行「label base → cur（deltaPct）」，分号连接。"""
        parts = [f"{r['label']} {r['base']} → {r['cur']}（{r['deltaPct']}）"
                 for r in rows if r["regressed"]]
        return "；".join(parts) if parts else "错误率上升超阈值"

    @staticmethod
    def _notify(webhook: str | None, verdict: dict) -> None:
        """对齐 notify：仅失败时告警，尽力而为。"""
        if verdict["passed"]:
            return
        try:
            notify_send(webhook, f"性能门禁未通过 #{verdict['taskId']}", verdict["summary"])
        except Exception as exc:  # noqa: BLE001 — 告警失败不影响门禁结论
            log.warning("perf gate notify failed, taskId=%s, error=%s", verdict["taskId"], exc)


# 兼容旧引用：StageData 供 gate 超时估算等场景显式使用
__all__ = [
    "GATE_TIMEOUT_OVERHEAD_SEC",
    "InlineRunner",
    "StressTaskService",
    "SubprocessRunner",
    "StageData",
    "engine_available",
    "run_task",
]
