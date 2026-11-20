"""DBAPI 批量执行器（自 datagen/executor.py 原样收编终态）。

移植自 executor/JdbcBatchExecutor.java。

承接原 DataGenService 批量插入逻辑（addBatch/executeBatch → executemany），
元素已由引擎转换为 DBAPI 友好类型。

性能警告：热点倾斜极高（refSkew 使单热行承载 >50% 子行）时，高并发压测中
数据库行锁冲突会抵消批量写入收益。建议调用方降低 BATCH_SIZE（如 500→200）
或为子表分区。
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from ptde.adapters.direct.spi import BatchExecutor

DEFAULT_BATCH_SIZE = 500


class DbApiBatchExecutor(BatchExecutor):
    """DBAPI 批量执行器（executemany 等价 JDBC addBatch/executeBatch）。"""

    def __init__(self, batch_size: int = DEFAULT_BATCH_SIZE) -> None:
        self.batch_size = max(1, batch_size)

    def get_batch_size(self) -> int:
        return self.batch_size

    def execute_batch(self, conn, sql: str, rows: list[list]) -> int:
        if not rows:
            return 0
        cur = conn.cursor()
        try:
            cur.executemany(sql, [_adapt_values(r) for r in rows])
        finally:
            cur.close()
        # rewriteBatchedStatements 场景 executeBatch 不返回精确行数，
        # 调用方（引擎）以 rows 数量作为权威插入计数（对齐 Java）
        return len(rows)


def _adapt_values(row: list) -> list:
    """按值运行时类型转换（Java setObject 兜底 LocalDateTime 等价）。

    DBAPI 不原生支持的类型在此适配：datetime/date → 文本、Decimal → float。
    """
    return [_adapt(v) for v in row]


def _adapt(v):
    if v is None or isinstance(v, (bool, int, float, str, bytes)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return str(v)
    return str(v)
