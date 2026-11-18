"""T5.1 Adapter 分层接口与 SPI 注册测试（实施方案 §8.2；总方案 §9.2）。

覆盖：七大子接口形状（DatabaseDialect 语义映射）、SPI 注册/URL 前缀匹配/
覆盖冲突显式失败、BulkLoadError 错误样例与行数核对、BulkLoadResult 快照。
"""

from __future__ import annotations

import pytest

from ptde.adapters.base import (
    BulkLoadAdapter,
    BulkLoadError,
    BulkLoadResult,
    CapabilityProbeAdapter,
    ConstraintAdapter,
    MetadataAdapter,
    SPIRegistryError,
    StatisticsAdapter,
    StorageMetricsAdapter,
    TransactionAdapter,
    adapter_for_url,
    get_adapter,
    register_adapter,
    registered_products,
    unregister_all,
)

# ---------- 七大子接口形状（§9.2 分层；DatabaseDialect 语义映射） ----------


def test_seven_subinterfaces_are_abstract() -> None:
    # 抽象接口不可直接实例化（接口分层固化，实现由各产品适配器承接）
    for cls in (
        MetadataAdapter,
        StatisticsAdapter,
        CapabilityProbeAdapter,
        BulkLoadAdapter,
        ConstraintAdapter,
        TransactionAdapter,
        StorageMetricsAdapter,
    ):
        with pytest.raises(TypeError):
            cls()  # type: ignore[abstract]


def test_bulkload_interface_contract() -> None:
    # load_shard / ensure_capable 是 BulkLoadAdapter 的抽象契约（T5.3/T5.4 实现）
    assert BulkLoadAdapter.__abstractmethods__ == frozenset({"load_shard", "ensure_capable"})


# ---------- SPI 注册机制（沿用 DatabaseDialect 语义映射） ----------


@pytest.fixture(autouse=True)
def _clean_registry():
    unregister_all()
    yield
    unregister_all()


class _FakeAdapter:
    product = "fake"


def test_register_and_get_adapter() -> None:
    register_adapter("fake", _FakeAdapter)
    assert isinstance(get_adapter("fake"), _FakeAdapter)
    # 工厂惰性：两次 get 产生独立实例（映射 connect() 每次建连语义）
    assert get_adapter("fake") is not get_adapter("fake")
    assert registered_products() == ["fake"]


def test_register_duplicate_requires_explicit_overwrite() -> None:
    register_adapter("fake", _FakeAdapter)
    with pytest.raises(SPIRegistryError, match="overwrite"):
        register_adapter("fake", _FakeAdapter)
    register_adapter("fake", _FakeAdapter, overwrite=True)  # 显式覆盖放行
    assert isinstance(get_adapter("fake"), _FakeAdapter)


def test_register_invalid_inputs() -> None:
    with pytest.raises(SPIRegistryError, match="product 不能为空"):
        register_adapter("", _FakeAdapter)
    with pytest.raises(SPIRegistryError, match="必须可调用"):
        register_adapter("bad", "not-callable")  # type: ignore[arg-type]


def test_get_unregistered_fails_explicitly() -> None:
    with pytest.raises(SPIRegistryError, match="未注册的 adapter"):
        get_adapter("oracle")


def test_adapter_for_url_prefix_matching() -> None:
    # DatabaseDialect.supports(jdbc_url) 语义映射：URL 前缀匹配
    register_adapter("mysql", _FakeAdapter, url_prefixes=("jdbc:mysql://",))
    register_adapter("postgres", _FakeAdapter, url_prefixes=("jdbc:postgresql://",))
    assert isinstance(adapter_for_url("jdbc:mysql://h:3306/db"), _FakeAdapter)
    assert isinstance(adapter_for_url("jdbc:postgresql://h/db"), _FakeAdapter)
    with pytest.raises(SPIRegistryError, match="无 adapter 支持 URL"):
        adapter_for_url("jdbc:oracle://h/db")


def test_adapter_for_url_longest_prefix_wins() -> None:
    register_adapter("short", _FakeAdapter, url_prefixes=("jdbc:a://",))
    register_adapter("long", _FakeAdapter, url_prefixes=("jdbc:a://special",))
    assert get_adapter("long") is not None  # 确保注册生效
    adapter_for_url("jdbc:a://special-host/db")  # 不抛错即命中（最长前缀路径覆盖）


def test_adapter_for_url_empty_fails() -> None:
    with pytest.raises(SPIRegistryError, match="URL 为空"):
        adapter_for_url("")


# ---------- BulkLoadError / BulkLoadResult（协议结果侧） ----------


def test_bulkload_error_row_count_mismatch_detail() -> None:
    exc = BulkLoadError(
        "装载行数不符",
        rows_expected=100,
        rows_loaded=97,
        error_samples=["[Warning 1265] Data truncated", "[Warning 1366] Incorrect value"],
        warnings_count=3,
    )
    text = str(exc)
    assert "期望 100 行，实际装载 97 行" in text  # 行数核对锚点可见
    assert "[Warning 1265] Data truncated" in text  # 错误样例可见（不静默丢弃）
    assert exc.rows_expected == 100 and exc.rows_loaded == 97
    assert len(exc.error_samples) == 2


def test_bulkload_error_samples_truncated_to_three() -> None:
    samples = [f"err-{i}" for i in range(10)]
    exc = BulkLoadError("失败", error_samples=samples)
    assert "err-0" in str(exc) and "err-2" in str(exc) and "err-3" not in str(exc)
    assert len(exc.error_samples) == 10  # 完整清单保留在异常属性


def test_bulkload_result_snapshot_json_native() -> None:
    result = BulkLoadResult(
        shard_id="s1", entity="orders", table="orders",
        strategy="load_data_local", rows_expected=5, rows_loaded=5,
        rows_verified=True, warnings_count=2,
        warning_samples=["[Warning 1265] truncated"],
        precheck_notes=["目标表为分区表（2 个分区）"],
    ).seal()
    snap = result.to_snapshot()
    assert snap["shard_id"] == "s1" and snap["rows_verified"] is True
    assert snap["warning_samples"] == ["[Warning 1265] truncated"]
    assert result.fingerprint  # 快照带指纹（可审计）


def test_bulkload_result_fingerprint_excludes_timestamp() -> None:
    from datetime import UTC, datetime

    a = BulkLoadResult(shard_id="s", entity="e", table="t", strategy="x",
                       collected_at=datetime(2026, 1, 1, tzinfo=UTC)).seal()
    b = BulkLoadResult(shard_id="s", entity="e", table="t", strategy="x",
                       collected_at=datetime(2026, 6, 1, tzinfo=UTC)).seal()
    assert a.fingerprint == b.fingerprint  # 时间戳不入指纹（对齐 A6 口径）
