"""R0 验收：配置框架（严格模式默认关闭、DSN 校验与回退告警，实施方案 §3.2 T0.6）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ptde.config import PtdeConfig


def test_defaults_strict_mode_off():
    cfg = PtdeConfig()
    assert cfg.strict_mode is False
    assert cfg.profile_dsn is None and cfg.target_dsn is None
    assert cfg.sampling_rate == 1.0 and cfg.collect_timeout_seconds > 0


def test_strict_mode_can_be_enabled():
    assert PtdeConfig(strict_mode=True).strict_mode is True


def test_dsn_validation_rejects_blank():
    with pytest.raises(ValidationError):
        PtdeConfig(target_dsn="   ")
    with pytest.raises(ValidationError):
        PtdeConfig(profile_dsn="", target_dsn="x")


def test_resource_validation():
    with pytest.raises(ValidationError):
        PtdeConfig(sampling_rate=0)
    with pytest.raises(ValidationError):
        PtdeConfig(sampling_rate=1.5)
    with pytest.raises(ValidationError):
        PtdeConfig(collect_timeout_seconds=0)
    with pytest.raises(ValidationError):
        PtdeConfig(collect_max_rows=-1)


def test_resolve_dsns_fallback_warning():
    """A5：仅配置 target_dsn 时回退同值 + 显式 warning（R2 采集器消费，此处占位验证）。"""
    cfg = PtdeConfig(target_dsn="mysql://target")
    res = cfg.resolve_dsns()
    assert res.profile_dsn == "mysql://target"
    assert res.fallback_same_value is True
    assert len(res.warnings) == 1 and "profile_dsn" in res.warnings[0]


def test_resolve_dsns_separated_no_warning():
    """A5：双 DSN 分别配置时无回退、无告警。"""
    cfg = PtdeConfig(profile_dsn="mysql://readonly", target_dsn="mysql://write")
    res = cfg.resolve_dsns()
    assert res.profile_dsn == "mysql://readonly"
    assert res.target_dsn == "mysql://write"
    assert res.fallback_same_value is False and res.warnings == []


def test_resolve_dsns_requires_target():
    with pytest.raises(ValueError, match="target_dsn"):
        PtdeConfig(profile_dsn="mysql://ro").resolve_dsns()


def test_from_env(monkeypatch):
    monkeypatch.delenv("PTDE_STRICT_MODE", raising=False)
    monkeypatch.delenv("PTDE_PROFILE_DSN", raising=False)
    monkeypatch.delenv("PTDE_TARGET_DSN", raising=False)
    cfg = PtdeConfig.from_env()
    assert cfg.strict_mode is False and cfg.target_dsn is None

    cfg2 = PtdeConfig.from_env({"PTDE_STRICT_MODE": "true",
                                "PTDE_PROFILE_DSN": "ro://1",
                                "PTDE_TARGET_DSN": "rw://1"})
    assert cfg2.strict_mode is True
    assert cfg2.profile_dsn == "ro://1" and cfg2.target_dsn == "rw://1"
