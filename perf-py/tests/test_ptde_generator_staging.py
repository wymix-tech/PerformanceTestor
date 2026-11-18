"""T4.9 验收：文件中转模式（实施方案 §7.4 格式要素全集 + §8.2 检查点协议）。

- 文件格式要素：UTF-8 头声明 / RFC4180 转义 / NULL 显式标记与空串区分 /
  Decimal 字符串化保精度 / datetime UTC ISO / bytes Base64 / sha256+行数校验和；
- StagingArea：状态机 pending→generated→loaded、manifest 原子落盘、
  断点恢复、完整性校验显式失败。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ptde.generator.file_format import (
    FORMAT_VERSION,
    NULL_TOKEN,
    ShardFileMeta,
    format_value,
    read_shard_file,
    serialize_row,
    verify_shard_file,
    write_shard_file,
)
from ptde.generator.staging import ShardStatus, StagingArea

# ---------------------------------------------------------------- 单值格式化（§7.4）


def test_format_value_null_vs_empty_string():
    assert format_value(None) == NULL_TOKEN == "\\N"
    assert format_value("") == ""  # 空串与 NULL 显式区分


def test_format_value_decimal_precision_preserved():
    # Decimal 字符串化保精度（禁止 float 往返）
    assert format_value(Decimal("0.10000000001")) == "0.10000000001"
    assert format_value(Decimal("10.00")) == "10.00"


def test_format_value_datetime_utc_iso_and_bool_and_bytes():
    dt = datetime(2026, 11, 18, 8, 30, 0, tzinfo=UTC)
    assert format_value(dt) == "2026-11-18T08:30:00+00:00"
    assert format_value(True) == "1" and format_value(False) == "0"
    assert format_value(b"ab") == "b64:YWI="


def test_serialize_row_rfc4180_quoting():
    line = serialize_row(["plain", "with,comma", 'with"quote', "with\nnewline"])
    parsed = next(iter(_rows_of(line)))
    assert parsed == ["plain", "with,comma", 'with"quote', "with\nnewline"]


def _rows_of(text: str):
    import csv
    import io

    yield from csv.reader(io.StringIO(text))


# ---------------------------------------------------------------- 分片文件读写与校验和


def test_write_read_roundtrip_with_meta_header(tmp_path):
    header = ["id", "name", "amount", "created_at", "note"]
    rows = [
        [1, "alice", Decimal("12.34"), datetime(2026, 1, 2, 3, 4, 5), None],
        [2, "b,ob", None, None, ""],
    ]
    path = tmp_path / "shards" / "orders-0000.csv"
    meta = write_shard_file(path, header, rows)
    assert meta.rows == 2 and meta.format_version == FORMAT_VERSION
    verify_shard_file(path, meta)  # 行数 + sha256 复核通过

    records = list(read_shard_file(path))
    assert records[0][0] == header
    r1 = records[0][1]
    assert r1[0] == "1"  # CSV 无类型，类型由列定义解释
    assert r1[2] == "12.34"
    assert r1[4] is None  # NULL 显式还原
    assert records[1][1][2] is None and records[1][1][4] == ""  # NULL/空串区分


def test_verify_detects_tampering_and_row_mismatch(tmp_path):
    path = tmp_path / "shard.csv"
    meta = write_shard_file(path, ["id"], [[1], [2]])
    with pytest.raises(ValueError, match="行数不符"):
        verify_shard_file(path, ShardFileMeta(path=str(path), rows=3,
                                              checksum=meta.checksum))
    with pytest.raises(ValueError, match="sha256 不符"):
        verify_shard_file(path, ShardFileMeta(path=str(path), rows=2,
                                              checksum="0" * 64))
    path2 = tmp_path / "missing.csv"
    with pytest.raises(FileNotFoundError):
        verify_shard_file(path2, meta)


def test_multiline_quoted_field_row_count_correct(tmp_path):
    # RFC 4180 引号字段内嵌换行：行计数按 csv 记录数（按物理行会算错）
    path = tmp_path / "shard.csv"
    meta = write_shard_file(path, ["id", "text"], [[1, "a\nb"], [2, "c"]])
    assert meta.rows == 2
    verify_shard_file(path, meta)


def test_write_rejects_invalid_delimiter(tmp_path):
    with pytest.raises(ValueError, match="单字符"):
        write_shard_file(tmp_path / "x.csv", ["a"], [], delimiter=",; ")


# ---------------------------------------------------------------- StagingArea 状态机与检查点


def test_staging_state_machine_and_totals(tmp_path):
    area = StagingArea(tmp_path / "staging", plan_fingerprint="fp-1")
    area.register("orders", "orders-0000")
    area.register("orders", "orders-0001")
    path = area.shard_path("orders", "orders-0000")
    meta = write_shard_file(path, ["id"], [[1], [2]])
    area.mark_generated("orders-0000", meta)
    assert area.totals() == {
        "shards": 2, "pending": 1, "generated": 1, "loaded": 0,
        "rows_generated": 2, "rows_loaded": 0,
    }
    with pytest.raises(ValueError, match="生成行数"):
        area.mark_loaded("orders-0000", 5)  # 数量复核锚点
    area.mark_loaded("orders-0000", 2)
    assert area.pending() == [area.records()[1]]  # 断点续跑只领 pending


def test_staging_state_machine_explicit_failures(tmp_path):
    area = StagingArea(tmp_path / "staging")
    with pytest.raises(ValueError, match="重复登记"):
        area.register("orders", "s1")
        area.register("orders", "s1")
    with pytest.raises(ValueError, match="未登记"):
        area.mark_generated("ghost", ShardFileMeta(path="p", rows=0, checksum="x"))
    area.register("orders", "s2")
    with pytest.raises(ValueError, match="状态机"):
        area.mark_loaded("s2", 0)  # pending 不能直接 loaded


def test_staging_checkpoint_atomic_and_restore(tmp_path):
    area = StagingArea(tmp_path / "staging", plan_fingerprint="fp-1")
    area.register("orders", "orders-0000")
    meta = write_shard_file(area.shard_path("orders", "orders-0000"), ["id"], [[1]])
    area.mark_generated("orders-0000", meta)
    payload = area.checkpoint()
    assert payload["manifest_version"] == 1
    assert (tmp_path / "staging" / "manifest.json").exists()
    # 无 .tmp 残留（原子替换）
    assert not list((tmp_path / "staging").glob("*.tmp"))

    restored = StagingArea(tmp_path / "staging").restore()
    assert restored.records()[0].status == ShardStatus.GENERATED.value
    assert restored.records()[0].rows == 1
    with pytest.raises(FileNotFoundError):
        StagingArea(tmp_path / "nowhere").restore()


def test_staging_verify_complete_explicit_failure(tmp_path):
    area = StagingArea(tmp_path / "staging")
    with pytest.raises(ValueError, match="无任何分片"):
        area.verify_complete()
    area.register("orders", "s1")
    with pytest.raises(ValueError, match="未装载分片"):
        area.verify_complete()
    meta = write_shard_file(area.shard_path("orders", "s1"), ["id"], [[1]])
    area.mark_generated("s1", meta)
    area.mark_loaded("s1", 1)
    totals = area.verify_complete()
    assert totals == {"shards": 1, "pending": 0, "generated": 0, "loaded": 1,
                      "rows_generated": 1, "rows_loaded": 1}
