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
- 主键区间（pk_columns，任意单整型主键）：``existing_max``（SELECT MAX(pk)）
  接入 IdRangeAllocator（T4.5 既有数据检查）——追加灌数自既有最大值 +1
  续区间，取代 legacy ``refresh_seq_starts``「seq 起始值探测回写」的
  切换编排层决策落点；
- 非整型主键（varchar GUID/编码等）：任务内构建显式失败（hash 整数伪装
  形态不可取）——须走存量父表通道（父表已有存量数据、只灌子表），父列
  类型感知后按存量真实值池抽样（外键值全部真实，形态与存量一致）；
- 规则翻译：``translate_fields``（orphan_rate 链：表级 → 全局 → 0；
  nullable_columns 由内省注入）；
- ref 父表在任务清单内 → 声明关系四元组（db_fk；orphan_rate>0 降级
  logical），子列外键零孤儿由 planned span 注入；
  父表不在任务清单（存量父表）→ 不声明关系（存量父表不重建），子列
  ref 分布以父表存量 ID 域显式给出——域稠密时 uniform 采样与 legacy
  池采样统计等价；域稀疏（失败批次回滚烧号/删数造成空洞）时按真实
  ID 全量 random 抽样；大跨度稀疏域（雪花/随机 ID 等不连续主键）按
  均匀探点统计抽样真实 ID（空洞永不命中，FK 值全部真实），不再产生孤儿；
- seed：spec.seed 缺省时随机分配并落 definition.seed（可追溯复现，
  非不可复现伪装路径）；
- dry-run：完整内省/翻译/装配，不 generate/load；
  estimated_max_txn_ms 沿用 legacy 经验公式（每批 5ms）；
- A1 归一：legacy max_rows 钳制随接管移除（上限由入口校验层承担：
  服务层 MAX_ROWS / CLI spec 自负）。

已知差异（观察期验收按统计容差口径，交接文档留痕）：
- seq 规则落在主键列（pk_columns，缺省 ``id``，非 id 名主键按内省显式
  给出）→ 主键区间由 planned_range 接管（seq_prefix/seq_start 不生效——
  连续性由 existing_max +1 结构保证）；非主键列 seq 语义逐值一致
  （row_index 上下文已接入 worker）；
- 单实体分片大小固定 2,000,000 行；>2M 行多分片 sequential 列按实体
  全局行偏移连续推进（技术债清理 2026-11-20：per-shard offset 落地）；
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

# 存量父表 ID 域稀疏时全量抽样的跨度上限（超出改走探点抽样；200k int
# 内存与内省成本可接受）
PARENT_ID_VALUES_LIMIT = 200_000
# 大跨度稀疏域（雪花/随机 ID）：探点抽样池上限与探点参数——每次探点
# ``WHERE id >= 随机点 ORDER BY id LIMIT batch`` 走主键索引，成本与表
# 大小无关；探点在 [min, max] 均匀散布，返回最近邻真实 ID（稀疏表亦可命中）
PARENT_ID_SAMPLE_SIZE = 20_000
PARENT_ID_PROBES = 2_000
PARENT_ID_PROBE_BATCH = 32

# 数值域感知的父列类型（information_schema.DATA_TYPE）：整型/数值族 →
# MIN/MAX 数值域路径；其余（varchar GUID/编码、日期等）→ 真实值池路径
_INT_DATA_TYPES = frozenset({
    "bigint", "int", "integer", "mediumint", "smallint", "tinyint", "year",
    "decimal", "numeric"})

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
    size: int = 0  # CHARACTER_MAXIMUM_LENGTH（文本列缺省生成长度口径）
    enum_candidates: tuple[str, ...] = ()  # ENUM/SET/列注释候选值（自动生成枚举口径）


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
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY, EXTRA, "
            "COALESCE(CHARACTER_MAXIMUM_LENGTH, 0), COLUMN_TYPE, COLUMN_COMMENT "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "ORDER BY ORDINAL_POSITION", (table,))
        rows = cur.fetchall()
    finally:
        cur.close()
    if not rows:
        raise ValueError("表不存在或无可见列: " + table)
    cols = []
    for row in rows:
        if len(row) >= 8:
            (name, data_type, nullable, key, extra, char_len,
             col_type, comment) = row[:8]
        else:
            name, data_type, nullable, key, extra, char_len = row[:6]
            col_type, comment = "", ""
        cols.append(TargetColumn(
            name=name,
            type_name=_column_type_name(data_type),
            nullable=str(nullable).upper() == "YES",
            auto_increment="auto_increment" in str(extra or "").lower(),
            primary_key=key == "PRI",
            size=int(char_len or 0),
            enum_candidates=_enum_candidates(data_type, col_type, comment),
        ))
    return cols


def _foreign_keys(conn, table: str) -> list[tuple[str, str, str]]:
    """目标库外键内省：[(子列, 父表, 父列)]。

    用于「字段规则自动生成」时补齐隐式外键语义：没有显式 ref 规则的 FK 列
    不再按 BIGINT 随机取值，而是按父表存量 ID 域采样，避免外键约束失败（1452）。
    外键不可读时按普通字段兜底，不阻断其余字段。
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
            "FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND REFERENCED_TABLE_NAME IS NOT NULL",
            (table,),
        )
        rows = cur.fetchall() or []
        return [(r[0], str(r[1]), str(r[2])) for r in rows]
    except Exception:  # noqa: BLE001 — 外键不可读时按普通字段兜底
        return []
    finally:
        cur.close()


def _enum_candidates(data_type: str, col_type: str, comment: str) -> tuple[str, ...]:
    """列枚举候选值（内联 ENUM/CHECK/注释，与 verifyMin 同一口径）。

    字段规则自动生成时，状态/类型等枚举列不再随机取值，
    而是按候选值生成，避免灌后枚举合法率校验不通过。
    """
    from ptde_common.enum_value_discoverer import discover

    class _EnumColumn:
        type_name = col_type or data_type

        def __init__(self, comment_value: str) -> None:
            self.comment = comment_value

    try:
        vals = discover(_EnumColumn(comment), None, None)
        return tuple(str(v) for v in vals or [])
    except Exception:  # noqa: BLE001 — 候选值发现失败按普通字段兜底
        return ()


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
    pk_columns: dict[str, str]
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
            try:
                # 关系/映射一致性门禁与正式执行同口径：OfflinePipeline 构造时
                # normalize_relations 会校验，而 dry-run 不构造 pipeline——
                # 此处直接调用同一函数（不写元数据），避免「试运行通过、
                # 正式灌数失败」的口径漂移
                from server.offline_pipeline import normalize_relations
                from server.realdb_pipeline import entity_mappings_from_fields

                mappings = entity_mappings_from_fields(
                    plan.fields_by_entity, pk_by_entity=plan.pk_columns)
                normalize_relations(plan.relations, mappings,
                                    pk_by_entity=plan.pk_columns)
            except Exception as e:  # noqa: BLE001 — 顶层同容错口径
                return self._failure(result, e)
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

        # existing_max + pk_columns：单整型主键（自增/非自增、任意列名——
        # 技术债清理：非 id 名非自增主键 planned_range 泛化）→ 追加区间
        existing_max: dict[str, int] = {}
        pk_columns: dict[str, str] = {}
        for name, cols in metas.items():
            if name not in active:
                continue
            pk_cols = [c for c in cols if c.primary_key]
            if len(pk_cols) != 1:
                continue
            pk = pk_cols[0]
            if pk.type_name not in _INT_PK_TYPES:
                # 非整型单列主键（varchar GUID/编码等）：任务内构建唯一路径是
                # hash 派生整数写入该列（形态错误且子表外键无法引用一致值）——
                # 显式失败并指引存量父表通道（A3：不伪装可用）
                raise ValueError(
                    f"表 {name} 主键列 {pk.name} 为非整型（{pk.type_name}）："
                    "任务内构建仅支持整型主键（planned_range 连续区间语义）。"
                    "非整型主键表请勿纳入任务表组——确保其已有存量数据后只灌"
                    "子表，子表外键将按父表存量真实值自动抽样")
            pk_columns[name] = pk.name
            val = _scalar(conn, f"SELECT MAX({_validate_ident(pk.name, '主键列')}) "
                                f"FROM {_validate_ident(name, '表名')}")
            existing_max[name] = int(val) if val is not None else 0
            if existing_max[name] > 0:
                warnings.append(
                    f"表 {name} 已有存量主键（MAX={existing_max[name]}）："
                    "主键/外键区间自既有最大值 +1 续接（planned_range 追加语义）")
            if not pk.auto_increment:
                warnings.append(
                    f"表 {name} 非自增主键列 {pk.name} 由 planned_range 接管"
                    "（连续区间自既有最大值 +1，规则/缺省生成不再作用于该列）")

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
                dist = translated.distributions.get(col_key)
                params = dict(dist["params"]) if dist else {}
                has_semantics = any(float(params.get(k) or 0) != 0
                                    for k in ("hot_percent", "hot_share",
                                              "orphan_rate", "orphan_null_ratio"))
                if not has_semantics:
                    # 无热行/孤儿语义 → 与自动 FK 同口径：稀疏域按真实 ID 全量抽样
                    pool, note = self._parent_id_pool(conn, parent_table, parent_col)
                    translated.distributions[col_key] = pool
                    warnings.append(
                        f"ref 父表 {parent_table} 不在任务表清单（存量父表）："
                        f"列 {col_key} 按 {parent_table}.{parent_col} {note}")
                    continue
                lo, hi = self._parent_domain(conn, parent_table, parent_col)
                params["min"], params["max"] = lo, hi
                translated.distributions[col_key] = {
                    "type": "ref" if dist is not None else "uniform",
                    "params": params}
                warnings.append(
                    f"ref 父表 {parent_table} 不在任务表清单（存量父表）："
                    f"列 {col_key} 按 {parent_table}.{parent_col} 存量 ID 域 "
                    f"[{lo}, {hi}] 采样（域稀疏时孤儿由 verifyMin 如实暴露）")

            # 字段规则自动生成时，数据库外键列按外键语义处理：
            # - 父表在任务清单 → 声明关系，由 pipeline 按父表 planned span 生成；
            # - 父表有存量数据 → 按父表存量 ID 域采样；
            # - 父表不存在/空表 → 显式失败，避免随机 BIGINT 违反外键约束。
            for child_col, parent_table, parent_col in _foreign_keys(conn, name):
                col_key = child_col.lower()
                if col_key in ts.fields or col_key not in col_by_lower:
                    continue
                parent = _task_entity_of(parent_table, entity_names)
                if parent is not None and parent in active_set:
                    relations.append({
                        "child_table": name,
                        "child_column": child_col,
                        "parent_table": parent,
                        "parent_column": _actual_column(
                            parent_col, {c.name.lower(): c for c in metas[parent]}),
                        "relation_kind": "db_fk"})
                    warnings.append(
                        f"数据库外键 {name}.{child_col} → {parent_table}.{parent_col}："
                        f"字段规则自动生成，父表在任务清单，按 planned span 生成")
                    continue
                pool, note = self._parent_id_pool(conn, parent_table, parent_col)
                translated.distributions[col_key] = pool
                warnings.append(
                    f"数据库外键 {name}.{child_col} → {parent_table}.{parent_col}："
                    f"字段规则自动生成，按 {parent_table}.{parent_col} {note}")

            # 数据库未声明外键 → 自动生成口径按命名约定推断逻辑外键：
            # <stem>_<子表主键名> 列 → 同库父表（stem/单复数归一，须为存量表）
            # ——父表不重建，子列按父表真实值池/稠密域抽样（数据业务可关联）；
            # 任务内父表/自引用/多候选不猜测，普通字段兜底并留痕
            skip_keys = {k.lower() for k in ts.fields} | set(translated.distributions)
            for item in self._infer_naming_fk(conn, name, cols, skip_keys,
                                              active_set):
                if item["dist"] is not None:
                    translated.distributions[item["col_key"]] = item["dist"]
                warnings.append(item["note"])

            # 字段规则自动生成时，枚举/状态/类型列按 DB 候选值生成，
            # 避免随机文本导致灌后枚举合法率校验不通过
            for c in cols:
                col_key = c.name.lower()
                if col_key in ts.fields or not c.enum_candidates:
                    continue
                translated.distributions[col_key] = {
                    "type": "random", "params": {"values": list(c.enum_candidates)}}
                warnings.append(
                    f"枚举候选值 {name}.{c.name}：字段规则自动生成，"
                    f"按候选值 {','.join(c.enum_candidates)} 生成")

            fields_by_entity[name] = [
                ({"name": c.name, "type_name": c.type_name,
                  "size": c.size or 0}
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
            existing_max=existing_max, pk_columns=pk_columns, seed=int(seed),
            def_name=f"datagen-{first}",
            def_key=f"datagen-{uuid.uuid4().hex[:12]}",
            first_entity=first, last_entity=entity_names[-1],
            warnings=warnings)

    @classmethod
    def _parent_domain(cls, conn, table: str, column: str) -> tuple[int, int]:
        """存量父表 ID 域（MIN/MAX；空表/缺表显式失败——legacy 兜底随机数
        为「伪装可用」路径，PTDE 按 A3 口径拒绝）。

        非整型父列显式失败：MIN/MAX 区间语义（含 refSkew 热行/orphan 参数）
        不适用于 GUID/编码等形态——去除相关参数后自动走真实值池抽样。
        """
        if cls._parent_column_kind(conn, table, column) != "int":
            raise ValueError(
                f"ref 父列 {table}.{column} 为非整型：MIN/MAX 区间语义（热行/"
                "孤儿参数）不适用——去除 ref/refSkew 的 hot/orphan 参数后，"
                "将自动按父表存量真实值池抽样")
        lo, hi, _ = cls._parent_domain_stats(conn, table, column)
        return lo, hi

    @classmethod
    def _parent_domain_stats(cls, conn, table: str, column: str) -> tuple[int, int, int]:
        """存量父表 ID 域统计（MIN/MAX/COUNT 单往返；空表/缺表显式失败）。"""
        row = None
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT MIN({_validate_ident(column, 'ref 父列')}), "
                        f"MAX({_validate_ident(column, 'ref 父列')}), "
                        f"COUNT({_validate_ident(column, 'ref 父列')}) "
                        f"FROM {_validate_ident(table, 'ref 父表')} "
                        f"WHERE {_validate_ident(column, 'ref 父列')} IS NOT NULL")
            row = cur.fetchone()
        finally:
            cur.close()
        lo = row[0] if row else None
        hi = row[1] if row else None
        cnt = row[2] if row and len(row) > 2 else None
        if lo is None or hi is None:
            raise ValueError(
                f"ref 父表 {table} 不存在或无存量数据（列 {column}）：PTDE 通道要求"
                "父表随任务一同构建或已有存量数据（显式失败，不伪装可用）")
        return int(lo), int(hi), int(cnt or 0)

    @classmethod
    def _parent_id_pool(cls, conn, table: str, column: str) -> tuple[dict, str]:
        """存量父表 ID 域 → 子列外键采样分布（1452 防线：不产生孤儿）。

        - 非整型父列（varchar GUID/编码、日期等）→ **真实值池** ``random
          values`` 抽样（``_parent_value_pool``；值形态与存量一致）；
        - 整型父列：域稠密（行数 = 跨度）→ ``uniform [min, max]``（legacy
          池采样统计等价，成本最低）；
        - 域稀疏小跨度（失败批次回滚烧号/删数造成空洞——uniform 必中
          空洞触发 1452）→ 跨度 ≤ ``PARENT_ID_VALUES_LIMIT`` 时按**真实
          ID 全量** ``random values`` 抽样（空洞永不命中）；
        - 大跨度稀疏域（雪花/随机 ID 等不连续主键）→ 均匀探点邻近回填
          ``random values`` 统计抽样（``_probe_parent_ids``，池上限
          ``PARENT_ID_SAMPLE_SIZE``；FK 值全部真实，分布为抽样口径并留痕）。
        """
        if cls._parent_column_kind(conn, table, column) != "int":
            return cls._parent_value_pool(conn, table, column)
        lo, hi, cnt = cls._parent_domain_stats(conn, table, column)
        span = hi - lo + 1
        if cnt >= span:
            return ({"type": "uniform", "params": {"min": lo, "max": hi}},
                    f"存量 ID 域 [{lo}, {hi}] 均匀采样（{cnt} 行，域稠密）")
        if span <= PARENT_ID_VALUES_LIMIT:
            ids = cls._fetch_parent_ids(conn, table, column)
            return ({"type": "random", "params": {"values": ids}},
                    f"存量 ID 域稀疏（{cnt} 行 / 跨度 {span}，含空洞）→ "
                    f"按 {len(ids)} 个真实 ID 全量抽样")
        ids = cls._probe_parent_ids(conn, table, column, lo, hi)
        if not ids:
            raise ValueError(
                f"ref 父表 {table} 存量 ID 探点抽样未命中任何行（列 {column}，"
                f"{cnt} 行 / 跨度 {span}）：显式失败，不伪装可用")
        return ({"type": "random", "params": {"values": ids}},
                f"大跨度稀疏 ID 域（{cnt} 行 / 跨度 {span}，雪花/随机形态）→ "
                f"均匀探点统计抽样 {len(ids)} 个真实 ID（FK 值全部真实，"
                "分布为抽样口径）")

    @staticmethod
    def _parent_column_kind(conn, table: str, column: str) -> str:
        """父列类型感知（``"int"`` 数值域 / ``"value"`` 真实值池）。

        内省缺失（父列不可读/无行）缺省 ``int``（legacy MIN/MAX 口径；
        空表/缺表由域查询显式失败兜底），不阻断既有路径。
        """
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
                "AND COLUMN_NAME = %s", (table, column))
            rows = cur.fetchall() or []
        except Exception:  # noqa: BLE001 — 类型不可读按 legacy 数值口径兜底
            return "int"
        finally:
            cur.close()
        for r in rows:
            if str(r[0]).lower() == (column or "").lower():
                return "int" if str(r[1]).lower() in _INT_DATA_TYPES else "value"
        return "int"

    @classmethod
    def _parent_value_pool(cls, conn, table: str, column: str) -> tuple[dict, str]:
        """非整型父列（GUID/编码/日期等）→ 存量真实值池 ``random`` 抽样。

        值形态与存量一致（字符串原样传递，采样器直取）；存量超上限时按
        ``ORDER BY`` 序截断为统计口径并留痕。空表/缺表显式失败。
        """
        col = _validate_ident(column, "ref 父列")
        tbl = _validate_ident(table, "ref 父表")
        cnt = 0
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT COUNT({col}) FROM {tbl} WHERE {col} IS NOT NULL")
            row = cur.fetchone()
            cnt = int(row[0] or 0) if row else 0
        finally:
            cur.close()
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT {col} FROM {tbl} WHERE {col} IS NOT NULL "
                        f"ORDER BY {col} LIMIT {PARENT_ID_VALUES_LIMIT}")
            values = [str(r[0]) for r in (cur.fetchall() or [])]
        finally:
            cur.close()
        if not values:
            raise ValueError(
                f"ref 父表 {table} 不存在或无存量数据（列 {column}）：PTDE 通道要求"
                "父表随任务一同构建或已有存量数据（显式失败，不伪装可用）")
        if cnt <= len(values):
            note = (f"非整型父列（{cnt} 个存量值）→ 按真实值全量抽样")
        else:
            note = (f"非整型父列（存量 {cnt} 个 > 上限 {PARENT_ID_VALUES_LIMIT}）"
                    f"→ 按真实值截断抽样 {len(values)} 个（统计口径）")
        return {"type": "random", "params": {"values": values}}, note

    @staticmethod
    def _probe_parent_ids(conn, table: str, column: str,
                          lo: int, hi: int) -> list[int]:
        """大跨度稀疏域真实 ID 抽样：均匀探点 + 邻近回填（主键索引范围扫）。

        每次探点 ``WHERE col >= 随机点 ORDER BY col LIMIT batch`` 返回最近邻
        真实 ID——稀疏表亦可命中（只要探点 ≤ 当前最大 ID）；探点种子由
        表/列名派生（同库同表状态可复现，不受进程哈希随机化影响）。
        """
        import random as _random

        digest = sha256(f"{table}.{column}".encode("utf-8")).digest()
        rng = _random.Random(int.from_bytes(digest[:8], "big"))
        pool: set[int] = set()
        cur = conn.cursor()
        try:
            for _ in range(PARENT_ID_PROBES):
                cur.execute(
                    f"SELECT {_validate_ident(column, 'ref 父列')} "
                    f"FROM {_validate_ident(table, 'ref 父表')} "
                    f"WHERE {_validate_ident(column, 'ref 父列')} >= %s "
                    f"ORDER BY {_validate_ident(column, 'ref 父列')} "
                    f"LIMIT {PARENT_ID_PROBE_BATCH}", (rng.randint(lo, hi),))
                for r in (cur.fetchall() or []):
                    pool.add(int(r[0]))
                if len(pool) >= PARENT_ID_SAMPLE_SIZE:
                    break
        finally:
            cur.close()
        return sorted(pool)

    @staticmethod
    def _fetch_parent_ids(conn, table: str, column: str) -> list[int]:
        """存量父表真实 ID 全量（DISTINCT 去重升序；稀疏域抽样池）。"""
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT DISTINCT {_validate_ident(column, 'ref 父列')} "
                        f"FROM {_validate_ident(table, 'ref 父表')} "
                        f"WHERE {_validate_ident(column, 'ref 父列')} IS NOT NULL "
                        f"ORDER BY {_validate_ident(column, 'ref 父列')}")
            return [int(r[0]) for r in (cur.fetchall() or [])]
        finally:
            cur.close()

    # ---------------------------------------------- 命名推断（无数据库外键）

    def _infer_naming_fk(self, conn, table: str, cols,
                         skip_keys: set[str], task_tables: set[str]) -> list[dict]:
        """无数据库外键时的逻辑外键命名推断（自动生成口径）。

        命名约定（naming_resolver 单复数语义）：列 ``<stem>_<子表主键名>``
        → 同库父表 ``stem``/单数/复数；推断成立须：
        - 子列为整数族（防编码/时间列误判，resolver 同口径）；
        - 父表不在任务表组（任务内父表走 DB 外键/ref 规则通道声明关系）、
          非自引用（自引用装载序显式失败，不猜测）、候选唯一（多候选不猜）；
        - 父表有主键列（任意命名——按内省实际 PRI 列）且有存量数据。
        满足 → 子列按父表 ``_parent_id_pool`` 抽样（真实值/稠密域）；
        不满足 → dist=None 仅留痕，普通字段兜底不阻断。
        """
        from ptde.analyzer.naming_resolver import plural, singular

        out: list[dict] = []
        child_pk = next((c.name for c in cols if c.primary_key), "id")
        tables = self._db_tables(conn)
        if not tables:
            return out
        suffix = "_" + (child_pk or "id").lower()
        task_lower = {t.lower() for t in task_tables}
        for c in cols:
            if c.auto_increment or c.primary_key:
                continue
            col_key = c.name.lower()
            if col_key in skip_keys:
                continue
            if c.type_name not in ("BIGINT", "INT", "SMALLINT", "TINYINT"):
                continue
            col_l = col_key
            if not col_l.endswith(suffix) or col_l == suffix:
                continue
            stem = col_l[:-len(suffix)]
            if not stem:
                continue
            # 候选匹配：精确 / 单复数（resolver 语义）+ 常见前缀形态
            # （t_user / perf_user / sys_user —— "表名以 _<stem> 结尾"）
            forms = {stem, singular(stem), plural(stem)}
            prefixed = {"_" + f for f in forms}
            candidates = sorted({t for t in tables
                                 if t.lower() in forms
                                 or t.lower().endswith(tuple(prefixed))})
            if not candidates:
                continue
            if len(candidates) > 1:
                out.append({"col_key": col_key, "dist": None,
                            "note": f"命名推断 {table}.{c.name}：候选父表不唯一"
                                    f"（{'、'.join(candidates)}），不猜测——按普通字段生成"})
                continue
            parent_table = candidates[0]
            if parent_table.lower() == table.lower():
                continue  # 自引用：装载序显式失败语义，不猜测
            if parent_table.lower() in task_lower:
                continue  # 任务内父表：由 DB 外键/ref 规则通道声明关系
            parent_pk = self._parent_pk_column(conn, parent_table)
            if parent_pk is None:
                out.append({"col_key": col_key, "dist": None,
                            "note": f"命名推断 {table}.{c.name} → {parent_table}："
                                    "父表无主键列，按普通字段生成"})
                continue
            try:
                dist, note = self._parent_id_pool(conn, parent_table, parent_pk)
            except ValueError as exc:
                out.append({"col_key": col_key, "dist": None,
                            "note": f"命名推断 {table}.{c.name} → "
                                    f"{parent_table}.{parent_pk}：父表无存量数据，"
                                    f"按普通字段生成（{exc}）"})
                continue
            out.append({"col_key": col_key, "dist": dist,
                        "note": f"数据库未声明外键：{table}.{c.name} 按命名约定推断"
                                f"逻辑外键 → {parent_table}.{parent_pk}：{note}"
                                "（父表为存量表不重建，verify 关系层不核对）"})
        return out

    @staticmethod
    def _db_tables(conn) -> list[str]:
        """目标库基表清单（命名推断候选域；不可读 → 空，不阻断）。"""
        cur = conn.cursor()
        try:
            cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA = DATABASE() "
                        "AND TABLE_TYPE = 'BASE TABLE'")
            return [str(r[0]) for r in (cur.fetchall() or [])]
        except Exception:  # noqa: BLE001 — 表清单不可读按无候选兜底
            return []
        finally:
            cur.close()

    @staticmethod
    def _parent_pk_column(conn, table: str) -> str | None:
        """父表主键列名（任意命名——内省实际 PRI 列；无/不可读 → None）。"""
        cur = conn.cursor()
        try:
            cur.execute("SELECT COLUMN_NAME, COLUMN_KEY "
                        "FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
                        "ORDER BY ORDINAL_POSITION", (table,))
            rows = cur.fetchall() or []
        except Exception:  # noqa: BLE001 — 内省不可读按无主键兜底
            return None
        finally:
            cur.close()
        for r in rows:
            # 行形态兼容：真实内省 2 元组 (name, key)；宿主宽行内省含 key 列
            cells = [str(x).upper() for x in r[1:]]
            if "PRI" in cells:
                return str(r[0])
        return None

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
            existing_max=plan.existing_max or None,
            pk_columns=plan.pk_columns or None)
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
