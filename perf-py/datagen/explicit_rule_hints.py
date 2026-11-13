"""显式规则提取：移植自 engine/ExplicitRuleHints.java。

从 TableSpec.fields 中提取 ref/refSkew 边（四级来源中最高优先级）。
指向组外表的规则被忽略（引擎层 dry-run 产出警告）。
"""

from __future__ import annotations

from datagen.model import DataGenSpec, FieldRule, RelationHint, SchemaMetadata


def extract(spec: DataGenSpec | None, meta_map: dict[str, SchemaMetadata]) -> list[RelationHint]:
    hints: list[RelationHint] = []
    if spec is None:
        return hints
    for ts in spec.tables:
        child_table = ts.table_name.lower() if ts.table_name is not None else None
        if child_table is None or child_table not in meta_map:
            continue
        for column_key, fr in ts.fields.items():
            if fr is None:
                continue
            is_ref = fr.type in (FieldRule.TYPE_REF, FieldRule.TYPE_REF_SKEW)
            if not is_ref or fr.table is None or fr.column is None:
                continue
            parent_table = fr.table.lower()
            if parent_table not in meta_map:
                continue
            h = RelationHint(child_table, column_key, parent_table, fr.column,
                             RelationHint.SRC_EXPLICIT, 1.0)
            h.source_refs.append("request")
            hints.append(h)
    return hints
