"""T6.2 结构关系验证（实施方案 §9.2 T6.2；总方案 §18.4 条件 3）。

检查项（T6.2 原文全集）：
- 主键完整性 / 唯一约束：COUNT(*) vs COUNT(DISTINCT cols)——DISTINCT 漏
  NULL，二者相等 ⇔ 无重复且无 NULL（单条 SQL 覆盖两类破损）；
- 外键完整性 / 孤儿记录：**零孤儿要求的关系全量核对（非抽样）**——
  QW3 verify_min 孤儿抽检（≤200 行采样）收编升级为全量 LEFT JOIN，
  通过即提供强完整性证据（记入 evidence）；
- M:N 重复：联结表 (left, right) 重复对检测（COUNT(*) vs COUNT(DISTINCT)）；
- 逻辑关联：LOGICAL 关系（命名/语义推断，A3 三态）孤儿违规降级为 WARN
  （推断关系不作为硬规则）；DB_FK / ORM 关系为声明事实 → FAIL；
- 关联基数：每父实体子实体数上限核对（GROUP BY + HAVING 全量）。
"""

from __future__ import annotations

from typing import Any

from ptde.models.dataset import LayerKind, LayerResult, ValidationStatus
from ptde.models.entity import RelationModel
from ptde.validator import (
    layer_result,
    make_check,
    readonly_scalar,
    validate_identifier,
)

__all__ = ["validate_relationships"]

# 推断来源关系（A3：命名/注解推断不作为硬规则）→ 孤儿违规降级 WARN
_INFERRED_KINDS = frozenset({"logical"})


def _q(name: str) -> str:
    return validate_identifier(name)


def _check_keys(
    conn, kind: str, keys: dict[str, list[str]],
    checks: list, metrics: dict[str, Any],
) -> None:
    """PK/唯一键完整性：COUNT(*) == COUNT(DISTINCT cols)（NULL 与重复一网打尽）。"""
    for table in sorted(keys):
        cols = keys[table]
        if not cols:
            checks.append(make_check(
                f"{kind}:{table}", ValidationStatus.FAIL,
                detail="键列清单为空（无法核对，显式失败）",
            ))
            continue
        t = _q(table)
        total = int(readonly_scalar(conn, f"SELECT COUNT(*) FROM {t}") or 0)
        distinct_cols = ", ".join(_q(c) for c in cols)
        distinct = int(readonly_scalar(
            conn, f"SELECT COUNT(DISTINCT {distinct_cols}) FROM {t}") or 0)
        if total == distinct:
            checks.append(make_check(
                f"{kind}:{table}", ValidationStatus.PASS,
                detail=f"{len(cols)} 列键全表核对：无重复且无 NULL（{total} 行）",
            ))
        else:
            bad = total - distinct
            checks.append(make_check(
                f"{kind}:{table}", ValidationStatus.FAIL,
                detail=f"键完整性破损：{total} 行 vs DISTINCT {distinct}"
                f"（{bad} 行 NULL 或重复，全表非抽样核对）",
            ))
        metrics[f"{kind}_tables"] = metrics.get(f"{kind}_tables", 0) + 1


def validate_relationships(
    conn,
    relations: list[RelationModel],
    *,
    pk_keys: dict[str, list[str]] | None = None,
    unique_keys: dict[str, list[str]] | None = None,
    join_tables: dict[str, tuple[str, str]] | None = None,
    cardinality_limits: dict[str, int] | None = None,
) -> LayerResult:
    """关系完整性验证（表/关系按名称排序，确定性输出）。

    - ``relations``：表间关系四元组（RelationModel；DB_FK/ORM 孤儿违规
      → FAIL，LOGICAL → WARN）；
    - ``pk_keys`` / ``unique_keys``：表 → 键列清单（完整性核对）；
    - ``join_tables``：M:N 联结表 → (left_col, right_col)（重复对检测）；
    - ``cardinality_limits``：``子表.外键列`` → 每父实体最大子实体数。
    """
    checks: list = []
    metrics: dict[str, Any] = {"relations_checked": 0, "orphan_free_relations": 0}
    evidence: list[str] = []

    # 1. 外键完整性：零孤儿全量核对（非抽样；去重防同四元组重复核对）
    seen: set[tuple[str, str, str, str]] = set()
    for rel in sorted(
        relations,
        key=lambda r: (r.child_table, r.child_column, r.parent_table, r.parent_column),
    ):
        if not (rel.child_table and rel.child_column
                and rel.parent_table and rel.parent_column):
            checks.append(make_check(
                "fk:incomplete", ValidationStatus.FAIL,
                detail=f"关系定义不完整：{rel.child_table}.{rel.child_column}"
                f" -> {rel.parent_table}.{rel.parent_column}（显式失败）",
            ))
            continue
        key = (rel.child_table, rel.child_column, rel.parent_table, rel.parent_column)
        if key in seen:
            continue
        seen.add(key)
        name = f"fk:{key[0]}.{key[1]}->{key[2]}.{key[3]}"
        sql = (
            f"SELECT COUNT(*) FROM (SELECT {_q(key[1])} FROM {_q(key[0])} "
            f"WHERE {_q(key[1])} IS NOT NULL) s "
            f"LEFT JOIN {_q(key[2])} p ON s.{_q(key[1])} = p.{_q(key[3])} "
            f"WHERE p.{_q(key[3])} IS NULL"
        )
        orphans = int(readonly_scalar(conn, sql) or 0)
        metrics["relations_checked"] += 1
        inferred = rel.relation_kind.value in _INFERRED_KINDS
        if orphans == 0:
            metrics["orphan_free_relations"] += 1
            checks.append(make_check(
                name, ValidationStatus.PASS,
                detail=f"全量核对孤儿=0（非抽样，强完整性证据；"
                f"关系来源 {rel.relation_kind.value}）",
            ))
            evidence.append(f"{name}: 全量 LEFT JOIN 核对零孤儿（非抽样）")
        elif inferred:
            checks.append(make_check(
                name, ValidationStatus.WARN,
                detail=f"发现 {orphans} 条孤儿（关系来源 "
                f"{rel.relation_kind.value} 为推断——不作为硬规则，"
                "降级 WARN 记录）",
            ))
        else:
            checks.append(make_check(
                name, ValidationStatus.FAIL,
                detail=f"发现 {orphans} 条孤儿（声明关系 {rel.relation_kind.value}"
                "，零孤儿要求全量核对非抽样——显式失败）",
            ))

    # 2. 主键 / 唯一键完整性
    _check_keys(conn, "pk", pk_keys or {}, checks, metrics)
    _check_keys(conn, "unique", unique_keys or {}, checks, metrics)

    # 3. M:N 联结表重复对
    for table in sorted(join_tables or {}):
        left, right = join_tables[table]
        t = _q(table)
        total = int(readonly_scalar(conn, f"SELECT COUNT(*) FROM {t}") or 0)
        distinct = int(readonly_scalar(
            conn,
            f"SELECT COUNT(DISTINCT {_q(left)}, {_q(right)}) FROM {t}",
        ) or 0)
        if total == distinct:
            checks.append(make_check(
                f"mn:{table}", ValidationStatus.PASS,
                detail=f"联结对全表唯一（{total} 对，无重复）",
            ))
        else:
            checks.append(make_check(
                f"mn:{table}", ValidationStatus.FAIL,
                detail=f"M:N 联结对重复：{total} 行 vs DISTINCT {distinct}"
                f"（{total - distinct} 行重复，全表核对）",
            ))
        metrics["join_tables"] = metrics.get("join_tables", 0) + 1

    # 4. 关联基数上限（每父实体子实体数；键 = 子表.外键列）
    for rel_key in sorted(cardinality_limits or {}):
        limit = int(cardinality_limits[rel_key])
        child_table, child_col = rel_key.split(".", 1)
        sql = (
            f"SELECT COUNT(*) FROM (SELECT {_q(child_col)} FROM {_q(child_table)} "
            f"WHERE {_q(child_col)} IS NOT NULL GROUP BY {_q(child_col)} "
            f"HAVING COUNT(*) > {limit}) t"
        )
        violations = int(readonly_scalar(conn, sql) or 0)
        if violations == 0:
            checks.append(make_check(
                f"cardinality:{rel_key}", ValidationStatus.PASS,
                detail=f"无父实体子实体数超过上限 {limit}（全量 GROUP BY 核对）",
            ))
        else:
            checks.append(make_check(
                f"cardinality:{rel_key}", ValidationStatus.FAIL,
                detail=f"{violations} 个父实体子实体数超过上限 {limit}"
                "（关联基数越界，显式失败）",
            ))
        metrics["cardinality_limits"] = metrics.get("cardinality_limits", 0) + 1

    return layer_result(LayerKind.RELATIONSHIP, checks, metrics=metrics, evidence=evidence)
