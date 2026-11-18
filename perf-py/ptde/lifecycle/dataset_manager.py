"""T6.6/T6.9 数据集管理 + READY 门禁（实施方案 §9.2；总方案 §12.1/§18.4）。

T6.6 DatasetDefinition/Instance 分离管理：
- 同一 Definition 可建多 Instance（Definition 可复用，Instance 绑定环境）；
- Definition 版本化：def_key+version 唯一，重复注册显式失败（须显式版本
  bump）；spec_fingerprint 自动封存；
- 状态流转唯一入口 = R0 状态机 ``transition()``（非法转换显式失败）。

T6.9 READY 门禁整合（§18.4 八项完成定义逐项检查，程序化非人工宣告）：
任何一项 FAIL → 不进 READY（调用方转 FAILED）；可降级层 WARN 放行由
``degradable_layers`` 配置（默认仅 DISTRIBUTION，实施方案 §9.4）。
"""

from __future__ import annotations

import uuid

from pydantic import Field

from ptde.lifecycle.cleanup import CleanupDecision
from ptde.lifecycle.manifest import missing_elements
from ptde.lifecycle.state_machine import transition
from ptde.models.base import DbRef
from ptde.models.dataset import (
    CheckResult,
    DatasetDefinition,
    DatasetInstance,
    DatasetState,
    LayerKind,
    TargetEnvironment,
    ValidationReport,
    ValidationStatus,
)
from ptde.models.spec import DatasetSpec, ExecutionManifest
from ptde_common.model_base import VersionedModel

__all__ = [
    "DuplicateDefinitionError",
    "DatasetManager",
    "ReadyDecision",
    "ReadyDenied",
    "evaluate_ready",
    "require_ready",
]

# §18.4 五层验证必须齐全（缺层 = 报告不完整，条件 7 不满足）
_ALL_LAYERS: frozenset[LayerKind] = frozenset(LayerKind)
# 可降级层默认集（§9.4：WARN 是否放行由配置决定；默认仅分布统计容差层）
_DEFAULT_DEGRADABLE: frozenset[LayerKind] = frozenset({LayerKind.DISTRIBUTION})


class DuplicateDefinitionError(Exception):
    """def_key+version 重复注册（须显式版本 bump，不静默覆盖）。"""


class ReadyDenied(Exception):
    """READY 门禁拒绝（未满足项随异常返回）。"""


class ReadyDecision(VersionedModel):
    """§18.4 八项逐项结论（程序化判定，非人工宣告）。"""

    allowed: bool = False
    checks: list[CheckResult] = Field(default_factory=list)

    def failed(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status is ValidationStatus.FAIL]


def _layer_status(
    report_layers: dict[LayerKind, ValidationStatus],
    kind: LayerKind,
    degradable: frozenset[LayerKind],
) -> tuple[ValidationStatus, str]:
    """单层判定：FAIL 拒绝；WARN 仅在可降级集内放行（PASS 正常）。

    层缺失 → FAIL（报告不完整，不得视为通过）。
    """
    status = report_layers.get(kind)
    if status is None:
        return ValidationStatus.FAIL, f"{kind.value} 层验证缺失（报告不完整）"
    if status is ValidationStatus.FAIL:
        return ValidationStatus.FAIL, f"{kind.value} 层存在 FAIL 检查项"
    if status is ValidationStatus.WARN:
        if kind in degradable:
            return ValidationStatus.PASS, (
                f"{kind.value} 层 WARN（可降级项：容差内偏差放行，随报告留痕）"
            )
        return ValidationStatus.FAIL, f"{kind.value} 层 WARN 且不在可降级集内"
    return ValidationStatus.PASS, f"{kind.value} 层全部检查项 PASS"


def evaluate_ready(
    instance: DatasetInstance,
    report: ValidationReport,
    manifest: ExecutionManifest,
    cleanup: CleanupDecision,
    *,
    degradable_layers: frozenset[LayerKind] | None = None,
) -> ReadyDecision:
    """§18.4 八项完成定义逐项检查（全部满足才允许进 READY）。"""
    degradable = degradable_layers if degradable_layers is not None else _DEFAULT_DEGRADABLE
    report_layers: dict[LayerKind, ValidationStatus] = {
        layer.layer: layer.status for layer in report.layers
    }
    checks: list[CheckResult] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append(CheckResult(
            name=name,
            status=ValidationStatus.PASS if ok else ValidationStatus.FAIL,
            detail=detail,
        ))

    # 1. 数量达标（count 层）
    s, d = _layer_status(report_layers, LayerKind.COUNT, degradable)
    add("ready.count", s is not ValidationStatus.FAIL, d)

    # 2. 所有分片完成、无未处理装载错误（checkpoint 汇总 + count 的执行侧证据）
    summaries = manifest.shard_summaries
    if not summaries:
        add("ready.shards_complete", False, "Manifest 无分片摘要（分片完成状态不可核对）")
    else:
        unfinished = [
            x.shard_id for x in summaries
            if x.status != "loaded" or x.loaded_rows != x.generated_rows
        ]
        add(
            "ready.shards_complete", not unfinished,
            f"{len(summaries)} 分片全部 loaded 且 loaded_rows==generated_rows"
            if not unfinished else f"未完成/行数不符分片：{unfinished}",
        )

    # 3. 关键实体关系通过验证
    s, d = _layer_status(report_layers, LayerKind.RELATIONSHIP, degradable)
    add("ready.relationship", s is not ValidationStatus.FAIL, d)

    # 4. 配置的分布验证通过（可降级项）
    s, d = _layer_status(report_layers, LayerKind.DISTRIBUTION, degradable)
    add("ready.distribution", s is not ValidationStatus.FAIL, d)

    # 5. 必要业务不变量通过（仅 CONFIRMED）
    s, d = _layer_status(report_layers, LayerKind.BUSINESS, degradable)
    add("ready.invariant", s is not ValidationStatus.FAIL, d)

    # 6. 索引与统计信息达约定就绪状态
    s, d = _layer_status(report_layers, LayerKind.DB_READY, degradable)
    add("ready.db_ready", s is not ValidationStatus.FAIL, d)

    # 7. Manifest/执行清单/验证报告完整（链路 ID 完整可查）
    missing = missing_elements(manifest)
    absent_layers = [
        k.value for k in sorted(_ALL_LAYERS, key=lambda x: x.value)
        if k not in report_layers
    ]
    manifest_id_ok = bool(manifest.instance_id) and manifest.instance_id == instance.id
    add(
        "ready.artifacts_complete",
        not missing and not absent_layers and manifest_id_ok,
        (
            f"Manifest 要素齐备（缺：{missing}）；五层报告齐全"
            f"（缺：{absent_layers}）；instance_id 链路一致"
            if (not missing and not absent_layers and manifest_id_ok)
            else f"Manifest 缺项 {missing}；报告缺层 {absent_layers}；"
            f"manifest.instance_id={manifest.instance_id!r} vs instance.id={instance.id!r}"
        ),
    )

    # 8. 清理策略、所有权、目标环境明确
    env = instance.target_environment
    env_clear = bool(env.environment_id or env.database)
    add(
        "ready.cleanup_ownership",
        (not cleanup.blocked) and bool(instance.owner_token) and env_clear,
        (
            "清理档位已定、owner_token 非空、目标环境明确"
            if ((not cleanup.blocked) and instance.owner_token and env_clear)
            else f"cleanup.blocked={cleanup.blocked}（{cleanup.reasons}）；"
            f"owner_token={'有' if instance.owner_token else '无'}；"
            f"目标环境={env.environment_id or env.database!r}"
        ),
    )

    return ReadyDecision(
        allowed=all(c.status is not ValidationStatus.FAIL for c in checks),
        checks=checks,
    )


def require_ready(decision: ReadyDecision) -> ReadyDecision:
    """READY 门禁强制：任一 FAIL 抛 ReadyDenied（带未满足项明细）。"""
    failed = decision.failed()
    if failed:
        raise ReadyDenied(
            "READY 门禁拒绝（"
            + "; ".join(f"{c.name}: {c.detail}" for c in failed)
            + "）——Instance 不得进入 READY"
        )
    return decision


class DatasetManager:
    """Definition/Instance 分离管理（内存模型层；DB 落库随 B7 API 批次）。"""

    def __init__(self) -> None:
        self._definitions: dict[tuple[str, str], DatasetDefinition] = {}
        self._instances: dict[str, DatasetInstance] = {}

    # ------------------------------------------------ Definition 管理（T6.6）

    def register_definition(
        self,
        def_key: str,
        version: str,
        name: str,
        spec: DatasetSpec,
        *,
        created_by: str | None = None,
    ) -> DatasetDefinition:
        """注册 Definition（def_key+version 唯一；重复显式失败不覆盖）。"""
        key = (def_key, version)
        if key in self._definitions:
            raise DuplicateDefinitionError(
                f"Definition {def_key!r} v{version!r} 已存在（def_key+version 唯一——"
                "须显式版本 bump，不静默覆盖）"
            )
        definition = DatasetDefinition(
            def_key=def_key,
            version=version,
            name=name,
            spec=spec,
            spec_fingerprint=spec.compute_fingerprint(),
            created_by=created_by,
        )
        self._definitions[key] = definition
        return definition

    def get_definition(self, def_key: str, version: str) -> DatasetDefinition:
        key = (def_key, version)
        if key not in self._definitions:
            raise KeyError(
                f"Definition {def_key!r} v{version!r} 不存在（显式失败不静默）"
            )
        return self._definitions[key]

    def list_versions(self, def_key: str) -> list[str]:
        """同 def_key 全部版本（排序返回，确定性）。"""
        return sorted(v for (k, v) in self._definitions if k == def_key)

    def list_definitions(self) -> list[DatasetDefinition]:
        return [
            self._definitions[k]
            for k in sorted(self._definitions)
        ]

    # ------------------------------------------------ Instance 管理（T6.6）

    def create_instance(
        self,
        definition: DatasetDefinition,
        *,
        environment_id: str | None = None,
        target_database: DbRef | None = None,
        plan_id: str | None = None,
    ) -> DatasetInstance:
        """同一 Definition 建多 Instance（新实例初始 PLANNED，UUID 标识）。"""
        instance = DatasetInstance(
            id=uuid.uuid4().hex,
            definition_id=f"{definition.def_key}@{definition.version}",
            definition_version=definition.version,
            plan_id=plan_id,
            target_environment=TargetEnvironment(
                environment_id=environment_id,
                database=target_database,
            ),
        )
        self._instances[instance.id] = instance
        return instance

    def get_instance(self, instance_id: str) -> DatasetInstance:
        if instance_id not in self._instances:
            raise KeyError(f"Instance {instance_id!r} 不存在（显式失败不静默）")
        return self._instances[instance_id]

    def list_instances(self, def_key: str | None = None) -> list[DatasetInstance]:
        """实例清单（可按 def_key 前缀过滤；按 id 排序，确定性）。"""
        items = sorted(self._instances.values(), key=lambda i: i.id)
        if def_key is None:
            return items
        return [
            i for i in items
            if i.definition_id.split("@", 1)[0] == def_key
        ]

    def transition_instance(
        self,
        instance_id: str,
        to_state: DatasetState,
        *,
        operator: str,
        reason: str,
        manifest_id: str | None = None,
    ) -> DatasetInstance:
        """状态流转唯一入口（R0 状态机；非法转换 InvalidStateTransition）。"""
        instance = self.get_instance(instance_id)
        return transition(
            instance, to_state, operator=operator, reason=reason,
            manifest_id=manifest_id,
        )

    # ------------------------------------------------ READY 判定（T6.9）

    def finalize_verification(
        self,
        instance_id: str,
        decision: ReadyDecision,
        *,
        operator: str,
        manifest_id: str | None = None,
    ) -> DatasetInstance:
        """VERIFYING → READY/FAILED：门禁全过进 READY，否则转 FAILED。

        非法流转（如当前非 VERIFYING）经状态机显式失败。
        """
        instance = self.get_instance(instance_id)
        if decision.allowed:
            return transition(
                instance, DatasetState.READY, operator=operator,
                reason="READY 门禁八项全部满足（程序化判定，§18.4）",
                manifest_id=manifest_id,
            )
        failed = "; ".join(f"{c.name}: {c.detail}" for c in decision.failed())
        return transition(
            instance, DatasetState.FAILED, operator=operator,
            reason=f"READY 门禁未满足：{failed}",
            manifest_id=manifest_id,
        )
