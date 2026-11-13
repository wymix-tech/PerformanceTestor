"""命名推断数据抽样验证（移植 NamingResolverSamplingTest）：包含率 → 置信度分级。

Java 侧用 H2 内存库；Python 侧等价载体用 sqlite3（LIMIT/引号标识符兼容），
FakeDialect 仅提供 quote_identifier。
"""

import sqlite3

from datagen.model import ColumnMeta, RelationHint, SchemaMetadata
from datagen.naming_relation_resolver import NamingRelationResolver


class FakeDialect:
    @staticmethod
    def quote_identifier(name: str) -> str:
        return f'"{name}"'


def _init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE seller (id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO seller (id) VALUES (?)", [(i,) for i in range(1, 41)])
    conn.execute("CREATE TABLE goods (id INTEGER PRIMARY KEY, seller_id INTEGER)")
    # 子表 10 行全部引用有效父 ID（包含率 1.0）
    conn.executemany("INSERT INTO goods (seller_id) VALUES (?)", [(i,) for i in range(1, 11)])
    return conn


def meta(name: str, row_count: int) -> SchemaMetadata:
    m = SchemaMetadata(table_name=name, row_count=row_count)
    pk = ColumnMeta(name="id", type_name="BIGINT",
                    primary_key=True, auto_increment=True)
    m.columns.append(pk)
    m.primary_keys.add("id")
    return m


def test_high_contain_rate_boosts_confidence():
    conn = _init_db()
    goods = meta("goods", 10)
    goods.columns.append(ColumnMeta(name="seller_id", type_name="BIGINT"))
    parents = {"seller": meta("seller", 40)}
    hints = NamingRelationResolver().resolve(goods, parents, conn, FakeDialect())
    assert len(hints) == 1
    h = hints[0]
    assert h.parent_table == "seller"
    # 包含率 1.0 ≥ 95% → 置信度 0.85
    assert h.confidence == 0.85
    assert any("containRate=1.00" in s for s in h.source_refs)
    conn.close()


def test_no_match_yields_no_hint():
    conn = _init_db()
    goods = meta("goods", 10)
    goods.columns.append(ColumnMeta(name="amount", type_name="BIGINT"))
    parents = {"seller": meta("seller", 40)}
    hints = NamingRelationResolver().resolve(goods, parents, conn, FakeDialect())
    assert not hints
    conn.close()


def test_empty_parent_gives_low_confidence_fallback():
    conn = _init_db()
    goods = meta("goods2", 0)
    goods.columns.append(ColumnMeta(name="seller_id", type_name="BIGINT"))
    parents = {"seller": meta("seller", 0)}  # 空表
    hints = NamingRelationResolver().resolve(goods, parents, conn, FakeDialect())
    assert len(hints) == 1
    assert hints[0].source == RelationHint.SRC_NAMING_EMPTY
    assert hints[0].confidence == 0.3
    conn.close()


def test_low_contain_rate_yields_low_confidence():
    """包含率 < 95% → 置信度 0.4（抽样验证的拒绝分支）。"""
    conn = _init_db()
    # 清空子表后插入一半无效引用 → 包含率 0.5
    conn.execute("DELETE FROM goods")
    conn.executemany("INSERT INTO goods (seller_id) VALUES (?)",
                     [(i,) for i in range(1, 11)])  # 1~5 有效、6~10 无效（父表只有 1~40？）
    # 注意父表为 1~40，全部有效；改为插入 60+ 制造无效引用
    conn.execute("DELETE FROM goods")
    conn.executemany("INSERT INTO goods (seller_id) VALUES (?)",
                     [(i,) for i in list(range(1, 6)) + [998, 999, 1000, 1001, 1002]])
    goods = meta("goods", 10)
    goods.columns.append(ColumnMeta(name="seller_id", type_name="BIGINT"))
    parents = {"seller": meta("seller", 40)}
    hints = NamingRelationResolver().resolve(goods, parents, conn, FakeDialect())
    assert len(hints) == 1
    assert hints[0].confidence == 0.4
    conn.close()
