"""R0 验收：A4 共享抽取（ptde_common）正确性。

- legacy re-export 后对外符号集合不变（dir() 对比，实施方案 §3.6 验收 5）；
- 行为抽查：经 legacy import 路径使用共享实现，结果一致；
- ptde_common 无对 datagen/ptde 的反向 import（源码扫描 + 运行时 sys.modules 检查）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import datagen.enum_value_discoverer as legacy_evd
import datagen.id_pool as legacy_id_pool
import ptde_common.enum_value_discoverer as common_evd
import ptde_common.id_pool as common_id_pool

ROOT = Path(__file__).resolve().parent.parent

EXPECTED_EVD_SYMBOLS = {
    "COMMENT_PAIR",
    "CHECK_IN",
    "CHECK_EQ",
    "QUOTED",
    "SUSPECT_NAME",
    "discover",
    "discover_with_db",
    "parse_inline_enum",
    "parse_check_clause",
    "parse_comment",
    "match_java_enum",
    "suspect_enum_column",
}

_EXPECTED_EXTRAS_COMMON = {"ColumnLike", "Protocol"}  # 共享包内部类型标注，不入 re-export


def _public_names(module) -> set[str]:
    return {n for n in dir(module) if not n.startswith("_") and n != "annotations"}


def test_id_pool_symbols_unchanged():
    assert _public_names(legacy_id_pool) == _public_names(common_id_pool)
    assert {"MAX_POOL_SIZE", "IdPool"} <= _public_names(legacy_id_pool)


def test_enum_value_discoverer_symbols_unchanged():
    legacy = _public_names(legacy_evd)
    common = _public_names(common_evd)
    assert EXPECTED_EVD_SYMBOLS <= legacy, f"legacy 缺符号: {EXPECTED_EVD_SYMBOLS - legacy}"
    # re-export 不新增符号（common 仅多共享包内部 Protocol 相关名）
    assert common - legacy <= _EXPECTED_EXTRAS_COMMON
    assert legacy - common == set()


def test_id_pool_behavior_via_legacy_path():
    import random

    random.seed(7)
    pool = legacy_id_pool.IdPool()
    pool.add_all([10, 20, 30])
    assert pool.size() == 3 and pool.min() == 10 and pool.max() == 30
    assert pool.sample_uniform() in {10, 20, 30}
    pool.build_skew_weights(50, 80)
    assert pool.sample_skewed() in {10, 20, 30}
    assert pool.sample_invalid_id() > 30
    assert legacy_id_pool.MAX_POOL_SIZE == 500_000


def test_enum_discoverer_behavior_via_legacy_path():
    col = type("C", (), {"name": "order_status", "type_name": "ENUM('A','B')", "comment": None})()
    assert legacy_evd.parse_inline_enum(col.type_name) == ["A", "B"]
    assert legacy_evd.discover(col) == ["A", "B"]
    assert legacy_evd.suspect_enum_column(col) is True
    # 鸭子类型生效：不依赖 datagen.model.ColumnMeta 类型
    from datagen.model import ColumnMeta

    assert legacy_evd.discover(ColumnMeta(name="status", type_name="ENUM('X','Y')")) == ["X", "Y"]


def _import_roots(py_file: Path) -> set[str]:
    """AST 解析文件的全部 import 根模块名（忽略文档字符串误报）。"""
    import ast

    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_ptde_common_no_reverse_import_source_scan():
    """共享包源码不得出现对 datagen / ptde 的 import（A4 单向依赖，AST 扫描）。"""
    for py_file in (ROOT / "ptde_common").glob("*.py"):
        roots = _import_roots(py_file)
        bad = roots & {"datagen", "ptde"}
        assert not bad, f"{py_file.name} 含反向依赖: {bad}"


def test_ptde_common_import_pulls_no_legacy_or_drivers():
    """子进程导入 ptde_common 后，sys.modules 不得出现 datagen/ptde/数据库驱动。"""
    code = (
        "import sys; import ptde_common; "
        "bad = [m for m in sys.modules if m.split('.')[0] in "
        "{'datagen', 'ptde', 'pymysql', 'psycopg2', 'sqlalchemy'}]; "
        "assert not bad, bad; print('ok')"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout
