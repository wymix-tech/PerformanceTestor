"""装载降级链编排（R5 T5.5，实施方案 §8.2/§8.3；总方案 §9.4/§10.3）。

降级链（总方案 §10.3，按档显式判定，逐档留痕——门禁 4「降级有说明」）：
  1. external KV/装载侧执行通道（B4.2 遗留问题 1 接入点，未实现仅预留装配）；
  2. 原生批量装载（前段 T5.3/T5.4 BulkLoadAdapter，``ensure_capable`` 判定，
     ``BulkLoadUnsupported``（带原因）即降级判定输入）；
  3. 官方替代批量接口（MySQL 服务端 LOAD DATA INFILE，受 secure_file_priv
     白名单约束——与 LOCAL 通道相反，勿混淆 B5 §4 结论）；
  4. 参数化批量 INSERT（收编 legacy ``datagen/executor.py`` 为最后档，
     ``DEFAULT_BATCH_SIZE=500`` 与 refSkew>50% 热行 batch 调优警告语义保留；
     legacy 本体保留不动——A1 分轨）。

预算语义（总方案 §10.3）：目标规模无法在资源预算内可靠完成 → 执行前明确失败
（``BudgetExceededError``），不伪装成功；吞吐校准值必须来自 Pilot Run 实测
（无通用固定值，对齐 §9.5 口径）。

A3 约束：降级只发生在「通道不可用」（BulkLoadUnsupported）——运行时失败
（BulkLoadError）原样抛出不静默换通道；降级原因逐档记录并随结果回传。

探测→策略接线（B5 §7 衔接项）：``plan_cycle_strategy`` 把 T5.2 Capability
快照（deferred_constraints 能力位）接入 ``resolve_cycle`` 调用方；
``staged_load_guard`` 为 STAGED_LOAD 策略提供 Constraint/Transaction 执行
原语（默认保留全部约束，关闭 FK 校验须显式批准——总方案 §9.4/§19）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import Field

from ptde.adapters.base import (
    BulkLoadAdapter,
    BulkLoadError,
    BulkLoadResult,
    BulkLoadUnsupported,
)
from ptde.adapters.mysql.bulkload import IGNORE_LINES, MysqlBulkLoadAdapter
from ptde.adapters.mysql.statistics import validate_identifier
from ptde.adapters.postgres.bulkload import PostgresBulkLoadAdapter
from ptde_common.model_base import VersionedModel

__all__ = [
    "BudgetExceededError",
    "DbApiBatchInsertLoader",
    "EXTERNAL_LOADER_REGISTRY",
    "FallbackLoader",
    "FallbackOutcome",
    "HOT_ROW_SKEW_THRESHOLD",
    "LoadBudget",
    "MysqlServerInfileAdapter",
    "default_chain_for_product",
    "deferred_constraints_of",
    "hot_row_batch_warning",
    "plan_cycle_strategy",
    "register_external_loader",
    "staged_load_guard",
]

DEFAULT_BATCH_SIZE = 500  # 收编 datagen/executor.py（A1：legacy 本体保留不动）
HOT_ROW_SKEW_THRESHOLD = 0.5  # refSkew > 50% 热行（executor.py 警告语义）


# ============================================================ 最后档：参数化批量 INSERT


def hot_row_batch_warning(ref_skew_ratio: float, batch_size: int) -> str:
    """热点倾斜 batch 调优警告（收编 executor.py docstring 语义，勿改口径）。

    refSkew ≤ 50% 不告警（返回空串）——>50% 才触发。
    """
    if ref_skew_ratio <= HOT_ROW_SKEW_THRESHOLD:
        return ""
    suggested = max(200, batch_size // 2)
    return (
        f"热点倾斜 refSkew={ref_skew_ratio:.0%} >50%：单热行承载子行过高时"
        "数据库行锁冲突抵消批量写入收益，建议降低 batch_size"
        f"（{batch_size}→{suggested}）或为子表分区（executor.py 口径收编）"
    )


class DbApiBatchInsertLoader(BulkLoadAdapter):
    """参数化批量 INSERT 降级档（收编 ``datagen/executor.py``，最后档）。

    与 legacy ``DbApiBatchExecutor`` 语义对齐：
    - ``executemany`` 等价 JDBC addBatch/executeBatch；逐批 commit（事务
      边界可控，总方案 §10.3 第 3 条）；
    - 返回行数以 rows 数量为权威（rewriteBatchedStatements 场景
      executeBatch 不返回精确行数，对齐 Java）；
    - DEFAULT_BATCH_SIZE=500；refSkew>50% 热行 batch 调优警告语义保留。
    """

    product = ""
    strategy = "dbapi_batch_insert"

    def __init__(self, batch_size: int = DEFAULT_BATCH_SIZE, product: str = "") -> None:
        self.batch_size = max(1, batch_size)
        self.product = product

    def ensure_capable(self, conn) -> list[str]:  # noqa: ARG002
        return [
            "参数化批量 INSERT 为 DBAPI 通用能力（降级档；批次/并发降档，"
            "总方案 §10.3）；装载行数以分片行数为权威并显式核对"
        ]

    def load_shard(
        self,
        conn,
        path: str,
        table: str,
        columns: Sequence[str],
        rows_expected: int,
        *,
        shard_id: str = "",
        entity: str = "",
        ref_skew_ratio: float | None = None,
        on_batch: Callable[[int, int, int], None] | None = None,
    ) -> BulkLoadResult:
        result = BulkLoadResult(
            shard_id=shard_id,
            entity=entity,
            table=table,
            strategy=self.strategy,
            rows_expected=rows_expected,
            collected_at=_now(),
        )
        validate_identifier(table)
        for col in columns:
            validate_identifier(col)
        if ref_skew_ratio is not None and ref_skew_ratio > HOT_ROW_SKEW_THRESHOLD:
            result.precheck_notes.append(
                hot_row_batch_warning(ref_skew_ratio, self.batch_size)
            )

        from ptde.generator.file_format import read_shard_file

        col_list = ", ".join(columns)
        sql = (
            f"INSERT INTO {table} ({col_list}) VALUES "
            f"({', '.join(['%s'] * len(columns))})"
        )
        rows_loaded = 0
        chunk_id = 0
        cur = conn.cursor()
        try:
            for batch in _batches(read_shard_file(path), self.batch_size):
                try:
                    cur.executemany(sql, batch)
                except Exception as exc:  # A3：运行时失败不静默换通道
                    raise BulkLoadError(
                        f"批量 INSERT 失败（分片 {shard_id or path}，chunk {chunk_id}）：{exc}",
                        rows_expected=rows_expected,
                        rows_loaded=rows_loaded,
                    ) from exc
                conn.commit()  # 逐批 commit（事务边界可控，§10.3）
                rows_loaded += len(batch)
                if on_batch is not None:
                    # chunk 编号从 1 起（§12.4：chunk_id=0 为 shard 级汇总行）
                    on_batch(chunk_id + 1, len(batch), rows_loaded)
                chunk_id += 1
        finally:
            cur.close()

        result.rows_loaded = rows_loaded
        if rows_loaded != rows_expected:
            raise BulkLoadError(
                f"分片 {shard_id or path} 装载行数不符（错误记录不静默丢弃）",
                rows_expected=rows_expected,
                rows_loaded=rows_loaded,
            )
        result.rows_verified = True
        result.seal()
        return result


def _batches(row_iter: Iterator[list], size: int) -> Iterator[list[list]]:
    """流式分批（不整文件驻内存；staging 读取器逐行产出）。"""
    batch: list[list] = []
    for _header, row in row_iter:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# ============================================================ 官方替代档（MySQL 服务端 INFILE）


class MysqlServerInfileAdapter(MysqlBulkLoadAdapter):
    """官方替代批量接口档：服务端 ``LOAD DATA INFILE``（LOCAL 不可用时第二档）。

    与 LOCAL 通道的安全语义相反（B5 §4 结论勿混淆）：
    - 服务端 INFILE **受** ``secure_file_priv`` 白名单约束；
    - 分片文件必须位于服务端白名单目录内（客户端 staging 路径不在其中
      → ``BulkLoadUnsupported`` 带原因，须先复制到服务端可读路径）。
    """

    product = "mysql"
    strategy = "load_data_server"

    def ensure_capable(self, conn) -> list[str]:
        secure = self._scalar(conn, "SELECT @@secure_file_priv")
        if secure is None or str(secure).strip() in ("", "NULL"):
            raise BulkLoadUnsupported(
                f"secure_file_priv={secure!r}：服务端 LOAD DATA INFILE 不可用"
                "（官方替代档受服务端文件白名单约束；LOCAL 通道才绕开该限制）"
            )
        return [f"secure_file_priv={secure!r}（服务端白名单，官方替代档前置条件）"]

    def load_shard(
        self,
        conn,
        path: str,
        table: str,
        columns,
        rows_expected: int,
        *,
        shard_id: str = "",
        entity: str = "",
    ) -> BulkLoadResult:
        secure = self._scalar(conn, "SELECT @@secure_file_priv")
        if secure and not Path(path).is_relative_to(Path(secure)):
            raise BulkLoadUnsupported(
                f"分片文件 {path} 不在 secure_file_priv={secure!r} 目录内——"
                "官方替代档（服务端 INFILE）须先复制到服务端白名单路径"
                "（或改用降级档批量 INSERT）"
            )
        return super().load_shard(
            conn, path, table, columns, rows_expected,
            shard_id=shard_id, entity=entity,
        )

    def _load_statement(self, path: str, table: str, columns) -> str:
        col_list = ", ".join(columns)
        return (
            f"LOAD DATA INFILE '{path}' INTO TABLE {table} "
            "FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"' "
            f"LINES TERMINATED BY '\\n' IGNORE {IGNORE_LINES} LINES ({col_list})"
        )


def default_chain_for_product(product: str) -> list[BulkLoadAdapter]:
    """按产品组装降级链（原生 → 官方替代 → 批量 INSERT；未知产品显式失败）。

    链序固定（总方案 §10.3）；batch 档批次大小属 Pilot Run 校准项（T5.8），
    构造方可用 ``FallbackLoader(chain=[...])`` 注入校准后的批次。
    """
    p = (product or "").lower()
    if p in ("mysql", "mariadb"):
        return [
            MysqlBulkLoadAdapter(),
            MysqlServerInfileAdapter(),
            DbApiBatchInsertLoader(DEFAULT_BATCH_SIZE, product="mysql"),
        ]
    if p in ("postgres", "postgresql", "pg"):
        return [
            PostgresBulkLoadAdapter(),
            DbApiBatchInsertLoader(DEFAULT_BATCH_SIZE, product="postgres"),
        ]
    raise ValueError(
        f"产品 {product!r} 无内置降级链（已支持: mysql/mariadb/postgres；"
        "显式失败，不静默换用）"
    )


# ============================================================ 预算（总方案 §10.3）


class BudgetExceededError(Exception):
    """目标规模无法在资源预算内可靠完成（执行前明确失败，不伪装成功）。"""


class LoadBudget(VersionedModel):
    """装载预算（执行前判定 §10.3；吞吐校准值必须来自 Pilot Run 实测）。"""

    exclude_fingerprint_fields = frozenset({"collected_at"})

    budget_seconds: int | None = None
    calibrated_rows_per_second: float | None = None  # 无通用固定值（§9.5）
    source: str | None = None  # 校准来源（如 pilot:entity）
    collected_at: datetime | None = None

    def ensure_feasible(self, rows_expected: int) -> str:
        """执行前预算判定：不可判定 → 返回说明（不伪装）；超预算 → 显式失败。"""
        if self.budget_seconds is None:
            return "预算上限未配置（无法执行前判定；接受运行后实测判定）"
        if self.calibrated_rows_per_second is None:
            return (
                "预算上限已配置但无 Pilot Run 校准吞吐（无通用固定值，"
                "不做通用假设——运行后实测判定，§9.5 口径）"
            )
        projected = rows_expected / self.calibrated_rows_per_second
        if projected > self.budget_seconds:
            raise BudgetExceededError(
                f"预计装载时长 {projected:.1f}s 超出预算 {self.budget_seconds}s"
                f"（rows={rows_expected}，校准吞吐 "
                f"{self.calibrated_rows_per_second} rows/s，来源 {self.source}）"
                "——总方案 §10.3：执行前明确失败，不伪装成功"
            )
        return (
            f"预算内：预计 {projected:.1f}s / 上限 {self.budget_seconds}s"
            f"（校准吞吐 {self.calibrated_rows_per_second} rows/s）"
        )

    @classmethod
    def from_pilot(cls, report, budget_seconds: int) -> LoadBudget:
        """由 GenerationPilotReport 校准（load_ms 缺失/为 0 显式失败——不伪装）。"""
        if not report.rows_loaded or not getattr(report, "load_ms", 0):
            raise ValueError(
                "Pilot 报告无实测装载耗时/行数，不可校准吞吐"
                "（干跑报告不得伪装校准值，显式失败）"
            )
        rate = report.rows_loaded / (report.load_ms / 1000)
        return cls(
            budget_seconds=budget_seconds,
            calibrated_rows_per_second=round(rate, 2),
            source=f"pilot:{report.entity}",
            collected_at=_now(),
        )


# ============================================================ M:N 外部装载接入点（B4.2 遗留问题 1）

EXTERNAL_LOADER_REGISTRY: dict[str, Callable[[], Any]] = {}


def register_external_loader(
    product: str,
    factory: Callable[[], Any],
    *,
    overwrite: bool = False,
) -> None:
    """注册 M:N external KV/装载侧执行通道（B4.2 遗留问题 1 接入点）。

    现状：**未实现**——sharded 侧归 R4.3 partitioner/staging（语义已锁定，
    ``select_mn_strategy`` 枚举勿改）；external 装载通道归 B6。本注册点仅
    预留装配位：注册后 FallbackLoader 在原生档之前消费。
    """
    if not product or not callable(factory):
        raise ValueError("external loader 注册须提供 product 与可调用 factory（显式失败）")
    key = product.lower()
    if key in EXTERNAL_LOADER_REGISTRY and not overwrite:
        raise ValueError(f"external loader '{product}' 已注册（须显式 overwrite=True）")
    EXTERNAL_LOADER_REGISTRY[key] = factory


# ============================================================ 降级链编排


class FallbackOutcome(VersionedModel):
    """单分片降级链装载结果（策略留痕 + 降级原因可见——门禁 4）。"""

    exclude_fingerprint_fields = frozenset({"collected_at"})

    shard_id: str
    entity: str
    table: str
    strategy_used: str = ""
    degraded: bool = False  # 非最高档（原生）即降级
    degradation_trace: list[dict[str, str]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)  # 降级告警（可见不吞）
    result: BulkLoadResult | None = None
    collected_at: datetime | None = None


class FallbackLoader:
    """降级链编排器（逐档显式判定 + 原因留痕 + 预算执行前判定）。"""

    def __init__(
        self,
        product: str,
        *,
        chain: Sequence[BulkLoadAdapter] | None = None,
        budget: LoadBudget | None = None,
    ) -> None:
        self.product = (product or "").lower()
        self.chain = list(chain) if chain is not None else default_chain_for_product(product)
        self.budget = budget

    def load_shard(
        self,
        conn,
        path: str,
        table: str,
        columns: Sequence[str],
        rows_expected: int,
        *,
        shard_id: str = "",
        entity: str = "",
        ref_skew_ratio: float | None = None,
        on_batch: Callable[[int, int, int], None] | None = None,
    ) -> FallbackOutcome:
        outcome = FallbackOutcome(
            shard_id=shard_id,
            entity=entity,
            table=table,
            collected_at=_now(),
        )
        if self.budget is not None:
            outcome.warnings.append(self.budget.ensure_feasible(rows_expected))

        # 档 0：M:N external KV/装载侧执行通道（B4.2 遗留问题 1 接入点）
        external = EXTERNAL_LOADER_REGISTRY.get(self.product)
        if external is None:
            outcome.degradation_trace.append({
                "tier": "external_kv_loader",
                "status": "not_registered",
                "detail": (
                    "external KV/装载侧执行通道未注册（B4.2 遗留问题 1："
                    "接入点已预留，实现归 B6）"
                ),
            })
        else:
            self._try_tier(outcome, conn, path, table, columns, rows_expected,
                           shard_id, entity, tier="external_kv_loader",
                           adapter=external())

        for adapter in self.chain:
            if outcome.result is not None:
                break
            self._try_tier(outcome, conn, path, table, columns, rows_expected,
                           shard_id, entity, tier=f"adapter:{adapter.strategy}",
                           adapter=adapter,
                           ref_skew_ratio=ref_skew_ratio if
                           isinstance(adapter, DbApiBatchInsertLoader) else None,
                           on_batch=on_batch if
                           isinstance(adapter, DbApiBatchInsertLoader) else None)

        if outcome.result is None:
            reasons = "; ".join(
                f"[{t['tier']}] {t['detail']}" for t in outcome.degradation_trace
            )
            raise BulkLoadUnsupported(
                f"降级链穷尽：分片 {shard_id or path} 无可用装载通道。逐档原因：{reasons}"
            )

        outcome.strategy_used = outcome.result.strategy
        native = self.chain and outcome.result.strategy == self.chain[0].strategy
        outcome.degraded = not native
        if outcome.degraded:
            skip_reasons = [
                f"[{t['tier']}] {t['detail']}"
                for t in outcome.degradation_trace
                if t["status"] in ("unsupported", "not_registered")
            ]
            warning = (
                f"装载降级告警：策略 {outcome.strategy_used}（非原生档 {self.chain[0].strategy}）；"
                f"降级原因：{'；'.join(skip_reasons) if skip_reasons else '（见 trace）'}"
            )
            outcome.warnings.append(warning)
            # 降级原因随结果回传（门禁 4：降级有说明，可见不吞）
            outcome.result.precheck_notes.append(warning)
        return outcome

    # ------------------------------------------------------------ 内部

    def _try_tier(
        self,
        outcome: FallbackOutcome,
        conn,
        path: str,
        table: str,
        columns: Sequence[str],
        rows_expected: int,
        shard_id: str,
        entity: str,
        *,
        tier: str,
        adapter: Any,
        ref_skew_ratio: float | None = None,
        on_batch: Callable[[int, int, int], None] | None = None,
    ) -> None:
        """尝试单档：不可用（BulkLoadUnsupported）→ 留痕降级；
        运行时失败（BulkLoadError）→ 原样抛出（A3：不静默换通道）。"""
        try:
            if isinstance(adapter, BulkLoadAdapter):
                adapter.ensure_capable(conn)
            result = adapter.load_shard(
                conn, path, table, columns, rows_expected,
                shard_id=shard_id, entity=entity,
                **({"ref_skew_ratio": ref_skew_ratio} if ref_skew_ratio is not None else {}),
                **({"on_batch": on_batch} if on_batch is not None else {}),
            )
        except BulkLoadUnsupported as exc:
            outcome.degradation_trace.append({
                "tier": tier,
                "status": "unsupported",
                "detail": str(exc),
            })
            return
        outcome.result = result


# ============================================================ 探测→策略接线（B5 §7 衔接项）


def deferred_constraints_of(capability: Any) -> bool | None:
    """AdapterCapability（或其 to_snapshot() 字典）→ deferred_constraints 能力位。

    未知（None）不得视为 True——调用方（resolve_cycle）语义：未知时不启用
    延迟约束策略（A3：不伪装已探测）。
    """
    if capability is None:
        return None
    if isinstance(capability, dict):
        caps = capability.get("capabilities")
    else:
        caps = getattr(capability, "capabilities", None)
    if not isinstance(caps, dict) or "deferred_constraints" not in caps:
        return None
    value = caps["deferred_constraints"]
    return None if value is None else bool(value)


def plan_cycle_strategy(
    cycle_members: Sequence[str],
    fk_edges: Sequence[Any],
    capability: Any,
    *,
    allow_constraint_disabled: bool = False,
):
    """探测→策略接线：resolve_cycle 调用方消费 Capability 快照（勿另起一套）。

    ``supports_deferred_constraints`` 由 T5.2 探测快照填充（未知 → False，
    显式不启用）；``allow_constraint_disabled`` 对应 STAGED_LOAD 特殊策略
    的显式批准（默认保留全部约束，总方案 §9.4/§19）。
    """
    from ptde.generator.cycle_policy import resolve_cycle

    supports = deferred_constraints_of(capability)
    return resolve_cycle(
        cycle_members,
        fk_edges,
        supports_deferred_constraints=bool(supports),
        allow_constraint_disabled=allow_constraint_disabled,
    )


@contextmanager
def staged_load_guard(
    conn,
    constraint_adapter,
    transaction_adapter,
    *,
    approved: bool = False,
):
    """STAGED_LOAD 执行原语（Constraint/Transaction 接口接线，B5 §7 衔接项）。

    默认保留全部约束：关闭 FK 校验属显式批准的特殊策略（approved=False
    直接拒绝）；退出时恢复 FK 校验并提交事务（Transaction 边界可控）。
    """
    if not approved:
        raise ValueError(
            "STAGED_LOAD 需显式批准关闭 FK 校验（默认保留全部约束，"
            "总方案 §9.4/§19：特殊策略须显式批准+验证）"
        )
    constraint_adapter.disable_foreign_key_checks(conn)
    try:
        yield conn
    finally:
        constraint_adapter.enable_foreign_key_checks(conn)
        transaction_adapter.commit(conn)


def _now() -> datetime:
    return datetime.now(UTC)
