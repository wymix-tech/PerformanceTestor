"""PTDE 数据集生命周期服务（B7 · T7.2 前置 + T6.6/T6.9 落库收编）。

桥接两层：
- 领域层：``ptde/lifecycle``（状态机唯一入口 ``transition()``、READY 门禁
  ``evaluate_ready``、清理 ``plan_cleanup``/``execute_cleanup``）——语义不重造；
- 落库层：``server/ptde_repo.py`` 三表 repo（定义/实例/验证报告持久化）。

服务职责（总方案 §15.2）：
- Definition 注册（def_key+version 唯一，重复显式失败）、Instance 创建、
  状态流转（全经状态机，转换记录随 Instance 持久化 = 审计）；
- 构建/验证异步执行（线程池，不占 HTTP 线程；任务 ID = instance id，幂等）；
- **生产环境不提供构建与清理入口**（总方案 §15.1 / K5）：环境标识含
  production 的实例显式拒绝 build；
- 验证报告五层落 ``ptde_validation_report``，READY 八项判定结果写入
  ``instance.validation_summary``（R6 §7 待接线项收编）；
- ExecutionManifest 落 ``ptde_execution_manifest``（B7 续：READY 门禁
  条件 7 的持久化侧；READY/FAILED 均留痕——可追溯性不因门禁结果而失）；
- 清理走 ptde 清理档位决策 + SecurityGate CLEANUP 授权（决策与执行分离，
  K5 口径）。

实际生成/装载/验证执行体为注入缝（``pipeline``）：真库端到端执行体随
A2 真库环境批次接线（B6 §7F 口径）；未注入时显式失败，不伪装可用。
"""

from __future__ import annotations

import base64
import json
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from ptde.lifecycle.cleanup import (
    CleanupBlocked,
    CleanupContext,
    execute_cleanup,
    plan_cleanup,
)
from ptde.lifecycle.dataset_manager import evaluate_ready
from ptde.lifecycle.state_machine import InvalidStateTransition, transition
from ptde.loader.gate import DestructiveApproval, SecurityGate
from ptde.models.dataset import DatasetState, ValidationReport
from ptde.models.spec import DatasetSpec, ExecutionManifest
from server.ptde_repo import PtdeDatasetInstanceModel, PtdeDatasetRepository

__all__ = [
    "DatasetCancelled",
    "DatasetLifecycleService",
    "DatasetServiceError",
    "UnwiredPipeline",
]

PRODUCTION_MARKERS: tuple[str, ...] = ("production", "prod")

# 可取消状态集（§3.5 转换表出边评审结论，B7 续批次 3）：PLANNED/GENERATING/
# LOADING → CANCELLED 三条出边自 R0 起即在转换表（实施方案 §3.5 / 总方案
# §12.2 与 ptde/models/dataset.py ALLOWED_TRANSITIONS 三方一致）；B7 前段
# 交接 §7「LOADING→CANCELLED 无出边」系误记，本批核实后无需改模型层。
CANCELABLE_STATES: frozenset[DatasetState] = frozenset({
    DatasetState.PLANNED,
    DatasetState.GENERATING,
    DatasetState.LOADING,
})


class DatasetServiceError(ValueError):
    """服务层显式失败（API 层映射 code=1，文案原样透出）。"""


class DatasetCancelled(Exception):
    """取消信号触达执行体（协作式退出协议：pipeline 在安全点检查
    ctx["cancel_event"]，置位即抛本异常——杀线程不可靠，不强制 kill）。"""


class UnwiredPipeline:
    """未接线执行体：任何执行请求显式失败（真库环境批次落地，不伪装可用）。"""

    @staticmethod
    def _fail(stage: str) -> None:
        raise DatasetServiceError(
            f"数据集执行体未接线（{stage} 阶段）：真库端到端执行体随 A2 真库"
            "环境批次落地；当前请注入 pipeline（generate/load/verify 三段缝）"
        )

    def generate(self, ctx: dict) -> dict:
        self._fail("generate")

    def load(self, ctx: dict) -> dict:
        self._fail("load")

    def verify(self, ctx: dict) -> dict:
        self._fail("verify")

    def cleanup_executor(self):
        self._fail("cleanup")


class DatasetLifecycleService:
    """数据集生命周期门面（定义/实例/构建/验证/清理；全落库可追溯）。"""

    def __init__(self, repo: PtdeDatasetRepository, *, pipeline: Any | None = None,
                 max_workers: int = 2, operator: str = "api",
                 degradable_layers: frozenset | None = None):
        self._repo = repo
        self._pipeline = pipeline if pipeline is not None else UnwiredPipeline()
        self._operator = operator
        # 可降级层集（§9.4：WARN 是否放行由装配方显式配置；缺省走 ptde
        # 默认集 = 仅 DISTRIBUTION。离线通道 db_ready 的 [未执行] WARN 项
        # 按 db_ready 文档口径为可降级，接线 OfflinePipeline 时须显式传入）。
        self._degradable_layers = degradable_layers
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers), thread_name_prefix="ptde-dataset")
        self._tasks: dict[int, Future] = {}
        # 取消编排（B7 续批次 3）：状态锁序化「工作线程段落流转 ↔ 取消流转」
        # （取消 = 置位事件 + 经状态机流转 CANCELLED，两步原子完成）；取消事件
        # 按 instance 行主键登记，随任务结束清理。
        self._state_lock = threading.Lock()
        self._cancel_flags: dict[int, threading.Event] = {}

    # ------------------------------------------------------------ Definition（T6.6）

    def register_definition(self, def_key: str, version: str, name: str,
                            spec: dict, *, created_by: str | None = None,
                            profile_fingerprint: str | None = None,
                            seed: int | None = None) -> dict:
        """注册 Definition：spec 形态校验 + 指纹封存；def_key+version 唯一。"""
        if not def_key or not def_key.strip():
            raise DatasetServiceError("defKey 不能为空")
        if not name or not name.strip():
            raise DatasetServiceError("name 不能为空")
        try:
            spec_model = DatasetSpec.model_validate(spec)
        except Exception as exc:  # noqa: BLE001 — pydantic 校验失败原样透出
            raise DatasetServiceError(f"DatasetSpec 校验失败：{exc}") from None
        row_id = self._repo.insert_definition(
            def_key=def_key.strip(), version=version or "1", name=name.strip(),
            spec_json=spec_model.model_dump_json(),
            spec_fingerprint=spec_model.compute_fingerprint(),
            profile_fingerprint=profile_fingerprint, seed=seed,
            created_by=created_by,
        )
        if row_id == -1:
            raise DatasetServiceError(
                f"Definition {def_key!r} v{version!r} 已存在（def_key+version 唯一——"
                "须显式版本 bump，不静默覆盖）")
        return self.get_definition(def_key, version or "1")

    def get_definition(self, def_key: str, version: str) -> dict:
        row = self._repo.find_definition(def_key, version)
        if row is None:
            raise DatasetServiceError(
                f"Definition {def_key!r} v{version!r} 不存在（显式失败不静默）")
        model = self._repo.definition_to_model(row)
        return {
            "id": row.id,
            "defKey": row.def_key,
            "version": row.version,
            "name": row.name,
            "spec": model.spec.model_dump(),
            "specFingerprint": row.spec_fingerprint,
            "profileFingerprint": row.profile_fingerprint,
            "seed": row.seed,
            "createdBy": row.created_by,
            "createTime": row.create_time.isoformat(timespec="seconds"),
        }

    def list_definitions(self, limit: int = 50) -> list[dict]:
        return [
            {
                "id": r.id, "defKey": r.def_key, "version": r.version,
                "name": r.name, "specFingerprint": r.spec_fingerprint,
                "createdBy": r.created_by,
                "createTime": r.create_time.isoformat(timespec="seconds"),
            }
            for r in self._repo.list_definitions(limit)
        ]

    def list_versions(self, def_key: str) -> list[str]:
        return self._repo.list_versions(def_key)

    # ------------------------------------------------------------ Instance（T6.6）

    def create_instance(self, def_key: str, version: str, *,
                        environment_id: str, plan_id: int | None = None,
                        operator: str | None = None) -> dict:
        """同一 Definition 建多 Instance（初始 PLANNED；owner_token 随建即发）。"""
        definition = self._repo.find_definition(def_key, version)
        if definition is None:
            raise DatasetServiceError(
                f"Definition {def_key!r} v{version!r} 不存在（先注册再建实例）")
        if not environment_id or not environment_id.strip():
            raise DatasetServiceError("environmentId 不能为空（目标环境须明确，§18.4 条件 8）")
        owner_token = uuid.uuid4().hex
        row_id = self._repo.insert_instance(
            definition_row_id=definition.id,
            definition_version=definition.version,
            environment_label=environment_id.strip(),
            status="PLANNED",
            plan_id=plan_id,
        )
        self._repo.set_owner_token(row_id, owner_token)
        return self.get_instance(str(row_id))

    def get_instance(self, instance_id: str, *, with_reports: bool = False) -> dict:
        row = self._require_instance_row(instance_id)
        model = self._instance_model(row)
        data = self._instance_camel(row, model)
        if with_reports:
            data["reports"] = self.instance_reports(str(row.id))
        return data

    def list_instances(self, status: str | None = None, limit: int = 50) -> list[dict]:
        rows = self._repo.list_instances(status=status, limit=limit)
        return [self._instance_camel(r, self._instance_model(r)) for r in rows]

    def list_instances_page(self, *, status: str | None = None,
                            size: int = 50, cursor: str | None = None) -> dict:
        """实例列表游标分页（CursorPage：{items,nextCursor,hasMore}）。

        - 游标为本端点签发的不透明串：base64url(json {"id": 上页末行行主键})；
          解码失败显式拒绝（code=1，不静默当首页）；
        - keyset id 降序（自增主键与创建序单调一致）；hasMore 由多取一行判定，
          nextCursor 取末行 id，末页 nextCursor=None；
        - 与 status 过滤可组合。
        """
        before_id: int | None = None
        if cursor:
            try:
                padded = cursor + "=" * (-len(cursor) % 4)
                payload = json.loads(base64.urlsafe_b64decode(padded))
                before_id = int(payload["id"])
                if before_id <= 0:
                    raise ValueError(f"非法游标 id：{before_id}")
            except (ValueError, TypeError, KeyError) as exc:
                raise DatasetServiceError(
                    f"cursor 非法（须为本端点签发的游标）：{exc}") from None
        rows = self._repo.list_instances_page(
            status=status, before_id=before_id, limit=min(max(size, 1), 200) + 1)
        has_more = len(rows) > min(max(size, 1), 200)
        rows = rows[:min(max(size, 1), 200)]
        items = [self._instance_camel(r, self._instance_model(r)) for r in rows]
        next_cursor = None
        if has_more and items:
            token = json.dumps({"id": int(rows[-1].id)})
            next_cursor = base64.urlsafe_b64encode(
                token.encode("utf-8")).decode("ascii").rstrip("=")
        return {"items": items, "nextCursor": next_cursor, "hasMore": has_more}

    def instance_reports(self, instance_id: str) -> list[dict]:
        row = self._require_instance_row(instance_id)
        rows = self._repo.reports_for_instance(row.id)
        report = self._repo.report_to_model(str(row.id), rows)
        return [layer.model_dump() for layer in report.layers]

    def instance_manifest(self, instance_id: str) -> dict:
        """执行清单查询（B7 续：§12.4 可复现要素全链可溯——READY 条件 7）。"""
        row = self._require_instance_row(instance_id)
        rows = self._repo.manifests_for_instance(row.id)
        if not rows:
            raise DatasetServiceError(
                f"Instance {instance_id!r} 无执行清单（未构建或 manifest 未落库）")
        return self._repo.manifest_summary(
            self._repo.manifest_to_model(rows[-1]))

    # ------------------------------------------------------------ 构建（T7.2 异步）

    def build(self, instance_id: str, *, dry_run: bool = False, seed: int | None = None,
              batch: int | None = None, resume: bool = False,
              manifest_id: str | None = None,
              operator: str | None = None) -> dict:
        """构建（异步执行）：PLANNED→GENERATING→LOADING→VERIFYING→READY/FAILED。

        - 幂等：同一 instance 已在执行 → 返回 running 状态不重复排队；
        - 生产环境显式拒绝（总方案 §15.1：生产环境不提供构建入口）；
        - PARTIAL 恢复（resume=true）必须携带 manifest_id（§3.5 约束 3）。
        """
        row = self._require_instance_row(instance_id)
        self._reject_production(row, "构建")
        future = self._tasks.get(row.id)
        if future is not None and not future.done():
            return {"taskId": str(row.id), "accepted": False, "state": "running"}
        params = {"dry_run": dry_run, "seed": seed, "batch": batch,
                  "resume": resume, "manifest_id": manifest_id}
        if resume:
            if row.status != "PARTIAL":
                raise DatasetServiceError(
                    f"resume 仅适用 PARTIAL 实例（当前 {row.status}）")
            if not manifest_id:
                raise DatasetServiceError(
                    "PARTIAL 恢复转换必须携带恢复上下文 manifest_id（§3.5 约束 3）")
        elif row.status != "PLANNED":
            raise DatasetServiceError(
                f"构建仅适用 PLANNED 实例（当前 {row.status}；"
                "PARTIAL 恢复请走 resume=true）")
        cancel_event = threading.Event()
        self._cancel_flags[row.id] = cancel_event
        future = self._executor.submit(
            self._run_build, row.id, params, operator or self._operator,
            cancel_event)
        self._tasks[row.id] = future
        return {"taskId": str(row.id), "accepted": True, "state": "running"}

    def _run_build(self, row_id: int, params: dict, operator: str,
                   cancel_event: threading.Event | None = None) -> None:
        """工作线程：三段执行体编排 + 状态机推进 + 门禁终局（不占 HTTP 线程）。

        取消协议（B7 续批次 3）：cancel_event 经 ctx 注入执行体（generate 段
        内部安全点可检查）；工作线程在段落边界检查信号，协作退出不落 FAILED
        （取消是主动处置，非失败）。
        """
        reason_suffix = "dry-run" if params.get("dry_run") else "执行"
        ctx: dict = {"params": params, "cancel_event": cancel_event}
        try:
            self._check_cancelled(row_id, cancel_event, "启动")
            model = self._transition(
                row_id, DatasetState.GENERATING, operator,
                f"数据集构建启动（{reason_suffix}）",
                manifest_id=params.get("manifest_id"))
            ctx["instance"] = model.model_dump()
            gen_summary = self._pipeline.generate(ctx)
            self._check_cancelled(row_id, cancel_event, "装载前")
            model = self._transition(
                row_id, DatasetState.LOADING, operator,
                f"生成完成（{self._summary_hint(gen_summary)}），进入装载")
            load_summary = self._pipeline.load(ctx)
            self._check_cancelled(row_id, cancel_event, "验证前")
            model = self._transition(
                row_id, DatasetState.VERIFYING, operator,
                f"装载完成（{self._summary_hint(load_summary)}），进入验证")
            self._finalize_verification(row_id, ctx, model, operator)
        except DatasetCancelled:
            # 协作退出：cancel() 已在状态锁内完成 CANCELLED 流转落库；
            # 信号置位而状态未流转（协议违规）兜底走失败路径。
            row = self._repo.find_instance(row_id)
            if row is not None and row.status != DatasetState.CANCELLED.value:
                self._record_failure(row_id, operator, DatasetCancelled(
                    "取消信号置位但状态未流转（协议违规兜底）"))
        except Exception as exc:  # noqa: BLE001 — 工作线程兜底：失败显式落库
            self._record_failure(row_id, operator, exc)
        finally:
            self._cancel_flags.pop(row_id, None)

    def verify(self, instance_id: str, *, operator: str | None = None) -> dict:
        """独立验证（异步）：仅 LOADING 实例可进 VERIFYING（READY 复验须走重建）。"""
        row = self._require_instance_row(instance_id)
        self._reject_production(row, "验证")
        future = self._tasks.get(row.id)
        if future is not None and not future.done():
            return {"taskId": str(row.id), "accepted": False, "state": "running"}
        if row.status != "LOADING":
            raise DatasetServiceError(
                f"验证仅适用 LOADING 实例（当前 {row.status}；"
                "READY/FAILED 复验须经状态机合法流转）")
        future = self._executor.submit(
            self._run_verify, row.id, operator or self._operator)
        self._tasks[row.id] = future
        return {"taskId": str(row.id), "accepted": True, "state": "running"}

    def _run_verify(self, row_id: int, operator: str) -> None:
        try:
            model = self._transition(
                row_id, DatasetState.VERIFYING, operator, "验证启动（独立验证请求）")
            ctx = {"instance": model.model_dump(), "params": {"verify_only": True}}
            self._finalize_verification(row_id, ctx, model, operator)
        except Exception as exc:  # noqa: BLE001 — 工作线程兜底
            self._record_failure(row_id, operator, exc)

    def _finalize_verification(self, row_id: int, ctx: dict, model, operator: str) -> None:
        """验证终局：报告落库 → manifest 落库 → READY 八项程序化判定 →
        READY/FAILED（经状态机）。"""
        result = self._pipeline.verify(ctx) or {}
        report = ValidationReport.model_validate(result["report"])
        if report.instance_id != model.id:
            raise DatasetServiceError(
                f"验证报告 instance_id 链路不一致：报告 {report.instance_id!r} vs "
                f"实例 {model.id!r}（可追溯性要求，门禁 4）")
        for layer in report.layers:
            self._repo.report_from_layer(row_id, layer)
        manifest = ExecutionManifest.model_validate(result["manifest"])
        if manifest.instance_id != model.id:
            raise DatasetServiceError(
                f"ExecutionManifest.instance_id 链路不一致：{manifest.instance_id!r} vs "
                f"实例 {model.id!r}（§12.4 链路可溯，READY 条件 7）")
        self._repo.insert_manifest(row_id, manifest)  # READY/FAILED 均留痕（可追溯）
        cleanup_ctx = CleanupContext.model_validate(result["cleanup"])
        cleanup_decision = plan_cleanup(cleanup_ctx)
        decision = evaluate_ready(model, report, manifest, cleanup_decision,
                                  degradable_layers=self._degradable_layers)
        summary = {
            "ready": decision.allowed,
            "checks": [c.model_dump() for c in decision.checks],
        }
        if decision.allowed:
            transitioned = transition(
                model, DatasetState.READY, operator=operator,
                reason="READY 门禁八项全部满足（程序化判定，§18.4）",
            )
        else:
            failed = "; ".join(f"{c.name}: {c.detail}" for c in decision.failed())
            transitioned = transition(
                model, DatasetState.FAILED, operator=operator,
                reason=f"READY 门禁未满足：{failed}")
        self._repo.update_instance_status(
            row_id, transitioned.status.value,
            self._repo.transition_log_to_json(transitioned))
        self._repo.update_instance_result(
            row_id, validation_summary=json.dumps(summary, ensure_ascii=False))

    def task_status(self, instance_id: str) -> dict:
        row = self._require_instance_row(instance_id)
        future = self._tasks.get(row.id)
        running = future is not None and not future.done()
        return {"taskId": str(row.id), "running": running, "instanceStatus": row.status}

    # ------------------------------------------------------------ 清理（T6.8 收编）

    def cleanup(self, instance_id: str, *, env_class: str, approvals=None,
                executor=None, referenced_by_others: bool = False,
                partitioned: bool = False, rows_to_clean: int = 0,
                batch_plan_ready: bool = False,
                operator: str | None = None) -> dict:
        """清理（同步）：决策（plan_cleanup）→ CLEANING → 授权执行 → CLEANED。

        决策与执行分离（K5）：blocked 决策直接抛 CleanupBlocked（原因清单
        随异常返回）；执行须过 SecurityGate CLEANUP 授权 + 三级审计。
        生产/未知环境在 plan_cleanup 内即被阻止（§13.1）。
        """
        row = self._require_instance_row(instance_id)
        model = self._instance_model(row)
        decision = plan_cleanup(CleanupContext(
            env_class=env_class,
            owner_token=row.owner_token,
            partitioned=partitioned,
            referenced_by_others=referenced_by_others,
            rows_to_clean=rows_to_clean,
            batch_plan_ready=batch_plan_ready,
        ))
        if decision.blocked:
            raise CleanupBlocked(
                f"清理被阻止：{decision.reasons}（不得绕过决策——破坏性操作须显式处置）")
        current = model.status
        if current not in (DatasetState.READY, DatasetState.FAILED):
            raise DatasetServiceError(
                f"清理仅适用 READY/FAILED 实例（当前 {current.value}）")
        transitioned = transition(
            model, DatasetState.CLEANING, operator=operator or self._operator,
            reason=f"清理启动（档位 {decision.tier.value}，环境 {env_class}）")
        self._repo.update_instance_status(
            row.id, transitioned.status.value,
            self._repo.transition_log_to_json(transitioned))
        gate = SecurityGate()
        executor = executor if executor is not None else self._pipeline.cleanup_executor()
        result = execute_cleanup(
            decision, gate, env_class=env_class,
            approvals=[self._to_approval(a) for a in (approvals or ())],
            executor=executor,
        )
        transitioned = transition(
            transitioned, DatasetState.CLEANED, operator=operator or self._operator,
            reason=f"清理完成（档位 {decision.tier.value}，已过 CLEANUP 授权与三级审计）")
        self._repo.update_instance_status(
            row.id, transitioned.status.value,
            self._repo.transition_log_to_json(transitioned))
        return {"tier": decision.tier.value, **result}

    # ------------------------------------------------------------ 内部工具

    def _require_instance_row(self, instance_id: str) -> PtdeDatasetInstanceModel:
        try:
            row_id = int(instance_id)
        except (TypeError, ValueError):
            raise DatasetServiceError(f"instanceId 非法：{instance_id!r}") from None
        row = self._repo.find_instance(row_id)
        if row is None:
            raise DatasetServiceError(f"Instance {instance_id!r} 不存在（显式失败不静默）")
        return row

    def _instance_model(self, row: PtdeDatasetInstanceModel):
        definition = self._repo.find_definition_by_id(row.definition_id)
        return self._repo.instance_to_model(
            row, definition.def_key if definition is not None else None)

    def _instance_camel(self, row: PtdeDatasetInstanceModel, model) -> dict:
        definition = self._repo.find_definition_by_id(row.definition_id)
        return {
            "id": str(row.id),
            "definitionId": f"{definition.def_key}@{row.definition_version}"
            if definition is not None else str(row.definition_id),
            "definitionRowId": row.definition_id,
            "definitionVersion": row.definition_version,
            "environmentId": row.environment_label,
            "status": row.status,
            "schemaFingerprint": row.schema_fingerprint,
            "actualRows": row.actual_rows,
            "actualStorageBytes": row.actual_storage_bytes,
            "validationSummary": (
                json.loads(row.validation_summary) if row.validation_summary else None),
            "errorMsg": row.error_msg,
            "transitionLog": [t.model_dump(mode="json") for t in model.transition_log],
            "createTime": row.create_time.isoformat(timespec="seconds"),
        }

    def _transition(self, row_id: int, to_state: DatasetState, operator: str,
                    reason: str, manifest_id: str | None = None):
        row = self._repo.find_instance(row_id)
        assert row is not None
        model = self._instance_model(row)
        transitioned = transition(
            model, to_state, operator=operator, reason=reason,
            manifest_id=manifest_id)
        self._repo.update_instance_status(
            row_id, transitioned.status.value,
            self._repo.transition_log_to_json(transitioned))
        return transitioned

    # ------------------------------------------------------------ 取消（B7 续批次 3）

    def cancel(self, instance_id: str, *, operator: str | None = None,
               reason: str | None = None) -> dict:
        """主动取消（PLANNED/GENERATING/LOADING → CANCELLED，经状态机唯一入口）。

        - 出边评审结论（§3.5 / 总方案 §12.2 / ALLOWED_TRANSITIONS 三方一致，
          自 R0 即在）：三态可取消；VERIFYING/READY/终态/PARTIAL 无出边，
          显式拒绝并指明处置路径（不放宽转换表）；
        - operator/reason 必填，transition_log 落库即审计，禁止绕过状态机；
        - 执行中实例：状态锁内「置位取消事件 + 流转 CANCELLED」原子完成，
          工作线程在段落边界/分片间观察到信号后协作退出（不强制 kill）；
        - 幂等：已 CANCELLED 返回 accepted=false，不二次流转；
        - 已产生的分片/staging 保留供排查（不做物理清理；CANCELLED 为终态，
          重建须新建实例）。
        """
        if not operator or not operator.strip():
            raise DatasetServiceError("operator 不能为空（取消须经状态机留痕审计）")
        if not reason or not reason.strip():
            raise DatasetServiceError("reason 不能为空（取消须经状态机留痕审计）")
        row = self._require_instance_row(instance_id)
        current = DatasetState(row.status)
        if current == DatasetState.CANCELLED:
            return {"taskId": str(row.id), "accepted": False,
                    "state": "CANCELLED",
                    "message": "实例已取消（幂等：不二次流转）"}
        if current not in CANCELABLE_STATES:
            raise DatasetServiceError(
                f"实例当前 {current.value}，不可取消（§3.5 转换表该态无 "
                "CANCELLED 出边；READY/FAILED 处置走 /cleanup，VERIFYING/"
                "PARTIAL 请等待终局或走恢复路径）")
        event = self._cancel_flags.get(row.id)
        with self._state_lock:
            if event is not None:
                event.set()  # 先置信号：协作式退出（杀线程不可靠，不强制 kill）
            row = self._repo.find_instance(row.id)
            assert row is not None
            model = self._instance_model(row)
            transitioned = transition(
                model, DatasetState.CANCELLED, operator=operator.strip(),
                reason=reason.strip())
            self._repo.update_instance_status(
                row.id, transitioned.status.value,
                self._repo.transition_log_to_json(transitioned))
        return {"taskId": str(row.id), "accepted": True,
                "cancelled": True, "state": "CANCELLED"}

    def _check_cancelled(self, row_id: int, event: threading.Event | None,
                         where: str) -> None:
        """段落边界取消检查（锁内原子判定：事件置位 ⇔ 已流转 CANCELLED）。"""
        if event is None:
            return
        with self._state_lock:
            row = self._repo.find_instance(row_id)
            if row is not None and row.status == DatasetState.CANCELLED.value:
                raise DatasetCancelled(f"实例 {row_id} 已取消（{where}协作退出）")
            if event.is_set():
                raise DatasetCancelled(
                    f"实例 {row_id} 取消信号已置位（{where}，协议违规兜底）")

    def _record_failure(self, row_id: int, operator: str, exc: Exception) -> None:
        """工作线程兜底：状态机允许范围内转 FAILED 并落 error_msg（不伪装成功）。"""
        row = self._repo.find_instance(row_id)
        if row is None or row.status == DatasetState.CANCELLED.value:
            return  # 取消终局不落失败痕迹（取消是主动处置，非失败）
        self._repo.update_instance_error(row_id, f"{type(exc).__name__}: {exc}")
        model = self._instance_model(row)
        if model.status in (DatasetState.GENERATING, DatasetState.LOADING,
                            DatasetState.VERIFYING):
            try:
                transitioned = transition(
                    model, DatasetState.FAILED, operator=operator,
                    reason=f"构建/验证失败：{type(exc).__name__}: {exc}")
                self._repo.update_instance_status(
                    row_id, transitioned.status.value,
                    self._repo.transition_log_to_json(transitioned))
            except InvalidStateTransition:
                pass  # 已在终态或流转不允许：error_msg 已留痕

    def _reject_production(self, row: PtdeDatasetInstanceModel, action: str) -> None:
        label = (row.environment_label or "").strip().lower()
        if any(marker in label for marker in PRODUCTION_MARKERS):
            raise DatasetServiceError(
                f"生产环境不提供数据集{action}入口（总方案 §15.1；"
                f"environmentId={row.environment_label!r}）")

    @staticmethod
    def _to_approval(item: Any) -> DestructiveApproval:
        """授权条目归一：camelCase API 形态（authorizedBy）→ 模型字段。"""
        if isinstance(item, DestructiveApproval):
            return item
        if not isinstance(item, dict):
            raise DatasetServiceError(
                '授权条目形态非法：' + repr(item) +
                '（须为 {"operation", "target", "authorizedBy"} JSON 对象）')
        try:
            return DestructiveApproval.model_validate({
                "operation": item.get("operation"),
                "target": item.get("target"),
                "authorized_by": item.get("authorizedBy") or item.get("authorized_by"),
                "ticket": item.get("ticket") or "",
            })
        except Exception as exc:  # noqa: BLE001 — 形态错误显式透出
            raise DatasetServiceError(f"授权条目校验失败：{exc}") from None

    @staticmethod
    def _summary_hint(summary: Any) -> str:
        if isinstance(summary, dict):
            for key in ("summary", "hint", "rows", "loaded_rows", "generated_rows"):
                if key in summary:
                    return str(summary[key])
            return f"{len(summary)} 项"
        return str(summary) if summary else "无摘要"
