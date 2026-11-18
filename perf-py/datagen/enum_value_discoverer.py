"""枚举值四级发现：共享层 re-export（A4 过渡）。

实现自 R0 起迁入 `ptde_common/enum_value_discoverer.py`；本模块保留原 import
路径与符号集合，legacy（default_engine / datagen_service）不改 import。
R1 由 `ptde/analyzer/enum_finder.py` 收编（输出补证据三态），R7 随 datagen 包退役。
"""

from ptde_common.enum_value_discoverer import *  # noqa: F401,F403
from ptde_common.enum_value_discoverer import (  # noqa: F401 显式补齐星导可见性
    CHECK_EQ,
    CHECK_IN,
    COMMENT_PAIR,
    QUOTED,
    SUSPECT_NAME,
    ColumnLike,
    discover,
    discover_with_db,
    match_java_enum,
    parse_check_clause,
    parse_comment,
    parse_inline_enum,
    suspect_enum_column,
)
from ptde_common.enum_value_discoverer import re as re  # noqa: F401 保持原模块 re 符号可见
