"""T7.4 可观测性（实施方案 §10.2 T7.4；总方案 §16.1/§16.2）。

- 指标：§16.1 关键指标全集的记录与聚合快照（生成/装载吞吐、端到端时长、
  资源、Staging Size、日志增长、恢复时长、预估 vs 实际存储）；
- 结构化日志：§16.2 字段集逐条携带（dataset_id/plan_id/task_id/table_id/
  shard_id/chunk_id/worker_id/database_id/operation/status/duration/错误分类）；
- **脱敏硬约束**：禁止口令/明文敏感值入日志——键名敏感字段打码、
  DSN 内嵌凭据打码（§16.2「禁止把数据库口令、生产明文数据和敏感字段值
  写入日志」；A5 凭据脱敏口径延续）。

本模块为纯进程内组件（无外部依赖），供 CLI/API/管线各层统一接入。
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime

from pydantic import Field

from ptde_common.model_base import VersionedModel

__all__ = [
    "LOG_FIELDS",
    "METRIC_NAMES",
    "MetricSample",
    "ObservabilityRecorder",
    "build_log_record",
    "classify_error",
    "is_sensitive_key",
    "redact_dsn",
    "redact_mapping",
]

# ---------------------------------------------------------------- §16.1 指标名全集

METRIC_NAMES: tuple[str, ...] = (
    "generation_throughput",       # 每秒生成记录数（rows/s）
    "load_throughput",             # 每秒数据库装载记录数（rows/s）
    "end_to_end_duration",         # 生成到验证完成总耗时（s）
    "generator_cpu",               # 生成 Worker CPU（%）
    "peak_memory",                 # 峰值内存（bytes）
    "staging_size",                # 中间文件空间（bytes）
    "database_cpu",                # 目标数据库 CPU（%）
    "database_io",                 # 目标数据库 IO（ops/s）
    "network_throughput",          # 生成端/暂存层/数据库间网络吞吐（bytes/s）
    "transaction_log_growth",      # 事务日志增长（bytes）
    "load_errors",                 # 装载错误数
    "load_warnings",               # 装载警告数
    "resume_recovery_time",        # 故障恢复时间（s）
    "validation_duration",         # 验证耗时（s）
    "estimated_vs_actual_storage",  # 预估与实际存储偏差（bytes）
)

# ---------------------------------------------------------------- §16.2 日志字段集

LOG_FIELDS: tuple[str, ...] = (
    "dataset_id", "plan_id", "task_id", "table_id", "shard_id", "chunk_id",
    "worker_id", "database_id", "operation", "status", "duration",
    "error_class",
)

# 键名含以下片段视为敏感（A5 凭据脱敏口径；大小写不敏感）
_SENSITIVE_KEY_FRAGMENTS: tuple[str, ...] = (
    "password", "passwd", "pwd", "secret", "token", "credential",
    "api_key", "apikey", "authorization", "private_key", "dsn",
)
_REDACTED = "***"

# DSN 内嵌凭据：scheme://user:password@host → scheme://user:***@host
_DSN_CREDENTIAL_RE = re.compile(r"^(?P<scheme>[A-Za-z][\w+.-]*://)(?P<user>[^:@/\s]+):(?P<secret>[^@/\s]+)@")


def is_sensitive_key(key: str) -> bool:
    """键名是否敏感（命中敏感片段即视为敏感，含大小写/驼峰变体）。"""
    lowered = key.lower()
    return any(frag in lowered for frag in _SENSITIVE_KEY_FRAGMENTS)


def redact_dsn(dsn: str | None) -> str | None:
    """DSN 内嵌凭据打码（口令永不入日志；无凭据形态原样返回）。"""
    if not dsn:
        return dsn
    return _DSN_CREDENTIAL_RE.sub(lambda m: f"{m.group('scheme')}{m.group('user')}:{_REDACTED}@", dsn)


def redact_mapping(value: object) -> object:
    """递归打码：dict 键名敏感 → 值替换为 ***；str 经 DSN 凭据打码；其余原样。

    输入输出均为可 JSON 序列化对象（结构不变，仅敏感值替换）。
    """
    if isinstance(value, dict):
        return {
            k: (_REDACTED if is_sensitive_key(str(k)) else redact_mapping(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_mapping(v) for v in value]
    if isinstance(value, str):
        return redact_dsn(value)
    return value


def classify_error(exc: BaseException | None) -> str:
    """异常 → 错误分类（§16.2「错误分类」字段；无异常为空串）。

    分类依据：异常类型名 + 消息文本（lower 归一），先命中先归类。
    """
    if exc is None:
        return ""
    text = f"{type(exc).__name__} {exc}".lower()
    if "timeout" in text:
        return "timeout"
    if "denied" in text or "forbidden" in text or "unauthorized" in text:
        return "auth_denied"
    if "connect" in text:
        return "connect_error"
    if "load" in text or "bulk" in text:
        return "load_error"
    if "valid" in text or "whitelist" in text:
        return "validation_error"
    if "state" in text or "transition" in text:
        return "state_error"
    if "blocked" in text or "gate" in text:
        return "gate_blocked"
    return "internal_error"


def build_log_record(
    *,
    dataset_id: str | None = None,
    plan_id: str | None = None,
    task_id: str | None = None,
    table_id: str | None = None,
    shard_id: str | None = None,
    chunk_id: int | None = None,
    worker_id: str | None = None,
    database_id: str | None = None,
    operation: str | None = None,
    status: str | None = None,
    duration: float | None = None,
    error_class: str | None = None,
    error: BaseException | None = None,
    ts: str | None = None,
    **extra: object,
) -> dict:
    """构造 §16.2 结构化日志记录：字段集逐条在位（缺省 None），extra 脱敏后并入。

    error 未显式给 error_class 时按异常类型自动分类；extra 中敏感键值打码。
    """
    record: dict = {name: None for name in LOG_FIELDS}
    record.update({
        "dataset_id": dataset_id,
        "plan_id": plan_id,
        "task_id": task_id,
        "table_id": table_id,
        "shard_id": shard_id,
        "chunk_id": chunk_id,
        "worker_id": worker_id,
        "database_id": database_id,
        "operation": operation,
        "status": status,
        "duration": duration,
        "error_class": error_class or classify_error(error),
    })
    record["ts"] = ts or datetime.now(UTC).isoformat(timespec="seconds")
    redacted_extra = redact_mapping(extra)
    if isinstance(redacted_extra, dict):
        record.update(redacted_extra)
    return record


def emit_log_record(record: dict, logger: logging.Logger | None = None) -> None:
    """结构化日志落 logging（JSON 单行；脱敏已在 build_log_record 完成）。"""
    (logger or logging.getLogger("ptde.observability")).info(
        json.dumps(record, ensure_ascii=False, default=str))


class MetricSample(VersionedModel):
    """单条指标样本（§16.1 指标名 + 值 + 维度标签）。"""

    name: str
    value: float
    unit: str = ""
    dataset_id: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    ts: str = Field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))


class ObservabilityRecorder:
    """进程内指标记录与聚合（线程安全；容量上限环形截断，最旧样本先丢）。"""

    def __init__(self, capacity: int = 10_000) -> None:
        if capacity <= 0:
            raise ValueError("capacity 必须为正整数")
        self._capacity = capacity
        self._samples: list[MetricSample] = []
        self._lock = threading.Lock()

    def record(self, name: str, value: float, *, unit: str = "",
               dataset_id: str | None = None,
               labels: dict[str, str] | None = None) -> MetricSample:
        """记录一条指标样本（指标名不在 §16.1 全集 → 显式拒绝，防口径漂移）。"""
        if name not in METRIC_NAMES:
            raise ValueError(
                f"未知指标 {name!r}（须为 §16.1 全集：{', '.join(METRIC_NAMES)}）"
            )
        sample = MetricSample(
            name=name, value=float(value), unit=unit,
            dataset_id=dataset_id, labels=dict(labels or {}),
        )
        with self._lock:
            self._samples.append(sample)
            if len(self._samples) > self._capacity:
                del self._samples[: len(self._samples) - self._capacity]
        return sample

    @contextlib.contextmanager
    def observe_duration(self, name: str, *, dataset_id: str | None = None,
                         labels: dict[str, str] | None = None) -> Iterator[None]:
        """耗时指标便捷记录：with 块退出时按实际耗时落样本（秒）。"""
        start = time.monotonic()
        try:
            yield
        finally:
            self.record(name, time.monotonic() - start, unit="s",
                        dataset_id=dataset_id, labels=labels)

    def snapshot(self, *, dataset_id: str | None = None) -> dict[str, dict]:
        """按指标名聚合：count/sum/min/max/last/unit（可按 dataset 维度过滤）。"""
        with self._lock:
            samples = list(self._samples)
        if dataset_id is not None:
            samples = [s for s in samples if s.dataset_id == dataset_id]
        grouped: dict[str, list[MetricSample]] = {}
        for s in samples:
            grouped.setdefault(s.name, []).append(s)
        out: dict[str, dict] = {}
        for name in sorted(grouped):
            values = [s.value for s in grouped[name]]
            out[name] = {
                "count": len(values),
                "sum": sum(values),
                "min": min(values),
                "max": max(values),
                "last": values[-1],
                "unit": grouped[name][-1].unit,
            }
        return out

    def samples(self, *, dataset_id: str | None = None) -> list[MetricSample]:
        """原始样本（按记录序返回；可按 dataset 维度过滤）。"""
        with self._lock:
            samples = list(self._samples)
        if dataset_id is not None:
            samples = [s for s in samples if s.dataset_id == dataset_id]
        return samples
