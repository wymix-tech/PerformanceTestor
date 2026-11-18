"""执行前安全门禁（R5 T5.9，实施方案 §8.2/§8.4；总方案 §13.1/§13.2/§13.3）。

检查项（§8.4 清单逐项，§13.3 全集）：
  环境识别 / 环境策略 / Schema 隔离 / 授权与写入权限 / 资源预算 /
  外部副作用 / 破坏性操作 / 凭据分离。

硬约束（A3，显式失败不静默放行）：
- **环境识别不得仅凭连接名、主机名或环境变量名判断**（总方案 §13.1）——
  识别凭据 = T5.2 Capability Probe 实测快照（版本不可得 →
  ``CapabilityProbeError``，即「无法识别 → 禁止写入」已就位）；
- **生产库禁止执行数据构建、更新和清理**（§13.1）；无法识别的环境禁止写入；
- 破坏性操作（DDL/TRUNCATE/DROP/清理）须显式授权（DestructiveApproval）
  + 审计记录（AuditLog）；
- 凭据分离：统计采集（profile_dsn）与装载（target_dsn）凭据/权限/审计
  分离（A5 延续）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from ptde.adapters.probe import CapabilityProbeError, probe_adapter
from ptde_common.model_base import VersionedModel

__all__ = [
    "AuditLog",
    "DESTRUCTIVE_OPERATIONS",
    "DestructiveApproval",
    "EnvClass",
    "GateCheck",
    "GateDenied",
    "GateReport",
    "SecurityGate",
]

DESTRUCTIVE_OPERATIONS: tuple[str, ...] = ("DDL", "TRUNCATE", "DROP", "CLEANUP")


class EnvClass(StrEnum):
    """环境用途（操作者**显式声明**；门禁绝不从连接名/主机名/环境变量推断）。"""

    DEDICATED_TEST_DB = "dedicated_test_db"  # 独立测试库：授权后允许
    DEDICATED_TEST_SCHEMA = "dedicated_test_schema"  # 独立测试 Schema：授权后允许
    SHARED_TEST = "shared_test"  # 共享测试库：须确认隔离策略
    PRODUCTION = "production"  # 生产库：禁止构建/更新/清理
    UNKNOWN = "unknown"  # 无法识别：禁止写入


class DestructiveApproval(VersionedModel):
    """破坏性操作显式授权（§13.3：所有破坏性动作必须有明确授权和审计）。"""

    operation: str  # DESTRUCTIVE_OPERATIONS 子集
    target: str  # 目标（表/Schema）
    authorized_by: str
    ticket: str = ""

    def model_post_init(self, __context: Any) -> None:
        if self.operation not in DESTRUCTIVE_OPERATIONS:
            raise ValueError(
                f"破坏性操作 {self.operation!r} 不在授权范围 {DESTRUCTIVE_OPERATIONS}"
                "（显式失败）"
            )
        if not self.authorized_by.strip():
            raise ValueError("授权人 authorized_by 不得为空（审计要求，显式失败）")

    def covers(self, operation: str, target: str) -> bool:
        return self.operation == operation and (
            not self.target or self.target == target
        )


class GateCheck(VersionedModel):
    """§8.4 单项检查结论。"""

    name: str
    passed: bool
    detail: str = ""

    @property
    def not_executed(self) -> bool:
        return self.detail.startswith("[未执行]")


class GateReport(VersionedModel):
    """门禁评估报告（随审计落库；通过与否显式判定）。"""

    product: str
    db_version: str | None = None
    declared_env_class: str = ""
    checks: list[GateCheck] = Field(default_factory=list)
    capability_snapshot: dict[str, Any] | None = None

    def failed(self) -> list[GateCheck]:
        return [c for c in self.checks if not c.passed]

    def passed_all(self) -> bool:
        return not self.failed()

    def require_passed(self) -> GateReport:
        if not self.passed_all():
            failed = "; ".join(f"{c.name}: {c.detail}" for c in self.failed())
            raise GateDenied(f"执行前安全门禁拒绝（{len(self.failed())} 项未过）：{failed}")
        return self


class GateDenied(Exception):
    """门禁拒绝（含未过项明细；决策已入审计）。"""


class AuditLog:
    """审计记录（追加式；破坏性授权与门禁决策均入审计——§13.3）。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, event_type: str, **detail: Any) -> None:
        self.events.append({
            "event_type": event_type,
            "at": datetime.now(UTC).isoformat(),
            **detail,
        })

    def destructive_granted(self, approval: DestructiveApproval) -> None:
        self.record(
            "destructive_granted",
            operation=approval.operation,
            target=approval.target,
            authorized_by=approval.authorized_by,
            ticket=approval.ticket,
        )

    def to_snapshot(self) -> list[dict[str, Any]]:
        return list(self.events)


class SecurityGate:
    """执行前安全门禁（§8.4 清单逐项；决策与破坏性授权均入审计）。"""

    def __init__(self, audit: AuditLog | None = None) -> None:
        self.audit = audit or AuditLog()

    # ------------------------------------------------------------ 装载门禁

    def evaluate_write(
        self,
        conn,
        product: str,
        *,
        target_dsn: str,
        profile_dsn: str,
        declared_env_class: str | EnvClass,
        permission_ok: bool | None = None,
        trigger_count: int | None = None,
        schema_isolation_confirmed: bool = False,
        side_effect_isolation_confirmed: bool = False,
        budget_probe: Callable[[], dict[str, float]] | None = None,
        approvals: Sequence[DestructiveApproval] = (),
        requested_destructive: Sequence[str] = (),
    ) -> GateReport:
        """装载门禁评估（返回报告；调用方 require_passed() 强制或拒绝）。

        - ``permission_ok``：由预检（T5.6/原生适配器）实测 INSERT 权限回填；
          None = 未实测 → 不通过（不得视为通过）；
        - ``trigger_count``：预检触发器计数；>0 且未确认副作用隔离 → 拒绝
          （外部副作用默认隔离，§8.4/§13.3）；
        - ``budget_probe``：磁盘/日志/连接数余量预检回调；未提供 → 记
          「未执行」（不伪装通过，§8.4 资源预算行）。
        """
        env = EnvClass(declared_env_class)
        report = GateReport(
            product=(product or "").lower(),
            declared_env_class=env.value,
        )

        # 1. 环境识别（§13.1：不得仅凭连接名/主机名/环境变量名——凭探测快照）
        try:
            cap = probe_adapter(conn, product)
            report.db_version = cap.db_version
            report.capability_snapshot = cap.to_snapshot()
            report.checks.append(GateCheck(
                name="environment_recognition", passed=True,
                detail=(
                    f"识别凭据=Capability Probe 实测（product={cap.product}, "
                    f"version={cap.db_version}）；不得仅凭连接名/主机名/"
                    "环境变量名判断（§13.1）"
                ),
            ))
        except CapabilityProbeError as exc:
            report.checks.append(GateCheck(
                name="environment_recognition", passed=False,
                detail=(
                    f"无法识别环境禁止写入（§13.1）：{exc}"
                    "——识别凭据须为实测探测，不得仅凭连接名/主机名/环境变量名"
                ),
            ))
            self.audit.record("gate_denied", gate="write", product=product,
                              reason="environment_unrecognized")
            return report

        # 2. 环境策略（§13.1 表）
        if env is EnvClass.PRODUCTION:
            report.checks.append(GateCheck(
                name="environment_policy", passed=False,
                detail="生产数据库禁止执行数据构建、更新和清理（§13.1）",
            ))
        elif env is EnvClass.UNKNOWN:
            report.checks.append(GateCheck(
                name="environment_policy", passed=False,
                detail="无法识别的环境禁止写入（§13.1）",
            ))
        elif env is EnvClass.SHARED_TEST and not schema_isolation_confirmed:
            report.checks.append(GateCheck(
                name="environment_policy", passed=False,
                detail="共享测试库须确认数据空间/命名空间/所有权策略（§13.1）",
            ))
        else:
            report.checks.append(GateCheck(
                name="environment_policy", passed=True,
                detail=f"{env.value}：授权后允许（§13.1）",
            ))

        # 3. Schema 隔离（§13.3）
        report.checks.append(GateCheck(
            name="schema_isolation",
            passed=bool(schema_isolation_confirmed) or env is not EnvClass.SHARED_TEST,
            detail=(
                "目标 Schema 独占/隔离策略已确认"
                if schema_isolation_confirmed
                else "目标 Schema 隔离策略未确认（§13.3：确认目标 Schema 是否"
                "独占或有数据隔离策略）"
            ),
        ))

        # 4. 授权与写入权限（§13.3）
        if permission_ok is None:
            report.checks.append(GateCheck(
                name="write_privilege", passed=False,
                detail="写入权限未实测（不得视为通过；由预检回填实测值）",
            ))
        elif permission_ok:
            report.checks.append(GateCheck(
                name="write_privilege", passed=True,
                detail="目标凭据具备装载所需最小权限（实测）",
            ))
        else:
            report.checks.append(GateCheck(
                name="write_privilege", passed=False,
                detail="目标凭据写入权限不足（显式失败，§8.4）",
            ))

        # 5. 资源预算（§13.3：磁盘/日志/连接数余量预检）
        if budget_probe is None:
            report.checks.append(GateCheck(
                name="resource_budget", passed=False,
                detail="[未执行] 资源预算预检未提供（磁盘/日志/连接数余量，"
                "§8.4——不伪装通过；须显式提供 budget_probe 或在报告中确认豁免）",
            ))
        else:
            try:
                metrics = budget_probe()
                report.checks.append(GateCheck(
                    name="resource_budget", passed=True,
                    detail=f"资源余量实测：{metrics}",
                ))
            except Exception as exc:
                report.checks.append(GateCheck(
                    name="resource_budget", passed=False,
                    detail=f"资源预算预检失败（显式失败）：{exc}",
                ))

        # 6. 外部副作用（§8.4：触发器外部副作用默认隔离）
        if trigger_count is None:
            report.checks.append(GateCheck(
                name="external_side_effects", passed=False,
                detail="触发器计数未提供（须由预检回填——不伪装无风险）",
            ))
        elif trigger_count > 0 and not side_effect_isolation_confirmed:
            report.checks.append(GateCheck(
                name="external_side_effects", passed=False,
                detail=(
                    f"目标表存在 {trigger_count} 个触发器且副作用隔离未确认"
                    "（消息/邮件/支付等外部副作用默认隔离，§8.4）"
                ),
            ))
        else:
            report.checks.append(GateCheck(
                name="external_side_effects", passed=True,
                detail=(
                    f"触发器 {trigger_count} 个，副作用隔离已确认"
                    if trigger_count else "无触发器，无外部副作用入口"
                ),
            ))

        # 7. 破坏性操作（§8.4：显式授权参数 + 审计记录）
        uncovered = [
            op for op in requested_destructive
            if not _approved_for(op, approvals)
        ]
        if uncovered:
            report.checks.append(GateCheck(
                name="destructive_operations", passed=False,
                detail=f"破坏性操作 {uncovered} 无显式授权（§8.4/§13.3：须授权+审计）",
            ))
        else:
            report.checks.append(GateCheck(
                name="destructive_operations", passed=True,
                detail=(
                    f"破坏性操作 {list(requested_destructive)} 均已显式授权"
                    if requested_destructive else "无破坏性操作请求"
                ),
            ))
            for approval in approvals:
                if approval.operation in requested_destructive:
                    self.audit.destructive_granted(approval)

        # 8. 凭据分离（A5 延续：profile_dsn 与 target_dsn 分离）
        if not target_dsn or not profile_dsn:
            report.checks.append(GateCheck(
                name="credential_separation", passed=False,
                detail="target_dsn/profile_dsn 不得为空（A5：凭据分离）",
            ))
        elif target_dsn == profile_dsn:
            report.checks.append(GateCheck(
                name="credential_separation", passed=False,
                detail="统计采集与装载凭据相同（A5：凭据/权限/审计分离）",
            ))
        else:
            report.checks.append(GateCheck(
                name="credential_separation", passed=True,
                detail="profile_dsn ≠ target_dsn（A5 凭据分离）",
            ))

        if not report.passed_all():
            self.audit.record(
                "gate_denied", gate="write", product=report.product,
                env_class=env.value,
                failed=[c.name for c in report.failed()],
            )
        else:
            self.audit.record(
                "gate_allowed", gate="write", product=report.product,
                env_class=env.value,
            )
        return report

    # ------------------------------------------------------------ 清理门禁

    def evaluate_cleanup(
        self,
        *,
        declared_env_class: str | EnvClass,
        target_dsn: str = "",
        approvals: Sequence[DestructiveApproval] = (),
    ) -> GateReport:
        """清理门禁（生产库禁止清理；破坏性操作须授权+审计——§13.1/§13.3）。"""
        env = EnvClass(declared_env_class)
        report = GateReport(product="", declared_env_class=env.value)
        if env is EnvClass.PRODUCTION:
            report.checks.append(GateCheck(
                name="environment_policy", passed=False,
                detail="生产数据库禁止执行数据清理（§13.1）",
            ))
        elif env is EnvClass.UNKNOWN:
            report.checks.append(GateCheck(
                name="environment_policy", passed=False,
                detail="无法识别的环境禁止写入/清理（§13.1）",
            ))
        else:
            report.checks.append(GateCheck(
                name="environment_policy", passed=True,
                detail=f"{env.value}：清理须授权后执行（§13.3）",
            ))
        if not _approved_for("CLEANUP", approvals):
            report.checks.append(GateCheck(
                name="destructive_operations", passed=False,
                detail="清理属破坏性操作：无 CLEANUP 显式授权（§13.3）",
            ))
        else:
            report.checks.append(GateCheck(
                name="destructive_operations", passed=True,
                detail="CLEANUP 已显式授权（审计记录）",
            ))
            for approval in approvals:
                if approval.operation == "CLEANUP":
                    self.audit.destructive_granted(approval)
        if not report.passed_all():
            self.audit.record("gate_denied", gate="cleanup", env_class=env.value)
        else:
            self.audit.record("gate_allowed", gate="cleanup", env_class=env.value)
        return report


def _approved_for(operation: str, approvals: Sequence[DestructiveApproval]) -> bool:
    return any(
        a.operation == operation and (not a.target or a.target)
        for a in approvals
    )
