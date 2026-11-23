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

# K5 生产环境标识（与 server/dataset_service.py 同口径小写包含匹配）
PRODUCTION_MARKERS = ("production", "prod")

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
        sub = stage_scenario(request, stage, with_warmup=(i == 0), task_id=task_id)
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
            scenario = to_scenario(request, task_id=task_id)
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
    """生产执行器：独立 worker 子进程执行（gevent monkey-patch 隔离在子进程内）。

    P1-3 运维控制：
    - 进程注册表 ``_procs``（task_id → Popen）支撑 cancel(SIGTERM→SIGKILL)；
    - 有界并发槽位（PERF_STRESS_MAX_CONCURRENT，默认 2）防失控刷机；
    - watcher 线程在子进程退出后回收注册表并删除临时 payload 文件。
    """

    def __init__(self, db_url: str, python: str | None = None,
                 max_concurrent: int | None = None):
        self._db_url = db_url
        self._python = python or sys.executable
        self._package_root = Path(__file__).resolve().parent.parent
        limit = max_concurrent if max_concurrent is not None \
            else int(os.environ.get("PERF_STRESS_MAX_CONCURRENT") or 2)
        self._max_concurrent = max(1, limit)
        self._slots = threading.BoundedSemaphore(self._max_concurrent)
        self._procs: dict[int, subprocess.Popen] = {}
        self._paths: dict[int, str] = {}
        self._lock = threading.Lock()

    def submit(self, task_id: int, payload: dict) -> None:
        if ":memory:" in (self._db_url or ""):
            raise RuntimeError(
                "内存库不支持子进程执行压测：请配置 PERF_PY_DATABASE_URL 指向共享数据库")
        if not self._slots.acquire(blocking=False):
            raise ValueError(
                f"并发压测任务数已达上限（PERF_STRESS_MAX_CONCURRENT="
                f"{self._max_concurrent}），请等待运行中的任务结束或取消后重试")
        fd, path = tempfile.mkstemp(prefix="perf-py-task-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        env = dict(os.environ)
        env["PERF_PY_DATABASE_URL"] = self._db_url
        try:
            proc = subprocess.Popen(  # noqa: S603 — 固定模块入口，无外部输入拼接
                [self._python, "-m", "server.stress_worker", path, str(task_id)],
                cwd=str(self._package_root), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            os.unlink(path)
            self._slots.release()
            raise
        with self._lock:
            self._procs[task_id] = proc
            self._paths[task_id] = path
        threading.Thread(target=self._reap, args=(task_id, proc, path),
                         daemon=True).start()

    def _reap(self, task_id: int, proc: subprocess.Popen, path: str) -> None:
        """子进程退出回收：注册表摘除 + 临时文件删除 + 并发槽位释放。"""
        try:
            proc.wait()
        except Exception:  # noqa: BLE001 — wait 异常不影响回收
            pass
        with self._lock:
            self._procs.pop(task_id, None)
            self._paths.pop(task_id, None)
        _unlink_quiet(path)
        self._slots.release()

    def cancel(self, task_id: int) -> bool:
        """中止压测子进程：SIGTERM → 最多等 5s → SIGKILL。返回是否命中运行中进程。"""
        with self._lock:
            proc = self._procs.get(task_id)
        if proc is None or proc.poll() is not None:
            return False
        try:
            proc.terminate()  # SIGTERM：worker 信号钩子优雅退出（不再跑后续档位）
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()  # SIGKILL 兜底
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover — 极端场景
                pass
        return True


class InlineRunner:
    """测试/内嵌执行器：后台线程进程内执行（须注入共享 session_factory）。"""

    def __init__(self, session_factory, engine=None):
        self._factory = session_factory
        self._engine = engine

    def submit(self, task_id: int, payload: dict) -> None:
        threading.Thread(target=run_task, daemon=True,
                         args=(task_id, payload, self._factory, self._engine)).start()

    def cancel(self, task_id: int) -> bool:
        """进程内线程不可安全强杀：内嵌执行器不支持取消（显式失败不伪装）。"""
        return False


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except (FileNotFoundError, OSError):
        pass


# ---------------------------------------------------------------- 任务服务


class StressTaskService:
    """对齐 StressTaskService + StressGateService 的写路径门面。"""

    def __init__(self, repo: StressRepository, runner: Any,
                 engine_availability: Callable[[str], bool] | None = None,
                 dataset_lookup: Callable[[str], dict | None] | None = None,
                 dataset_reset: Callable[[str, str], None] | None = None):
        self._repo = repo
        self._runner = runner
        self._availability = engine_availability or engine_available
        # T7.3 数据集绑定反查缝：instanceId → {definitionVersion, schemaFingerprint,
        # validationSummary, status,...}（app 工厂注入 dataset 域查询；未注入时绑定拒绝）
        self._dataset_lookup = dataset_lookup
        # P1-3 数据集重置钩子：resetPolicy 非 none 时按 phase("before"/"after") 回调
        self._dataset_reset = dataset_reset

    # -------------------------------------------------------- POST /tasks

    def create_and_run(self, payload: dict) -> dict:
        """对齐 createAndRun：校验 → 建任务 → 提交执行 → 返回任务 wire 形态。

        返回 dict 与 Java 实体 Jackson 序列化一致；createTime/finishTime/resultJson/
        errorMsg 均为 null（MyBatis 插入后实体未回填，DB 默认值仅入库）。
        """
        # 绑定解析提前：样本 CSV 注入与 K5 生产校验发生在常规校验之前
        binding = self._resolve_dataset_binding(payload)
        self._inject_sample_csv(payload, binding)
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
            # P0-4：持久化完整请求快照（含 body/headers/scenario/warmupSec/stages/
            # dataset），修复验证重放按原样还原，保证与基线压测 100% 同构
            request_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            dataset_instance_id=binding.get("instanceId") if binding else None,
            dataset_definition_version=binding.get("definitionVersion") if binding else None,
            dataset_schema_fingerprint=binding.get("schemaFingerprint") if binding else None,
            dataset_validation_json=binding.get("validationJson") if binding else None,
        )
        self._run_before_reset(task_id, binding)
        self._runner.submit(task_id, payload)
        self._schedule_after_reset(task_id, binding)
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

    def _resolve_dataset_binding(self, payload: dict) -> dict | None:
        """T7.3 数据集绑定解析：payload.dataset.instanceId → READY 实例快照。

        绑定仅允许 READY 实例（验证通过的数据集才可承载压测语义）；
        未注入 dataset_lookup 时携带绑定请求显式拒绝（不静默忽略）。

        P1-3 扩展（可选字段，向后兼容）：
        - ``useSampleCsv``：绑定数据集存在参数样本 CSV 时自动注入压测参数池；
        - ``resetPolicy``（none/after/before_and_after，默认 none）：写压测前后
          数据重置钩子；**K5 红线——environmentId 含生产标识的实例直接拒绝**。
        """
        raw = payload.get("dataset")
        if not isinstance(raw, dict) or raw.get("instanceId") is None:
            return None
        instance_id = str(raw["instanceId"]).strip()
        if not instance_id:
            return None
        if self._dataset_lookup is None:
            raise ValueError("数据集绑定未装配（dataset 查询缝未注入）")
        info = self._dataset_lookup(instance_id)
        if info is None:
            raise ValueError(f"绑定的数据集实例不存在：{instance_id}")
        if info.get("status") != "READY":
            raise ValueError(
                f"绑定的数据集实例非 READY（当前 {info.get('status')}）——"
                "仅验证通过（READY）的数据集可绑定压测任务")
        reset_policy = str(raw.get("resetPolicy") or "none").strip().lower()
        if reset_policy not in ("none", "after", "before_and_after"):
            raise ValueError(
                f"dataset.resetPolicy 非法：{raw.get('resetPolicy')!r}"
                "（可选 none / after / before_and_after）")
        if reset_policy != "none":
            env_label = str(info.get("environmentId") or "").strip().lower()
            if any(m in env_label for m in PRODUCTION_MARKERS):
                raise ValueError(
                    f"生产环境数据集禁止自动重置（K5 红线；environmentId="
                    f"{info.get('environmentId')!r}，resetPolicy={reset_policy}）"
                    "——请在任务创建时移除 resetPolicy 或改用非生产数据集")
        return {
            "instanceId": int(info["rowId"]),
            "definitionVersion": info.get("definitionVersion"),
            "schemaFingerprint": info.get("schemaFingerprint"),
            "validationJson": (json.dumps(info.get("validationSummary"), ensure_ascii=False)
                               if info.get("validationSummary") is not None else None),
            # P1-3 附加（不落库，供注入与重置钩子消费）
            "resetPolicy": reset_policy,
            "resetKey": instance_id,
            "useSampleCsv": bool(raw.get("useSampleCsv")),
            "sampleCsvPath": info.get("sampleCsvPath"),
            "sampleCsvRows": info.get("sampleCsvRows") or 0,
        }

    def _inject_sample_csv(self, payload: dict, binding: dict | None) -> None:
        """useSampleCsv=true 且请求未显式携带 csv 时注入数据集参数样本（内联文本）。"""
        if not binding or not binding.get("useSampleCsv"):
            return
        if payload.get("csv"):
            return  # 用户显式配置的参数池优先，不覆盖
        path = binding.get("sampleCsvPath")
        if not path:
            raise ValueError(
                "dataset.useSampleCsv=true 但绑定数据集实例无参数样本导出"
                "（READY 重建时未生成样本 CSV）")
        sample_path = Path(path)
        if not sample_path.is_file():
            raise ValueError(f"数据集参数样本文件缺失：{path}（请重新构建数据集）")
        try:
            text = sample_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"数据集参数样本读取失败：{exc}") from None
        # 防磁盘撑爆：样本导出侧限行（≤10000），注入侧再限体积（≤1MB，对齐 csv_provider）
        if len(text.encode("utf-8")) > 1024 * 1024:
            raise ValueError("数据集参数样本超过 1MB 上限（请缩小样本规模后重建）")
        payload["csv"] = {"mode": "sequential", "text": text}

    def _run_before_reset(self, task_id: int, binding: dict | None) -> None:
        """P0 修复：``before`` 重置必须在 ``submit`` 之前同步执行。

        resetPolicy == "before_and_after" 时先清理数据集再发压——绝不在
        空库/坏库上盲目发压；before 重置失败 → 任务标 failed 并向上抛出
        拒绝本次发压（压测任务已建档留痕，不静默吞掉）。
        """
        if not binding or binding.get("resetPolicy") != "before_and_after" \
                or self._dataset_reset is None:
            return
        key = binding["resetKey"]
        try:
            self._dataset_reset(key, "before")
        except Exception as exc:  # noqa: BLE001 — 显式拒绝：失败不盲目发压
            log.error("dataset before-reset failed, instance=%s, taskId=%s（拒绝发压）",
                      key, task_id, exc_info=True)
            self._repo.update_failed(
                task_id, f"dataset before-reset failed: {exc}")
            raise ValueError(
                f"数据集 before 重置失败（实例 {key}），已拒绝发压：{exc}") from exc

    def _schedule_after_reset(self, task_id: int, binding: dict | None) -> None:
        """数据集重置钩子调度（after 段）：后台线程轮询任务终局后触发（尽力而为）。"""
        if not binding or binding.get("resetPolicy", "none") == "none" \
                or self._dataset_reset is None:
            return
        key = binding["resetKey"]

        def _invoke(phase: str) -> None:
            try:
                self._dataset_reset(key, phase)
            except Exception:  # noqa: BLE001 — 重置失败不阻断压测主流程（留痕日志）
                log.warning("dataset reset hook failed, instance=%s phase=%s, taskId=%s",
                            key, phase, task_id, exc_info=True)

        if binding.get("resetPolicy") in ("after", "before_and_after"):
            def _wait_then_reset() -> None:
                deadline = time.time() + 3600
                while time.time() < deadline:
                    task = self._repo.find_by_id(task_id)
                    if task is not None and task.status in ("finished", "failed", "cancelled"):
                        break
                    time.sleep(2)
                _invoke("after")

            threading.Thread(target=_wait_then_reset, daemon=True).start()

    # -------------------------------------------------------- POST /tasks/{id}/cancel

    def cancel(self, task_id: int) -> dict:
        """中止运行中/排队中的压测任务：runner SIGTERM→SIGKILL，状态置 cancelled。"""
        task = self._repo.find_by_id(task_id)
        if task is None:
            raise ValueError(f"压测任务不存在：{task_id}")
        if task.status not in ("pending", "running"):
            raise ValueError(f"任务已结束（当前 {task.status}），无法取消")
        cancel_hook = getattr(self._runner, "cancel", None)
        if not callable(cancel_hook) or not cancel_hook(task_id):
            raise ValueError("当前运行器不支持取消（任务可能刚结束或为内嵌执行器）")
        self._repo.update_cancelled(task_id)
        return self._repo.find_by_id(task_id).to_camel()

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
