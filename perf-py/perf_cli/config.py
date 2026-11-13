"""配置层：环境变量 + profile 文件（TOML）。

约定（对齐 scripts/perf-gate.sh 的既有环境变量，便于平滑替换）：

- 平台连接：PERF_SERVER / PERF_API_TOKEN
- 门禁参数：GATE_URL / GATE_APP / GATE_METHOD / GATE_BODY / GATE_CONCURRENCY /
  GATE_DURATION / GATE_WARMUP / GATE_RAMP_UP / GATE_QPS_LIMIT / GATE_BASELINE /
  GATE_TIMEOUT / GATE_FAIL_NO_BASE / GATE_WEBHOOK
- profile：PERF_PROFILE 指定名称，从 profiles/<name>.toml 读取，文件值优先级低于环境变量
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------- exit codes
# 语义对齐 scripts/perf-gate.sh：0 通过；1 未通过；2 调用异常（参数/网络）
EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_INVOCATION_ERROR = 2

# perf-py 工程根目录（perf-py/pyproject.toml 所在目录）
PERF_PY_ROOT = Path(__file__).resolve().parent.parent

# 默认目标对齐 perf-demo 实际端口与路径（scripts/perf-gate.sh 里的 18080 是过时默认值）
DEFAULT_SERVER = "http://localhost:8080"
DEFAULT_TARGET_URL = "http://localhost:8081/api/order/list"
DEFAULT_APP = "demo-app"


@dataclass(frozen=True)
class ServerConfig:
    """平台连接配置。"""

    server_url: str = DEFAULT_SERVER
    api_token: str = ""
    request_timeout: float = 1800.0

    @property
    def token_missing(self) -> bool:
        return not self.api_token.strip()


@dataclass(frozen=True)
class GateConfig:
    """门禁/压测参数，字段语义与 StressTaskRequest 一致。"""

    target_url: str = DEFAULT_TARGET_URL
    app_name: str = DEFAULT_APP
    method: str = "GET"
    body: str = ""
    concurrency: int = 20
    duration_sec: int = 60
    warmup_sec: int = 5
    ramp_up_sec: int = 0
    qps_limit: int = 0
    baseline_task_id: int | None = None
    timeout_sec: int = 0
    fail_on_no_baseline: bool = False
    webhook: str = ""


def _to_bool(raw: str, default: bool = False) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _load_profile(profile: str | None) -> dict:
    """读取 profile TOML；返回原始 dict（未命中返回空）。"""
    if not profile:
        return {}
    path = PERF_PY_ROOT / "profiles" / f"{profile}.toml"
    if not path.is_file():
        raise FileNotFoundError(f"profile 文件不存在: {path}")
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def load_server_config(profile: str | None = None) -> ServerConfig:
    """环境变量优先，profile 兜底。"""
    data = _load_profile(profile)
    server = data.get("server", {}) if isinstance(data.get("server"), dict) else {}
    return ServerConfig(
        server_url=os.environ.get("PERF_SERVER") or server.get("url", DEFAULT_SERVER),
        api_token=os.environ.get("PERF_API_TOKEN") or server.get("api_token", ""),
        request_timeout=float(os.environ.get("PERF_REQUEST_TIMEOUT") or server.get("request_timeout", 1800)),
    )


def load_gate_config(profile: str | None = None) -> GateConfig:
    """GATE_* 环境变量优先，profile 的 [gate] 段兜底。"""
    data = _load_profile(profile)
    gate = data.get("gate", {}) if isinstance(data.get("gate"), dict) else {}
    baseline = os.environ.get("GATE_BASELINE") or gate.get("baseline_task_id") or ""
    return GateConfig(
        target_url=os.environ.get("GATE_URL") or gate.get("target_url", DEFAULT_TARGET_URL),
        app_name=os.environ.get("GATE_APP") or gate.get("app_name", DEFAULT_APP),
        method=os.environ.get("GATE_METHOD") or gate.get("method", "GET"),
        body=os.environ.get("GATE_BODY") or gate.get("body", ""),
        concurrency=int(os.environ.get("GATE_CONCURRENCY") or gate.get("concurrency", 20)),
        duration_sec=int(os.environ.get("GATE_DURATION") or gate.get("duration_sec", 60)),
        warmup_sec=int(os.environ.get("GATE_WARMUP") or gate.get("warmup_sec", 5)),
        ramp_up_sec=int(os.environ.get("GATE_RAMP_UP") or gate.get("ramp_up_sec", 0)),
        qps_limit=int(os.environ.get("GATE_QPS_LIMIT") or gate.get("qps_limit", 0)),
        baseline_task_id=int(baseline) if str(baseline).strip().isdigit() else None,
        timeout_sec=int(os.environ.get("GATE_TIMEOUT") or gate.get("timeout_sec", 0)),
        fail_on_no_baseline=_to_bool(
            os.environ.get("GATE_FAIL_NO_BASE") or str(gate.get("fail_on_no_baseline", "")),
            default=False,
        ),
        webhook=os.environ.get("GATE_WEBHOOK") or gate.get("webhook", ""),
    )
