"""分片文件格式（R4 T4.9，实施方案 §7.4 设计草案 A；总方案 §8.3 要素全集）。

要素约定（勿另起一套）：
- 字符集：统一 UTF-8，文件头首行声明；
- 分隔符：默认 CSV（RFC 4180 兼容转义），字段分隔符可配置（单字符、非空白）；
- NULL 表达：显式 ``\\N`` 标记，与空串区分；
- 日期时间与时区：统一 UTC 存储（naive 视为 UTC），ISO 格式字符串；
- Decimal：字符串化保精度（禁止 float 往返）；
- 二进制/LOB：Base64（``b64:`` 前缀，文件头声明编码）；
- 校验和：每分片文件 sha256 + 行数（写入侧流式计算，读取侧复核）；
- 格式版本：``format_version`` 写入分片元数据（A2 语言无关协议要求）。

首行元数据声明（META_PREFIX = "#"）；行计数用 csv 记录数（RFC 4180 引号
字段可内嵌换行，按物理行计数会算错）。
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

FORMAT_VERSION = "csv_v1"
FILE_ENCODING = "utf-8"
NULL_TOKEN = "\\N"
DEFAULT_DELIMITER = ","
META_PREFIX = "#"
B64_PREFIX = "b64:"


@dataclass(frozen=True)
class ShardFileMeta:
    """分片文件元数据（语言无关：字段 snake_case，值 JSON 原生——协议 §7.5）。"""

    path: str
    rows: int
    checksum: str  # sha256 十六进制（全文件字节）
    format_version: str = FORMAT_VERSION
    encoding: str = FILE_ENCODING

    def to_manifest(self) -> dict:
        return {
            "path": self.path,
            "rows": self.rows,
            "checksum": self.checksum,
            "format_version": self.format_version,
            "encoding": self.encoding,
        }

    @classmethod
    def from_manifest(cls, data: dict) -> ShardFileMeta:
        return cls(
            path=data["path"],
            rows=int(data["rows"]),
            checksum=data["checksum"],
            format_version=data.get("format_version", FORMAT_VERSION),
            encoding=data.get("encoding", FILE_ENCODING),
        )


def format_value(value) -> str:
    """单值 → 文件字符串表示（Decimal 保精度 / None 显式 NULL / bytes Base64）。"""
    if value is None:
        return NULL_TOKEN
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, Decimal):
        return str(value)  # 字符串化保精度，禁止 float 往返
    if isinstance(value, (datetime, date)):
        return value.isoformat()  # UTC 语义由文件头声明统一
    if isinstance(value, bytes):
        return B64_PREFIX + base64.b64encode(value).decode("ascii")
    if isinstance(value, float):
        return repr(value)  # repr 保 float 往返精度
    return str(value)


def serialize_row(row: Sequence, delimiter: str = DEFAULT_DELIMITER) -> str:
    """单行序列化（RFC 4180 兼容转义，csv 模块；行尾 LF）。"""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n",
                        quoting=csv.QUOTE_MINIMAL)
    writer.writerow([format_value(v) for v in row])
    return buf.getvalue()


def _meta_line(delimiter: str) -> str:
    return (
        f"{META_PREFIX} format_version={FORMAT_VERSION} encoding={FILE_ENCODING} "
        f"null={NULL_TOKEN} delimiter={delimiter} timezone=UTC"
    )


def _parse_meta(first_line: str) -> dict[str, str]:
    if not first_line.startswith(META_PREFIX):
        raise ValueError(
            "分片文件缺少首行格式声明（文件头必须声明 format_version/encoding）"
        )
    meta: dict[str, str] = {}
    for token in first_line.removeprefix(META_PREFIX).split():
        if "=" in token:
            k, v = token.split("=", 1)
            meta[k] = v
    return meta


def _declared_delimiter(meta_tokens: dict[str, str], path) -> str:
    delim = meta_tokens.get("delimiter")
    if not delim or len(delim) != 1 or delim.isspace():
        raise ValueError(
            f"分片文件 {path} 分隔符声明非法：{delim!r}"
            "（必须为单字符且非空白，显式失败）"
        )
    return delim


def write_shard_file(
    path: str | Path,
    header: Sequence[str],
    rows: Iterable[Sequence],
    delimiter: str = DEFAULT_DELIMITER,
) -> ShardFileMeta:
    """写分片文件：首行格式声明 + 列头 + 数据行；流式写 + sha256 流式计算。"""
    if len(delimiter) != 1 or delimiter.isspace():
        raise ValueError(
            f"分隔符必须为单字符且非空白，收到 {delimiter!r}（显式失败）"
        )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    count = 0

    def emit(s: str) -> None:
        hasher.update(s.encode(FILE_ENCODING))
        f.write(s)

    with open(path, "w", encoding=FILE_ENCODING, newline="") as f:
        emit(_meta_line(delimiter) + "\n")
        emit(serialize_row(header, delimiter))
        for row in rows:
            emit(serialize_row(row, delimiter))
            count += 1
    return ShardFileMeta(path=str(path), rows=count, checksum=hasher.hexdigest())


def _parse_value(raw: str):
    if raw == NULL_TOKEN:
        return None
    if raw.startswith(B64_PREFIX):
        return base64.b64decode(raw[len(B64_PREFIX):])
    return raw


def read_shard_file(
    path: str | Path, delimiter: str | None = None
) -> Iterator[tuple[list[str], list]]:
    """流式逐行读取分片文件 → (列头, 行迭代器)；分隔符缺省按文件头声明解析。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"分片文件缺失：{path}（显式失败）")
    with open(path, encoding=FILE_ENCODING, newline="") as f:
        tokens = _parse_meta(f.readline())
        delim = delimiter if delimiter is not None else _declared_delimiter(tokens, path)
        reader = csv.reader(f, delimiter=delim)
        header = next(reader, None)
        if header is None:
            return
        for parsed in reader:
            yield header, [_parse_value(v) for v in parsed]


def verify_shard_file(path: str | Path, meta: ShardFileMeta) -> None:
    """复核分片文件：sha256 + csv 记录行数与元数据一致（不静默吞差异）。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"分片文件缺失：{path}（staging 不完整，显式失败）")
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            hasher.update(chunk)
    with open(path, encoding=FILE_ENCODING, newline="") as f:
        tokens = _parse_meta(f.readline())
        reader = csv.reader(f, delimiter=_declared_delimiter(tokens, path))
        next(reader, None)  # 跳过列头行，只计数据行
        rows = sum(1 for _ in reader)
    if rows != meta.rows:
        raise ValueError(
            f"分片文件 {path} 行数不符：实际 {rows}，元数据 {meta.rows}（显式失败）"
        )
    if hasher.hexdigest() != meta.checksum:
        raise ValueError(
            f"分片文件 {path} sha256 不符（文件可能损坏或被篡改，显式失败）"
        )


def atomic_write_json(path: str | Path, payload: dict) -> None:
    """检查点协议：manifest 原子落盘（tmp + os.replace，断电不留半写）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding=FILE_ENCODING) as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)
