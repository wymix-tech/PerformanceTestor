"""T1.7/T1.8/T1.9/T1.10 测试：A3 分级裁决 + ptde_finding 采纳流 + 布局识别 + 项目快照。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine

from ptde.analyzer.finding_policy import (
    HardConstraintViolation,
    classify,
    enforce,
    evaluate,
)
from ptde.analyzer.finding_repo import FindingRepository
from ptde.analyzer.lang_java.layout import detect_migration_and_test_files
from ptde.analyzer.snapshot_builder import SnapshotBuilder
from ptde.models.snapshot import DatabaseSchemaSnapshot


def _f(**kw):
    base = {"type": "naming_relation", "status_three": "INFERRED",
            "source_object": "orders.product_id"}
    base.update(kw)
    return base


# ---------- T1.7 A3 分级 ----------

def test_classify_three_buckets() -> None:
    assert classify(_f(type="primary_key_missing")) == "hard"
    assert classify(_f(type="metadata_incomplete")) == "hard"
    assert classify(_f(type="naming_relation")) == "inferred"
    assert classify(_f(type="magic_hint")) == "inferred"
    assert classify(_f(type="unclassified_info")) == "info"


def test_hard_failure_blocks_in_both_modes() -> None:
    bad = _f(type="primary_key_missing", status_three="UNKNOWN",
             source_object="orders", reason="no primary key detected")
    for strict in (False, True):
        with pytest.raises(HardConstraintViolation):
            enforce([bad], strict_mode=strict)  # 严格与非严格模式均阻断（硬约束类）


def test_hard_failure_raises_with_object_and_reason() -> None:
    bad = _f(type="metadata_incomplete", status_three="UNKNOWN",
             source_object="mysql:orders.columns",
             reason="columns query failed: permission denied")
    with pytest.raises(HardConstraintViolation) as ei:
        enforce([bad])
    msg = str(ei.value)
    assert "mysql:orders.columns" in msg and "permission denied" in msg


def test_confirmed_hard_fact_is_not_violation() -> None:
    ok = _f(type="primary_key_missing", status_three="CONFIRMED")
    assert enforce([ok]).ok is True


def test_inferred_warning_not_blocking() -> None:
    result = enforce([_f()], strict_mode=False)
    assert result.ok
    assert len(result.warnings) == 1
    assert result.failures == []


def test_strict_mode_blocks_inferred() -> None:
    result = evaluate([_f()], strict_mode=True)
    assert not result.ok
    assert len(result.blocked_inferred) == 1
    with pytest.raises(HardConstraintViolation):
        enforce([_f(type="enum_guess", source_object="orders.status")], strict_mode=True)


def test_evaluate_does_not_raise() -> None:
    result = evaluate([_f(type="primary_key_missing", status_three="UNKNOWN")], strict_mode=False)
    assert not result.ok and len(result.failures) == 1


# ---------- T1.8 ptde_finding 落库与采纳流 ----------

@pytest.fixture()
def repo(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/finding.db")
    return FindingRepository(engine, create_all=True)


def _upsert(repo, value=None, **kw):
    payload = {
        "repo_id": 1,
        "finding_type": "relation",
        "value": value or {"child_table": "orders", "child_column": "user_id"},
        "status_three": "INFERRED",
        "source_object": "orders.user_id",
        "confidence": 0.5,
        "analyzer_version": "0.1.0",
    }
    payload.update(kw)
    return repo.upsert_finding(**payload)


def test_finding_upsert_idempotent(repo) -> None:
    id1 = _upsert(repo)
    id2 = _upsert(repo)  # 同 value_fingerprint → 更新不重复插行
    assert id1 == id2
    assert repo.count_all() == 1


def test_finding_upsert_preserves_adopt_status(repo) -> None:
    """命中更新不回滚用户采纳状态（对齐 hint_upsert 语义）。"""
    fid = _upsert(repo)
    assert repo.update_adopt_status(fid, "adopted")
    _upsert(repo, confidence=0.9)
    row = repo.by_id(fid)
    assert row.adopt_status == "adopted"
    assert row.confidence == 0.9


def test_finding_adopt_status_transitions(repo) -> None:
    fid = _upsert(repo)
    assert repo.by_id(fid).adopt_status == "pending"
    assert repo.update_adopt_status(fid, "dismissed")
    assert repo.by_id(fid).adopt_status == "dismissed"
    with pytest.raises(ValueError):
        repo.update_adopt_status(fid, "bogus")


def test_finding_different_value_new_row(repo) -> None:
    _upsert(repo)
    _upsert(repo, value={"child_table": "orders", "child_column": "product_id"})
    assert repo.count_all() == 2


def test_finding_list_by_type_and_status(repo) -> None:
    _upsert(repo)
    _upsert(repo, finding_type="enum", value={"column": "status"},
            source_object="orders.status")
    assert len(repo.list_by_type("relation")) == 1
    assert len(repo.list_by_type("enum", adopt_status="pending")) == 1
    assert repo.list_by_type("none") == []


# ---------- T1.9 迁移文件/测试代码识别 ----------

def test_layout_detection(tmp_path: Path) -> None:
    (tmp_path / "src/main/java/db/migration").mkdir(parents=True)
    (tmp_path / "src/main/java/db/migration/V1__init.sql").write_text("CREATE TABLE t(id INT);")
    (tmp_path / "src/main/resources").mkdir(parents=True)
    (tmp_path / "src/main/resources/changelog-master.xml").write_text("<changelog/>")
    (tmp_path / "src/test/java").mkdir(parents=True)
    (tmp_path / "src/test/java/UserServiceTest.java").write_text("class UserServiceTest {}")
    (tmp_path / "src/main/java/OrderService.java").write_text("class OrderService {}")
    findings = detect_migration_and_test_files([tmp_path])
    by_type = {}
    for f in findings:
        by_type.setdefault(f.type, []).append(f)
    flyway = [f for f in by_type.get("migration_file", []) if "V1__init" in f.source_file]
    liquibase = [f for f in by_type.get("migration_file", []) if "changelog-master" in f.source_file]
    tests = by_type.get("test_fixture", [])
    assert len(flyway) == 1 and len(liquibase) == 1
    assert len(tests) == 1 and "UserServiceTest" in tests[0].source_file
    # 全部 UNKNOWN（识别不解析，不伪装为已解析事实）
    assert all(f.status.value == "UNKNOWN" for f in findings)
    assert all(f.value["resolution"] == "identified_not_parsed" for f in findings)
    # 业务源码不误报
    assert not any("OrderService" in (f.source_file or "") for f in findings)


def test_layout_test_fixture_file_names(tmp_path: Path) -> None:
    (tmp_path / "fixture").mkdir()
    (tmp_path / "fixture" / "order_builder.json").write_text("{}")
    findings = detect_migration_and_test_files([tmp_path])
    assert findings and findings[0].type == "test_fixture"


# ---------- T1.10 ProjectSnapshot ----------

def _write_demo_project(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text("""
<project>
  <artifactId>perf-demo</artifactId>
  <dependencies>
    <dependency><artifactId>spring-boot-starter-web</artifactId></dependency>
    <dependency><artifactId>mysql-connector-j</artifactId></dependency>
  </dependencies>
</project>
""")
    (tmp_path / "application.yml").write_text(
        "spring:\n  datasource:\n    url: jdbc:mysql://127.0.0.1:3306/demo?password=secret\n"
    )


def test_snapshot_tech_stack_declared_vs_runtime(tmp_path: Path) -> None:
    _write_demo_project(tmp_path)
    snap = SnapshotBuilder().build_project_snapshot(tmp_path, source_revision="rev-1")
    assert snap.source_revision == "rev-1"
    # 声明 vs 运行时分离（总方案 §3.2）
    lang = snap.tech_stack["language"]
    assert lang.declared == "java" and lang.status == "CONFIRMED"
    fw = snap.tech_stack["framework"]
    assert fw.declared == "spring-boot"
    driver = snap.tech_stack["database_driver"]
    assert driver.declared == "mysql" and driver.runtime == "mysql"
    assert driver.status == "CONFIRMED"
    # 依赖有结构化证据
    names = [d.name for d in snap.dependencies]
    assert "mysql-connector-j" in names and "spring-boot-starter-web" in names
    assert snap.dependencies[0].evidence[0].file == "pom.xml"
    # 敏感值不入快照（datasource url 只取产品名）
    assert "secret" not in snap.to_json()


def test_snapshot_unknown_when_no_evidence(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("nothing here")
    snap = SnapshotBuilder().build_project_snapshot(tmp_path)
    assert snap.tech_stack["language"].status == "UNKNOWN"
    assert snap.tech_stack["database_driver"].status == "UNKNOWN"
    assert snap.source_revision.startswith("content:")


def test_snapshot_schema_fingerprint_links(tmp_path: Path) -> None:
    _write_demo_project(tmp_path)
    schema = DatabaseSchemaSnapshot(id="snap-1", tables=[])
    schema.seal()
    snap = SnapshotBuilder().build_project_snapshot(tmp_path, schema_snapshot=schema)
    assert snap.schema_fingerprint == schema.fingerprint
    assert snap.fingerprint  # 快照自身带指纹


def test_snapshot_runtime_only_driver_inferred(tmp_path: Path) -> None:
    (tmp_path / "application.yml").write_text(
        "spring:\n  datasource:\n    url: jdbc:postgresql://h/db\n"
    )
    snap = SnapshotBuilder().build_project_snapshot(tmp_path)
    driver = snap.tech_stack["database_driver"]
    assert driver.declared is None and driver.runtime == "postgresql"
    assert driver.status == "INFERRED"  # 声明缺失 → 推断，不伪装 CONFIRMED
