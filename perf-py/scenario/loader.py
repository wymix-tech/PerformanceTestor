"""场景文件加载与校验（YAML）。

- 支持 `${ENV_NAME}` 环境变量插值（字符串值内替换，如预置 token），未定义的环境变量替换为空串并在结果中提示
- 校验失败时聚合全部错误一次性抛出（ScenarioError.errors），便于 CLI 一次性报完整清单
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from scenario.model import Scenario

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# YAML 中 snake_case 键到模型 snake_case 字段的兼容别名（camelCase 容错）
KEY_ALIASES: dict[str, str] = {
    "taskName": "task_name",
    "stageConcurrencies": "stage_concurrencies",
    "durationSec": "duration_sec",
    "warmupSec": "warmup_sec",
    "rampUpSec": "ramp_up_sec",
    "qpsLimit": "qps_limit",
    "sampleIntervalSec": "sample_interval_sec",
    "baselineTaskId": "baseline_task_id",
    "timeoutSec": "timeout_sec",
    "failOnNoBaseline": "fail_on_no_baseline",
}


class ScenarioError(ValueError):
    """场景文件加载/校验失败，携带全部错误。"""

    def __init__(self, path: Path, errors: list[str]) -> None:
        self.path = path
        self.errors = errors
        joined = "\n  - ".join(errors)
        super().__init__(f"场景文件校验失败: {path}\n  - {joined}")


def _interpolate(node: object, missing: list[str]) -> object:
    """递归替换字符串节点中的 ${ENV}。"""
    if isinstance(node, str):
        def _sub(match: re.Match[str]) -> str:
            name = match.group(1)
            value = os.environ.get(name)
            if value is None:
                missing.append(name)
                return ""
            return value

        return ENV_PATTERN.sub(_sub, node)
    if isinstance(node, dict):
        return {k: _interpolate(v, missing) for k, v in node.items()}
    if isinstance(node, list):
        return [_interpolate(item, missing) for item in node]
    return node


def _normalize_keys(node: object) -> object:
    """递归把已知 camelCase 键归一为 snake_case（容错存量命名习惯）。"""
    if isinstance(node, dict):
        out: dict = {}
        for k, v in node.items():
            key = KEY_ALIASES.get(k, k) if isinstance(k, str) else k
            out[key] = _normalize_keys(v)
        return out
    if isinstance(node, list):
        return [_normalize_keys(item) for item in node]
    return node


def parse_scenario(data: object, source: Path | None = None) -> Scenario:
    """从已解析的 YAML 数据构造 Scenario，聚合校验错误。"""
    errors: list[str] = []
    missing_env: list[str] = []
    interpolated = _interpolate(data, missing_env)
    normalized = _normalize_keys(interpolated)

    if not isinstance(normalized, dict):
        errors.append("场景文件顶层必须是 YAML 映射（mapping）")
        raise ScenarioError(source or Path("<memory>"), errors)

    if missing_env:
        errors.append(f"以下环境变量未定义（插值为空串）: {sorted(set(missing_env))}")

    try:
        scenario = Scenario.model_validate(normalized)
    except Exception as exc:  # pydantic ValidationError → 聚合为可读清单
        for err in getattr(exc, "errors", lambda: [])():
            loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
            errors.append(f"{loc}: {err.get('msg', err)}")

    if errors:
        raise ScenarioError(source or Path("<memory>"), errors)
    assert scenario is not None
    return scenario


def load_scenario(path: Path | str) -> Scenario:
    """读取并校验场景 YAML 文件。校验失败抛 ScenarioError。"""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"场景文件不存在: {file_path}")
    try:
        with open(file_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ScenarioError(file_path, [f"YAML 解析失败: {exc}"]) from exc
    return parse_scenario(data, source=file_path)


def validate_scenario(path: Path | str) -> Scenario:
    """validate 语义入口：成功返回模型（供 CLI 打印摘要），失败抛 ScenarioError。"""
    return load_scenario(path)
