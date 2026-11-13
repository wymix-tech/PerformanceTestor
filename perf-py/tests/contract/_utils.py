"""契约测试公共工具：规范化规则 + golden 加载 + 结构比对。

normalize 与 scripts/capture_contract.py 共用本实现（避免规则漂移）。
golden 文件由 scripts/capture-contract.sh 对运行中的 Java perf-server 捕获生成：
- normalized：dict 保留键名、list 取首元素、标量 → 类型标记（结构即契约）
- raw：原始响应样本（人工核对用）
"""

from __future__ import annotations

import json
from pathlib import Path

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


def normalize(value) -> object:
    """值 → 类型标记；结构与键名即契约。"""
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(value[0])] if value else []
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return None
    return "string"


def load_golden(name: str) -> dict:
    return json.loads((GOLDEN_DIR / f"{name}.json").read_text(encoding="utf-8"))


def assert_matches_golden(actual_body, golden: dict, actual_status: int | None = None) -> None:
    """断言实际响应与 golden 结构一致（键集合与类型；标量值不比对）。"""
    if actual_status is not None:
        assert actual_status == golden["status"], \
            f"状态码不一致: {actual_status} != {golden['status']}"
    actual_norm = normalize(actual_body)
    expected = golden["normalized"]
    assert actual_norm == expected, (
        "契约结构不一致\n  expected: " + json.dumps(expected, ensure_ascii=False)
        + "\n  actual:   " + json.dumps(actual_norm, ensure_ascii=False)
    )
