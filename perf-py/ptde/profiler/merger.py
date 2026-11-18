"""统一画像融合（R2 T2.6）：按总方案 §4.4 优先级表逐项裁决。

裁决规则（§4.4 / 实施方案 §5.4）：
- 字段类型、主键、外键：以目标 Schema 为准；统计不覆盖——冲突记入 MergeReport；
- 字段值分布：脱敏生产统计为准；可被 DatasetSpec 目标覆盖，
  覆盖动作记入 MergeReport（sources=[stats, dataset_spec], winner=dataset_spec）；
- 热点分布：以统计为准，裁决记录带统计新鲜度（collected_at）与置信度
  （sampling_rate / fit_quality）；
- 缺失特征：显式标记 estimated，不得伪装为生产统计。

字段含义（源码/人工，AI 推断带 INFERRED 标记）与业务规则由 R1 analyzer 三态
发现流承载，不在本融合器裁决范围。
"""

from __future__ import annotations

from ptde.models.profile import (
    ColumnProfile,
    DistributionFit,
    EntityProfile,
    MergeConflict,
    MergeDecision,
    MergeReport,
    RelationCardinalityProfile,
    StatisticsProfile,
    TableProfile,
    UnifiedDataProfile,
)
from ptde.models.snapshot import DatabaseSchemaSnapshot, TableSchema

__all__ = ["ProfileMerger"]


class ProfileMerger:
    """StatisticsProfile（统计）+ DatabaseSchemaSnapshot（Schema 硬约束）
    → UnifiedDataProfile + MergeReport。"""

    def merge(
        self,
        statistics: StatisticsProfile,
        schema: DatabaseSchemaSnapshot | None = None,
        value_overrides: dict[str, dict | DistributionFit] | None = None,
        profile_id: str = "",
    ) -> tuple[UnifiedDataProfile, MergeReport]:
        """融合入口。

        value_overrides：DatasetSpec 字段值分布覆盖，键为 "table.column"，
        值为 DistributionFit 或等价 dict。
        """
        report = MergeReport()
        entities: dict[str, EntityProfile] = {}
        stats_tables = {t.table: t for t in statistics.tables}
        schema_tables = {t.name: t for t in schema.tables} if schema is not None else {}

        ordered = list(schema_tables) + [t for t in stats_tables if t not in schema_tables]
        for table in ordered:
            entities[table] = self._merge_table(
                table,
                schema_tables.get(table),
                stats_tables.get(table),
                statistics,
                value_overrides or {},
                report,
            )

        unified = UnifiedDataProfile(
            id=profile_id or f"unified-{statistics.id}",
            sources=[statistics.source],
            entities=entities,
            merge_report=report,
        ).seal()
        return unified, report

    # ---------- 单表裁决 ----------

    def _merge_table(
        self,
        table: str,
        tschema: TableSchema | None,
        stprofile: TableProfile | None,
        statistics: StatisticsProfile,
        value_overrides: dict,
        report: MergeReport,
    ) -> EntityProfile:
        entity = EntityProfile(entity=table, table=table)
        self._decide_primary_key(table, tschema, stprofile, report)

        # 关系（外键边以 Schema 为准；子实体数分布来自统计，缺失记 estimated）
        if tschema is not None:
            for fk in tschema.foreign_keys:
                rel = self._merge_relation(
                    table, fk.parent_table, stprofile, statistics, report
                )
                entity.relation_cardinalities.append(rel)
        elif stprofile is not None:
            entity.relation_cardinalities.extend(stprofile.relations)

        # 列（Schema 列序优先；统计多出的列保留但注明来源）
        stats_cols = {c.column: c for c in (stprofile.columns if stprofile else [])}
        ordered_cols = [c.name for c in tschema.columns] if tschema is not None else []
        ordered_cols += [c for c in stats_cols if c not in ordered_cols]
        for col in ordered_cols:
            entity.columns[col] = self._merge_column(
                table,
                col,
                stats_cols.get(col),
                value_overrides.get(f"{table}.{col}"),
                tschema is not None,
                statistics,
                report,
            )
        return entity

    # ---------- 主键（硬约束行） ----------

    def _decide_primary_key(
        self, table: str, tschema: TableSchema | None,
        stprofile: TableProfile | None, report: MergeReport,
    ) -> None:
        """主键裁决：Schema 为准；统计侧声称与 Schema 不一致即冲突（验收门禁 3）。"""
        schema_pk = list(tschema.primary_keys) if tschema is not None else []
        stats_pk = list(stprofile.pk_columns) if stprofile else []
        if tschema is None and stats_pk:
            # 无 Schema 快照：统计侧主键观察仅登记为 estimated，不作为硬约束
            report.decisions.append(
                MergeDecision(
                    subject=f"{table}.primary_key",
                    basis="stats",
                    chosen_from="stats",
                    note=f"无 Schema 快照，统计侧主键观察 {stats_pk} 按 estimated 登记，"
                         "不作为硬约束",
                )
            )
            return
        report.decisions.append(
            MergeDecision(
                subject=f"{table}.primary_key",
                basis="schema",
                chosen_from="schema",
                note=f"schema={schema_pk}",
            )
        )
        if stats_pk and stats_pk != schema_pk:
            report.conflicts.append(
                MergeConflict(
                    subject=f"{table}.primary_key",
                    sources=["schema", "stats"],
                    winner="schema",
                    reason="主键属源码/Schema 硬约束，统计不覆盖（总方案 §4.4）："
                           f"schema={schema_pk} vs stats={stats_pk}",
                )
            )

    # ---------- 关联基数（外键行） ----------

    def _merge_relation(
        self, child_table: str, parent_table: str, stprofile: TableProfile | None,
        statistics: StatisticsProfile, report: MergeReport,
    ) -> RelationCardinalityProfile:
        rel = None
        if stprofile is not None:
            for r in stprofile.relations:
                if r.child_table == child_table and r.parent_table == parent_table:
                    rel = r
                    break
        if rel is None:
            rel = RelationCardinalityProfile(
                child_table=child_table, parent_table=parent_table
            )
            report.decisions.append(
                MergeDecision(
                    subject=f"{child_table}->{parent_table}:cardinality",
                    basis="schema",
                    chosen_from="schema",
                    note="外键边来自 Schema；子实体数分布缺失，标 estimated，"
                         "不得伪装为生产统计",
                )
            )
        else:
            report.decisions.append(
                MergeDecision(
                    subject=f"{child_table}->{parent_table}:cardinality",
                    basis="stats",
                    chosen_from="stats",
                    note=self._freshness(statistics),
                )
            )
        return rel

    # ---------- 列与分布（字段值分布行 / 缺失特征行） ----------

    def _merge_column(
        self, table: str, col: str, stats_cp: ColumnProfile | None, override,
        has_schema: bool, statistics: StatisticsProfile, report: MergeReport,
    ) -> ColumnProfile:
        if override is not None:
            fitted = (
                override if isinstance(override, DistributionFit)
                else DistributionFit.model_validate(override)
            )
            chosen = (
                stats_cp.model_copy(update={"distribution": fitted})
                if stats_cp is not None
                else ColumnProfile(column=col, distribution=fitted)
            )
            # 字段值分布可被 DatasetSpec 覆盖，但覆盖动作记入 MergeReport（§4.4）
            report.conflicts.append(
                MergeConflict(
                    subject=f"{table}.{col}.distribution",
                    sources=["stats", "dataset_spec"],
                    winner="dataset_spec",
                    reason="字段值分布属可覆盖项（测试目标优先），覆盖动作记入报告",
                )
            )
            return chosen
        if stats_cp is not None:
            if stats_cp.distribution is not None:
                report.decisions.append(
                    MergeDecision(
                        subject=f"{table}.{col}.distribution",
                        basis="stats",
                        chosen_from="stats",
                        note=self._freshness(statistics),
                    )
                )
            return stats_cp
        # 缺失特征：显式 estimated，不伪装为生产统计（§4.4 末行）
        report.decisions.append(
            MergeDecision(
                subject=f"{table}.{col}.distribution",
                basis="schema" if has_schema else "none",
                chosen_from="estimated",
                note="统计画像缺失该列特征，按 estimated 处理，不得伪装为生产统计",
            )
        )
        return ColumnProfile(column=col, accuracy="estimated")

    # ---------- 新鲜度/置信度注记（热点分布行） ----------

    @staticmethod
    def _freshness(statistics: StatisticsProfile) -> str:
        collected = (
            statistics.collected_at.isoformat()
            if statistics.collected_at is not None
            else "unknown"
        )
        rate = (
            statistics.source.sampling_rate
            if statistics.source.sampling_rate is not None
            else "unknown"
        )
        return (
            f"统计新鲜度 collected_at={collected} sampling_rate={rate}"
            "（置信度随采样率递增）"
        )
