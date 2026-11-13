"""命名约定关联推断（第四级兜底）：移植自 engine/NamingRelationResolver.java。

子表列 <parent>_id（单复数归一）匹配父表主键 + 类型兼容校验。
空表兜底：父表 0 行 → 无条件采纳（0.3 / naming_empty_fallback）。
抽样：父表 <100 行全表、>100 抽 1000 条；超时 500ms 降级纯命名（0.5）。

抽样经 DBAPI cursor（conn.cursor()/execute），表名/列名由 dialect.quote_identifier
加引号——方言 SPI 在阶段 3 步骤 4e（DefaultDataGenEngine 编排）接入。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from datagen.model import RelationHint, SchemaMetadata
from datagen.rule_engine import is_integer_family

SAMPLE_SIZE = 1000
SAMPLE_TIMEOUT_S = 0.5
FULL_SCAN_THRESHOLD = 100
CONTAIN_RATE_THRESHOLD = 0.95


class NamingRelationResolver:

    def __init__(self) -> None:
        # Java 侧单线程 daemon sampler + 超时降级；Python 用单 worker 池等价
        self._sampler = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="datagen-naming-sampler")

    def resolve(self, child: SchemaMetadata | None,
                parents: dict[str, SchemaMetadata],
                conn=None, dialect=None) -> list[RelationHint]:
        hints: list[RelationHint] = []
        if child is None or not parents:
            return hints
        pk = next(iter(child.primary_keys)) if child.primary_keys else "id"
        for col in child.columns:
            # 注：Java 对 type_name 为 null 会在 baseType 抛 NPE；Python 侧按空串
            # 处理返回 False（仅影响元数据残缺的病态输入，语义等价）
            if col.primary_key or col.auto_increment or not is_integer_family(col.type_name or ""):
                continue
            parent_table = match_parent_table(col.name, pk, parents.keys())
            if parent_table is None:
                continue
            parent = parents.get(parent_table)
            if parent is None or not parent.primary_keys:
                continue
            parent_pk = next(iter(parent.primary_keys))
            parent_pk_col = parent.find_column(parent_pk)
            if parent_pk_col is None or not is_integer_family(parent_pk_col.type_name or ""):
                continue
            if conn is None or dialect is None:
                # 无连接 → 纯命名推断 0.5
                hints.append(RelationHint(child.table_name, col.name,
                                          parent_table, parent_pk,
                                          RelationHint.SRC_NAMING, 0.5))
                continue
            hints.append(self.verify_by_sampling(child, col.name, parent, parent_pk,
                                                 conn, dialect))
        return hints

    def verify_by_sampling(self, child: SchemaMetadata, child_column: str,
                           parent: SchemaMetadata, parent_pk: str,
                           conn, dialect) -> RelationHint:
        if parent.row_count == 0:
            return RelationHint.naming_fallback(child.table_name, child_column,
                                                parent.table_name, parent_pk)
        parent_ids = self._sample_parent_ids(parent.table_name, parent_pk,
                                             parent.row_count, conn, dialect)
        if not parent_ids:
            return RelationHint(child.table_name, child_column, parent.table_name, parent_pk,
                                RelationHint.SRC_NAMING, 0.5)
        contain_rate = self._child_contain_rate(child.table_name, child_column,
                                                parent_ids, conn, dialect)
        if contain_rate < 0:
            return RelationHint(child.table_name, child_column, parent.table_name, parent_pk,
                                RelationHint.SRC_NAMING, 0.5)
        confidence = 0.85 if contain_rate >= CONTAIN_RATE_THRESHOLD else 0.4
        h = RelationHint(child.table_name, child_column, parent.table_name, parent_pk,
                         RelationHint.SRC_NAMING, confidence)
        h.source_refs.append(f"sample(containRate={contain_rate:.2f})")
        return h

    def _sample_parent_ids(self, table: str, pk: str, row_count: int, conn, dialect) -> list[int]:
        def _do() -> list[int]:
            ids: list[int] = []
            sql = f"SELECT {dialect.quote_identifier(pk)} FROM {dialect.quote_identifier(table)}"
            if row_count > FULL_SCAN_THRESHOLD:
                sql += f" ORDER BY {dialect.quote_identifier(pk)} LIMIT {SAMPLE_SIZE}"
            cur = conn.cursor()
            try:
                cur.execute(sql)
                for row in cur.fetchmany(SAMPLE_SIZE):
                    ids.append(int(row[0]))
            finally:
                cur.close()
            return ids

        future = self._sampler.submit(_do)
        try:
            return future.result(timeout=SAMPLE_TIMEOUT_S)
        except TimeoutError:
            future.cancel()
            return []
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
                if v is None:  # 对齐 JDBC wasNull：NULL 行不计入分母
                    continue
                total += 1
                if v in pool:
                    hit += 1
            return -1.0 if total == 0 else hit / total
        except Exception:  # noqa: BLE001 查询失败降级纯命名
            return -1.0


def match_parent_table(column_name: str, child_pk: str,
                       table_names) -> str | None:
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
