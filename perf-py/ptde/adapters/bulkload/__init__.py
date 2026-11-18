"""装载降级链包（R5 T5.5，实施方案 §8.3：``bulkload/fallback.py``）。

收编口径：legacy ``datagen/executor.py`` 为降级链最后档（本体保留不动，
A1 分轨）；官方替代档（MySQL 服务端 INFILE）复用 T5.3 装载语义。
"""

from ptde.adapters.bulkload.fallback import (
    DEFAULT_BATCH_SIZE,
    BudgetExceededError,
    DbApiBatchInsertLoader,
    FallbackLoader,
    FallbackOutcome,
    LoadBudget,
    MysqlServerInfileAdapter,
    default_chain_for_product,
)

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "BudgetExceededError",
    "DbApiBatchInsertLoader",
    "FallbackLoader",
    "FallbackOutcome",
    "LoadBudget",
    "MysqlServerInfileAdapter",
    "default_chain_for_product",
]
