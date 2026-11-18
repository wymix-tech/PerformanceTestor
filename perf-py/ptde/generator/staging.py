"""文件中转 Staging 区与检查点协议（R4 T4.9，实施方案 §7.2；总方案 §8.1/§8.2）。

文件中转模式：生成分片文件后由 Loader 装载——适合超大数据、跨机器生成、
数据文件复用与断点恢复。与流式直传（stream.py）共用：
- 分片元数据（ShardTask / ShardFileMeta，协议 §7.5 形态）；
- 格式定义（file_format.FORMAT_VERSION）；
- 检查点协议（manifest.json 原子落盘：tmp + os.replace）。

状态机：pending → generated → loaded（对齐 ShardSummary.status 语义）；
任何不完整/不一致在 verify_complete 显式失败，不静默放行。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ptde.generator.file_format import (
    FORMAT_VERSION,
    ShardFileMeta,
    atomic_write_json,
)

MANIFEST_VERSION = 1  # 协议 §7.5：语言无关，版本变更需 bump
MANIFEST_FILENAME = "manifest.json"


class ShardStatus(StrEnum):
    """分片状态（协议结果侧 status 语义）。"""

    PENDING = "pending"
    GENERATED = "generated"
    LOADED = "loaded"


@dataclass
class ShardRecord:
    """manifest 内单分片记录（JSON 原生字段，language-agnostic）。"""

    shard_id: str
    entity: str
    status: str = ShardStatus.PENDING.value
    rows: int = 0
    loaded_rows: int | None = None
    checksum: str | None = None
    path: str | None = None

    def to_manifest(self) -> dict:
        return {
            "shard_id": self.shard_id,
            "entity": self.entity,
            "status": self.status,
            "rows": self.rows,
            "loaded_rows": self.loaded_rows,
            "checksum": self.checksum,
            "path": self.path,
        }

    @classmethod
    def from_manifest(cls, data: dict) -> ShardRecord:
        return cls(
            shard_id=data["shard_id"],
            entity=data["entity"],
            status=data.get("status", ShardStatus.PENDING.value),
            rows=int(data.get("rows", 0)),
            loaded_rows=data.get("loaded_rows"),
            checksum=data.get("checksum"),
            path=data.get("path"),
        )


class StagingArea:
    """Staging 区管理：分片路径分配 + manifest 检查点（原子写、可恢复）。"""

    def __init__(self, root: str | Path, plan_fingerprint: str | None = None,
                 format_version: str = FORMAT_VERSION) -> None:
        self.root = Path(root)
        self.plan_fingerprint = plan_fingerprint
        self.format_version = format_version
        self._records: dict[str, ShardRecord] = {}

    # -------------------------------------------------------------- 路径与登记

    def shard_path(self, entity: str, shard_id: str) -> Path:
        """分片文件路径：root/entity/shard_id.csv（每实体独立目录）。"""
        return self.root / entity / f"{shard_id}.csv"

    def register(self, entity: str, shard_id: str) -> ShardRecord:
        """登记分片（pending）；重复登记显式失败（分片 ID 全局唯一）。"""
        if shard_id in self._records:
            raise ValueError(f"分片 {shard_id} 重复登记（分片 ID 必须全局唯一，显式失败）")
        record = ShardRecord(shard_id=shard_id, entity=entity)
        self._records[shard_id] = record
        return record

    # -------------------------------------------------------------- 状态迁移

    def mark_generated(self, shard_id: str, meta: ShardFileMeta) -> ShardRecord:
        """pending → generated：绑定文件元数据（行数/校验和/路径）。"""
        record = self._require(shard_id)
        if record.status != ShardStatus.PENDING.value:
            raise ValueError(
                f"分片 {shard_id} 状态为 {record.status}，不能标记 generated（状态机显式失败）"
            )
        record.status = ShardStatus.GENERATED.value
        record.rows = meta.rows
        record.checksum = meta.checksum
        record.path = meta.path
        return record

    def mark_loaded(self, shard_id: str, loaded_rows: int) -> ShardRecord:
        """generated → loaded：装载行数回填（数量复核锚点，不静默吞差异）。"""
        record = self._require(shard_id)
        if record.status != ShardStatus.GENERATED.value:
            raise ValueError(
                f"分片 {shard_id} 状态为 {record.status}，不能标记 loaded（状态机显式失败）"
            )
        if loaded_rows != record.rows:
            raise ValueError(
                f"分片 {shard_id} 装载行数 {loaded_rows} ≠ 生成行数 {record.rows}"
                "（数量复核锚点不一致，显式失败）"
            )
        record.status = ShardStatus.LOADED.value
        record.loaded_rows = loaded_rows
        return record

    # -------------------------------------------------------------- 查询

    def records(self) -> list[ShardRecord]:
        """全部分片记录（按 shard_id 排序，确定性）。"""
        return [self._records[k] for k in sorted(self._records)]

    def pending(self) -> list[ShardRecord]:
        """未生成分片（断点恢复：Worker 只领取 pending）。"""
        return [r for r in self.records() if r.status == ShardStatus.PENDING.value]

    def totals(self) -> dict:
        """汇总：各状态计数 + 生成/装载行数（协议结果侧汇总）。"""
        recs = self.records()
        return {
            "shards": len(recs),
            "pending": sum(1 for r in recs if r.status == ShardStatus.PENDING.value),
            "generated": sum(1 for r in recs if r.status == ShardStatus.GENERATED.value),
            "loaded": sum(1 for r in recs if r.status == ShardStatus.LOADED.value),
            "rows_generated": sum(r.rows for r in recs),
            "rows_loaded": sum(r.loaded_rows or 0 for r in recs),
        }

    # -------------------------------------------------------------- 检查点协议

    def checkpoint(self) -> dict:
        """manifest 原子落盘并返回 payload（协议 §7.5：版本化 JSON）。"""
        payload = {
            "manifest_version": MANIFEST_VERSION,
            "format_version": self.format_version,
            "plan_fingerprint": self.plan_fingerprint,
            "totals": self.totals(),
            "shards": [r.to_manifest() for r in self.records()],
        }
        atomic_write_json(self.root / MANIFEST_FILENAME, payload)
        return payload

    def restore(self) -> StagingArea:
        """从 manifest.json 恢复（断点续跑；文件缺失显式失败）。"""
        import json

        manifest_path = self.root / MANIFEST_FILENAME
        if not manifest_path.exists():
            raise FileNotFoundError(
                f"检查点缺失：{manifest_path}（无 manifest 不可恢复，显式失败）"
            )
        with open(manifest_path, encoding="utf-8") as f:
            payload = json.load(f)
        if payload.get("manifest_version") != MANIFEST_VERSION:
            raise ValueError(
                f"manifest 版本不符：{payload.get('manifest_version')} ≠ "
                f"{MANIFEST_VERSION}（协议变更，显式失败）"
            )
        self.format_version = payload.get("format_version", FORMAT_VERSION)
        self.plan_fingerprint = payload.get("plan_fingerprint")
        self._records = {
            d["shard_id"]: ShardRecord.from_manifest(d)
            for d in payload.get("shards", [])
        }
        return self

    def verify_complete(self) -> dict:
        """完整性校验：全部分片 loaded 且无 pending；返回汇总（不通过即抛错）。"""
        totals = self.totals()
        if totals["pending"] or totals["generated"]:
            unfinished = [
                r.shard_id for r in self.records()
                if r.status != ShardStatus.LOADED.value
            ]
            raise ValueError(
                f"staging 不完整：未装载分片 {unfinished}（断点数据不得静默放行，显式失败）"
            )
        if totals["shards"] == 0:
            raise ValueError("staging 无任何分片登记（显式失败，不得视为完成）")
        return totals

    # -------------------------------------------------------------- 内部

    def _require(self, shard_id: str) -> ShardRecord:
        record = self._records.get(shard_id)
        if record is None:
            raise ValueError(
                f"分片 {shard_id} 未登记（先 register 再标记，显式失败）"
            )
        return record


def default_staging_root(base_dir: str | Path, dataset_id: str) -> Path:
    """约定式 staging 根目录：base/ptde_staging/<dataset_id>。"""
    return Path(base_dir) / "ptde_staging" / dataset_id
