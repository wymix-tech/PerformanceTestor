"""修复效果自动验证（阶段 5 · 步骤 11）：Java IssueVerifyService 的逐语义移植。

以问题创建时的压测场景快照为基线，重放同场景压测并判定修复是否达标。

判定规则（同时满足才算达标）：
1. P95 相对基线下降 ≥ 30%（P95_DROP_PCT）；
2. 无回归（baseline_compare.diff().regressed == False：QPS 降幅 ≤10%、P95 涨幅 ≤20%、
   错误率上升 ≤5 个百分点）。
压测瓶颈类（stress/stress_bottleneck，吞吐/容量瓶颈）放宽为「或」关系：
P95 下降 ≥ 30% **或** QPS 提升 ≥ 15%（STRESS_QPS_GAIN_PCT），且无回归；
verify_json 附加 criteria 字段说明命中规则。slow_sql / hot_method 仍沿用原「且」规则。
达标则问题自动流转 resolved，并写 verify_json 结论。

异步语义（对齐 Java）：verify 只做「校验快照 + 启动重放 + 落库进行中结论」（毫秒级返回），
等待与判定交给验证线程池（默认 2 并发，队列 32 有界）；前端凭 verifyStatus 轮询。
服务重启残留的 running 结论按 verifyTime + TTL 兜底判中断（不依赖内存态）。
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from server.baseline_compare import diff as baseline_diff

log = logging.getLogger(__name__)

P95_DROP_PCT = 30.0      # 达标阈值：P95 相对基线下降百分比
STRESS_QPS_GAIN_PCT = 15.0  # 压测瓶颈类达标备选阈值：QPS 相对基线提升百分比
STRESS_ISSUE_TYPES = ("stress", "stress_bottleneck")  # 吞吐/容量瓶颈类问题类型
POLL_INTERVAL_MS = 500   # 轮询间隔毫秒
WAIT_BUFFER_SEC = 60     # 轮询等待时在压测时长之上额外预留的收尾时间（秒）
MIN_WAIT_SEC = 30        # 等待预算下限（秒）：防 timeoutSec/durationSec 误配 0 秒判超时
QUEUE_CAPACITY = 32      # 验证线程池排队容量：超出直接拒绝，不做无界堆积


def deadline_ms(now_ms: int, timeout_sec: int, duration_sec: int | None) -> int:
    """轮询等待的绝对截止时间戳（毫秒，对齐 deadlineMs 静态方法，便于单测注入）。

    取「配置超时」与「压测时长 + 收尾缓冲」的较小值：短任务不必空等满配置；
    任务时长超配置时以配置为准，避免无限等待。两侧均有 MIN_WAIT_SEC 下限。
    """
    budget_ms = max(MIN_WAIT_SEC, timeout_sec) * 1000
    warmup = WAIT_BUFFER_SEC if duration_sec is None else duration_sec + WAIT_BUFFER_SEC
    warmup_budget_ms = max(MIN_WAIT_SEC, warmup) * 1000
    return now_ms + min(budget_ms, warmup_budget_ms)


class _VerifyResult:
    """VerifyResult 移植：字段与 Java Jackson getter 序列化（camelCase）一一对应。"""

    def __init__(self, issue_id: int):
        self.issueId = issue_id
        self.noSnapshot = False
        self.replayFailed = False
        self.running = False
        self.passed = False
        self.snapshotTaskId = None
        self.replayTaskId = None
        self.p95Before = 0.0
        self.p95After = 0.0
        self.p95DropPct = 0.0
        self.qpsBefore = 0.0
        self.qpsAfter = 0.0
        self.regressed = False
        self.criteria = ""  # 压测瓶颈类：说明命中的验收规则（非空才写入 verify_json）
        self.status = ""
        self.message = ""
        self.verifyTime = None
        self.rows: list[dict] = []

    def to_camel(self) -> dict:
        """Jackson wire 形态（getter 派生字段名）。"""
        return {
            "issueId": self.issueId, "noSnapshot": self.noSnapshot,
            "replayFailed": self.replayFailed, "running": self.running,
            "passed": self.passed, "snapshotTaskId": self.snapshotTaskId,
            "replayTaskId": self.replayTaskId, "p95Before": self.p95Before,
            "p95After": self.p95After, "p95DropPct": self.p95DropPct,
            "qpsBefore": self.qpsBefore, "qpsAfter": self.qpsAfter,
            "regressed": self.regressed, "status": self.status,
            "message": self.message, "verifyTime": self.verifyTime, "rows": self.rows,
        }


def _fmt(v: float) -> str:
    """对齐 Java fmt（IssueVerifyService 版）：整值去小数，否则一位小数。"""
    return str(int(v)) if v == math.floor(v) else f"{v:.1f}"


class IssueVerifyService:
    """一键验证：校验快照 → 重放压测 → 基线 diff → 达标判定 → 落库结论。"""

    def __init__(self, issue_repo, stress_repo, stress_service,
                 timeout_sec: int = 300, max_concurrent: int = 2,
                 inline: bool = False):
        self._issues = issue_repo
        self._tasks = stress_repo
        self._stress = stress_service
        self.timeout_sec = timeout_sec
        self._inline = inline
        if inline:
            self._executor = None
            self._queued = None
        else:
            self._executor = ThreadPoolExecutor(
                max_workers=max(1, max_concurrent), thread_name_prefix="issue-verify")
            self._queued = 0
        self._lock = threading.Lock()
        # 正在验证的问题 ID：防止同一问题被重复发起重放压测
        self._running_issue_ids: set[int] = set()

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------ 入口

    def verify(self, issue_id: int) -> dict:
        """一键验证（异步）：校验快照 → 发起重放 → 立即返回进行中结论（wire 形态）。"""
        issue = self._require_issue(issue_id)
        snapshot, error = self._resolve_snapshot(issue)
        if error is not None:
            return error.to_camel()
        snapshot_id = snapshot.id
        # 同一问题重复点击：直接返回已有状态，避免并发发起多份重放压测
        with self._lock:
            if issue_id in self._running_issue_ids:
                return self._pending(issue, snapshot_id, None,
                                     "已有一次验证在进行中，请勿重复发起").to_camel()
            queued = self._queued
            if not self._inline:
                # 提交判定任务前先检查验证线程池队列余量：若先发起重放、后因队列满被拒，
                # 已发起的重放压测无法收回——白白对目标服务打一轮压测，还留下孤儿任务
                if queued >= QUEUE_CAPACITY:
                    rejected = self._replay_failed(issue, snapshot_id, None,
                                                   "当前等待验证的任务过多，请稍后再试")
                    self._persist(rejected)
                    return rejected.to_camel()
                self._queued = queued + 1
            self._running_issue_ids.add(issue_id)

        try:
            replay = self._stress.create_and_run(self._to_request(snapshot))
        except Exception as exc:  # noqa: BLE001
            self._release(issue_id)
            log.warning("verify replay start failed, issue=%s: %s", issue_id, exc)
            r = self._replay_failed(issue, snapshot_id, None,
                                    f"重放压测启动失败：{exc}")
            self._persist(r)
            return r.to_camel()

        started = self._pending(issue, snapshot_id, replay["id"],
                                f"已发起重放压测（任务 #{replay['id']}），"
                                "完成后自动给出验证结论")
        # 进行中状态也要落库：进程重启或前端刷新后仍能查到"有验证在跑"
        self._persist(started)
        if self._inline:
            # :memory: 测试库：同步判定（语义等价异步完成，含 running 标记释放）
            self._guarded_judge(issue, snapshot, replay["id"], snapshot_id, issue_id)
        else:
            self._executor.submit(self._guarded_judge, issue, snapshot,
                                  replay["id"], snapshot_id, issue_id)
        return started.to_camel()

    def run_verify(self, issue_id: int) -> dict:
        """同步执行完整验证（发起重放 + 轮询等待 + 判定 + 落库）。

        仅供单测直接驱动判定逻辑：线上入口必须是异步的 verify，否则一次验证
        最长会占死 HTTP 工作线程 perf.verify.timeout-sec 秒。
        """
        issue = self._require_issue(issue_id)
        snapshot, error = self._resolve_snapshot(issue)
        if error is not None:
            return error.to_camel()
        try:
            replay = self._stress.create_and_run(self._to_request(snapshot))
        except Exception as exc:  # noqa: BLE001
            log.warning("verify replay start failed, issue=%s: %s", issue_id, exc)
            r = self._replay_failed(issue, snapshot.id, None,
                                    f"重放压测启动失败：{exc}")
            self._persist(r)
            return r.to_camel()
        r = self._judge(issue, snapshot, replay["id"])
        self._persist(r)
        return r.to_camel()

    # ------------------------------------------------------------ 状态查询

    def verify_status(self, issue_id: int) -> dict | None:
        """读取最近一次验证结论（含进行中状态）——前端据此轮询；尚无结论返回 None。"""
        issue = self._require_issue(issue_id)
        json_text = issue.verify_json
        if not json_text or not json_text.strip():
            return None
        try:
            parsed = json.loads(json_text)
        except ValueError:
            log.warning("parse verify json failed, issue=%s", issue_id)
            return None
        # 服务重启会留下"进行中"的残留结论，靠内存态判断不可靠，一律按时间兜底：
        # 超出等待上限仍在 running，说明等待线程已随进程消失，需提示用户重新发起
        if parsed.get("running") is True and self._stale_running(issue.verify_time):
            parsed["running"] = False
            parsed["replayFailed"] = True
            parsed["message"] = "上次验证已中断（服务重启或验证进程异常），请重新发起验证"
        return parsed

    # ------------------------------------------------------------ 判定

    def _guarded_judge(self, issue, snapshot, replay_id: int,
                       snapshot_id: int, issue_id: int) -> None:
        try:
            r = self._judge(issue, snapshot, replay_id)
            self._persist(r)
        except Exception as exc:  # noqa: BLE001
            log.warning("verify issue %s failed: %s", issue_id, exc)
            self._persist(self._replay_failed(issue, snapshot_id, replay_id,
                                              f"验证过程异常：{exc}"))
        finally:
            self._release(issue_id)

    def _judge(self, issue, snapshot, replay_id: int) -> _VerifyResult:
        """轮询等待重放任务完成 → 与快照基线 diff → 达标判定（阻塞，不落库）。"""
        finished = self._await_finished(replay_id, snapshot.duration_sec)
        if finished is None or finished.status != "finished":
            reason = (f"重放压测在 {self.timeout_sec} 秒内未完成" if finished is None
                      else f"重放压测失败：{finished.error_msg}")
            return self._replay_failed(issue, snapshot.id, replay_id, reason)

        d = baseline_diff(self._tasks, finished.id, snapshot.id)
        p95_before = self._metric(snapshot, "p95Ms")
        p95_after = self._metric(finished, "p95Ms")
        drop_pct = 0 if p95_before <= 0 else (p95_before - p95_after) / p95_before * 100
        qps_before = self._metric(snapshot, "qps")
        qps_after = self._metric(finished, "qps")
        qps_gain_pct = 0 if qps_before <= 0 else (qps_after - qps_before) / qps_before * 100
        is_stress = issue.issue_type in STRESS_ISSUE_TYPES

        r = _VerifyResult(issue.id)
        r.snapshotTaskId = snapshot.id
        r.replayTaskId = finished.id
        r.p95Before = p95_before
        r.p95After = p95_after
        r.p95DropPct = _round(drop_pct)
        r.qpsBefore = qps_before
        r.qpsAfter = qps_after
        r.regressed = d["regressed"]
        r.rows = [{"label": row["label"], "base": row["base"], "cur": row["cur"],
                   "deltaPct": row["deltaPct"], "improved": row["improved"],
                   "regressed": row["regressed"]} for row in d["rows"]]
        r.verifyTime = datetime.now()
        if is_stress:
            # 吞吐/容量瓶颈：「P95 下降 ≥30% 或 QPS 提升 ≥15%」，且无回归
            r.passed = not d["regressed"] and (
                drop_pct >= P95_DROP_PCT or qps_gain_pct >= STRESS_QPS_GAIN_PCT)
            r.criteria = (
                ("P95 下降 ≥30%" if drop_pct >= P95_DROP_PCT else "QPS 提升 ≥15%")
                + "，且无指标回归"
                if r.passed else "P95 下降 ≥30% 或 QPS 提升 ≥15%，且无指标回归")
        else:
            # slow_sql / hot_method：沿用 P95 下降 ≥30% 且无回归
            r.passed = drop_pct >= P95_DROP_PCT and not d["regressed"]
        r.status = "resolved" if r.passed else issue.status
        r.message = (
            f"验证通过：P95 由 {_fmt(p95_before)}ms 降至 {_fmt(p95_after)}ms"
            f"（下降 {_fmt(r.p95DropPct)}%），无指标回归，问题已自动关闭"
            if r.passed and drop_pct >= P95_DROP_PCT else
            (f"验证通过：QPS 由 {_fmt(qps_before)} 提升至 {_fmt(qps_after)}"
             f"（提升 {_fmt(_round(qps_gain_pct))}%，命中 QPS ≥15% 验收规则），"
             "无指标回归，问题已自动关闭"
             if r.passed else self._not_passed_message(drop_pct, d, p95_before, p95_after)))
        return r

    def _not_passed_message(self, drop_pct: float, d: dict,
                            p95_before: float, p95_after: float) -> str:
        """未达标文案（逐字对齐 buildNotPassedMessage）。"""
        sb = (f"验证未达标：P95 由 {_fmt(p95_before)}ms 变为 {_fmt(p95_after)}ms"
              f"（下降 {_fmt(drop_pct)}%，需 ≥ {int(P95_DROP_PCT)}%）")
        if d["regressed"]:
            parts = [f"{row['label']} {row['deltaPct']}" for row in d["rows"]
                     if row["regressed"]]
            sb += "；且存在指标回归：" + "、".join(parts)
        sb += "。问题保持原状态，建议复查修复是否生效。"
        return sb

    def _await_finished(self, task_id: int, duration_sec: int | None):
        """轮询等待压测任务完成（finished/failed）；超时返回 None。"""
        deadline = deadline_ms(int(time.time() * 1000), self.timeout_sec, duration_sec)
        while True:
            t = self._tasks.find_by_id(task_id)
            if t is not None and t.status in ("finished", "failed"):
                return t
            if time.time() * 1000 >= deadline:
                return None
            time.sleep(POLL_INTERVAL_MS / 1000)

    def _stale_running(self, verify_time) -> bool:
        return verify_time is not None and (
            verify_time.timestamp() + self._running_ttl_sec() < time.time())

    def _running_ttl_sec(self) -> int:
        """进行中状态的最大存活时长（秒）：等待上限 + 收尾缓冲。"""
        return max(MIN_WAIT_SEC, self.timeout_sec) + WAIT_BUFFER_SEC

    # ------------------------------------------------------------ 快照与请求

    def _resolve_snapshot(self, issue):
        """解析场景快照；不可用时返回（None, noSnapshot 结论）。"""
        snapshot_id = issue.stress_task_id
        if snapshot_id is None or snapshot_id <= 0:
            return None, self._no_snapshot(issue, snapshot_id,
                                           "该问题未关联压测场景快照，无法自动验证；"
                                           "可从压测任务创建问题以建立场景快照")
        snapshot = self._tasks.find_by_id(snapshot_id)
        if snapshot is None:
            return None, self._no_snapshot(
                issue, snapshot_id,
                f"场景快照压测任务 #{snapshot_id} 已不存在，无法自动验证")
        return snapshot, None

    @staticmethod
    def _to_request(snapshot) -> dict:
        """快照任务 → 重放请求（P0-4：优先还原创建时的完整请求快照）。

        request_json 持久化了建任务时的完整请求（body/headers/scenario/warmupSec/
        rampUpSec/stages/csv/dataset 等），重放直接反序列化原快照并覆盖 taskName，
        保证验证压测与基线压测 100% 同构；快照缺失（旧任务/坏 JSON）回退到
        既有字段重建（URL/方法/并发/时长/限流/应用一致）。
        """
        if snapshot.request_json:
            try:
                replay = json.loads(snapshot.request_json)
                if isinstance(replay, dict):
                    replay = dict(replay)
                    replay["taskName"] = f"修复验证重放 #{snapshot.id}"
                    return replay
            except (ValueError, TypeError):
                log.warning("verify replay: task #%s request_json 损坏，回退字段重建",
                            snapshot.id)
        return {
            "taskName": f"修复验证重放 #{snapshot.id}",
            "engineType": snapshot.engine_type or "http",
            "targetUrl": snapshot.target_url,
            "appName": snapshot.app_name,
            "method": snapshot.method or "GET",
            "concurrency": snapshot.concurrency if snapshot.concurrency is not None else 1,
            "durationSec": snapshot.duration_sec if snapshot.duration_sec is not None else 60,
            "qpsLimit": snapshot.qps_limit if snapshot.qps_limit is not None else 0,
        }

    def _metric(self, task, field: str) -> float:
        """读取压测结果 JSON 中的指标（缺省 0）。"""
        try:
            node = json.loads(task.result_json or "{}")
            value = node.get(field)
            return float(value) if value is not None else 0.0
        except (ValueError, TypeError):
            log.warning("parse stress result failed, taskId=%s", task.id)
            return 0.0

    # ------------------------------------------------------------ 结论构造与落库

    def _no_snapshot(self, issue, snapshot_id, message: str) -> _VerifyResult:
        r = _VerifyResult(issue.id)
        r.snapshotTaskId = snapshot_id
        r.noSnapshot = True
        r.status = issue.status
        r.message = message
        r.verifyTime = datetime.now()
        return r

    def _replay_failed(self, issue, snapshot_id, replay_task_id, message: str) -> _VerifyResult:
        """重放/判定失败结论（保持问题原状态）。"""
        r = _VerifyResult(issue.id)
        r.snapshotTaskId = snapshot_id
        r.replayTaskId = replay_task_id
        r.replayFailed = True
        r.status = issue.status
        r.message = message
        r.verifyTime = datetime.now()
        return r

    def _pending(self, issue, snapshot_id, replay_task_id, message: str) -> _VerifyResult:
        """进行中结论：落库后前端可凭 verifyStatus 轮询，进程重启也能恢复展示。"""
        r = _VerifyResult(issue.id)
        r.snapshotTaskId = snapshot_id
        r.replayTaskId = replay_task_id
        r.running = True
        r.status = issue.status
        r.message = message
        r.verifyTime = datetime.now()
        return r

    def _persist(self, r: _VerifyResult) -> None:
        """落库：写 verify_json / verify_time，达标时置 resolved。

        无快照属于"无法验证"，不写入结论（避免污染问题状态与验证时间）。
        """
        if r.noSnapshot:
            return
        try:
            self._issues.update_issue_verify(r.issueId, self._to_json(r),
                                             r.verifyTime, r.status)
        except Exception as exc:  # noqa: BLE001
            log.warning("persist verify result failed, issue=%s: %s", r.issueId, exc)

    @staticmethod
    def _to_json(r: _VerifyResult) -> str:
        """结论 JSON（前端直接展示，键序与 Java ObjectNode put 序列一致）。"""
        node: dict = {
            "issueId": r.issueId,
            "passed": r.passed,
            "replayFailed": r.replayFailed,
            "running": r.running,
            "snapshotTaskId": 0 if r.snapshotTaskId is None else r.snapshotTaskId,
            "replayTaskId": 0 if r.replayTaskId is None else r.replayTaskId,
            "p95Before": r.p95Before,
            "p95After": r.p95After,
            "p95DropPct": r.p95DropPct,
            "qpsBefore": r.qpsBefore,
            "qpsAfter": r.qpsAfter,
            "regressed": r.regressed,
            "status": r.status,
            "message": r.message,
            "verifyTime": "" if r.verifyTime is None else r.verifyTime.isoformat(),
        }
        if r.criteria:
            node["criteria"] = r.criteria
        rows = []
        for row in r.rows or []:
            item: dict = {}
            for k, v in row.items():
                if isinstance(v, bool):
                    item[k] = v
                elif isinstance(v, (int, float)):
                    item[k] = float(v)
                else:
                    item[k] = "" if v is None else str(v)
            rows.append(item)
        node["rows"] = rows
        try:
            return json.dumps(node, ensure_ascii=False)
        except (TypeError, ValueError):
            return "{}"

    def _require_issue(self, issue_id: int):
        issue = self._issues.issue_by_id(issue_id)
        if issue is None:
            raise ValueError(f"问题不存在: {issue_id}")
        return issue

    def _release(self, issue_id: int) -> None:
        with self._lock:
            self._running_issue_ids.discard(issue_id)
            if not self._inline and self._queued > 0:
                self._queued -= 1


def _round(v: float) -> float:
    return round(v * 10) / 10
