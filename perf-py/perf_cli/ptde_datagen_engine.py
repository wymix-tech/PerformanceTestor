"""PTDE 路由灌数引擎（B7 续批次 10：T7.6/T7.7 接管切换，实施方案 §13.2 步骤 3/4）。

legacy 灌数入口（``perf datagen run`` / ``POST /api/datagen/tasks`` /
plans execute）的执行体路由到 PTDE 真库管线：宿主解供建立后
legacy ``DataGenSpec`` 仍按原形状进入本引擎（``execute(spec, listener)``
契约不变），字段规则经 ``server.rule_translator.translate_fields`` 翻译为
PTDE 字段分布 + 关系四元组，随后走 ``OfflinePipeline``（generate → load）。

语义映射（逐项对齐 legacy ``DefaultDataGenEngine``）：
- 方言路由：``jdbc:mysql://`` 前缀（PTDE 真库通道 MySQL-first；其余前缀
  沿用 legacy「未注册匹配方言」文案失败，含 PostgreSQL——显式失败不伪装）；
- 表元数据：information_schema 内省（列名/类型/可空/主键/自增），
  缺失表 → legacy 同文案「表不存在或无可见列」；
- 自增列：不进字段清单（DB 自动分配，legacy insert_columns 同口径）；
- id 主键区间：``existing_max``（SELECT MAX(pk)）接入 IdRangeAllocator
  （T4.5 既有数据检查）——追加灌数自既有最大值 +1 续区间，取代 legacy
  ``refresh_seq_starts``「seq 起始值探测回写」的切换编排层决策落点；
- 规则翻译：``translate_fields``（orphan_rate 链：表级 → 全局 → 0；
  nullable_columns 由内省注入）；
- ref 父表在任务清单内 → 声明关系四元组（db_fk；orphan_rate>0 降级
  logical），子列外键零孤儿由 planned span 注入；
  父表不在任务清单（存量父表）→ 不声明关系（存量父表不重建），子列
  ref 分布以父表存量 ID 域（MIN/MAX）显式给出——域稠密时与 legacy
  池采样统计等价，域稀疏时的孤儿由服务层 verifyMin 如实暴露；
- seed：spec.seed 缺省时随机分配并落 definition.seed（可追溯复现，
  非不可复现伪装路径）；
- dry-run：完整内省/翻译/装配，不 generate/load；
  estimated_max_txn_ms 沿用 legacy 经验公式（每批 5ms）；
- A1 归一：legacy max_rows 钳制随接管移除（上限由入口校验层承担：
  服务层 MAX_ROWS / CLI spec 自负）。

已知差异（观察期验收按统计容差口径，交接文档留痕）：
- seq 规则落在 ``id`` 命名列 → 主键区间由 planned_range 接管
  （seq_prefix/seq_start 不生效——连续性由 existing_max +1 结构保证）；
  非 id 列 seq 语义逐值一致（row_index 上下文已接入 worker）；
- 单实体分片大小固定 2,000,000 行（单分片覆盖 legacy 规模上限）；
  超过 2M 行的多分片 sequential 列按分片内行号重启；
- 语义提示（魔法值/ORM 关联 hints）与源码扫描不在 PTDE 通道
  （A2 映射产出器为 Schema 驱动）——on_hints 恒空，建议面板不再由
  灌数任务新增建议；
- pipeline.verify 的 count 层为全表精确口径（数据集构建语义），与
  追加灌数（表内已有存量）冲突——引擎走 generate/load 两段，验证由
  服务层 verifyMin（QW3）承担；装载成功后 staging 分片文件尽力清理
  （检查点行留目标库审计）。
"""

from __future__ import annotations

import json
import random
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from ptde.models.wire import DataGenSpec, EngineListener, EngineResult, TableStat

__all__ = ["DATAGEN_ROWS_PER_SHARD", "PtdeDatagenEngine", "jdbc_url_to_dsn"]

MYSQL_JDBC_PREFIX = "jdbc:mysql://"
TABLE_NAME_OK = re.compile(r"[A-Za-z0-9_]+")
# 单实体分片大小：覆盖 legacy 规模上限（单分片下 sequential 等行号敏感
# 分布无分片内重启问题；>2M 行多分片为已知差异，见模块 docstring）
DATAGEN_ROWS_PER_SHARD = 2_000_000

# information_schema.DATA_TYPE → FieldSpec.type_name（_MYSQL_TYPES 消费侧兼容）
_TYPE_NAMES: dict[str, str] = {
    "bigint": "BIGINT",
    "int": "INT", "integer": "INT", "mediumint": "INT",
    "smallint": "SMALLINT", "tinyint": "TINYINT", "year": "SMALLINT",
    "decimal": "DECIMAL", "numeric": "DECIMAL",
    "float": "FLOAT", "double": "DOUBLE",
    "varchar": "VARCHAR", "char": "CHAR", "enum": "VARCHAR", "set": "VARCHAR",
    "text": "TEXT", "tinytext": "TEXT", "mediumtext": "TEXT", "longtext": "TEXT",
    "json": "JSON",
    "blob": "BLOB", "tinyblob": "BLOB", "mediumblob": "BLOB", "longblob": "BLOB",
    "binary": "BLOB", "varbinary": "BLOB",
    "datetime": "DATETIME", "timestamp": "TIMESTAMP", "date": "DATE",
    "time": "TIME",
    "bit": "BOOLEAN", "bool": "BOOLEAN", "boolean": "BOOLEAN",
}

_INT_PK_TYPES = frozenset({"BIGINT", "INT", "SMALLINT", "TINYINT"})


def jdbc_url_to_dsn(jdbc_url: str | None, db_user: str | None,
                    db_password: str | None) -> str:
    """JDBC URL → mysql:// DSN（PTDE 真库通道入口；非 MySQL 前缀显式失败）。

    文案沿用 legacy 引擎「未注册匹配方言」口径（契约测试断言该子串）；
    JDBC query 参数丢弃（rewriteBatchedStatements 等为 JDBC 侧语义，
    pymysql 通道按自身缺省）。
    """
    if not jdbc_url or not jdbc_url.startswith(MYSQL_JDBC_PREFIX):
        raise ValueError(
            "不支持的 JDBC URL（未注册匹配方言）: " + (jdbc_url or "")
            + "（PTDE 通道当前仅支持 jdbc:mysql://，显式失败不伪装可用）")
    rest = jdbc_url[len(MYSQL_JDBC_PREFIX):].split("?", 1)[0]
    auth = ""
    if db_user:
        from urllib.parse import quote

        auth = quote(str(db_user), safe="")
        if db_password:
            auth += ":" + quote(str(db_password), safe="")
        auth += "@"
    return f"mysql://{auth}{rest}"


@dataclass(frozen=True)
class TargetColumn:
    """目标库列元数据（information_schema 内省产物）。"""

    name: str
    type_name: str
    nullable: bool
    auto_increment: bool
    primary_key: bool


def _column_type_name(data_type: str) -> str:
    t = _TYPE_NAMES.get((data_type or "").lower())
    if t is None:
        raise ValueError(
            f"列类型 {data_type!r} 无 PTDE 类型映射（显式失败，不伪装可用）")
    return t


def _introspect_table(conn, table: str) -> list[TargetColumn]:
    """目标表列元数据（ORDINAL_POSITION 序，即装载列序，对齐 legacy meta.columns）。"""
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY, EXTRA "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "ORDER BY ORDINAL_POSITION", (table,))
        rows = cur.fetchall()
    finally:
        cur.close()
    if not rows:
        raise ValueError("表不存在或无可见列: " + table)
    cols = []
    for name, data_type, nullable, key, extra in rows:
        cols.append(TargetColumn(
            name=name,
            type_name=_column_type_name(data_type),
            nullable=str(nullable).upper() == "YES",
            auto_increment="auto_increment" in str(extra or "").lower(),
            primary_key=key == "PRI",
        ))
    return cols


def _scalar(conn, sql: str):
    cur = conn.cursor()
    try:
        cur.execute(sql)
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()


def _validate_ident(name: str, what: str) -> str:
    """标识符白名单校验（规则 JSON 直接拼 SQL 前，防注入显式失败）。"""
    if not name or not TABLE_NAME_OK.fullmatch(name):
        raise ValueError(f"{what} 仅允许字母数字下划线: {name!r}")
    return name


@dataclass
class _RunPlan:
    """_prepare 产出：翻译/内省后的管线装配输入（dry-run 亦走此路径）。"""

    dsn: str
    conn_factory: Callable[[], object]
    entities: dict[str, int]
    fields_by_entity: dict[str, list[dict]]
    relations: list[dict]
    existing_max: dict[str, int]
    seed: int
    def_name: str
    def_key: str
    first_entity: str
    last_entity: str
    warnings: list[str] = field(default_factory=list)


class PtdeDatagenEngine:
    """legacy 灌数入口的 PTDE 路由执行体（``execute(spec, listener)`` 同契约）。

    测试/装配方注入缝：``conn_factory``（目标库连接工厂，FakeConn 协议级
    验证）、``repo``（PtdeDatasetRepository，元数据库缺省自建）、
    ``metadata_url``（元数据库 SQLAlchemy URL，缺省 PERF_PY_DATABASE_URL）。
    """

    def __init__(self, *, batch_size: int = 500, workers: int = 1,
                 staging_base: str | Path | None = None,
                 repo=None, conn_factory: Callable[[], object] | None = None,
                 metadata_url: str | None = None) -> None:
        self._batch_size = max(1, batch_size)
        self._workers = max(1, workers)
        self._staging_base = staging_base
        self._repo = repo
        self._conn_factory = conn_factory
        self._metadata_url = metadata_url

    # ------------------------------------------------------------ 引擎契约

    def execute(self, spec: DataGenSpec,
                listener: EngineListener | None = None) -> EngineResult:
        if listener is None:
            listener = _NullListener()
        result = EngineResult()
        result.dry_run = bool(spec.dry_run)
        result.total_rows = sum(t.row_count for t in spec.tables)
        try:
            plan = self._prepare(spec)
        except Exception as e:  # noqa: BLE001 — 对齐 legacy 引擎顶层容错
            return self._failure(result, e)
        result.warnings.extend(plan.warnings)
        result.estimated_max_txn_ms = self._estimate(spec)
        if spec.dry_run:
            for ts in spec.tables:
                result.table_stats.append(TableStat(ts.table_name, ts.row_count))
            listener.on_progress(result.total_rows, 0, plan.first_entity)
            result.success = True
            return result
        try:
            self._run_pipeline(plan, listener, result)
        except Exception as e:  # noqa: BLE001 — 对齐 legacy 引擎顶层容错
            return self._failure(result, e)
        result.success = True
        return result

    # ------------------------------------------------------------ 装配

    def _prepare(self, spec: DataGenSpec) -> _RunPlan:
        """方言路由 → 内省 → 规则翻译 → 关系/域装配（含 existing_max 探测）。"""
        if not spec.tables:
            raise ValueError("表组为空")
        for ts in spec.tables:
            name = ts.table_name
            if name is None or not TABLE_NAME_OK.fullmatch(name):
                raise ValueError("表名仅允许字母数字下划线: " + (name or ""))
            if ts.row_count < 0:
                raise ValueError("行数非法: " + (name or ""))
        if sum(t.row_count for t in spec.tables) < 1:
            raise ValueError("表组总行数必须为正（legacy max_rows 钳制已随接管"
                             "按 A1 归一移除，上限由入口校验层承担）")

        from server.realdb_pipeline import mysql_conn_factory

        dsn = jdbc_url_to_dsn(spec.jdbc_url, spec.db_user, spec.db_password)
        factory = self._conn_factory or mysql_conn_factory(dsn)
        conn = factory()
        try:
            return self._assemble(spec, conn, dsn, factory)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _assemble(self, spec: DataGenSpec, conn, dsn: str,
                  factory: Callable[[], object]) -> _RunPlan:
        from server.rule_translator import translate_fields

        entity_names = [ts.table_name for ts in spec.tables]
        warnings: list[str] = []
        if spec.source_files:
            warnings.append("[PTDE] 源码语义扫描不适用于 PTDE 通道"
                            "（魔法值/ORM 关联提示未生成，建议面板不再由灌数任务新增）")

        # 内省（缺失表 legacy 同文案显式失败）
        metas: dict[str, list[TargetColumn]] = {}
        for name in entity_names:
            metas[name] = _introspect_table(conn, name)

        # 0 行表跳过（legacy no-op 同口径；不做 0 行实体映射）
        active = {ts.table_name: ts.row_count for ts in spec.tables
                  if ts.row_count > 0}

        # existing_max：单整型主键（自增任意名 / 非自增限 id 名）→ 追加区间
        existing_max: dict[str, int] = {}
        for name, cols in metas.items():
            if name not in active:
                continue
            pk_cols = [c for c in cols if c.primary_key]
            if len(pk_cols) != 1:
                continue
            pk = pk_cols[0]
            if not (pk.auto_increment or pk.name.lower() == "id"):
                continue
            if pk.type_name not in _INT_PK_TYPES:
                continue
            val = _scalar(conn, f"SELECT MAX({_validate_ident(pk.name, '主键列')}) "
                                f"FROM {_validate_ident(name, '表名')}")
            existing_max[name] = int(val) if val is not None else 0
            if existing_max[name] > 0:
                warnings.append(
                    f"表 {name} 已有存量主键（MAX={existing_max[name]}）："
                    "主键/外键区间自既有最大值 +1 续接（planned_range 追加语义）")

        active_set = set(active)
        relations: list[dict] = []
        fields_by_entity: dict[str, list[dict]] = {}
        for name, cols in metas.items():
            if name not in active:
                continue
            ts = next(t for t in spec.tables if t.table_name == name)
            orphan_rate = _resolve_orphan_rate(ts, spec)
            translated = translate_fields(
                ts.fields, table=name, orphan_rate=orphan_rate,
                nullable_columns=[c.name for c in cols if c.nullable])
            warnings.extend(translated.warnings)

            col_by_lower = {c.name.lower(): c for c in cols}
            # 存量父表（不在任务清单或 0 行）→ 不声明关系；子列 ref 分布
            # 以父表存量 ID 域显式给出
            for col_key, rule in ts.fields.items():
                if (rule.type or "").strip().lower() not in ("ref", "refskew"):
                    continue
                parent = _task_entity_of(rule.table or "", entity_names)
                if parent is not None and parent in active_set:
                    continue
                parent_table = _validate_ident(rule.table or "", "ref 父表")
                parent_col = _validate_ident(rule.column or "id", "ref 父列")
                lo, hi = self._parent_domain(conn, parent_table, parent_col)
                dist = translated.distributions.get(col_key)
                params = dict(dist["params"]) if dist else {}
                params["min"], params["max"] = lo, hi
                translated.distributions[col_key] = {
                    "type": "ref" if dist is not None else "uniform",
                    "params": params}
                warnings.append(
                    f"ref 父表 {parent_table} 不在任务表清单（存量父表）："
                    f"列 {col_key} 按 {parent_table}.{parent_col} 存量 ID 域 "
                    f"[{lo}, {hi}] 采样（域稀疏时孤儿由 verifyMin 如实暴露）")

            fields_by_entity[name] = [
                ({"name": c.name, "type_name": c.type_name}
                 | ({"distribution": dist}
                    if (dist := translated.distributions.get(c.name.lower())) is not None
                    else {}))
                for c in cols if not c.auto_increment]

            # 任务内父表 → 声明关系四元组（列名对齐实际大小写）
            for rel in translated.relations:
                parent = _task_entity_of(rel["parent_table"], entity_names)
                if parent is None or parent not in active_set:
                    continue
                relations.append({
                    "child_table": name,
                    "child_column": _actual_column(rel["child_column"], col_by_lower),
                    "parent_table": parent,
                    "parent_column": _actual_column(
                        rel["parent_column"],
                        {c.name.lower(): c for c in metas[parent]}),
                    "relation_kind": rel["relation_kind"],
                })

        seed = spec.seed
        if seed is None:
            seed = random.SystemRandom().randrange(1, 2_147_483_647)
            warnings.append("未显式提供 seed：已随机分配并在数据集定义中落库"
                            "（可追溯复现，非不可复现伪装路径）")
        first = entity_names[0]
        return _RunPlan(
            dsn=dsn, conn_factory=factory, entities=dict(active),
            fields_by_entity=fields_by_entity, relations=relations,
            existing_max=existing_max, seed=int(seed),
            def_name=f"datagen-{first}",
            def_key=f"datagen-{uuid.uuid4().hex[:12]}",
            first_entity=first, last_entity=entity_names[-1],
            warnings=warnings)

    @staticmethod
    def _parent_domain(conn, table: str, column: str) -> tuple[int, int]:
        """存量父表 ID 域（MIN/MAX；空表/缺表显式失败——legacy 兜底随机数
        为「伪装可用」路径，PTDE 按 A3 口径拒绝）。"""
        row = None
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT MIN({_validate_ident(column, 'ref 父列')}), "
                        f"MAX({_validate_ident(column, 'ref 父列')}) "
                        f"FROM {_validate_ident(table, 'ref 父表')} "
                        f"WHERE {_validate_ident(column, 'ref 父列')} IS NOT NULL")
            row = cur.fetchone()
        finally:
            cur.close()
        lo = row[0] if row else None
        hi = row[1] if row else None
        if lo is None or hi is None:
            raise ValueError(
                f"ref 父表 {table} 不存在或无存量数据（列 {column}）：PTDE 通道要求"
                "父表随任务一同构建或已有存量数据（显式失败，不伪装可用）")
        return int(lo), int(hi)

    # ------------------------------------------------------------ 执行

    def _run_pipeline(self, plan: _RunPlan, listener: EngineListener,
                      result: EngineResult) -> None:
        repo = self._repo or self._make_repo()
        from server.realdb_pipeline import build_realdb_pipeline

        pipeline = build_realdb_pipeline(
            repo, None, plan.fields_by_entity,
            conn_factory=plan.conn_factory,
            batch_size=self._batch_size,
            rows_per_shard=DATAGEN_ROWS_PER_SHARD,
            staging_base=self._staging_base,
            workers=self._workers,
            relations=plan.relations or None,
            existing_max=plan.existing_max or None)
        spec_payload = {"name": plan.def_name,
                        "targets": {"rows": plan.entities},
                        "reproducibility": {"seed": plan.seed}}
        spec_json = json.dumps(spec_payload, ensure_ascii=False)
        def_row_id = repo.insert_definition(
            def_key=plan.def_key, version="1", name=plan.def_name,
            spec_json=spec_json,
            spec_fingerprint=sha256(spec_json.encode("utf-8")).hexdigest(),
            seed=plan.seed)
        inst_id = repo.insert_instance(
            definition_row_id=def_row_id, definition_version="1",
            environment_label="datagen-switch")
        ctx = {"instance": {"id": str(inst_id),
                            "definition_id": f"{plan.def_key}@1"},
               "params": {}}
        pipeline.generate(ctx)
        listener.on_progress(result.total_rows, 0, plan.first_entity)
        load = pipeline.load({"instance": {"id": str(inst_id)}})
        listener.on_progress(result.total_rows, int(load["loaded_rows"]),
                             plan.last_entity)
        result.inserted_rows = int(load["loaded_rows"])
        for name, rows in plan.entities.items():
            result.table_stats.append(TableStat(name, rows))
        self._cleanup_staging(pipeline, inst_id)

    @staticmethod
    def _cleanup_staging(pipeline, inst_id: int) -> None:
        """装载成功后尽力清理 staging 分片文件（检查点行留目标库审计）。"""
        import shutil

        try:
            runtime = pipeline._require_runtime(inst_id, "cleanup")
            shutil.rmtree(runtime["staging"].root, ignore_errors=True)
        except Exception:  # noqa: BLE001 — 尽力而为
            pass

    def _make_repo(self):
        """元数据库缺省装配：PERF_PY_DATABASE_URL（缺省 sqlite 内存/本地），
        ptde_* 四表幂等建齐（生产库由 init.sql 预建，checkfirst 无害）。"""
        from sqlalchemy.orm import sessionmaker

        import server.ptde_repo  # noqa: F401  注册模型
        from server.db import Base, database_url, make_engine
        from server.ptde_repo import (
            PtdeDatasetDefinitionModel,
            PtdeDatasetInstanceModel,
            PtdeDatasetRepository,
            PtdeExecutionManifestModel,
            PtdeValidationReportModel,
        )

        engine = make_engine(self._metadata_url or database_url())
        Base.metadata.create_all(engine, tables=[
            PtdeDatasetDefinitionModel.__table__,
            PtdeDatasetInstanceModel.__table__,
            PtdeValidationReportModel.__table__,
            PtdeExecutionManifestModel.__table__,
        ])
        return PtdeDatasetRepository(
            sessionmaker(bind=engine, expire_on_commit=False))

    # ------------------------------------------------------------ 辅助

    def _estimate(self, spec: DataGenSpec) -> int:
        """legacy 经验公式：每批 5ms（批次大小取 PTDE 装载通道校准值）。"""
        max_rows = max((t.row_count for t in spec.tables), default=0)
        return (max_rows // self._batch_size) * 5

    @staticmethod
    def _failure(result: EngineResult, exc: Exception) -> EngineResult:
        result.success = False
        result.error_msg = str(exc) if str(exc) else type(exc).__name__
        return result


def _resolve_orphan_rate(ts, spec: DataGenSpec) -> float:
    """orphan_rate 缺省链（legacy 同口径）：表级 → 全局级 → 0。"""
    rate = ts.orphan_rate if ts.orphan_rate is not None else spec.orphan_rate
    return float(rate) if rate is not None else 0.0


def _task_entity_of(parent_table: str, entity_names: list[str]) -> str | None:
    """规则父表名 → 任务实体键（大小写不敏感匹配；不在清单 → None）。"""
    low = (parent_table or "").lower()
    for name in entity_names:
        if name.lower() == low:
            return name
    return None


def _actual_column(name: str, col_by_lower: dict) -> str:
    """规则列名 → 实际列名（大小写不敏感；缺失原样返回由管线显式失败）。"""
    col = col_by_lower.get((name or "").lower())
    return col.name if col is not None else (name or "")


class _NullListener(EngineListener):
    """listener 缺省时的空实现（对齐 legacy 引擎同款兜底）。"""

    def on_progress(self, total_rows, inserted_rows, current_table) -> None:
        pass

    def on_hints(self, magic_hints, relation_hints) -> None:
        pass

    def on_warn(self, message: str) -> None:
        pass
