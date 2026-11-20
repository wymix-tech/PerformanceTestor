"""`perf ptde` 命令族等价测试（T7.1）：命令形状 / 退出码 / flag 透传 / 生产守卫。

- dataset 生命周期命令经本地 service（--db 指向临时 sqlite）直连落库层；
- 执行体为注入缝（真库端到端执行体随 A2 真库环境批次接线）；
- 退出码：0 = 成功；1 = 领域失败；2 = 调用异常。
"""

import json

from typer.testing import CliRunner

from perf_cli.config import EXIT_GATE_FAILED, EXIT_INVOCATION_ERROR, EXIT_OK
from perf_cli.main import app

runner = CliRunner()

SPEC = {"name": "订单数据集", "targets": {"rows": {"t_order": 100}}}


def _spec_file(tmp_path, body=SPEC) -> str:
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return str(p)


def _db(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'ptde_cli.sqlite3'}"


# ---------------------------------------------------------------- analyze / profile


def test_analyze_missing_dir_exit_2(tmp_path):
    result = runner.invoke(app, ["ptde", "analyze", str(tmp_path / "nope")])
    assert result.exit_code == EXIT_INVOCATION_ERROR


def test_analyze_empty_project_ok(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    out = tmp_path / "snap.json"
    result = runner.invoke(
        app, ["ptde", "analyze", str(project), "--output", str(out)])
    assert result.exit_code == EXIT_OK
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["id"].endswith("proj")


def test_profile_inspect_missing_file_exit_2(tmp_path):
    result = runner.invoke(app, ["ptde", "profile", str(tmp_path / "nope.json")])
    assert result.exit_code == EXIT_INVOCATION_ERROR


def test_profile_inspect_without_file_exit_2():
    result = runner.invoke(app, ["ptde", "profile"])
    assert result.exit_code == EXIT_INVOCATION_ERROR


def test_profile_collect_requires_config():
    result = runner.invoke(app, ["ptde", "profile", "--collect"])
    assert result.exit_code == EXIT_INVOCATION_ERROR
    assert "--config" in result.output


# ---------------------------------------------------------------- plan


def test_plan_create_registers_definition(tmp_path):
    out = tmp_path / "def.json"
    result = runner.invoke(app, [
        "ptde", "plan", _spec_file(tmp_path), "--def-key", "orders",
        "--db", _db(tmp_path), "--output", str(out)])
    assert result.exit_code == EXIT_OK
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["defKey"] == "orders"
    assert data["specFingerprint"]


def test_plan_create_duplicate_exit_1(tmp_path):
    args = ["ptde", "plan", _spec_file(tmp_path), "--def-key", "orders",
            "--db", _db(tmp_path)]
    assert runner.invoke(app, args).exit_code == EXIT_OK
    result = runner.invoke(app, args)
    assert result.exit_code == EXIT_GATE_FAILED
    assert "已存在" in result.output


def test_plan_create_requires_def_key(tmp_path):
    result = runner.invoke(app, ["ptde", "plan", _spec_file(tmp_path)])
    assert result.exit_code == EXIT_INVOCATION_ERROR


def test_plan_preview_uses_seed(tmp_path, monkeypatch):
    import ptde.planner as planner_mod

    captured = {}

    class FakePlan:
        def model_dump_json(self):
            return json.dumps({"ok": True})

    def fake_build_plan(spec, *args, **kwargs):
        captured["seed"] = spec.reproducibility.seed
        captured["rows"] = spec.targets.rows
        return FakePlan()

    monkeypatch.setattr(planner_mod, "build_plan", fake_build_plan)
    result = runner.invoke(app, [
        "ptde", "plan", _spec_file(tmp_path), "--preview", "--seed", "42",
        "--output", str(tmp_path / "plan.json")])
    assert result.exit_code == EXIT_OK, result.output
    assert captured["seed"] == 42
    assert captured["rows"] == {"t_order": 100}
    assert json.loads((tmp_path / "plan.json").read_text(encoding="utf-8")) == {"ok": True}


def test_plan_preview_invalid_spec_exit_1(tmp_path):
    result = runner.invoke(
        app, ["ptde", "plan", _spec_file(tmp_path, {"bad": 1}), "--preview"])
    assert result.exit_code == EXIT_GATE_FAILED


# ---------------------------------------------------------------- dataset


def test_dataset_create_and_status(tmp_path):
    db = _db(tmp_path)
    runner.invoke(app, ["ptde", "plan", _spec_file(tmp_path), "--def-key",
                        "orders", "--db", db])
    out = tmp_path / "inst.json"
    result = runner.invoke(app, [
        "ptde", "dataset", "create", "--def-key", "orders",
        "--environment", "dev-1", "--db", db, "--output", str(out)])
    assert result.exit_code == EXIT_OK
    iid = json.loads(out.read_text(encoding="utf-8"))["id"]
    status = tmp_path / "status.json"
    result = runner.invoke(app, [
        "ptde", "dataset", "status", iid, "--db", db, "--output", str(status)])
    assert result.exit_code == EXIT_OK
    assert json.loads(status.read_text(encoding="utf-8"))["status"] == "PLANNED"


def test_dataset_list(tmp_path):
    db = _db(tmp_path)
    runner.invoke(app, ["ptde", "plan", _spec_file(tmp_path), "--def-key",
                        "orders", "--db", db])
    runner.invoke(app, ["ptde", "dataset", "create", "--def-key", "orders",
                        "--environment", "dev-1", "--db", db])
    out = tmp_path / "list.json"
    result = runner.invoke(app, ["ptde", "dataset", "list", "--db", db,
                                 "--output", str(out)])
    assert result.exit_code == EXIT_OK
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["total"] == 1


def test_dataset_build_unwired_pipeline_exit_1(tmp_path):
    """执行体未接线 → 显式失败（不伪装可用；真库环境批次落地）。"""
    db = _db(tmp_path)
    runner.invoke(app, ["ptde", "plan", _spec_file(tmp_path), "--def-key",
                        "orders", "--db", db])
    out = tmp_path / "inst.json"
    runner.invoke(app, ["ptde", "dataset", "create", "--def-key", "orders",
                        "--environment", "dev-1", "--db", db, "--output", str(out)])
    iid = json.loads(out.read_text(encoding="utf-8"))["id"]
    result = runner.invoke(app, [
        "ptde", "dataset", "build", iid, "--dry-run", "--seed", "1",
        "--batch", "1000", "--db", db])
    assert result.exit_code == EXIT_GATE_FAILED
    assert "未接线" in result.output


def test_dataset_build_production_environment_exit_1(tmp_path):
    """生产环境不提供构建入口（总方案 §15.1）。"""
    db = _db(tmp_path)
    runner.invoke(app, ["ptde", "plan", _spec_file(tmp_path), "--def-key",
                        "orders", "--db", db])
    out = tmp_path / "inst.json"
    runner.invoke(app, ["ptde", "dataset", "create", "--def-key", "orders",
                        "--environment", "production", "--db", db,
                        "--output", str(out)])
    iid = json.loads(out.read_text(encoding="utf-8"))["id"]
    result = runner.invoke(app, ["ptde", "dataset", "build", iid, "--db", db])
    assert result.exit_code == EXIT_GATE_FAILED
    assert "生产环境" in result.output


def test_dataset_build_with_fake_pipeline_reaches_ready(tmp_path, monkeypatch):
    db = _db(tmp_path)
    runner.invoke(app, ["ptde", "plan", _spec_file(tmp_path), "--def-key",
                        "orders", "--db", db])
    out = tmp_path / "inst.json"
    runner.invoke(app, ["ptde", "dataset", "create", "--def-key", "orders",
                        "--environment", "dev-1", "--db", db, "--output", str(out)])
    iid = json.loads(out.read_text(encoding="utf-8"))["id"]

    import perf_cli.ptde_runner as pr
    from tests.test_server_dataset_api import FakePipeline

    real_default = pr._default_service

    def fake_default(db_url=None, pipeline=None):
        return real_default(db_url, pipeline=FakePipeline())

    monkeypatch.setattr(pr, "_default_service", fake_default)
    result = runner.invoke(app, ["ptde", "dataset", "build", iid, "--db", db])
    assert result.exit_code == EXIT_OK, result.output


def test_dataset_verify_requires_loading(tmp_path):
    db = _db(tmp_path)
    runner.invoke(app, ["ptde", "plan", _spec_file(tmp_path), "--def-key",
                        "orders", "--db", db])
    out = tmp_path / "inst.json"
    runner.invoke(app, ["ptde", "dataset", "create", "--def-key", "orders",
                        "--environment", "dev-1", "--db", db, "--output", str(out)])
    iid = json.loads(out.read_text(encoding="utf-8"))["id"]
    result = runner.invoke(app, ["ptde", "dataset", "verify", iid, "--db", db])
    assert result.exit_code == EXIT_GATE_FAILED
    assert "仅适用 LOADING" in result.output


def test_dataset_cleanup_requires_env_class(tmp_path):
    db = _db(tmp_path)
    runner.invoke(app, ["ptde", "plan", _spec_file(tmp_path), "--def-key",
                        "orders", "--db", db])
    out = tmp_path / "inst.json"
    runner.invoke(app, ["ptde", "dataset", "create", "--def-key", "orders",
                        "--environment", "dev-1", "--db", db, "--output", str(out)])
    iid = json.loads(out.read_text(encoding="utf-8"))["id"]
    result = runner.invoke(app, ["ptde", "dataset", "cleanup", iid, "--db", db])
    assert result.exit_code == EXIT_INVOCATION_ERROR


def test_dataset_cleanup_unknown_instance_exit_1(tmp_path):
    result = runner.invoke(app, [
        "ptde", "dataset", "cleanup", "999", "--env-class", "dedicated_test_db",
        "--db", _db(tmp_path)])
    assert result.exit_code == EXIT_GATE_FAILED


def test_dataset_unknown_action_exit_2(tmp_path):
    result = runner.invoke(app, ["ptde", "dataset", "frobnicate", "1"])
    assert result.exit_code == EXIT_INVOCATION_ERROR
