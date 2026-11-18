"""模型基座与共享词汇：VersionedModel、Evidence、DbRef、三态枚举。"""

from __future__ import annotations

import enum

from ptde_common.model_base import (
    MODEL_SCHEMA_VERSION,
    VersionedModel,
)

__all__ = [
    "MODEL_SCHEMA_VERSION",
    "DbRef",
    "Evidence",
    "EvidenceStatus",
    "VersionedModel",
]


class EvidenceStatus(enum.StrEnum):
    """证据三态（A3 分级显式基础）：确认 / 推断 / 未知——不得把推断伪装为事实。"""

    CONFIRMED = "CONFIRMED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


class Evidence(VersionedModel):
    """结构化证据引用：文件路径+行号 或 数据库对象名；不接受纯文本断言。"""

    type: str  # database_foreign_key / source_mapping / ddl / comment / probe ...
    file: str | None = None
    line: int | None = None
    object_name: str | None = None  # SQL/DB 对象名（约束名、索引名等）
    detail: str | None = None
    status: EvidenceStatus = EvidenceStatus.UNKNOWN


class DbRef(VersionedModel):
    """目标数据库引用：产品/版本/兼容模式/引擎（Capability Probe 填充，非猜测）。

    数据库版本字段名为 `db_version`——与基座 VersionedModel.version（模型 schema 版本）
    显式区分，避免同名遮蔽。
    """

    product: str | None = None  # mysql / postgres / ...
    db_version: str | None = None
    compat_mode: str | None = None  # 兼容模式（如 MySQL 对 Oracle 语法兼容层）
    engine: str | None = None  # 存储引擎（InnoDB 等）
