import tomllib

from perf_cli import __version__
from perf_cli.config import (
    EXIT_GATE_FAILED,
    EXIT_INVOCATION_ERROR,
    EXIT_OK,
    PERF_PY_ROOT,
    GateConfig,
    ServerConfig,
    load_gate_config,
    load_server_config,
)


def test_version():
    assert __version__ == "0.1.0"


def test_pyproject_declares_console_script():
    pyproject = PERF_PY_ROOT / "pyproject.toml"
    with open(pyproject, "rb") as fh:
        data = tomllib.load(fh)
    assert data["project"]["scripts"]["perf"] == "perf_cli.main:app"
    deps = " ".join(data["project"]["dependencies"])
    # Issue #23 阶段 1 关键依赖：typer CLI / pydantic 模型 / yaml 场景 / httpx 客户端
    for key in ("typer", "pydantic", "pyyaml", "httpx"):
        assert key in deps.lower()


def test_exit_code_semantics_aligned_with_perf_gate_sh():
    # scripts/perf-gate.sh: 0 = 通过；1 = 未通过；2 = 调用异常
    assert (EXIT_OK, EXIT_GATE_FAILED, EXIT_INVOCATION_ERROR) == (0, 1, 2)


def test_server_config_defaults():
    cfg = ServerConfig()
    assert cfg.server_url == "http://localhost:8080"
    assert cfg.token_missing is True


def test_gate_config_defaults_use_real_demo_target():
    # 默认目标必须是 perf-demo 真实端点（8081 + /api/order/list），
    # 而非 scripts/perf-gate.sh 里过时的 18080 默认值
    cfg = GateConfig()
    assert cfg.target_url == "http://localhost:8081/api/order/list"
    assert cfg.app_name == "demo-app"
    assert cfg.concurrency == 20
    assert cfg.duration_sec == 60


def test_load_server_config_from_env(monkeypatch):
    monkeypatch.setenv("PERF_SERVER", "http://perf.example:9000")
    monkeypatch.setenv("PERF_API_TOKEN", "tk-123")
    cfg = load_server_config()
    assert cfg.server_url == "http://perf.example:9000"
    assert cfg.api_token == "tk-123"
    assert cfg.token_missing is False


def test_load_gate_config_from_env(monkeypatch):
    monkeypatch.setenv("GATE_URL", "http://localhost:8081/api/user/list")
    monkeypatch.setenv("GATE_CONCURRENCY", "50")
    monkeypatch.setenv("GATE_FAIL_NO_BASE", "true")
    monkeypatch.setenv("GATE_BASELINE", "42")
    cfg = load_gate_config()
    assert cfg.target_url == "http://localhost:8081/api/user/list"
    assert cfg.concurrency == 50
    assert cfg.fail_on_no_baseline is True
    assert cfg.baseline_task_id == 42
