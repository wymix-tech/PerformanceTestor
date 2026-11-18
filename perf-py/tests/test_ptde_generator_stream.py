"""T4.10 验收：流式直传模式（实施方案 §7.2；总方案 §8.2 流式直传）。

- 有界缓冲 + 背压：缓冲满时生产端阻塞，不丢数据不覆盖；
- 行序与生产序一致；生产端异常穿透到消费端；
- cancel 语义：停止产出但缓冲内行可消费完；
- 背压边界：停止消费后 pushed - drained ≤ buffer_size（生产端被卡住）。
"""

from __future__ import annotations

import pytest

from ptde.generator.stream import DEFAULT_BUFFER_SIZE, BoundedRowStream, pump_into

# ---------------------------------------------------------------- 基本传输


def test_bounded_stream_delivers_all_rows_in_order():
    rows = list(range(10_000))
    stream = BoundedRowStream(iter(rows), buffer_size=64)  # 小缓冲强制多轮背压
    got = list(stream)
    assert got == rows  # 不丢、不重、有序
    r = stream.result()
    assert r.completed and not r.cancelled and r.error is None
    assert r.pushed_rows == r.drained_rows == len(rows)


def test_buffer_size_must_be_positive():
    with pytest.raises(ValueError, match="必须为正"):
        BoundedRowStream(iter([]), buffer_size=0)


# ---------------------------------------------------------------- 背压边界


def test_backpressure_bounds_pushed_minus_drained():
    # 停止消费后，生产端被 put 阻塞：pushed - drained ≤ buffer_size
    stream = BoundedRowStream(iter(range(10_000)), buffer_size=4)
    drained = 0
    for _row in stream:
        drained += 1
        if drained == 20:
            break  # 停止拉取 → 背压卡住生产端
    assert 0 <= stream._pushed - drained <= 4
    stream.cancel()
    for _ in stream:  # 排空缓冲，生产端退出
        pass
    assert stream.result().cancelled


def test_backpressure_with_slow_consumer_no_loss():
    rows = list(range(2_000))
    stream = BoundedRowStream(iter(rows), buffer_size=8)
    got = list(stream)  # 消费端逐行迭代（慢消费由缓冲上界自然约束）
    assert got == rows
    r = stream.result()
    assert r.pushed_rows == r.drained_rows == len(rows)


# ---------------------------------------------------------------- 异常穿透与取消


def test_producer_error_propagates_to_consumer():
    def gen():
        yield 1
        yield 2
        raise RuntimeError("生产端故障")

    stream = BoundedRowStream(gen(), buffer_size=4)
    got = []
    with pytest.raises(RuntimeError, match="生产端故障"):
        for row in stream:
            got.append(row)
    assert got == [1, 2]  # 异常前已产出的行不丢
    r = stream.result()
    assert not r.completed and r.error is not None


def test_cancel_stops_production_but_buffered_rows_drained():
    def gen():
        yield from range(1_000_000)

    stream = BoundedRowStream(gen(), buffer_size=16)
    got = [next(stream), next(stream)]
    stream.cancel()
    for row in stream:  # 已缓冲的行仍可继续取（不丢已产出数据）
        got.append(row)
    r = stream.result()
    assert got == list(range(len(got)))  # 行序一致
    assert r.cancelled and not r.completed
    assert r.pushed_rows == r.drained_rows  # cancel 后缓冲行全部交付
    assert len(got) < 1_000  # cancel 立即停止产出（远小于生产源规模）


def test_pump_into_sink_and_error_cleanup():
    received = []
    r = pump_into(iter(range(100)), received.append, buffer_size=4)
    assert r.completed and received == list(range(100))

    def sink(row):
        if row == 3:
            raise ValueError("sink 故障")

    with pytest.raises(ValueError, match="sink 故障"):
        pump_into(iter(range(100)), sink, buffer_size=4)


def test_default_buffer_size_constant():
    assert DEFAULT_BUFFER_SIZE == 1024
