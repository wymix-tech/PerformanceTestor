"""命名约定关联推断（T1.3 收编 `datagen/naming_relation_resolver.py`）。

承接语义：子表列 <parent>_id（单复数归一）匹配父表主键 + 整数族类型兼容校验；
置信度分级 0.85（抽样包含率 ≥0.95）/ 0.4（抽样未达标）/ 0.5（无连接或抽样失败）/
0.3（父表空表 naming_empty_fallback）。

三态升级（A3）：naming 级输出一律 `INFERRED`，不自动伪装为数据库事实；
低置信项不静默生效，交采纳流（RelationResolver → ptde_finding）。

差异说明（继承 legacy）：DBAPI 连接不可跨线程，抽样在调用方线程同步执行。
类型输入为 ptde `TableSchema`（DatabaseSchemaSnapshot.tables 元素）。
"""

from __future__ import annotations

from ptde.models.base import Evidence, EvidenceStatus
from ptde.models.entity import RelationModel
from ptde.models.snapshot import TableSchema

__all__ = [
    "CONTAIN_RATE_THRESHOLD",
    "FULL_SCAN_THRESHOLD",
    "SAMPLE_SIZE",
    "NamingRelationResolver",
    "match_parent_table",
    "plural",
    "singular",
]

SAMPLE_SIZE = 1000
FULL_SCAN_THRESHOLD = 100
CONTAIN_RATE_THRESHOLD = 0.95


def base_type(type_name: str) -> str:
    """ENUM('A','B') 等复合类型名的基础类型提取（结果大写；legacy rule_engine 语义）。"""
    t = type_name.upper()
    paren = t.find("(")
    head = t[:paren].strip() if paren > 0 else t.strip()
    if head == "ENUM" or head == "SET":
        return "TEXT"
    return head


def is_integer_family(type_name: str) -> bool:
    """是否整数族列（关联列类型兼容校验用；legacy rule_engine.is_integer_family 语义）。"""
    return base_type(type_name) in ("INT", "BIGINT")


class NamingRelationResolver:
    """无状态解析器（实例可复用；conn/dialect 由调用方在 resolve 时传入）。"""

    def resolve(
        self,
        child: TableSchema | None,
        parents: dict[str, TableSchema],
        conn=None,
        dialect=None,
    ) -> list[RelationModel]:
        relations: list[RelationModel] = []
        if child is None or not parents:
            return relations
        pk = child.primary_keys[0] if child.primary_keys else "id"
        for col in child.columns:
            if col.primary_key or col.auto_increment or not is_integer_family(col.type_name or ""):
                continue
            parent_table = match_parent_table(col.name, pk, parents.keys())
            if parent_table is None:
                continue
            parent = parents.get(parent_table)
            if parent is None or not parent.primary_keys:
                continue
            parent_pk = parent.primary_keys[0]
            parent_pk_col = next(
                (c for c in parent.columns if c.name == parent_pk), None
            )
            if parent_pk_col is None or not is_integer_family(parent_pk_col.type_name or ""):
                continue
            if conn is None or dialect is None:
                # 无连接 → 纯命名推断 0.5（INFERRED，不自动生效）
                relations.append(self._relation(child, col.name, parent, parent_pk, 0.5))
                continue
            relations.append(
                self.verify_by_sampling(child, col.name, parent, parent_pk, conn, dialect)
            )
        return relations

    def verify_by_sampling(
        self,
        child: TableSchema,
        child_column: str,
        parent: TableSchema,
        parent_pk: str,
        conn,
        dialect,
    ) -> RelationModel:
        if parent.row_estimate == 0:
            rel = self._relation(child, child_column, parent, parent_pk, 0.3)
            rel.evidence.append(_ref("naming-convention(empty-table-fallback)"))
            return rel
        parent_ids = self._sample_parent_ids(parent.name, parent_pk, parent.row_estimate, conn, dialect)
        if not parent_ids:
            return self._relation(child, child_column, parent, parent_pk, 0.5)
        contain_rate = self._child_contain_rate(child.name, child_column, parent_ids, conn, dialect)
        if contain_rate < 0:
            return self._relation(child, child_column, parent, parent_pk, 0.5)
        confidence = 0.85 if contain_rate >= CONTAIN_RATE_THRESHOLD else 0.4
        rel = self._relation(child, child_column, parent, parent_pk, confidence)
        rel.evidence.append(_ref(f"sample(containRate={contain_rate:.2f})"))
        return rel

    @staticmethod
    def _relation(child: TableSchema, child_column: str,
                  parent: TableSchema, parent_pk: str, confidence: float) -> RelationModel:
        rel = RelationModel(
            child_table=child.name,
            child_column=child_column,
            parent_table=parent.name,
            parent_column=parent_pk,
            confidence=confidence,
            status="INFERRED",  # naming 级一律 INFERRED（A3）
        )
        rel.evidence.append(_ref("naming-convention"))
        return rel

    def _sample_parent_ids(self, table: str, pk: str, row_estimate: int, conn, dialect) -> list[int]:
        def _do() -> list[int]:
            ids: list[int] = []
            sql = f"SELECT {dialect.quote_identifier(pk)} FROM {dialect.quote_identifier(table)}"
            if row_estimate > FULL_SCAN_THRESHOLD:
                sql += f" ORDER BY {dialect.quote_identifier(pk)} LIMIT {SAMPLE_SIZE}"
            cur = conn.cursor()
            try:
                cur.execute(sql)
                for row in cur.fetchmany(SAMPLE_SIZE):
                    ids.append(int(row[0]))
            finally:
                cur.close()
            return ids

        try:
            return _do()  # 同步执行（DBAPI 连接不可跨线程，见模块 docstring）
        except Exception:  # noqa: BLE001 查询失败降级纯命名
            return []

    def _child_contain_rate(self, table: str, fk_column: str,
                            parent_ids: list[int], conn, dialect) -> float:
        try:
            pool = set(parent_ids)
            sql = (f"SELECT {dialect.quote_identifier(fk_column)} FROM "
                   f"{dialect.quote_identifier(table)} LIMIT {SAMPLE_SIZE}")
            cur = conn.cursor()
            try:
                cur.execute(sql)
                rows = cur.fetchall()
            finally:
                cur.close()
            total = hit = 0
            for row in rows:
                v = row[0]
                if v is None:  # 对齐 legacy wasNull 语义：NULL 行不计入分母
                    continue
                total += 1
                if v in pool:
                    hit += 1
            return -1.0 if total == 0 else hit / total
        except Exception:  # noqa: BLE001 查询失败降级纯命名
            return -1.0


def match_parent_table(column_name: str, child_pk: str, table_names) -> str | None:
    col = column_name.lower()
    suffix = "_" + child_pk.lower()
    if not col.endswith(suffix):
        return None
    stem = col[: -len(suffix)]
    if not stem:
        return None
    for t in table_names:
        if t == stem or t == singular(stem) or t == plural(stem):
            return t
    return None


def singular(s: str) -> str:
    if s.endswith("ies") and len(s) > 3:
        return s[:-3] + "y"
    if s.endswith("ses") and len(s) > 3:
        return s[:-2]
    if s.endswith("s") and not s.endswith("ss") and len(s) > 1:
        return s[:-1]
    return s


def plural(s: str) -> str:
    if (s.endswith("y") and len(s) > 1
            and s[-2] in "bcdfgjklmnpqrstvwz"):
        return s[:-1] + "ies"
    if s.endswith("s") or s.endswith("x") or s.endswith("ch") or s.endswith("sh"):
        return s + "es"
    return s + "s"


def _ref(detail: str) -> Evidence:
    return Evidence(type="naming_inference", detail=detail, status=EvidenceStatus.INFERRED)
