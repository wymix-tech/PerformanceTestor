"""PTDE 共享组件包（A4 前置抽取）。

本包是 `ptde/`（新管线）与 `datagen/`（legacy 管线）的共享层：
- 不得反向 import `datagen` 或 `ptde`（单向依赖：两边 → ptde_common）；
- 迁入模块保持行为不变（legacy 侧 re-export 过渡，R7 随整包退役）。

成员：
- id_pool: 父表主键池（自 datagen/id_pool.py 原样迁入）；
- enum_value_discoverer: 枚举值四级发现（自 datagen/enum_value_discoverer.py 迁入，
  仅将 `datagen.model.ColumnMeta` 类型标注替换为本包内 Protocol，行为不变）；
- model_base: VersionedModel 基座（version + fingerprint、规范 JSON、sha256）。
"""

from ptde_common.enum_value_discoverer import (
    discover,
    discover_with_db,
    match_java_enum,
    parse_check_clause,
    parse_comment,
    parse_inline_enum,
    suspect_enum_column,
)
from ptde_common.id_pool import MAX_POOL_SIZE, IdPool
from ptde_common.model_base import (
    MODEL_SCHEMA_VERSION,
    VersionedModel,
    canonical_json,
    sha256_hex,
)

__all__ = [
    "MAX_POOL_SIZE",
    "MODEL_SCHEMA_VERSION",
    "IdPool",
    "VersionedModel",
    "canonical_json",
    "discover",
    "discover_with_db",
    "match_java_enum",
    "parse_check_clause",
    "parse_comment",
    "parse_inline_enum",
    "sha256_hex",
    "suspect_enum_column",
]
