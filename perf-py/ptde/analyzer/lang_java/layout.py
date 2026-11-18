"""迁移文件与测试代码识别（T1.9，降级为"识别并登记，不解析"）。

识别范围（实施方案 T1.9）：
- Flyway 迁移目录：`db/migration`（含 `V*__*.sql` 命名约定）
- Liquibase changelog：`changelog` 目录 / `*.changelog*.xml|sql|yaml` 文件
- 测试代码：`src/test/` 目录、`*Test.java`、`*Tests.java`、Fixture/Factory 命名

登记语义：发现一律 Finding status=UNKNOWN（总方案 §3.1：仅识别结构演进与
Fixture 复用线索，本期不解析 DDL/断言）；不伪装为已解析事实（A3）。
"""

from __future__ import annotations

import re
from pathlib import Path

from ptde.analyzer.language_adapter import Finding, FindingStatus

__all__ = ["detect_migration_and_test_files"]

_FLYWAY_DIR = "db/migration"
_LIQUIBASE_FILE = re.compile(r"(?:^|/)(?:db\.)?changelog[\w.-]*\.(?:xml|sql|yaml|yml)$", re.I)
_TEST_PATH = re.compile(r"(?:^|/)src/test/", re.I)
_TEST_FILE = re.compile(r"(?:Test|Tests|IT)\.java$", re.I)
_FIXTURE_FILE = re.compile(r"(?:fixture|factory|builder|testdata)", re.I)


def detect_migration_and_test_files(paths: list[Path]) -> list[Finding]:
    """对给定文件/目录集合做结构识别，返回 UNKNOWN 登记项（不解析内容）。"""
    findings: list[Finding] = []
    seen: set[str] = set()
    for raw in paths:
        p = Path(raw)
        candidates: list[Path] = []
        if p.is_dir():
            candidates = sorted(p.rglob("*"))
        else:
            candidates = [p]
        for f in candidates:
            if f.is_dir():
                continue
            rel = f.as_posix()
            kind = _classify(rel)
            if kind is None or rel in seen:
                continue
            seen.add(rel)
            findings.append(_register(kind, rel))
    return findings


def _classify(rel: str) -> str | None:
    lowered = rel.lower()
    if _FLYWAY_DIR in lowered:
        return "migration_file"
    if _LIQUIBASE_FILE.search(lowered):
        return "migration_file"
    if _TEST_PATH.search(lowered) or _TEST_FILE.search(rel):
        return "test_fixture"
    if _FIXTURE_FILE.search(lowered):
        return "test_fixture"
    return None


def _register(kind: str, rel: str) -> Finding:
    note = (
        "识别到迁移文件（Flyway/Liquibase），本期不解析 DDL"
        if kind == "migration_file"
        else "识别到测试代码/Fixture，本期不解析断言"
    )
    return Finding(
        id=f"{kind}:{rel}",
        type=kind,
        value={"path": rel, "resolution": "identified_not_parsed", "note": note},
        source_file=rel,
        evidence=[],
        confidence=1.0,
        status=FindingStatus.UNKNOWN,
        analyzer_version="0.1.0",
    )
