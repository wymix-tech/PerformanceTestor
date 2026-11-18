"""PTDE Loader：分片检查点、并发限流、执行前安全门禁（R5 T5.7~T5.9）。

T5.7 checkpoint：直接消费 ``ptde/generator/staging.py`` 检查点协议与
``BulkLoadResult.to_snapshot()``（不重复不遗漏，§8.6 门禁 1/2）；
T5.8 throttle：并发/批次经 Pilot Run 校准（无通用固定值）+ 软限流；
T5.9 gate：§8.4/§13.3 执行前安全门禁（生产库禁止写入 + 破坏性操作授权审计）。
"""

from ptde.loader.checkpoint import (
    CheckpointCoordinator,
    CheckpointStoreError,
    DbCheckpointStore,
    RecoveryPlan,
    ShardCheckpoint,
    SubmittedState,
)
from ptde.loader.gate import (
    AuditLog,
    DestructiveApproval,
    EnvClass,
    GateDenied,
    GateReport,
    SecurityGate,
)
from ptde.loader.orchestrator import (
    BackfillProvider,
    BackfillStep,
    LoadOrchestrator,
    LoadOrchestratorError,
    LoadReport,
)
from ptde.loader.throttle import (
    DEFAULT_LOG_CAPACITY_BYTES,
    ThrottleConfig,
    ThrottleGuard,
    ThrottleSignal,
    calibrate_from_pilot,
)

__all__ = [
    "DEFAULT_LOG_CAPACITY_BYTES",
    "AuditLog",
    "BackfillProvider",
    "BackfillStep",
    "CheckpointCoordinator",
    "CheckpointStoreError",
    "DbCheckpointStore",
    "DestructiveApproval",
    "EnvClass",
    "GateDenied",
    "GateReport",
    "LoadOrchestrator",
    "LoadOrchestratorError",
    "LoadReport",
    "RecoveryPlan",
    "SecurityGate",
    "ShardCheckpoint",
    "SubmittedState",
    "ThrottleConfig",
    "ThrottleGuard",
    "ThrottleSignal",
    "calibrate_from_pilot",
]
