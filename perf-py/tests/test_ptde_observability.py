"""T7.4 可观测性单测：指标全集/聚合快照、日志字段集、脱敏硬约束。

- §16.1 指标名全集外显式拒绝（防口径漂移）；样本容量环形截断；
- §16.2 字段集逐条在位；错误分类映射；
- 脱敏：键名敏感值打码、DSN 内嵌凭据打码——口令/敏感值不入日志。
"""

import json
import logging

import pytest

from ptde.observability import (
    LOG_FIELDS,
    METRIC_NAMES,
    ObservabilityRecorder,
    build_log_record,
    classify_error,
    emit_log_record,
    is_sensitive_key,
    redact_dsn,
    redact_mapping,
)

# ---------------------------------------------------------------- 指标


def test_metric_name_outside_full_set_rejected():
    r = ObservabilityRecorder()
    with pytest.raises(ValueError, match="未知指标"):
        r.record("made_up_metric", 1.0)


def test_record_and_snapshot_aggregates():
    r = ObservabilityRecorder()
    for v in (10, 20, 30):
        r.record("load_throughput", v, unit="rows/s", dataset_id="d1")
    snap = r.snapshot(dataset_id="d1")
    assert snap["load_throughput"]["count"] == 3
    assert snap["load_throughput"]["sum"] == 60
    assert snap["load_throughput"]["min"] == 10
    assert snap["load_throughput"]["max"] == 30
    assert snap["load_throughput"]["last"] == 30
    assert snap["load_throughput"]["unit"] == "rows/s"


def test_snapshot_dataset_filter_isolates():
    r = ObservabilityRecorder()
    r.record("staging_size", 100, dataset_id="d1")
    r.record("staging_size", 200, dataset_id="d2")
    assert r.snapshot(dataset_id="d1")["staging_size"]["sum"] == 100
    assert r.snapshot(dataset_id="d2")["staging_size"]["sum"] == 200
    assert r.snapshot()["staging_size"]["count"] == 2


def test_capacity_ring_truncates_oldest():
    r = ObservabilityRecorder(capacity=3)
    for v in range(5):
        r.record("peak_memory", v)
    samples = r.samples()
    assert len(samples) == 3
    assert [s.value for s in samples] == [2, 3, 4]


def test_capacity_must_be_positive():
    with pytest.raises(ValueError):
        ObservabilityRecorder(capacity=0)


def test_observe_duration_records_elapsed():
    import time

    r = ObservabilityRecorder()
    with r.observe_duration("validation_duration", dataset_id="d1"):
        time.sleep(0.01)
    snap = r.snapshot(dataset_id="d1")
    assert snap["validation_duration"]["count"] == 1
    assert snap["validation_duration"]["unit"] == "s"
    assert snap["validation_duration"]["min"] > 0


# ---------------------------------------------------------------- 日志字段与错误分类


def test_log_fields_full_set_present():
    rec = build_log_record(dataset_id="d1", operation="load", status="ok")
    for name in LOG_FIELDS:
        assert name in rec
    assert rec["dataset_id"] == "d1"
    assert rec["error_class"] is None or rec["error_class"] == ""


def test_log_error_auto_classification():
    rec = build_log_record(operation="load", error=TimeoutError("x"))
    assert rec["error_class"] == "timeout"
    rec2 = build_log_record(operation="load", error=ConnectionError("x"))
    assert rec2["error_class"] == "connect_error"
    assert classify_error(None) == ""


def test_metric_names_full_set_is_section_16_1():
    assert "generation_throughput" in METRIC_NAMES
    assert "load_throughput" in METRIC_NAMES
    assert "estimated_vs_actual_storage" in METRIC_NAMES
    assert "resume_recovery_time" in METRIC_NAMES


# ---------------------------------------------------------------- 脱敏


def test_sensitive_key_detection():
    assert is_sensitive_key("dbPassword")
    assert is_sensitive_key("api_key")
    assert is_sensitive_key("authorization")
    assert is_sensitive_key("target_dsn")
    assert not is_sensitive_key("table_name")
    assert not is_sensitive_key("rowCount")


def test_redact_mapping_masks_sensitive_values():
    out = redact_mapping({
        "dbUrl": "x", "dbPassword": "secret", "nested": {"apiKey": "k", "rows": 1},
        "list": [{"token": "t"}],
    })
    assert out["dbPassword"] == "***"
    assert out["nested"]["apiKey"] == "***"
    assert out["nested"]["rows"] == 1
    assert out["list"] == [{"token": "***"}]
    assert out["dbUrl"] == "x"


def test_redact_dsn_masks_embedded_credential():
    masked = redact_dsn("mysql://app_user:s3cret@10.0.0.1:3306/perf")
    assert "s3cret" not in masked
    assert masked.startswith("mysql://app_user:***@")
    assert redact_dsn("mysql://host/db") == "mysql://host/db"
    assert redact_dsn(None) is None


def test_build_log_record_redacts_extra_and_dsn():
    rec = build_log_record(
        dataset_id="d1", operation="load", target_dsn="mysql://u:p@h/db",
        db_password="pw")
    assert rec["target_dsn"] == "***"  # 键名敏感 → 直接打码
    assert rec["db_password"] == "***"
    assert "p@h" not in json.dumps(rec)


def test_emit_log_record_has_no_secret(caplog):
    rec = build_log_record(operation="load", status="failed",
                           error=RuntimeError("load_error"), db_password="pw",
                           dsn="mysql://u:secret@h/db")
    with caplog.at_level(logging.INFO, logger="ptde.observability"):
        emit_log_record(rec)
    text = caplog.text
    assert "pw" not in text
    assert "secret" not in text
    assert '"error_class": "load_error"' in text or "load_error" in text
