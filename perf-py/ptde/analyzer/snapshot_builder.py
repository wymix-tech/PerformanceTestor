"""ProjectSnapshot 产出（T1.10：源码版本 + 技术栈 + schema 指纹）。

技术栈识别原则（总方案 §3.2）：
1. 依据构建配置（pom.xml/build.gradle）、运行配置（application.yml/properties）与源码证据；
2. 声明依赖（declared）与运行时证据（runtime）分离——TechStackEntry 两个字段；
3. 无法确认 → status=UNKNOWN，不得仅凭目录名/文件名猜测；
4. 敏感值不入快照（datasource url 只提取产品名，不保存口令）。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from ptde.models.base import Evidence, EvidenceStatus
from ptde.models.snapshot import (
    DatabaseSchemaSnapshot,
    DependencyRef,
    ProjectSnapshot,
    TechStackEntry,
)

__all__ = ["SnapshotBuilder"]

ANALYZER_VERSION = "0.1.0"

_DRIVER_ARTIFACTS = {
    "mysql-connector-j": "mysql",
    "mysql-connector-java": "mysql",
    "postgresql": "postgres",
}
# jdbc URL → 数据库产品（仅取产品名，不保存 url 明文/口令）
_JDBC_PRODUCT = re.compile(r"jdbc:(mysql|postgresql|mariadb|oracle|sqlserver):", re.I)


class SnapshotBuilder:
    """ProjectSnapshot 构建器（纯源码证据；无证据项标 UNKNOWN）。"""

    def __init__(self, analyzer_version: str = ANALYZER_VERSION) -> None:
        self.analyzer_version = analyzer_version

    def build_project_snapshot(
        self,
        project_root: Path,
        schema_snapshot: DatabaseSchemaSnapshot | None = None,
        config_fingerprint: str | None = None,
        source_revision: str | None = None,
    ) -> ProjectSnapshot:
        root = Path(project_root)
        snapshot = ProjectSnapshot(
            id=str(root),
            source_revision=source_revision or self._resolve_revision(root),
            schema_fingerprint=schema_snapshot.fingerprint if schema_snapshot else None,
            config_fingerprint=config_fingerprint,
            analyzer_version=self.analyzer_version,
        )
        snapshot.tech_stack, snapshot.dependencies, evidence = self._analyze_stack(root)
        snapshot.evidence = evidence
        snapshot.seal()
        return snapshot

    # ---------- 源码版本 ----------

    def _resolve_revision(self, root: Path) -> str:
        """git commit 优先；非 git 目录用文件内容哈希（zip/快照形态）。"""
        try:
            out = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(root),
                capture_output=True, text=True, timeout=10, check=True,
            )
            return out.stdout.strip()
        except Exception:  # noqa: BLE001 无 git/超时 → 内容哈希
            return self._content_hash(root)

    @staticmethod
    def _content_hash(root: Path) -> str:
        h = hashlib.sha256()
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix in {".java", ".xml", ".yml", ".yaml", ".properties",
                                            ".gradle", ".kts"}:
                h.update(p.name.encode())
                h.update(p.read_bytes())
        return "content:" + h.hexdigest()[:64]

    # ---------- 技术栈与依赖 ----------

    def _analyze_stack(self, root: Path):
        tech: dict[str, TechStackEntry] = {}
        deps: list[DependencyRef] = []
        evidence: list[Evidence] = []

        pom = root / "pom.xml"
        if pom.exists():
            evidence.append(Evidence(type="build_config", file="pom.xml",
                                     status=EvidenceStatus.CONFIRMED))
            artifacts = _parse_pom(pom)
            tech["language"] = TechStackEntry(declared="java", status="CONFIRMED")
            deps = [
                DependencyRef(
                    name=a,
                    manager="maven",
                    evidence=[Evidence(type="build_config", file="pom.xml",
                                       status=EvidenceStatus.CONFIRMED)],
                )
                for a in artifacts
            ]
            # 框架声明
            if any(a.startswith("spring-boot") for a in artifacts):
                tech["framework"] = TechStackEntry(declared="spring-boot", status="CONFIRMED")
            # 数据库驱动声明
            for artifact, product in _DRIVER_ARTIFACTS.items():
                if artifact in artifacts:
                    tech["database_driver"] = TechStackEntry(
                        declared=product, status="CONFIRMED",
                    )
                    break
        else:
            tech["language"] = TechStackEntry(declared=None, runtime=None, status="UNKNOWN")

        # 运行时证据：application 配置中的 datasource url（只取产品名）
        runtime_product = self._runtime_db_product(root)
        if runtime_product:
            entry = tech.get("database_driver") or TechStackEntry(declared=None)
            entry.runtime = runtime_product
            entry.status = "CONFIRMED" if entry.declared == runtime_product else "INFERRED"
            tech["database_driver"] = entry
        elif "database_driver" not in tech:
            tech["database_driver"] = TechStackEntry(
                declared=None, runtime=None, status="UNKNOWN"
            )
        return tech, deps, evidence

    def _runtime_db_product(self, root: Path) -> str | None:
        for name in ("application.yml", "application.yaml", "application.properties",
                     "application-prod.yml", "application-test.yml"):
            p = root / name
            if not p.exists():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            m = _JDBC_PRODUCT.search(text)
            if m:
                return m.group(1).lower()
        return None


def _parse_pom(pom: Path) -> list[str]:
    """解析 pom.xml 依赖 artifactId 清单（构建文件证据；失败返回空）。"""
    try:
        tree = ET.parse(pom)
    except ET.ParseError:
        return []
    artifacts: list[str] = []
    tag = tree.getroot().tag
    ns_prefix = tag[: tag.index("}") + 1] if tag.startswith("{") else ""
    for dep in tree.getroot().iter(f"{ns_prefix}artifactId"):
        if dep.text and dep.text.strip():
            artifacts.append(dep.text.strip())
    return artifacts
