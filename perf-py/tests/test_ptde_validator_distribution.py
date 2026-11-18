"""T6.3 分布验证测试（实施方案 §9.2 T6.3 / §9.5 门禁 1）。

分布参数形态对齐 R4 采样器（weighted_categorical values/weights 与
{bucket: ratio} 两形态；精确配额 source=explicit 直查目标数量）。
marker 路由：具体 marker 优先（first-match），参数级匹配区分取值。
"""

from __future__ import annotations

import pytest

from ptde.models.dataset import ValidationStatus
from ptde.models.profile import ColumnProfile, DistributionFit
from ptde.models.spec import DistributionPlanEntry
from ptde.validator.distribution_validator import (
    DistributionTolerance,
    JointBucket,
    JointDistributionCheck,
    TimeBucket,
    TimeBucketCheck,
    _categorical_params,
    validate_distributions,
)


class FakeCursor:
    def __init__(self, routes, executed):
        self._routes = routes
        self._executed = executed
        self._rows: list[tuple] = []

    def execute(self, sql, params=None):
        self._executed.append((sql, list(params or [])))
        for marker, rows, rparams in self._routes:
            if marker in sql and (rparams is None or rparams == list(params or [])):
                self._rows = [tuple(r) for r in rows]
                return
        raise AssertionError(f"未路由 SQL: {sql!r} params={list(params or [])}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class FakeConn:
    def __init__(self, routes):
        self.routes = routes
        self.executed: list[tuple] = []

    def cursor(self):
        return FakeCursor(self.routes, self.executed)


def _entry(entity, field, dist, source="estimated"):
    return DistributionPlanEntry(
        entity=entity, field=field, distribution=dist, source=source)


def test_categorical_statistical_within_tolerance_warns():
    """概率分布用统计容差：容差内偏差 WARN（可降级），不伪装 PASS。"""
    dist = DistributionFit(type="weighted_categorical",
                           params={"values": ["A", "B"], "weights": [0.5, 0.5]})
    conn = FakeConn([
        ("WHERE status = %s", [(520,)], ["A"]),
        ("WHERE status = %s", [(480,)], ["B"]),
        ("FROM orders", [(1000,)], None),
    ])
    layer = validate_distributions(
        conn, [_entry("orders", "status", dist)],
        tolerance=DistributionTolerance(relative=0.05))
    assert layer.status is ValidationStatus.WARN
    assert any("占比目标 0.5，实测 0.520000" in c.detail for c in layer.checks)


def test_categorical_statistical_exact_match_passes():
    dist = DistributionFit(type="weighted_categorical",
                           params={"values": ["A", "B"], "weights": [0.5, 0.5]})
    conn = FakeConn([
        ("WHERE status = %s", [(500,)], ["A"]),
        ("WHERE status = %s", [(500,)], ["B"]),
        ("FROM orders", [(1000,)], None),
    ])
    layer = validate_distributions(conn, [_entry("orders", "status", dist)])
    assert layer.status is ValidationStatus.PASS


def test_categorical_statistical_beyond_tolerance_fails():
    dist = DistributionFit(type="weighted_categorical",
                           params={"values": ["A"], "weights": [0.5]})
    conn = FakeConn([
        ("WHERE status = %s", [(800,)], ["A"]),
        ("FROM orders", [(1000,)], None),
    ])
    layer = validate_distributions(
        conn, [_entry("orders", "status", dist)],
        tolerance=DistributionTolerance(relative=0.05))
    assert layer.status is ValidationStatus.FAIL


def test_categorical_exact_quota_direct_count():
    """精确配额（source=explicit）直查目标数量（T6.3 原文）。"""
    dist = DistributionFit(type="weighted_categorical",
                           params={"values": ["PAID", "PENDING"], "weights": [0.6, 0.4]})
    conn = FakeConn([
        ("WHERE status = %s", [(600,)], ["PAID"]),
        ("WHERE status = %s", [(400,)], ["PENDING"]),
        ("NOT IN", [(0,)], None),
        ("FROM orders", [(1000,)], None),
    ])
    layer = validate_distributions(
        conn, [_entry("orders", "status", dist, source="explicit")],
        exact_quota_totals={"orders": 1000})
    assert layer.status is ValidationStatus.PASS
    assert any("精确配额核对：600/1000" in c.detail for c in layer.checks)
    assert layer.metrics["quota_mode_entries"] == 1


def test_categorical_exact_quota_mismatch_fails():
    dist = DistributionFit(type="weighted_categorical",
                           params={"values": ["PAID"], "weights": [1.0]})
    conn = FakeConn([
        ("WHERE status = %s", [(599,)], ["PAID"]),
        ("NOT IN", [(401,)], None),
        ("FROM orders", [(1000,)], None),
    ])
    layer = validate_distributions(
        conn, [_entry("orders", "status", dist, source="explicit")],
        exact_quota_totals={"orders": 1000})
    assert layer.status is ValidationStatus.FAIL
    assert any("直查目标数量" in c.detail for c in layer.checks)


def test_categorical_exact_quota_extra_values_fail():
    """配额外取值（漏项/多桶）显式失败。"""
    dist = DistributionFit(type="weighted_categorical",
                           params={"values": ["PAID"], "weights": [1.0]})
    conn = FakeConn([
        ("WHERE status = %s", [(999,)], ["PAID"]),
        ("NOT IN", [(1,)], None),
        ("FROM orders", [(1000,)], None),
    ])
    layer = validate_distributions(
        conn, [_entry("orders", "status", dist, source="explicit")])
    assert layer.status is ValidationStatus.FAIL
    assert any("配额外取值" in c.detail for c in layer.checks)


def test_categorical_dict_param_form():
    """{bucket: ratio} 字典形态（B2 collector 产出）。"""
    dist = DistributionFit(type="weighted_categorical",
                           params={"gold": 0.3, "silver": 0.7})
    conn = FakeConn([
        ("WHERE tier = %s", [(30,)], ["gold"]),
        ("WHERE tier = %s", [(70,)], ["silver"]),
        ("FROM users", [(100,)], None),
    ])
    layer = validate_distributions(conn, [_entry("users", "tier", dist)])
    assert layer.status is ValidationStatus.PASS


def test_categorical_length_mismatch_raises():
    dist = DistributionFit(type="weighted_categorical",
                           params={"values": ["A", "B"], "weights": [1.0]})
    with pytest.raises(ValueError):
        _categorical_params(dist)


def test_hotspot_with_target():
    dist = DistributionFit(type="zipf", params={"exponent": 1.2, "top_ratio": 0.4})
    conn = FakeConn([
        ("SELECT MAX(c)", [(400,)], None),
        ("IS NOT NULL", [(1000,)], None),
    ])
    layer = validate_distributions(conn, [_entry("users", "city_id", dist)])
    assert layer.status is ValidationStatus.PASS
    assert "Top-1 占比目标 0.4" in layer.checks[0].detail


def test_hotspot_missing_target_warns_not_fakes():
    """无目标值 → WARN 观测留痕，不伪装 PASS。"""
    dist = DistributionFit(type="skewed", params={"exponent": 1.2})
    conn = FakeConn([
        ("SELECT MAX(c)", [(400,)], None),
        ("IS NOT NULL", [(1000,)], None),
    ])
    layer = validate_distributions(conn, [_entry("users", "city_id", dist)])
    assert layer.status is ValidationStatus.WARN
    assert "不伪装 PASS" in layer.checks[0].detail


def test_normal_mean_sigma_checks():
    dist = DistributionFit(type="normal", params={"mean": 100.0, "sigma": 10.0})
    conn = FakeConn([("AVG", [(100.0, 10.0, None, None)], None)])
    layer = validate_distributions(conn, [_entry("orders", "amount", dist)])
    assert layer.status is ValidationStatus.PASS


def test_normal_deviation_fails():
    dist = DistributionFit(type="normal", params={"mean": 100.0, "sigma": 10.0})
    conn = FakeConn([("AVG", [(150.0, 50.0, None, None)], None)])
    layer = validate_distributions(conn, [_entry("orders", "amount", dist)])
    assert layer.status is ValidationStatus.FAIL


def test_lognormal_theoretical_mean():
    import math
    dist = DistributionFit(type="lognormal", params={"mu": 0.0, "sigma": 0.5})
    expected = math.exp(0.0 + 0.5**2 / 2)
    conn = FakeConn([("AVG", [(expected, None, None, None)], None)])
    layer = validate_distributions(conn, [_entry("orders", "amount", dist)])
    assert layer.status is ValidationStatus.PASS


def test_uniform_range_check():
    dist = DistributionFit(type="uniform", params={"min": 0.0, "max": 100.0})
    conn = FakeConn([("AVG", [(None, None, 0.0, 100.0)], None)])
    layer = validate_distributions(conn, [_entry("orders", "score", dist)])
    assert layer.status is ValidationStatus.PASS


def test_unknown_type_warns_not_fakes():
    dist = DistributionFit(type="ipv4", params={})
    conn = FakeConn([])
    layer = validate_distributions(conn, [_entry("users", "ip", dist)])
    assert layer.status is ValidationStatus.WARN
    assert "不伪装 PASS" in layer.checks[0].detail


def test_sequential_ok_with_note():
    dist = DistributionFit(type="sequential", params={})
    conn = FakeConn([])
    layer = validate_distributions(conn, [_entry("users", "id", dist)])
    assert layer.status is ValidationStatus.PASS
    assert "不适用统计核对" in layer.checks[0].detail


def test_temporal_without_buckets_warns():
    dist = DistributionFit(type="temporal", params={})
    conn = FakeConn([])
    layer = validate_distributions(conn, [_entry("orders", "created_at", dist)])
    assert layer.status is ValidationStatus.WARN
    assert "time_bucket_checks" in layer.checks[0].detail


def test_null_ratio_entry_directs_to_profile_checks():
    dist = DistributionFit(type="null_ratio", params={"ratio": 0.1})
    conn = FakeConn([])
    layer = validate_distributions(conn, [_entry("orders", "note", dist)])
    assert layer.status is ValidationStatus.WARN
    assert "profile_checks" in layer.checks[0].detail


def test_entry_without_distribution_warns():
    conn = FakeConn([])
    layer = validate_distributions(conn, [
        DistributionPlanEntry(entity="orders", field="note", distribution=None)])
    assert layer.status is ValidationStatus.WARN


def test_profile_null_ratio_and_distinct():
    profile = ColumnProfile(column="status", null_ratio=0.1, distinct_count=3)
    conn = FakeConn([("FROM orders", [(1000, 100, 3)], None)])
    layer = validate_distributions(
        conn, [], profile_checks={"orders.status": profile})
    assert layer.status is ValidationStatus.PASS
    assert any(c.name.endswith("[null_ratio]") for c in layer.checks)


def test_profile_null_ratio_deviation_fails():
    profile = ColumnProfile(column="status", null_ratio=0.5)
    conn = FakeConn([("FROM orders", [(1000, 100, 3)], None)])
    layer = validate_distributions(
        conn, [], profile_checks={"orders.status": profile})
    assert layer.status is ValidationStatus.FAIL


def test_joint_distribution_exact_pass():
    check = JointDistributionCheck(
        table="orders", columns=["region", "status"],
        buckets=[JointBucket(values=["east", "PAID"], count=100)])
    conn = FakeConn([("GROUP BY", [("east", "PAID", 100)], None)])
    layer = validate_distributions(conn, [], joint_checks=[check])
    assert layer.status is ValidationStatus.PASS


def test_joint_distribution_missing_bucket_fails():
    """漏桶显式失败（不遗漏锚点）。"""
    check = JointDistributionCheck(
        table="orders", columns=["region"],
        buckets=[JointBucket(values=["west"], count=50)])
    conn = FakeConn([("GROUP BY", [("east", 100)], None)])
    layer = validate_distributions(conn, [], joint_checks=[check])
    assert layer.status is ValidationStatus.FAIL
    assert any("桶缺失" in c.detail for c in layer.checks)


def test_joint_distribution_extra_bucket_fails():
    """多桶（未声明组合）显式失败。"""
    check = JointDistributionCheck(
        table="orders", columns=["region"],
        buckets=[JointBucket(values=["east"], count=100)])
    conn = FakeConn([
        ("GROUP BY", [("east", 100), ("west", 5)], None)])
    layer = validate_distributions(conn, [], joint_checks=[check])
    assert layer.status is ValidationStatus.FAIL
    assert any("未声明组合" in c.detail for c in layer.checks)


def test_joint_distribution_statistical_tolerance():
    check = JointDistributionCheck(
        table="orders", columns=["region"], exact=False,
        buckets=[JointBucket(values=["east"], count=100)])
    conn = FakeConn([("GROUP BY", [("east", 105)], None)])
    layer = validate_distributions(
        conn, [], joint_checks=[check],
        tolerance=DistributionTolerance(relative=0.1))
    assert layer.status is ValidationStatus.PASS


def test_time_bucket_exact_pass_and_fail():
    check = TimeBucketCheck(table="orders", column="created_at", buckets=[
        TimeBucket(label="d1", start="2026-01-01", end="2026-01-02", expected=10),
    ])
    conn = FakeConn([("WHERE created_at", [(10,)], None)])
    layer = validate_distributions(conn, [], time_bucket_checks=[check])
    assert layer.status is ValidationStatus.PASS
    check_bad = TimeBucketCheck(table="orders", column="created_at", buckets=[
        TimeBucket(label="d1", start="2026-01-01", end="2026-01-02", expected=99),
    ])
    layer2 = validate_distributions(conn, [], time_bucket_checks=[check_bad])
    assert layer2.status is ValidationStatus.FAIL


def test_sorted_deterministic_output():
    dist = DistributionFit(type="sequential", params={})
    conn = FakeConn([])
    layer = validate_distributions(conn, [
        _entry("users", "zz", dist), _entry("users", "aa", dist)])
    names = [c.name for c in layer.checks]
    assert names == sorted(names)
