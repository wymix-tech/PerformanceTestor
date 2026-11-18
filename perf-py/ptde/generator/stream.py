"""流式直传模式：有界缓冲 + 背压（R4 T4.10，实施方案 §7.2；总方案 §8.2）。

流式直传：Generator 通过有界缓冲向 Loader 提供数据——适合中等规模、
临时数据集和中间存储空间有限的场景。与文件中转模式（staging.py）共用：
- 分片元数据（worker.ShardTask，协议 §7.5 形态）；
- 格式定义（file_format.FORMAT_VERSION，行序列化一致）；
- 检查点协议（ShardSummary.status / staging 记录语义）。

背压语义（勿另起一套）：
- 缓冲满时生产端阻塞（put 背压），**永不丢弃、永不覆盖**数据；
- 消费端按序逐行拉取，行序与生产序一致；
- cancel() 语义：生产端在下次 put 前停止产出，缓冲中已有行由消费端
  继续取完（已产出的行不丢），流随后关闭；
- 生产端异常在消费端重新抛出（错误穿透，不静默吞）。
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

DEFAULT_BUFFER_SIZE = 1024
_STREAM_CLOSED = object()  # 哨兵：流结束（不占用行值域）


@dataclass
class StreamResult:
    """流执行结果（行数复核锚点：pushed ≥ drained，cancel 时可能更大）。"""

    pushed_rows: int
    drained_rows: int
    completed: bool
    cancelled: bool = False
    error: str | None = None


class BoundedRowStream:
    """有界行流：生产端线程 + 有界队列 + 消费端迭代（背压不丢数据）。"""

    def __init__(self, rows: Iterable[Any], buffer_size: int = DEFAULT_BUFFER_SIZE) -> None:
        if buffer_size <= 0:
            raise ValueError(f"buffer_size 必须为正，收到 {buffer_size}（显式失败）")
        self._rows = rows
        self._queue: queue.Queue = queue.Queue(maxsize=buffer_size)
        self._cancel = threading.Event()
        self._pushed = 0
        self._drained = 0
        self._error: BaseException | None = None
        self._completed = False
        self._cancelled = False
        self._thread = threading.Thread(target=self._produce, daemon=True)
        self._thread.start()

    def __iter__(self):
        return self

    def __next__(self):
        item = self._queue.get()  # 阻塞拉取（背压传导到生产端）
        if item is _STREAM_CLOSED:
            if self._error is not None:
                raise self._error  # 生产端异常穿透到消费端（不静默吞）
            raise StopIteration
        self._drained += 1
        return item

    def cancel(self) -> None:
        """请求停止产出：生产端在下次 put 前退出；缓冲内行仍可消费完。"""
        self._cancelled = True
        self._cancel.set()

    def result(self) -> StreamResult:
        """等待生产端线程结束后返回结果（消费完成后调用）。"""
        self._thread.join()
        return StreamResult(
            pushed_rows=self._pushed,
            drained_rows=self._drained,
            completed=self._completed,
            cancelled=self._cancelled,
            error=str(self._error) if self._error is not None else None,
        )

    # -------------------------------------------------------------- 内部

    def _produce(self) -> None:
        try:
            for row in self._rows:
                if self._cancel.is_set():
                    return  # 已入缓冲的行不撤回，消费端可继续取完
                self._queue.put(row)
                self._pushed += 1
            self._completed = True
        except BaseException as exc:  # noqa: BLE001 - 错误穿透到消费端
            self._error = exc
        finally:
            self._queue.put(_STREAM_CLOSED)


def pump_into(rows: Iterable[Any], sink, buffer_size: int = DEFAULT_BUFFER_SIZE) -> StreamResult:
    """便捷通道：有界缓冲把行流推给 sink(row)（Loader 侧逐行消费）。

    sink 异常时先 cancel 并排空缓冲（解除生产端 put 阻塞）再上抛——
    不吞错误、不丢已消费行、不遗留阻塞线程。
    """
    stream = BoundedRowStream(rows, buffer_size)
    try:
        for row in stream:
            sink(row)
    except BaseException:
        stream.cancel()
        for _ in stream:  # 排空缓冲，生产端得以退出
            pass
        raise
    return stream.result()
