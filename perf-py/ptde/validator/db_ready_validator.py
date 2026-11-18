"""T6.5 数据库就绪验证（实施方案 §9.2 T6.5；总方案 §18.4 条件 6）。

检查项（T6.5 原文全集）：
- 表/索引大小：经 StorageMetricsAdapter 实测（预算已配置而不可实测 →
  FAIL，未配置 → ok+说明）；
- 索引完整性：计划索引逐个存在性核对（MySQL information_schema.STATISTICS /
  PG pg_indexes，产品分发，未支持产品显式失败）；
- 分区分布：分区数与计划一致（不配置 → ok+说明）；
- 统计信息更新（ANALYZE 状态）：MySQL UPDATE_TIME/CHECK_TIME /
  PG last_analyze[last_autoanalyze]——analyze_required=True 时未更新 →
  FAIL，否则 WARN（可降级项）；
- 核心查询执行计划：EXPLAIN 可执行（内层语句先过白名单校验）；
- 选择性接近画像 / 热点达成：实测核对；
- selfCheck 收编（datagen_service._self_check 口径）：端点抽检回调——
  0 命中 WARN、部分命中 PASS（与 legacy 一致：selfCheck 从不阻断），
  未提供回调 → WARN「[未执行]」（未执行 ≠ 通过，不伪装）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import Field

from ptde.adapters.base import StorageMetricsAdapter
from ptde.models.dataset import LayerKind, LayerResult, ValidationStatus
from ptde.models.spec import StorageEstimate
from ptde.profiler.collector import assert_readonly_sql
from ptde.validator import (
    layer_result,
    make_check,
    readonly_rows,
    readonly_scalar,
    validate_identifier,
)
from ptde_common.model_base import VersionedModel

__all__ = [
    "HotspotCheck",
    "SelectivityCheck",
    "validate_db_ready",
]


class SelectivityCheck(VersionedModel):
    """选择性核对：distinct/rows vs 预期（相对容差）。"""

    table: str
    column: str
    expected: float = Field(ge=0.0, le=1.0)
    relative: float = Field(default=0.1, gt=0.0, le=1.0)


class HotspotCheck(VersionedModel):
    """热点达成核对：Top-1 占比达到最低集中度。"""

    table: str
    column: str
    min_ratio: float = Field(gt=0.0, le=1.0)


def _q(name: str) -> str:
    return validate_identifier(name)


def _index_exists(conn, product: str, table: str, index: str) -> int | None:
    """计划索引存在性（返回计数；不支持产品显式失败）。"""
    if product == "mysql":
        return int(readonly_scalar(
            conn,
            "SELECT COUNT(*) FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND INDEX_NAME = %s",
            [table, index],
        ) or 0)
    if product == "postgres":
        return int(readonly_scalar(
            conn,
            "SELECT COUNT(*) FROM pg_indexes "
            "WHERE schemaname = current_schema() AND tablename = %s "
            "AND indexname = %s",
            [table, index],
        ) or 0)
    raise ValueError(f"产品 {product!r} 不支持索引存在性核对（显式失败）")


def _partition_count(conn, product: str, table: str) -> int | None:
    """分区数（不支持产品显式失败）。"""
    if product == "mysql":
        return int(readonly_scalar(
            conn,
            "SELECT COUNT(DISTINCT PARTITION_NAME) FROM information_schema.PARTITIONS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND PARTITION_NAME IS NOT NULL",
            [table],
        ) or 0)
    if product == "postgres":
        return int(readonly_scalar(
            conn,
            "SELECT COUNT(*) FROM pg_inherits i "
            "JOIN pg_class child ON i.inhrelid = child.oid "
            "JOIN pg_class parent ON i.inhparent = parent.oid "
            "WHERE parent.relname = %s",
            [table],
        ) or 0)
    raise ValueError(f"产品 {product!r} 不支持分区分布核对（显式失败）")


def _stats_freshness(
    conn, product: str, table: str,
) -> tuple[bool, str]:
    """统计信息更新状态：返回 (是否已更新, 说明)。"""
    if product == "mysql":
        row = readonly_rows(
            conn,
            "SELECT UPDATE_TIME, CHECK_TIME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            [table],
        )
        if not row:
            return False, "表元数据不可得"
        update_time, check_time = row[0][0], row[0][1]
        if update_time is not None or check_time is not None:
            return True, f"UPDATE_TIME={update_time} CHECK_TIME={check_time}"
        return False, "UPDATE_TIME/CHECK_TIME 均为空（InnoDB 须显式 ANALYZE 后核对）"
    if product == "postgres":
        row = readonly_rows(
            conn,
            "SELECT last_analyze, last_autoanalyze FROM pg_stat_user_tables "
            "WHERE schemaname = current_schema() AND relname = %s",
            [table],
        )
        if not row:
            return False, "pg_stat_user_tables 无记录（统计信息未更新）"
        last_analyze, last_auto = row[0][0], row[0][1]
        if last_analyze is not None or last_auto is not None:
            return True, f"last_analyze={last_analyze} last_autoanalyze={last_auto}"
        return False, "last_analyze/last_autoanalyze 均为空（统计信息未更新）"
    raise ValueError(f"产品 {product!r} 不支持统计信息核对（显式失败）")


def validate_db_ready(
    conn,
    product: str,
    *,
    planned_indexes: dict[str, list[str]] | None = None,
    storage_metrics: StorageMetricsAdapter | None = None,
    storage_tables: list[str] | None = None,
    storage_budget: StorageEstimate | None = None,
    partitioned_tables: dict[str, int] | None = None,
    analyzed_tables: list[str] | None = None,
    analyze_required: bool = False,
    core_queries: list[str] | None = None,
    selectivity_checks: list[SelectivityCheck] | None = None,
    hotspot_checks: list[HotspotCheck] | None = None,
    endpoint_probe: Callable[[], dict[str, int]] | None = None,
) -> LayerResult:
    """数据库就绪验证（product ∈ {mysql, postgres}，未支持产品显式失败）。

    - ``endpoint_probe``：selfCheck 收编回调（datagen_service._self_check
      口径），返回 {"checked": n, "nonEmpty": m}。
    """
    product = (product or "").lower()
    checks: list = []
    metrics: dict[str, Any] = {}

    # 1. 表/索引大小（预算已配置而不可实测 → FAIL；未配置 → ok+说明）
    if storage_budget is None and storage_metrics is None:
        checks.append(make_check(
            "storage_size", ValidationStatus.PASS,
            detail="未配置存储预算与实测适配器（不适用，ok+说明）",
        ))
    elif storage_budget is not None and storage_metrics is None:
        checks.append(make_check(
            "storage_size", ValidationStatus.FAIL,
            detail="存储预算已配置但未提供 StorageMetricsAdapter（不可实测——"
            "显式失败，不伪装通过）",
        ))
    else:
        measured_total = 0
        for table in sorted(storage_tables or []):
            table_bytes, index_bytes, _rows = storage_metrics.measure(
                conn, table, include_indexes=True)
            measured_total += int(table_bytes or 0) + int(index_bytes or 0)
        metrics["measured_storage_bytes"] = measured_total
        budget = storage_budget.estimated_total_size_bytes if storage_budget else None
        if budget is None:
            checks.append(make_check(
                "storage_size", ValidationStatus.PASS,
                detail=f"实测总大小 {measured_total} 字节（未配置预算基线，仅记录）",
            ))
        elif measured_total <= budget:
            checks.append(make_check(
                "storage_size", ValidationStatus.PASS,
                detail=f"实测 {measured_total} ≤ 预算 {budget} 字节",
            ))
        else:
            checks.append(make_check(
                "storage_size", ValidationStatus.FAIL,
                detail=f"实测 {measured_total} > 预算 {budget} 字节（超预算显式失败）",
            ))

    # 2. 索引完整性（计划索引逐个存在性）
    for table in sorted(planned_indexes or {}):
        for index in planned_indexes[table]:
            cnt = _index_exists(conn, product, table, index)
            name = f"index:{table}.{index}"
            if cnt:
                checks.append(make_check(
                    name, ValidationStatus.PASS, detail="计划索引已存在",
                ))
            else:
                checks.append(make_check(
                    name, ValidationStatus.FAIL,
                    detail="计划索引缺失（索引完整性核对显式失败）",
                ))
            metrics["planned_indexes"] = metrics.get("planned_indexes", 0) + 1

    # 3. 分区分布（分区数与计划一致；不配置 → ok+说明）
    if not partitioned_tables:
        checks.append(make_check(
            "partitions", ValidationStatus.PASS,
            detail="未配置分区表（不适用，ok+说明）",
        ))
    else:
        for table in sorted(partitioned_tables):
            expected = int(partitioned_tables[table])
            actual = _partition_count(conn, product, table)
            name = f"partitions:{table}"
            if actual == expected:
                checks.append(make_check(
                    name, ValidationStatus.PASS,
                    detail=f"分区数 {actual} 与计划一致",
                ))
            else:
                checks.append(make_check(
                    name, ValidationStatus.FAIL,
                    detail=f"分区数 {actual} ≠ 计划 {expected}（分布核对显式失败）",
                ))

    # 4. 统计信息更新（ANALYZE 状态）
    if not analyzed_tables:
        checks.append(make_check(
            "statistics_updated", ValidationStatus.WARN,
            detail="[未执行] 未提供待核对统计信息的表清单（不伪装通过）",
        ))
    else:
        for table in sorted(analyzed_tables):
            fresh, why = _stats_freshness(conn, product, table)
            name = f"analyze:{table}"
            if fresh:
                checks.append(make_check(name, ValidationStatus.PASS, detail=why))
            elif analyze_required:
                checks.append(make_check(
                    name, ValidationStatus.FAIL,
                    detail=f"统计信息未更新且 analyze_required（{why}——显式失败）",
                ))
            else:
                checks.append(make_check(
                    name, ValidationStatus.WARN,
                    detail=f"统计信息未更新（{why}——可降级项，建议装载后 ANALYZE）",
                ))

    # 5. 核心查询执行计划（内层语句先过白名单，再 EXPLAIN）
    for i, query in enumerate(core_queries or []):
        name = f"explain[{i}]"
        try:
            inner = assert_readonly_sql(query)
            cur = conn.cursor()
            try:
                cur.execute(f"EXPLAIN {inner}", [])
                plan = cur.fetchall()
            finally:
                cur.close()
            if plan:
                checks.append(make_check(
                    name, ValidationStatus.PASS,
                    detail=f"执行计划可产出（{len(plan)} 行计划）",
                ))
            else:
                checks.append(make_check(
                    name, ValidationStatus.FAIL, detail="执行计划为空（显式失败）",
                ))
        except Exception as exc:  # noqa: BLE001 — 计划失败显式记录
            checks.append(make_check(
                name, ValidationStatus.FAIL,
                detail=f"核心查询执行计划失败：{exc}",
            ))
        metrics["core_queries"] = metrics.get("core_queries", 0) + 1

    # 6. 选择性接近画像
    for chk in selectivity_checks or []:
        t, c = _q(chk.table), _q(chk.column)
        row = readonly_rows(
            conn, f"SELECT COUNT(*), COUNT(DISTINCT {c}) FROM {t}")
        if not row:
            checks.append(make_check(
                f"selectivity:{chk.table}.{chk.column}", ValidationStatus.FAIL,
                detail="统计量不可得（显式失败）",
            ))
            continue
        total, distinct = int(row[0][0] or 0), int(row[0][1] or 0)
        sel = (distinct / total) if total else 0.0
        ok = abs(sel - chk.expected) <= chk.relative * max(chk.expected, 1e-12)
        checks.append(make_check(
            f"selectivity:{chk.table}.{chk.column}",
            ValidationStatus.PASS if ok else ValidationStatus.FAIL,
            detail=f"选择性目标 {chk.expected}，实测 {sel:.6f}"
            f"（distinct {distinct}/{total}，容差 {chk.relative}）",
        ))

    # 7. 热点达成
    for chk in hotspot_checks or []:
        t, c = _q(chk.table), _q(chk.column)
        total = int(readonly_scalar(
            conn, f"SELECT COUNT(*) FROM {t} WHERE {c} IS NOT NULL") or 0)
        top = int(readonly_scalar(
            conn,
            f"SELECT MAX(c) FROM (SELECT COUNT(*) AS c FROM {t} "
            f"WHERE {c} IS NOT NULL GROUP BY {c}) x",
        ) or 0)
        ratio = (top / total) if total else 0.0
        ok = ratio >= chk.min_ratio
        checks.append(make_check(
            f"hotspot:{chk.table}.{chk.column}",
            ValidationStatus.PASS if ok else ValidationStatus.FAIL,
            detail=f"热点 Top-1 占比实测 {ratio:.6f}，达成线 {chk.min_ratio}",
        ))

    # 8. selfCheck 收编（datagen_service._self_check 口径：从不阻断）
    if endpoint_probe is None:
        checks.append(make_check(
            "self_check", ValidationStatus.WARN,
            detail="[未执行] 端点抽检回调未提供（收编自 datagen_service "
            "selfCheck——未执行 ≠ 通过，不伪装）",
        ))
    else:
        try:
            result = endpoint_probe()
        except Exception as exc:  # noqa: BLE001
            checks.append(make_check(
                "self_check", ValidationStatus.WARN,
                detail=f"端点抽检执行失败（{exc}——selfCheck 不阻断，WARN 记录）",
            ))
        else:
            checked = int(result.get("checked", 0))
            non_empty = int(result.get("nonEmpty", 0))
            rate = (non_empty / checked) if checked else 0.0
            if checked == 0:
                checks.append(make_check(
                    "self_check", ValidationStatus.WARN,
                    detail="端点抽检无端点可查（nonEmptyRate=0，WARN 与 legacy 一致）",
                ))
            elif rate == 0.0:
                checks.append(make_check(
                    "self_check", ValidationStatus.WARN,
                    detail=f"nonEmptyRate=0（{non_empty}/{checked} 命中——"
                    "legacy 口径：0 命中加 warning）",
                ))
            else:
                checks.append(make_check(
                    "self_check", ValidationStatus.PASS,
                    detail=f"nonEmptyRate={rate:.2f}（{non_empty}/{checked}）",
                ))

    return layer_result(LayerKind.DB_READY, checks, metrics=metrics)
