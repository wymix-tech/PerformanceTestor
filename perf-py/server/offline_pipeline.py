"""数据集构建执行体——离线组装（B7 续，交接 §7「可立即推进项」第 1 项）。

把 ptde 生成/装载/验证侧组件组装到 ``DatasetLifecycleService`` 的
``pipeline`` 注入缝（generate/load/verify 三段 + cleanup_executor）：

- **generate**：``build_plan`` 离线规划 → ``build_sharding_plan`` 分片 →
  ``shard_tasks_from_plan`` 装配 ShardTask → ``run_shards``（generator
  worker，只产数据不持连接）→ StagingArea 分片文件 + 检查点；resume 走
  ``staging.restore()`` 断点续跑（只补 pending 分片）；
- **load**：注入 conn（离线 = sqlite 文件库，真库 = MySQL，A2 批次换入）→
  实体建表（映射注入 DDL）→ FallbackLoader（``loader_chain`` 可注入原生
  装载链；缺省 DbApiBatchInsertLoader 降级档）→
  LoadOrchestrator 依赖序装载 + CheckpointCoordinator 检查点；
  装载段取消安全点：入口 + 实体步骤间（续批次 3 §4 决策 3 协议，
  已装载实体经 DB 检查点保留，协作式退出不静默丢弃）；
- **verify**：五层验证器（count/relationship/distribution/business/db_ready，
  真组件零重造）→ ValidationReport + ExecutionManifest（§12.4 全要素回填：
  backfill_from_staging / 指纹组 / ID 区间 / Adapter 能力快照）+
  CleanupContext（清理决策输入）；关系层经 ``relations`` 注入缝接入实映射
  （RelationModel 四元组；B7 续批次 8 前 verify 关系层显式空集）；
- **cleanup_executor**：按清理档位执行（SCHEMA_REBUILD → 重建；
  其余档 → 清表），破坏性动作经服务层 SecurityGate CLEANUP 授权。

映射缝（映射知识在生成侧，不在此重造）：实体字段规格与建表 DDL 由调用方
经 ``EntityMapping`` 注入——离线测试注入简单映射，真库批次由 Schema 映射
（A2）产出同一缝的映射。映射缺失/与分片计划不一致显式失败（A3 口径）。

sqlite 通道适配（fake DB 通道，不伪装成生产通道）：
- ``_QmarkConn``：DBAPI paramstyle ``%s`` → pysqlite ``?``（装载语句形态
  不变，仅占位符适配）；
- ``SqliteCheckpointStore``：检查点仓 sqlite 兼容实现（协议接口对齐
  DbCheckpointStore：upsert/get/list_for_instance）；
- db_ready 层离线产物含 [未执行] WARN（统计信息/端点抽检），接线时须在
  ``DatasetLifecycleService(degradable_layers=...)`` 显式配置（§9.4：WARN
  放行由装配方决定，默认不放宽）。

环境口径：离线通道按 dedicated_test_db（专用测试库档）组装；真库批次按
目标环境类别替换。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import ptde
from ptde.adapters.base import BulkLoadUnsupported
from ptde.adapters.bulkload.fallback import DbApiBatchInsertLoader, FallbackLoader
from ptde.generator.partitioner import build_sharding_plan
from ptde.generator.samplers.ref import warn_hot_row_share
from ptde.generator.staging import StagingArea, default_staging_root
from ptde.generator.worker import (
    FieldSpec,
    plan_row_factory,
    run_shard,
    run_shards,
    shard_tasks_from_plan,
)
from ptde.lifecycle.cleanup import CleanupContext, CleanupTier
from ptde.lifecycle.manifest import backfill_from_staging
from ptde.loader.checkpoint import CheckpointCoordinator, ShardCheckpoint
from ptde.loader.orchestrator import LoadOrchestrator
from ptde.models.dataset import ValidationReport
from ptde.models.entity import RelationModel
from ptde.models.profile import DistributionFit
from ptde.models.spec import DatasetSpec, ExecutionManifest, IdRange, ModelFingerprints
from ptde.planner.planner import build_plan
from ptde.validator import validate_identifier
from ptde.validator.count_validator import validate_counts
from ptde.validator.db_ready_validator import validate_db_ready
from ptde.validator.distribution_validator import validate_distributions
from ptde.validator.invariant_validator import validate_invariants
from ptde.validator.relationship_validator import validate_relationships
from server.dataset_service import DatasetCancelled
from server.ptde_repo import PtdeDatasetRepository

# 推断关系 kind 集（与 relationship_validator._INFERRED_KINDS 同口径；
# 故意孤儿只允许配推断关系——verify 降级 WARN 如实暴露）
_INFERRED_RELATION_KINDS = frozenset({"logical"})

__all__ = [
    "EntityMapping",
    "OfflinePipeline",
    "OfflinePipelineError",
    "SqliteCheckpointStore",
    "normalize_relations",
]

# sqlite 检查点表（列集对齐 DbCheckpointStore.DDL_PTDE_SHARD_CHECKPOINT；
# 主键 = uk_shard_chunk 同款唯一键，sqlite 方言重写）
_SQLITE_CHECKPOINT_DDL = (
    "CREATE TABLE IF NOT EXISTS ptde_shard_checkpoint ("
    "  instance_id INTEGER NOT NULL,"
    "  table_name TEXT NOT NULL,"
    "  shard_id TEXT NOT NULL,"
    "  chunk_id INTEGER NOT NULL DEFAULT 0,"
    "  expected_rows INTEGER NOT NULL,"
    "  generated_rows INTEGER NOT NULL DEFAULT 0,"
    "  loaded_rows INTEGER NOT NULL DEFAULT 0,"
    "  checksum TEXT,"
    "  last_chunk INTEGER NOT NULL DEFAULT 0,"
    "  submitted_state TEXT NOT NULL DEFAULT 'PENDING',"
    "  db_feedback_json TEXT,"
    "  error_msg TEXT,"
    "  PRIMARY KEY (instance_id, table_name, shard_id, chunk_id))"
)

_CHECKPOINT_COLUMNS = (
    "instance_id, table_name, shard_id, chunk_id, expected_rows, "
    "generated_rows, loaded_rows, checksum, last_chunk, "
    "submitted_state, db_feedback_json, error_msg"
)


class OfflinePipelineError(Exception):
    """离线执行体组装/执行违规（显式失败，不伪装可用）。"""


@dataclass(frozen=True)
class EntityMapping:
    """单实体映射（生成/装载/建表共用；映射知识由调用方注入）。

    - ``fields``：分片字段规格（列序 = 分片文件列序 = 装载列序；
      ``id`` 列由 worker 主键区间逻辑产出，也须列入）；
    - ``ddl``：建表语句（离线通道执行；``CREATE TABLE IF NOT EXISTS`` 形态）。
    """

    entity: str
    fields: tuple[FieldSpec, ...]
    ddl: str

    def __post_init__(self) -> None:
        validate_identifier(self.entity)
        if not self.fields:
            raise OfflinePipelineError(
                f"实体 {self.entity} 映射无字段（显式失败——空映射不得伪装可装载）")
        for f in self.fields:
            validate_identifier(f.name)
        if not self.ddl.strip().upper().startswith("CREATE TABLE"):
            raise OfflinePipelineError(
                f"实体 {self.entity} 建表语句须为 CREATE TABLE 形态（显式失败）")

    @property
    def columns(self) -> list[str]:
        return [f.name for f in self.fields]


def normalize_relations(
    relations: Sequence[RelationModel | Mapping] | None,
    mapping: Mapping[str, EntityMapping],
    pk_by_entity: Mapping[str, str] | None = None,
) -> list[RelationModel]:
    """关系规格归一化 + 显式校验（B7 续批次 8：关系映射接线缝）。

    - dict（JSON 原生四元组）→ ``RelationModel.model_validate``；
    - 四元组完整性、标识符合法性、子/父表与列均在实体映射内逐项校验
      （缺失/不一致显式失败，不静默降级为空集）；
    - 子列不得为该实体的 planned_range 主键列（pk_columns，缺省 ``id``；
      主键区间语义列不承载外键）。
    """
    normalized: list[RelationModel] = []
    for r in list(relations or []):
        rel = r if isinstance(r, RelationModel) else RelationModel.model_validate(dict(r))
        for role, table, col in (("子", rel.child_table, rel.child_column),
                                 ("父", rel.parent_table, rel.parent_column)):
            if not table or not col:
                raise OfflinePipelineError(
                    f"关系四元组不完整（{role}表/列为空，显式失败）")
            validate_identifier(table)
            validate_identifier(col)
            m = mapping.get(table)
            if m is None:
                raise OfflinePipelineError(
                    f"关系引用未映射实体 {table!r}（EntityMapping 缺失，显式失败）")
            if col not in m.columns:
                raise OfflinePipelineError(
                    f"关系列 {table}.{col} 不在实体映射字段中（清单不一致，显式失败）")
        pk_col = (pk_by_entity or {}).get(rel.child_table, "id")
        if rel.child_column == pk_col:
            raise OfflinePipelineError(
                f"关系子列不得为 {rel.child_table} 的主键区间语义列 {pk_col!r}"
                "（planned_range 接管列不承载外键，显式失败）")
        normalized.append(rel)
    return normalized


class SqliteCheckpointStore:
    """检查点仓 sqlite 实现（协议接口对齐 DbCheckpointStore；离线通道专用）。

    upsert 语义 = INSERT OR REPLACE（uk_shard_chunk 同款唯一键上覆盖）。
    """

    def __init__(self, conn, *, commit: bool = True) -> None:
        self._conn = conn
        self._commit = commit
        cur = conn.cursor()
        try:
            cur.execute(_SQLITE_CHECKPOINT_DDL)
        finally:
            cur.close()
        if commit:
            conn.commit()

    def upsert(self, cp: ShardCheckpoint) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT OR REPLACE INTO ptde_shard_checkpoint ({_CHECKPOINT_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cp.instance_id, cp.table_name, cp.shard_id, cp.chunk_id,
                    cp.expected_rows, cp.generated_rows, cp.loaded_rows,
                    cp.checksum, cp.last_chunk, cp.submitted_state,
                    json.dumps(cp.db_feedback, ensure_ascii=False)
                    if cp.db_feedback is not None else None,
                    cp.error_msg,
                ),
            )
        finally:
            cur.close()
        if self._commit:
            self._conn.commit()

    def get(self, instance_id, table_name, shard_id, chunk_id=0):
        row = self._fetch_one(
            "WHERE instance_id = ? AND table_name = ? AND shard_id = ? AND chunk_id = ?",
            (instance_id, table_name, shard_id, chunk_id))
        return self._row_to_cp(row) if row else None

    def list_for_instance(self, instance_id, table_name=None):
        if table_name is None:
            rows = self._fetch_all("WHERE instance_id = ?", (instance_id,))
        else:
            rows = self._fetch_all(
                "WHERE instance_id = ? AND table_name = ?", (instance_id, table_name))
        return [self._row_to_cp(r) for r in rows]

    # ------------------------------------------------------------ 内部

    def _fetch_one(self, where: str, params: tuple):
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"SELECT {_CHECKPOINT_COLUMNS} FROM ptde_shard_checkpoint {where}",
                params)
            return cur.fetchone()
        finally:
            cur.close()

    def _fetch_all(self, where: str, params: tuple) -> list:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"SELECT {_CHECKPOINT_COLUMNS} FROM ptde_shard_checkpoint {where} "
                "ORDER BY table_name, shard_id, chunk_id", params)
            return list(cur.fetchall())
        finally:
            cur.close()

    @staticmethod
    def _row_to_cp(row) -> ShardCheckpoint:
        return ShardCheckpoint(
            instance_id=int(row[0]), table_name=row[1], shard_id=row[2],
            chunk_id=int(row[3]), expected_rows=int(row[4]),
            generated_rows=int(row[5]), loaded_rows=int(row[6]),
            checksum=row[7], last_chunk=int(row[8]), submitted_state=row[9],
            db_feedback=json.loads(row[10]) if row[10] else None,
            error_msg=row[11],
        )


class _QmarkCursor:
    """DBAPI paramstyle 适配游标：``%s`` → pysqlite ``?``（语句形态不变）。"""

    def __init__(self, cur) -> None:
        self._cur = cur

    @staticmethod
    def _q(sql: str) -> str:
        return sql.replace("%s", "?")

    def execute(self, sql, params=()):
        return self._cur.execute(self._q(sql), tuple(params or ()))

    def executemany(self, sql, batch):
        return self._cur.executemany(self._q(sql), batch)

    def __getattr__(self, name):  # fetchone/fetchall/close 等透传
        return getattr(self._cur, name)


class _QmarkConn:
    """paramstyle 适配连接（离线 sqlite 通道；DbApiBatchInsertLoader 直用）。"""

    def __init__(self, conn) -> None:
        self._conn = conn

    def cursor(self):
        return _QmarkCursor(self._conn.cursor())

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()


class OfflinePipeline:
    """离线组装执行体（generate/load/verify 三段落 pipeline 缝，可测可用）。

    构造参数：
    - ``repo``：数据集落库层（Definition spec 取数：ctx 仅含 def_key@version）；
    - ``conn_factory``：目标库 DBAPI 连接工厂（离线 = sqlite 文件库；真库
      批次注入 MySQL 连接工厂 + DbCheckpointStore，编排不变）；
    - ``entity_mapping``：实体映射（EntityMapping，映射知识注入缝）；
    - ``product``/``batch_size``：装载产品标识与批量（FallbackLoader 组装）；
    - ``loader_chain``：装载链显式注入（缺省 None = 既有单降级档
      DbApiBatchInsertLoader；真库通道注入 ``default_chain_for_product``）
      ——原生档优先、逐档探测降级；
    - ``rows_per_shard``：分片大小（build_sharding_plan）；
    - ``staging_base``：staging 根目录基座（缺省 /tmp/ptde_offline）；
    - ``workers``：生成 Worker 数（离线默认单进程，跨进程复现一致）；
    - ``env_class``：清理决策环境类别（离线通道 = dedicated_test_db）；
    - ``checkpoint_store_factory``：检查点仓工厂（缺省 SqliteCheckpointStore；
      真库批次注入 ``lambda conn: DbCheckpointStore(conn)``）；
    - ``endpoint_probe``：db_ready 层 selfCheck 端点抽检回调（B6 §7F 接线缝：
      ``datagen_service.self_check_probe(repo_id)`` 适配产出，协议
      ``() -> {"checked": n, "nonEmpty": m}``；未提供 → [未执行] WARN，
      与既有口径一致）；
    - ``relations``：表间关系规格（RelationModel 或 JSON 原生四元组 dict，
      B7 续批次 8 接线缝；缺省 None = 空集，verify 关系层退回既有口径）。
      声明后生成侧自动为子表外键列推导父实体主键区间采样（见
      ``_apply_fk_fields``），verify 关系层全量零孤儿核对按实映射执行。
    - ``pk_columns``：各实体 planned_range 接管的主键列名（缺省 ``id``；
      非 id 名非自增主键由调用方按 Schema 显式给出——技术债清理泛化）。
    """

    def __init__(
        self,
        repo: PtdeDatasetRepository,
        conn_factory: Callable[[], object],
        entity_mapping: Mapping[str, EntityMapping],
        *,
        product: str = "sqlite",
        batch_size: int = 500,
        loader_chain: Sequence[object] | None = None,
        rows_per_shard: int = 250_000,
        staging_base: str | Path | None = None,
        workers: int = 1,
        env_class: str = "dedicated_test_db",
        checkpoint_store_factory: Callable[[object], object] | None = None,
        endpoint_probe: Callable[[], dict[str, int]] | None = None,
        relations: Sequence[RelationModel | Mapping] | None = None,
        existing_max: Mapping[str, int] | None = None,
        pk_columns: Mapping[str, str] | None = None,
    ) -> None:
        self._repo = repo
        self._conn_factory = conn_factory
        self._mapping = {
            k: (v if isinstance(v, EntityMapping) else EntityMapping(**v))
            for k, v in dict(entity_mapping).items()
        }
        if not self._mapping:
            raise OfflinePipelineError("entity_mapping 为空（映射缝不得留空伪装可用）")
        # 各实体 planned_range 接管的主键列名（技术债清理：非 id 名非自增
        # 主键泛化；缺省 "id" = 历史行为）
        self._pk_columns = dict(pk_columns or {})
        self._relations = normalize_relations(relations, self._mapping,
                                              pk_by_entity=self._pk_columns)
        self._validate_orphan_semantics()
        # 既有数据最大 ID（T4.5 三重检查之一）：追加灌数自既有最大值 +1 续区间；
        # 缺省 None = 空表构建口径（[1, N]，既有行为不变）
        self._existing_max = dict(existing_max or {})
        self._product = product
        self._batch_size = max(1, batch_size)
        self._loader_chain = list(loader_chain) if loader_chain is not None else None
        self._rows_per_shard = rows_per_shard
        self._staging_base = Path(staging_base) if staging_base else None
        self._workers = max(1, workers)
        self._env_class = env_class
        self._store_factory = checkpoint_store_factory or (
            lambda conn: SqliteCheckpointStore(conn))
        self._endpoint_probe = endpoint_probe
        self._lock = threading.Lock()
        self._runtime: dict[int, dict] = {}

    # ------------------------------------------------------------ generate

    def generate(self, ctx: dict) -> dict:
        instance_id = self._instance_id_of(ctx)
        params = ctx.get("params") or {}
        cancel_event = ctx.get("cancel_event")
        self._check_cancel(cancel_event, instance_id, "generate 入口")
        runtime = self._assemble_plan(instance_id, params, ctx)
        staging: StagingArea = runtime["staging"]
        manifest_path = staging.root / "manifest.json"
        if params.get("resume"):
            staging.restore()  # 断点续跑：无 manifest 显式失败（staging 协议口径）
            pending_ids = {r.shard_id for r in staging.pending()}
            tasks = [t for t in runtime["tasks"] if t.shard_id in pending_ids]
        elif manifest_path.exists():
            raise OfflinePipelineError(
                f"实例 {instance_id} staging 已有检查点 {manifest_path}"
                "（重复构建须显式处置：走 resume 或先清理 staging，不静默重建）")
        else:
            staging.root.mkdir(parents=True, exist_ok=True)
            tasks = runtime["tasks"]
        if self._workers == 1:
            # 单 worker 逐分片执行，分片间为取消安全点（协作式退出，
            # 已产出分片保留——staging 检查点可核查，不静默丢弃）
            results = []
            for t in sorted(tasks, key=lambda t: t.shard_id):
                self._check_cancel(cancel_event, instance_id, "分片间")
                results.append(run_shard(t, plan_row_factory, staging))
        else:
            results = run_shards(
                tasks, plan_row_factory, workers=self._workers, staging=staging)
        generated = sum(r.generated_rows for r in results)
        return {
            "generated_rows": generated,
            "shards": len(tasks),
            "staging_totals": staging.totals(),
        }

    @staticmethod
    def _check_cancel(cancel_event: object, instance_id: int, where: str) -> None:
        """取消信号协作检查（B7 续批次 3）：服务层 cancel() 经 ctx 注入
        threading.Event，置位即抛 DatasetCancelled 协作退出（杀线程不可靠，
        不强制 kill；信号传递协议在 fake 通道验证）。"""
        if isinstance(cancel_event, threading.Event) and cancel_event.is_set():
            raise DatasetCancelled(
                f"实例 {instance_id} 在 {where} 收到取消信号（协作式退出）")

    # ------------------------------------------------------------ load

    def load(self, ctx: dict) -> dict:
        instance_id = self._instance_id_of(ctx)
        cancel_event = ctx.get("cancel_event")
        self._check_cancel(cancel_event, instance_id, "load 入口")
        runtime = self._require_runtime(instance_id, "load")
        staging: StagingArea = runtime["staging"]
        conn = self._conn_factory()
        self._ensure_tables(conn)
        loader = self._build_loader()
        coordinator = CheckpointCoordinator(
            staging, self._store_factory(conn), instance_id)
        orchestrator = LoadOrchestrator(
            _QmarkConn(conn) if isinstance(conn, sqlite3.Connection) else conn,
            loader, coordinator, staging,
            entity_columns={e: m.columns for e, m in self._mapping.items()},
        )
        orchestrator.prepare_checkpoints()
        # 装载段取消安全点（续批次 3 §4 决策 3）：逐实体步骤装载，步骤间
        # 协作检查——置位即抛 DatasetCancelled；已装载实体经 DB 检查点保留
        # （不静默丢弃），恢复走 resume 协议
        loaded_rows = 0
        shards_loaded = 0
        entities_loaded: list[str] = []
        strategies_used: list[str] = []
        for step in sorted(self._mapping):
            self._check_cancel(cancel_event, instance_id,
                               f"装载步骤间（实体 {step} 前）")
            sub = orchestrator.run([step])
            loaded_rows += sub.rows_loaded
            shards_loaded += sub.shards_loaded
            entities_loaded.extend(sub.entities_loaded)
            strategies_used.extend(sub.strategies_used)
        with self._lock:
            runtime["loader"] = loader
        return {
            "loaded_rows": loaded_rows,
            "shards_loaded": shards_loaded,
            "entities_loaded": entities_loaded,
            "strategies_used": strategies_used,
        }

    # ------------------------------------------------------------ verify

    def verify(self, ctx: dict) -> dict:
        instance_id = self._instance_id_of(ctx)
        runtime = self._require_runtime(instance_id, "verify")
        params = ctx.get("params") or {}
        plan = runtime["plan"]
        staging: StagingArea = runtime["staging"]
        conn = self._conn_factory()
        expected = {name: ep.rows for name, ep in plan.entities.items()}
        shard_loaded: dict[str, int] = {}
        for r in staging.records():
            shard_loaded[r.entity] = shard_loaded.get(r.entity, 0) + (r.loaded_rows or 0)
        report = ValidationReport(
            instance_id=str(instance_id),
            layers=[
                validate_counts(conn, expected, shard_loaded=shard_loaded),
                validate_relationships(conn, list(self._relations)),
                validate_distributions(conn, plan.distribution_plan),
                validate_invariants(conn, []),  # 业务不变量随采纳流接线
                validate_db_ready(conn, self._product,
                                  endpoint_probe=self._endpoint_probe),
            ],
        )
        manifest = self._build_manifest(runtime, params, conn, instance_id)
        cleanup = CleanupContext(
            env_class=self._env_class, rows_to_clean=sum(expected.values()))
        return {
            "report": report.model_dump(),
            "manifest": manifest.model_dump(mode="json"),
            "cleanup": cleanup.model_dump(),
        }

    # ------------------------------------------------------------ cleanup

    def cleanup_executor(self):
        """清理执行回调（破坏性动作由服务层 SecurityGate CLEANUP 授权后调用）。"""

        def executor(tier: CleanupTier) -> dict:
            conn = self._conn_factory()
            cur = conn.cursor()
            tables = sorted(self._mapping)
            try:
                for entity in tables:
                    ident = validate_identifier(entity)
                    if tier == CleanupTier.SCHEMA_REBUILD:
                        cur.execute(f"DROP TABLE IF EXISTS {ident}")
                    else:
                        cur.execute(f"DELETE FROM {ident}")
            finally:
                cur.close()
            conn.commit()
            if tier == CleanupTier.SCHEMA_REBUILD:
                self._ensure_tables(conn)  # 重建后恢复可装载形态
            return {"tier": tier.value, "tables": tables}

        return executor

    # ------------------------------------------------------------ 组装内部

    def _assemble_plan(self, instance_id: int, params: dict, ctx: dict) -> dict:
        """离线规划装配：Definition spec → build_plan → sharding → ShardTask。"""
        def_key, version = self._def_ref_of(ctx)
        row = self._repo.find_definition(def_key, version)
        if row is None:
            raise OfflinePipelineError(
                f"Definition {def_key!r} v{version!r} 不存在（离线装配显式失败）")
        definition = self._repo.definition_to_model(row)
        spec: DatasetSpec = definition.spec
        if not spec.targets.rows:
            raise OfflinePipelineError(
                "DatasetSpec 无显式行数目标（targets.rows）——离线通道按显式目标"
                "组装，auto/scale_factor 规划随画像/Schema 批次接入（显式失败）")
        seed = (params.get("seed") if params.get("seed") is not None else None) \
            or row.seed or spec.reproducibility.seed
        if seed is None:
            raise OfflinePipelineError(
                "Seed 缺失（params.seed / Definition.seed / spec.reproducibility.seed "
                "均未提供）——不可复现路径不伪装（A3；§12.4 manifest 要素）")
        plan = build_plan(spec)
        sharding = self._sharding_of(plan)
        staging = StagingArea(
            self._staging_root(instance_id),
            plan_fingerprint=plan.compute_fingerprint(),
        )
        fields_by_entity = {e: list(m.fields) for e, m in self._mapping.items()}
        if self._relations:
            fields_by_entity = self._apply_fk_fields(fields_by_entity, sharding)
        tasks = shard_tasks_from_plan(
            sharding,
            fields_by_entity,
            root_seed=seed,
            plan_fingerprint=row.spec_fingerprint,
            staging=staging,  # 分片输出路径由 staging 分配（file 模式必需）
            pk_by_entity=self._pk_columns or None,
        )
        if not tasks:
            raise OfflinePipelineError("分片计划为空（无任何分片，显式失败）")
        runtime = {
            "plan": plan, "sharding": sharding, "tasks": tasks,
            "staging": staging, "seed": seed, "definition_row": row,
            "spec": spec, "started_at": datetime.now(),
        }
        with self._lock:
            self._runtime[instance_id] = runtime
        return runtime

    def _validate_orphan_semantics(self) -> None:
        """故意孤儿 × 声明关系一致性门禁（构造期 fail-fast，B7 续批次 9）。

        子列显式 ``ref`` 分布携带 orphan_rate>0（故意孤儿语义）时：
        - DB_FK/ORM 声明关系 → 零孤儿硬核对必 FAIL，构造期显式失败
          （提示改 LOGICAL——推断关系 verify 降级 WARN，孤儿如实暴露）；
        - LOGICAL → 放行（verify WARN 口径）。
        """
        for rel in self._relations:
            if rel.relation_kind.value in _INFERRED_RELATION_KINDS:
                continue
            m = self._mapping[rel.child_table]
            f = next((f for f in m.fields if f.name == rel.child_column), None)
            if f is None or f.distribution is None:
                continue
            d = f.distribution
            if (d.type or "").lower() == "ref" \
                    and float((d.params or {}).get("orphan_rate") or 0) > 0:
                raise OfflinePipelineError(
                    f"子列 {rel.child_table}.{rel.child_column} 显式 ref 分布携带"
                    f" orphan_rate>0（故意孤儿语义），与声明关系"
                    f" {rel.relation_kind.value} 的零孤儿硬核对冲突"
                    "（构造期显式失败：故意孤儿须配 logical 关系，"
                    "verify 降级 WARN 如实暴露，不伪装通过）")

    def _sharding_of(self, plan):
        sharding = build_sharding_plan(plan, rows_per_shard=self._rows_per_shard,
                                       existing_max=self._existing_max)
        planned = {s.entity for s in sharding.shards}
        unknown = planned - set(self._mapping)
        if unknown:
            raise OfflinePipelineError(
                f"分片计划含未映射实体 {sorted(unknown)}（EntityMapping 缺失，显式失败）")
        extra = set(self._mapping) - planned
        if extra:
            raise OfflinePipelineError(
                f"映射含分片计划外实体 {sorted(extra)}（清单不一致，显式失败）")
        return sharding

    def _apply_fk_fields(
        self,
        fields_by_entity: Mapping[str, list[FieldSpec]],
        sharding,
    ) -> dict[str, list[FieldSpec]]:
        """关系语义落地（生成侧）：子表外键列无显式分布时按父实体主键区间
        注入 uniform 采样（[id_start, id_end] 整数域）——零孤儿语义在生成侧
        成立，verify 关系层全量 LEFT JOIN 核对可 PASS（B7 续批次 8）。

        - 父实体主键区间由分片计划 planned_range 预分配区间合并（缺区间
          显式失败，不伪装可推导）；
        - 子列已有显式分布不静默覆盖（调用方口径优先，如倾斜/配额语义；
          孤儿越界由 verify 关系层如实暴露）；
        - 例外（B7 续批次 9）：子列显式 ``ref`` 分布缺父主键区间（min/max
          均缺）→ 注入父实体区间**补全**而非覆盖（热行/孤儿参数保留——
          legacy refSkew/orphan_rate 语义对齐走此缝；单边缺参显式失败）。
        """
        parents = {r.parent_table for r in self._relations}
        spans: dict[str, tuple[int, int]] = {}
        for shard in sharding.shards:
            if shard.entity in parents:
                if shard.id_start is None or shard.id_end is None:
                    raise OfflinePipelineError(
                        f"父实体 {shard.entity} 分片无预分配主键区间"
                        "（关系外键推导要求 planned_range 语义，显式失败）")
                cur = spans.get(shard.entity)
                lo = shard.id_start if cur is None else min(cur[0], shard.id_start)
                hi = shard.id_end if cur is None else max(cur[1], shard.id_end)
                spans[shard.entity] = (lo, hi)
        out: dict[str, list[FieldSpec]] = {
            e: list(fs) for e, fs in dict(fields_by_entity).items()}
        for rel in self._relations:
            lo, hi = spans[rel.parent_table]
            fields = out[rel.child_table]
            new_fields: list[FieldSpec] = []
            hit = False
            for f in fields:
                if f.name == rel.child_column:
                    hit = True
                    if f.distribution is None:
                        f = replace(f, distribution=DistributionFit(
                            type="uniform", params={"min": lo, "max": hi}))
                    elif (f.distribution.type or "").lower() == "ref":
                        # ref 分布补全父主键区间（热行/孤儿参数保留，非覆盖）
                        params = dict(f.distribution.params or {})
                        has_lo, has_hi = "min" in params, "max" in params
                        if has_lo != has_hi:
                            raise OfflinePipelineError(
                                f"子列 {rel.child_table}.{rel.child_column} ref 分布"
                                "父主键区间参数不完整（min/max 须同缺或同给，"
                                "显式失败）")
                        if not has_lo:
                            params["min"], params["max"] = lo, hi
                            f = replace(f, distribution=DistributionFit(
                                type="ref", params=params))
                        warn_hot_row_share(f.distribution.params)
                new_fields.append(f)
            if not hit:  # 防御（normalize_relations 已校验，不入此分支）
                raise OfflinePipelineError(
                    f"子表 {rel.child_table} 缺关系列 {rel.child_column}（显式失败）")
            out[rel.child_table] = new_fields
        return out

    def _build_manifest(self, runtime: dict, params: dict, conn,
                        instance_id: int) -> ExecutionManifest:
        """§12.4 全要素回填（missing_elements 零缺项——READY 条件 7 输入）。"""
        plan = runtime["plan"]
        sharding = runtime["sharding"]
        row = runtime["definition_row"]
        spec: DatasetSpec = runtime["spec"]
        loader = runtime.get("loader") or self._build_loader()
        capabilities: list[str] = []
        for adapter in loader.chain:  # 能力快照逐档采集（§12.4 Adapter 要素）
            ensure = getattr(adapter, "ensure_capable", None)
            if callable(ensure):
                try:
                    capabilities.extend(ensure(conn))
                except BulkLoadUnsupported as exc:
                    # 原生档不可用属降级链语义（load 段已留痕降档并随结果
                    # 回传）——快照如实记录不可用原因，不伪装可用也不崩溃
                    # （真库通道 verify 换新 conn 复查，可用性与装载时一致）
                    capabilities.append(
                        f"{getattr(adapter, 'strategy', '?')}: unsupported（{exc}）")
        ranges: dict[str, IdRange] = {}
        for shard in sharding.shards:
            if shard.id_start is None or shard.id_end is None:
                continue
            cur = ranges.get(shard.entity)
            start = shard.id_start if cur is None else min(cur.start, shard.id_start)
            end = shard.id_end if cur is None else max(cur.end, shard.id_end)
            ranges[shard.entity] = IdRange(
                start=start, end=end, strategy=shard.strategy)
        time_inputs = {
            k: str(v) for k, v in {
                "start": spec.time_range.start if spec.time_range else None,
                "end": spec.time_range.end if spec.time_range else None,
            }.items() if v is not None
        }
        manifest = backfill_from_staging(
            ExecutionManifest(
                id=params.get("manifest_id") or uuid.uuid4().hex,
                instance_id=str(instance_id),
                seed=runtime["seed"],
                generator_version=ptde.__version__,
                partition_algorithm_version=sharding.partition_algorithm_version,
                model_fingerprints=ModelFingerprints(
                    spec=row.spec_fingerprint,
                    plan=plan.compute_fingerprint(),
                    profile=row.profile_fingerprint,
                    schema_fp=self._schema_fingerprint(),
                ),
                adapter_capability_snapshot={
                    "product": self._product,
                    "chain": [a.strategy for a in loader.chain],
                    "capabilities": list(capabilities),
                },
                adapter_version=f"{loader.chain[0].strategy}@{self._batch_size}",
                time_inputs=time_inputs,
                actual_id_ranges=ranges,
                start_time=runtime["started_at"],
                end_time=datetime.now(),
            ),
            runtime["staging"].records(),
        )
        return manifest

    def _schema_fingerprint(self) -> str:
        """离线 Schema 指纹：映射（实体/字段/类型）规范化哈希（确定性）。"""
        payload = json.dumps(
            {e: [[f.name, f.type_name] for f in m.fields]
             for e, m in sorted(self._mapping.items())},
            ensure_ascii=False, sort_keys=True)
        return sha256(payload.encode("utf-8")).hexdigest()

    def _ensure_tables(self, conn) -> None:
        cur = conn.cursor()
        try:
            for m in self._mapping.values():
                cur.execute(m.ddl)
        finally:
            cur.close()
        conn.commit()

    def _build_loader(self) -> FallbackLoader:
        if self._loader_chain is not None:
            # 真库通道：原生档优先降级链（default_chain_for_product 或
            # 调用方校准链显式注入）
            return FallbackLoader(
                product=self._product, chain=list(self._loader_chain))
        return FallbackLoader(
            product=self._product,
            chain=[DbApiBatchInsertLoader(self._batch_size, product=self._product)],
        )

    def _staging_root(self, instance_id: int) -> Path:
        base = self._staging_base or Path("/tmp/ptde_offline")
        return default_staging_root(base, str(instance_id))

    def _require_runtime(self, instance_id: int, stage: str) -> dict:
        with self._lock:
            runtime = self._runtime.get(instance_id)
        if runtime is None:
            raise OfflinePipelineError(
                f"实例 {instance_id} 缺少离线装配上下文（{stage} 前须完成规划/生成——"
                "离线执行体不支持跨进程独立验证，显式失败）")
        return runtime

    # ------------------------------------------------------------ ctx 解析

    @staticmethod
    def _instance_id_of(ctx: dict) -> int:
        raw = (ctx or {}).get("instance", {}).get("id")
        if raw is None:
            raise OfflinePipelineError("ctx.instance.id 缺失（pipeline 缝协议违规）")
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise OfflinePipelineError(f"instance id 非法：{raw!r}") from None

    @staticmethod
    def _def_ref_of(ctx: dict) -> tuple[str, str]:
        def_ref = (ctx or {}).get("instance", {}).get("definition_id") or ""
        if "@" not in def_ref:
            raise OfflinePipelineError(
                f"definition_id 形态非法：{def_ref!r}（须为 def_key@version）")
        def_key, version = def_ref.rsplit("@", 1)
        return def_key, version
