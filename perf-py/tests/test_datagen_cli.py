"""perf datagen run CLI 等价测试：退出码语义与结果输出（阶段 3 步骤 5）。

退出码：0 = 成功；1 = 引擎失败（方言路由失败/表缺失/Dry-Run 未过）；
2 = 调用异常（spec 不存在 / JSON 解析失败）。
"""

import json

from typer.testing import CliRunner

from perf_cli.config import EXIT_GATE_FAILED, EXIT_INVOCATION_ERROR, EXIT_OK
from perf_cli.main import app

runner = CliRunner()


def _spec(tmp_path, body) -> str:
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return str(p)


def test_run_missing_spec_exit_2(tmp_path):
    result = runner.invoke(app, ["datagen", "run", str(tmp_path / "nope.json")])
    assert result.exit_code == EXIT_INVOCATION_ERROR


def test_run_invalid_json_exit_2(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not-json", encoding="utf-8")
    result = runner.invoke(app, ["datagen", "run", str(p)])
    assert result.exit_code == EXIT_INVOCATION_ERROR


def test_run_unsupported_dialect_exit_1(tmp_path):
    """未注册方言（jdbc:oracle）→ 引擎路由失败 → 退出码 1（非 2）。"""
    p = _spec(tmp_path, {"dbUrl": "jdbc:oracle://h/db", "dbUser": "u",
                         "dbPassword": "p", "dryRun": True,
                         "tables": [{"tableName": "t", "rowCount": 1}]})
    result = runner.invoke(app, ["datagen", "run", p])
    assert result.exit_code == EXIT_GATE_FAILED
    assert "未注册匹配方言" in result.output


def test_run_dry_run_override_wins(tmp_path, monkeypatch):
    """--dry-run 覆盖 spec 内 dryRun=false（捕获传入 spec 验证）。"""
    import perf_cli.datagen_runner as dr

    captured = {}

    class FakeEngine:
        def execute(self, spec, listener=None):
            captured["dry_run"] = spec.dry_run
            return dr.EngineResult.failure("路由后即失败")

    monkeypatch.setattr(dr, "build_default_engine", lambda: FakeEngine())
    p = _spec(tmp_path, {"dbUrl": "jdbc:mysql://127.0.0.1:1/nonexist", "dryRun": False,
                         "tables": [{"tableName": "perf_user", "rowCount": 1}]})
    result = runner.invoke(app, ["datagen", "run", p, "--dry-run"])
    assert result.exit_code == EXIT_GATE_FAILED
    assert captured["dry_run"] is True


def test_run_output_writes_camel_case_json(tmp_path):
    p = _spec(tmp_path, {"dbUrl": "jdbc:oracle://h/db", "dryRun": True,
                         "tables": [{"tableName": "t", "rowCount": 1}]})
    out = tmp_path / "out.json"
    result = runner.invoke(app, ["datagen", "run", p, "--output", str(out)])
    assert result.exit_code == EXIT_GATE_FAILED
    obj = json.loads(out.read_text(encoding="utf-8"))
    assert obj["success"] is False
    assert "errorMsg" in obj and "dryRun" in obj


def test_run_ok_exit_zero(tmp_path, monkeypatch):
    """成功路径退出码 0（引擎以测试替身注入，编排正确性由引擎单测覆盖）。"""
    import perf_cli.datagen_runner as dr

    class FakeEngine:
        def execute(self, spec, listener=None):
            r = dr.EngineResult()
            r.success = True
            r.dry_run = spec.dry_run
            r.total_rows = 10
            r.inserted_rows = 10
            return r

    monkeypatch.setattr(dr, "build_default_engine", lambda: FakeEngine())
    p = _spec(tmp_path, {"dbUrl": "jdbc:mysql://127.0.0.1:1/nonexist", "dryRun": True,
                         "tables": [{"tableName": "perf_user", "rowCount": 10}]})
    result = runner.invoke(app, ["datagen", "run", p])
    assert result.exit_code == EXIT_OK
    assert '"insertedRows": 10' in result.output
