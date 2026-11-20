"""时序表游标分页工具：对齐 Java CursorCodec / CursorPageRequest / CursorPage。

游标编码：Base64Url(f"{epoch_ms}|{id}")（去尾部 = 填充）；
分页语义：LIMIT size+1 探测 hasMore，nextCursor 取截断后最后一条（而非多取那条）。
时间参数兼容 epoch 毫秒（前端 Date.now()）与 ISO-8601 字符串。
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import datetime
from typing import Any

DEFAULT_SIZE = 50
MAX_SIZE = 500


def clamp_size(size: int | None, limit: int | None = None) -> int:
    """未传 size 时回退旧参数 limit，约束在 [1, 500]。"""
    raw = size if size is not None else (limit if limit is not None else DEFAULT_SIZE)
    return max(1, min(MAX_SIZE, int(raw)))


def parse_time(value: str | int | float | None) -> datetime | None:
    """兼容前端 epoch 毫秒（int/数字串）与 ISO-8601 字符串。"""
    if value is None or str(value).strip() == "":
        return None
    s = str(value).strip()
    if s.isdigit():
        return datetime.fromtimestamp(int(s) / 1000.0)
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)


def encode_cursor(ts: datetime, row_id: int) -> str:
    raw = f"{int(ts.timestamp() * 1000)}|{row_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[datetime, int] | None:
    if not cursor or not cursor.strip():
        return None
    padded = cursor.strip() + "=" * (-len(cursor.strip()) % 4)
    try:
        text = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        ms_str, id_str = text.split("|", 1)
        return datetime.fromtimestamp(int(ms_str) / 1000.0), int(id_str)
    except Exception as exc:  # noqa: BLE001 — 对齐 Java：坏游标统一 400/错误包装
        raise ValueError(f"非法游标格式: {cursor}") from exc


def build_cursor_page(
    rows: list[Any],
    size: int,
    time_getter: Callable[[Any], datetime],
    id_getter: Callable[[Any], int],
    serializer: Callable[[Any], dict],
) -> dict:
    """基于 size+1 查询结果组装 CursorPage（items/nextCursor/hasMore）。"""
    has_more = len(rows) > size
    page_rows = rows[:size]
    next_cursor = (
        encode_cursor(time_getter(page_rows[-1]), id_getter(page_rows[-1]))
        if has_more and page_rows
        else None
    )
    return {
        "items": [serializer(r) for r in page_rows],
        "nextCursor": next_cursor,
        "hasMore": has_more,
    }
