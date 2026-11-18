"""VersionedModel 基座（A6）：version + fingerprint、规范 JSON、sha256。

- `version`：模型 schema 版本（结构变更时 bump）；
- `fingerprint`：`sha256(canonical_json(模型数据))`，计算时排除 `fingerprint` 本身
  与子类声明的易变字段（`exclude_fingerprint_fields`，如 created_at / collected_at）；
- 规范 JSON：键排序 + 紧凑分隔符 + UTF-8 原样（ensure_ascii=False），同内容稳定；
- JSON/YAML 双读统一入口（A6：对外 YAML/JSON 双读，内部 pydantic 单一事实源）。

本模块只依赖 pydantic/标准库，不得依赖数据库驱动或 datagen/ptde 业务模块。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, ClassVar, Self

import yaml
from pydantic import BaseModel, ConfigDict

MODEL_SCHEMA_VERSION = "1"

# 指纹计算时全基座排除的字段（fingerprint 本身 + 时间戳类易变字段）
_BASE_FINGERPRINT_EXCLUDED: frozenset[str] = frozenset({"fingerprint", "created_at"})


def canonical_json(data: Any) -> str:
    """规范 JSON：键排序、紧凑分隔符、非 ASCII 原样——同内容产出同字符串。"""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    """sha256 十六进制摘要（64 位，与 ptde_* 表 CHAR(64) 指纹列对齐）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class VersionedModel(BaseModel):
    """全部 PTDE 模型的基座：版本 + 指纹 + 双格式读写。"""

    model_config = ConfigDict(extra="forbid")

    # 子类声明的不参与指纹的易变字段（时间戳、采集元数据等）
    exclude_fingerprint_fields: ClassVar[frozenset[str]] = frozenset()

    version: str = MODEL_SCHEMA_VERSION
    fingerprint: str | None = None
    created_at: datetime | None = None

    def fingerprint_payload(self) -> dict[str, Any]:
        """参与指纹计算的数据（排除 fingerprint 本身与声明的易变字段）。"""
        data = self.model_dump(mode="json")
        excluded = _BASE_FINGERPRINT_EXCLUDED | type(self).exclude_fingerprint_fields
        for name in excluded:
            data.pop(name, None)
        return data

    def compute_fingerprint(self) -> str:
        """sha256(canonical_json(payload))：同内容稳定；任一字段变更即变。"""
        return sha256_hex(canonical_json(self.fingerprint_payload()))

    def seal(self) -> Self:
        """计算并写入 fingerprint，返回自身（链式用法：m = Model(...).seal()）。"""
        self.fingerprint = self.compute_fingerprint()
        return self

    # ---- A6：JSON/YAML 双读双写统一入口 ----

    @classmethod
    def from_json(cls, text: str) -> Self:
        return cls.model_validate_json(text)

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> Self:
        return cls.model_validate(data)

    @classmethod
    def from_yaml(cls, text: str) -> Self:
        return cls.model_validate(yaml.safe_load(text))

    def to_json(self) -> str:
        return self.model_dump_json()

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.model_dump(mode="json"), allow_unicode=True, sort_keys=False
        )
