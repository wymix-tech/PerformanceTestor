"""PTDE CLI runner（B7 · T7.1）：`perf ptde` 命令族宿主装配与执行编排。

命令族（总方案 §15.1 + 实施方案 T7.1）：
- analyze / profile collect|inspect / plan create|preview
- dataset create|build|status|verify|list|cleanup

设计：
- dataset 生命周期命令复用 server 侧同一套落库 + 领域组件
  （PtdeDatasetRepository + DatasetLifecycleService；DB 落 --db，默认
  PTDE_CLI_DB 环境变量，再缺省本地 sqlite 文件——CLI 独立运行不依赖 server）；
- 构建执行体为注入缝（``pipeline``）：真库端到端执行体随 A2 真库环境批次
  接线（B6 §7F 口径），缺省显式失败不伪装可用；
- **生产环境不提供构建与清理入口**（总方案 §15.1）：构建在服务层按
  environmentId 拒绝；清理在 plan_cleanup 按环境类别拒绝（K5 口径）；
- 退出码：0 = 成功；1 = 领域失败（门禁拒绝/校验失败）；2 = 调用异常
  （文件不存在/JSON 解析失败）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from perf_cli.config import EXIT_GATE_FAILED, EXIT_INVOCATION_ERROR

DEFAULT_CLI_DB = "sqlite:///ptde_datasets.sqlite3"

_OK = 0


def _emit(data: dict, output: Path | None) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if output is not None:
        output.write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")


def _fail(message: str) -> int:
    print(f"[错误] {message}", file=sys.stderr)
    return EXIT_GATE_FAILED


def _invocation_error(message: str) -> int:
    print(f"[调用异常] {message}", file=sys.stderr)
    return EXIT_INVOCATION_ERROR


# ---------------------------------------------------------------- 基础命令


def run_analyze(project_dir: str, *, output: Path | None = None) -> int:
    """项目分析：SnapshotBuilder 纯源码证据快照（发现项落 JSON）。"""
    from ptde.analyzer.snapshot_builder import SnapshotBuilder

    root = Path(project_dir)
    if not root.is_dir():
        return _invocation_error(f"项目目录不存在：{project_dir}")
    try:
        snapshot = SnapshotBuilder().build_project_snapshot(root)
    except Exception as exc:  # noqa: BLE001 — CLI 边界统一兜底
        return _fail(f"项目分析失败：{type(exc).__name__}: {exc}")
    data = json.loads(snapshot.model_dump_json())
    _emit(data, output)
    return _OK


def run_profile_collect(
    config_file: str | None, *, tables: list[str] | None = None,
    source_type: str = "db_readonly", output: Path | None = None,
    collector=None) -> int:
    """统计画像采集（A5 双 DSN + SELECT 白名单；须提供 --config）。"""
    from ptde.config import PtdeConfig
    from ptde.profiler.collector import StatisticsCollector

    if collector is None:
        if not config_file:
            return _invocation_error("画像采集须提供 --config（PtdeConfig JSON）")
        try:
            body = json.loads(Path(config_file).read_text(encoding="utf-8"))
            config = PtdeConfig.model_validate(body)
        except OSError as exc:
            return _invocation_error(f"config 文件读取失败：{exc}")
        except (ValueError, json.JSONDecodeError) as exc:
            return _invocation_error(f"config 解析失败：{exc}")
        collector = StatisticsCollector(config)
    try:
        profile, manifest = collector.collect(tables=tables, source_type=source_type)
    except Exception as exc:  # noqa: BLE001 — 采集失败显式退出（含白名单拒绝）
        return _fail(f"画像采集失败：{type(exc).__name__}: {exc}")
    _emit({
        "profile": json.loads(profile.model_dump_json()),
        "manifest": json.loads(manifest.model_dump_json()),
    }, output)
    return _OK


def run_profile_inspect(profile_file: str, *, output: Path | None = None) -> int:
    """查看融合画像（文件来源画像导入 + 摘要输出；无生产库时入口）。"""
    from ptde.profiler.file_source import load_statistics_profile

    try:
        profile = load_statistics_profile(profile_file)
    except OSError as exc:
        return _invocation_error(f"画像文件读取失败：{exc}")
    except (ValueError, KeyError) as exc:
        return _fail(f"画像导入失败：{type(exc).__name__}: {exc}")
    _emit(json.loads(profile.model_dump_json()), output)
    return _OK


# ---------------------------------------------------------------- 规划命令


def load_spec(spec_file: str) -> dict:
    """spec JSON 文件 → dict（读取/解析异常上抛，由命令层归退出码）。"""
    return json.loads(Path(spec_file).read_text(encoding="utf-8"))


def run_plan_create(
    spec_file: str, def_key: str, *, version: str = "1", name: str | None = None,
    db_url: str | None = None, output: Path | None = None,
    service=None) -> int:
    """创建数据集规划（Definition 注册：spec 校验 + 指纹封存 + 落库）。"""
    try:
        spec = load_spec(spec_file)
    except OSError as exc:
        return _invocation_error(f"spec 文件读取失败：{exc}")
    except json.JSONDecodeError as exc:
        return _invocation_error(f"spec JSON 解析失败：{exc}")
    service = service or _default_service(db_url)
    try:
        data = service.register_definition(
            def_key, version, name or def_key, spec, created_by="cli")
    except ValueError as exc:
        return _fail(str(exc))
    _emit(data, output)
    return _OK


def run_plan_preview(
    spec_file: str, *, seed: int | None = None, output: Path | None = None,
    planner=None) -> int:
    """规模/分布/资源预算预览（build_plan 离线规划，不写库）。"""
    from ptde.planner import build_plan as ptde_build_plan

    try:
        spec = load_spec(spec_file)
    except OSError as exc:
        return _invocation_error(f"spec 文件读取失败：{exc}")
    except json.JSONDecodeError as exc:
        return _invocation_error(f"spec JSON 解析失败：{exc}")
    if planner is None:
        def planner(body, _seed=None):
            from ptde.models.spec import DatasetSpec

            spec_model = DatasetSpec.model_validate(body)
            if _seed is not None:
                spec_model.reproducibility.seed = _seed  # QW1/R4 Seed 链路配置入口
            return ptde_build_plan(spec_model)
    try:
        plan = planner(spec, seed)
    except (ValueError, TypeError, KeyError) as exc:
        return _fail(f"规划失败：{type(exc).__name__}: {exc}")
    _emit(json.loads(plan.model_dump_json()), output)
    return _OK


# ---------------------------------------------------------------- 数据集命令


def resolve_cli_db(db_url: str | None) -> str:
    """CLI 落库地址：--db → PTDE_CLI_DB → 本地 sqlite 文件。"""
    return db_url or os.environ.get("PTDE_CLI_DB") or DEFAULT_CLI_DB


def _default_service(db_url: str | None, pipeline=None):
    from server.dataset_service import DatasetLifecycleService
    from server.db import Base, make_session_factory
    from server.ptde_repo import PtdeDatasetRepository

    url = resolve_cli_db(db_url)
    factory = make_session_factory(url)
    if url.startswith("sqlite"):
        # CLI 本地库便捷建表；生产 MySQL 由 sql/init.sql 建表（不越权 DDL）
        import server.ptde_repo  # noqa: F401  注册模型
        Base.metadata.create_all(factory.kw["bind"])
    return DatasetLifecycleService(
        PtdeDatasetRepository(factory), pipeline=pipeline, operator="cli")


def _poll_until_done(service, instance_id: str, *, timeout_sec: float = 600.0) -> dict:
    """轮询任务运行态直至完成（build/verify 异步执行的 CLI 侧等待）。"""
    import time

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        status = service.task_status(instance_id)
        if not status["running"]:
            return service.get_instance(instance_id)
        time.sleep(0.05)
    return {"timeout": True, "taskId": instance_id}


def run_dataset_create(
    def_key: str, environment_id: str, *, version: str = "1",
    db_url: str | None = None, output: Path | None = None,
    service=None) -> int:
    """创建数据集实例（初始 PLANNED；owner_token 随建即发）。"""
    service = service or _default_service(db_url)
    try:
        data = service.create_instance(def_key, version, environment_id=environment_id)
    except ValueError as exc:
        return _fail(str(exc))
    _emit(data, output)
    return _OK


def run_dataset_build(
    dataset_id: str, *, dry_run: bool = False, seed: int | None = None,
    batch: int | None = None, resume: bool = False, manifest_id: str | None = None,
    db_url: str | None = None, output: Path | None = None,
    service=None) -> int:
    """构建数据集（异步执行 + CLI 侧等待终态；生产环境在服务层拒绝）。"""
    service = service or _default_service(db_url)
    try:
        service.build(dataset_id, dry_run=dry_run, seed=seed, batch=batch,
                      resume=resume, manifest_id=manifest_id)
        data = _poll_until_done(service, dataset_id)
    except ValueError as exc:
        return _fail(str(exc))
    if data.get("timeout"):
        return _fail(f"构建超时未完成（taskId={dataset_id}）")
    _emit(data, output)
    if data.get("status") != "READY":
        return _fail(
            f"数据集未达 READY（status={data.get('status')}；"
            f"errorMsg={data.get('errorMsg')}）")
    return _OK


def run_dataset_status(
    dataset_id: str, *, db_url: str | None = None, output: Path | None = None,
    service=None) -> int:
    service = service or _default_service(db_url)
    try:
        data = service.get_instance(dataset_id)
    except ValueError as exc:
        return _fail(str(exc))
    _emit(data, output)
    return _OK


def run_dataset_verify(
    dataset_id: str, *, db_url: str | None = None, output: Path | None = None,
    service=None) -> int:
    """验证数据集（异步执行 + CLI 侧等待；五层验证 + READY 门禁终局）。"""
    service = service or _default_service(db_url)
    try:
        service.verify(dataset_id)
        data = _poll_until_done(service, dataset_id)
    except ValueError as exc:
        return _fail(str(exc))
    if data.get("timeout"):
        return _fail(f"验证超时未完成（taskId={dataset_id}）")
    _emit(data, output)
    if data.get("status") != "READY":
        return _fail(
            f"数据集未达 READY（status={data.get('status')}；"
            f"errorMsg={data.get('errorMsg')}）")
    return _OK


def run_dataset_list(
    *, status: str | None = None, db_url: str | None = None,
    output: Path | None = None, service=None) -> int:
    service = service or _default_service(db_url)
    data = service.list_instances(status=status)
    _emit({"instances": data, "total": len(data)}, output)
    return _OK


def run_dataset_cleanup(
    dataset_id: str, *, env_class: str, approvals_file: str | None = None,
    referenced_by_others: bool = False, partitioned: bool = False,
    rows_to_clean: int = 0, batch_plan_ready: bool = False,
    db_url: str | None = None, output: Path | None = None,
    service=None) -> int:
    """清理数据集（直连 plan_cleanup/execute_cleanup；生产/未知环境拒绝）。"""
    approvals = []
    if approvals_file:
        try:
            approvals = json.loads(Path(approvals_file).read_text(encoding="utf-8"))
        except OSError as exc:
            return _invocation_error(f"授权文件读取失败：{exc}")
        except json.JSONDecodeError as exc:
            return _invocation_error(f"授权 JSON 解析失败：{exc}")
        if not isinstance(approvals, list):
            return _invocation_error("授权文件须为 approvals JSON 数组")
    service = service or _default_service(db_url)
    from ptde.lifecycle.cleanup import CleanupBlocked

    try:
        data = service.cleanup(
            dataset_id, env_class=env_class, approvals=approvals,
            referenced_by_others=referenced_by_others, partitioned=partitioned,
            rows_to_clean=rows_to_clean, batch_plan_ready=batch_plan_ready)
    except CleanupBlocked as exc:
        return _fail(str(exc))
    except ValueError as exc:
        return _fail(str(exc))
    _emit(data, output)
    return _OK


# ---------------------------------------------------------------- A4a 存量迁移


def run_legacy_migrate(
    *, db_url: str | None = None, output: Path | None = None,
    session_factory=None) -> int:
    """A4a 存量迁移（T7.8，实施方案 §12.5）：legacy 三表 → ptde_* 幂等可重放。

    legacy 与 ptde 同库（server 栈 DB）；输出迁移前后行数核对报告；
    失败即停不留半态（单事务，异常整体回滚）。
    """
    from server.db import make_engine, make_session_factory
    from server.ptde_migration import MigrationError, run_legacy_migration

    factory = session_factory
    if factory is None:
        url = resolve_cli_db(db_url)
        engine = make_engine(url)
        factory = make_session_factory(url)
        if url.startswith("sqlite"):
            # CLI 本地库便捷建表（含 legacy 三表，便于本地演练）；生产 MySQL
            # 由 sql/init.sql 建表（不越权 DDL，_default_service 同口径）
            import server.datagen_repo  # noqa: F401  注册模型
            import server.ptde_repo  # noqa: F401  注册模型
            from server.db import Base
            Base.metadata.create_all(engine)
            from ptde.analyzer.finding_repo import FindingBase
            FindingBase.metadata.create_all(engine)
    try:
        report = run_legacy_migration(factory)
    except MigrationError as exc:
        return _fail(f"存量迁移失败即停（未提交任何变更）：{exc}")
    _emit(report, output)
    return _OK
