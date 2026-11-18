"""枚举发现收编 + 证据登记（T1.5，自 `ptde_common/enum_value_discoverer.py` 包装）。

四级来源（F6）各自登记独立证据条目（发现一条不掩盖另一条）：
1. 方言 ENUM（MySQL get_enum_values / PG pg_enum——"方言内 CHECK"路径由
   discover_with_db 内部承接）
2. TYPE_NAME 内联 ENUM/SET
3. CHECK 约束 / 列注释键值对
4. Java 枚举反查（lang_java 源码扫描产物）

三态：1/2 为 CONFIRMED（数据库类型事实）；3/4 为 INFERRED（猜测，进采纳流）。
全部来源未命中但列名疑似枚举（status/type/…）→ UNKNOWN 登记提示人工确认。
"""

from __future__ import annotations

from typing import Any, Protocol

from ptde.analyzer.language_adapter import Finding, FindingStatus
from ptde.models.base import Evidence
from ptde_common.enum_value_discoverer import (
    match_java_enum,
    parse_check_clause,
    parse_comment,
    parse_inline_enum,
    suspect_enum_column,
)

__all__ = ["EnumColumn", "EnumFinder"]

ANALYZER_VERSION = "0.1.0"


class EnumColumn(Protocol):
    """枚举发现输入的最小列视图（TableSchema.columns 元素天然满足）。"""

    name: str | None
    type_name: str | None
    comment: str | None


class EnumFinder:
    """包装 ptde_common 四级发现，输出带三态与分级证据的 Finding。"""

    def find(
        self,
        table_name: str,
        col: EnumColumn,
        *,
        check_clause: str | None = None,
        java_enums: dict[str, list[str]] | None = None,
        conn=None,
        dialect=None,
        source_refs: list[str] | None = None,
    ) -> list[Finding]:
        """返回该列的发现清单（命中来源各一条；疑似枚举列返回 UNKNOWN 提示）。"""
        table = (table_name or "").lower()
        findings: list[Finding] = []
        refs = list(source_refs or [])
        emitted = False

        # 级别 1：方言 ENUM（MySQL COLUMN_TYPE / PG pg_enum + CHECK 承接；数据库事实）
        db_vals: list[Any] = []
        if conn is not None and dialect is not None and table:
            try:
                db_vals = list(dialect.get_enum_values(conn, table, col.name))
            except Exception:  # noqa: BLE001 方言查询失败降级后续来源（legacy 同语义）
                db_vals = []
            if db_vals:
                emitted = True
                findings.append(self._finding(
                    table, col, db_vals, "dialect_enum", FindingStatus.CONFIRMED, 1.0,
                    refs + ["db-enum"],
                ))
        inline_vals = list(parse_inline_enum(col.type_name) or [])
        if inline_vals and not db_vals:
            emitted = True
            findings.append(self._finding(
                table, col, inline_vals, "inline_enum", FindingStatus.CONFIRMED, 1.0,
                refs + ["column-type"],
            ))

        # 级别 3：CHECK 约束字面量（CONFIRMED——DDL 内的声明式约束）与注释（INFERRED）
        check_vals = list(parse_check_clause(check_clause) or []) if check_clause else []
        if check_vals:
            emitted = True
            findings.append(self._finding(
                table, col, check_vals, "check_constraint", FindingStatus.CONFIRMED, 1.0,
                refs + ["check-clause"], evidence_object=check_clause,
            ))
        comment_vals = list(parse_comment(col.comment, col) or [])
        if comment_vals:
            emitted = True
            findings.append(self._finding(
                table, col, comment_vals, "column_comment", FindingStatus.INFERRED, 0.6,
                refs + ["column-comment"],
            ))

        # 级别 4：Java 枚举反查（INFERRED）
        if java_enums and not emitted:
            candidates = match_java_enum(col.name, java_enums)
            if candidates:
                emitted = True
                findings.append(self._finding(
                    table, col, list(candidates), "java_enum_reflect",
                    FindingStatus.INFERRED, 0.7, refs + ["java-enum"],
                ))

        # 无命中：疑似枚举列 → UNKNOWN 提示（不伪装为事实）
        if not findings and suspect_enum_column(col):
            findings.append(self._finding(
                table, col, [], "suspect_enum", FindingStatus.UNKNOWN, 0.0, refs,
            ))
        return findings

    @staticmethod
    def _finding(
        table: str,
        col: EnumColumn,
        values: list[Any],
        source: str,
        status: FindingStatus,
        confidence: float,
        refs: list[str],
        *,
        evidence_object: str | None = None,
        db_vals: list[Any] | None = None,
    ) -> Finding:
        f = Finding(
            id=f"enum:{table}.{col.name}",
            type="enum",
            value={
                "table": table,
                "column": col.name,
                "values": values,
                "enum_source": source,
            },
            database_object=f"{table}.{col.name}",
            confidence=confidence,
            status=status,
            analyzer_version=ANALYZER_VERSION,
        )
        f.evidence = [Evidence(
            type="enum_discovery",
            object_name=evidence_object or f"{table}.{col.name}",
            detail=f"source={source}; refs={refs}",
            status=status.value,
        )]
        return f
