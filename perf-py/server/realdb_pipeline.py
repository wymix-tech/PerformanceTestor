"""数据集构建执行体——MySQL 真库通道装配（A2 真库批次，B7 续批次 7）。

把 ``OfflinePipeline`` 组装为 MySQL 生产通道（编排协议不变，仅换连接/检查点/
装载链三处注入）——延续 B7 前段「执行体三段缝」口径：

- **conn 工厂**：DSN（``mysql://user:pwd@host:port/db``）→ pymysql 连接，
  ``local_infile=True``（原生 LOAD DATA LOCAL INFILE 档客户端前提）、
  ``charset=utf8mb4``、``autocommit=False``（装载批间提交语义由 loader 控制）；
  DSN 缺省读环境变量 ``PTDE_REALDB_URL``（server 元数据库与目标数据库分离，
  不复用 ``PERF_PY_DATABASE_URL``）；
- **检查点仓**：``DbCheckpointStore``（MySQL 方言；协议与
  SqliteCheckpointStore 对齐，构造即建 ``ptde_shard_checkpoint``）；
- **装载链**：``[MysqlBulkLoadAdapter, MysqlServerInfileAdapter,
  DbApiBatchInsertLoader(batch_size)]``——原生档优先（§10.3 链序），
  ``ensure_capable`` 逐档探测降级（local_infile/secure_file_priv 不满足时
  留痕降下一档，不静默吞差异）；批次大小为 Pilot Run 校准项（T5.8）；
- **映射产出器（A2 最小版）**：字段规格（FieldSpec，映射知识在生成侧）
  → ``EntityMapping``（MySQL DDL 类型映射；未知类型显式失败防口径漂移）。
  完整 Schema 快照 → 映射（含索引/分区/环组）随 A2 后续扩展，本版覆盖
  ``fields_by_entity`` 显式注入路径；
- **关系映射接线（B7 续批次 8）**：``relations`` 四元组（JSON 原生 dict）
  透传 ``OfflinePipeline``——verify 关系层实映射（DB_FK 零孤儿全量核对），
  生成侧子表外键列按父实体主键区间自动推导采样（无显式分布时）。

装配示例（部署入口/测试装配方）::

    from server.realdb_pipeline import build_realdb_pipeline

    pipeline = build_realdb_pipeline(repo, dsn, fields_by_entity)
    service = DatasetLifecycleService(
        repo, pipeline=pipeline,
        degradable_layers=frozenset({LayerKind.DISTRIBUTION, LayerKind.DB_READY}))

``degradable_layers`` 由装配方显式决定（§9.4：WARN 放行不默认放宽）——
真库通道 db_ready 层统计信息新鲜度在未 ANALYZE 时为 WARN，装配方按
环境口径选择放行集。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from ptde.adapters.bulkload.fallback import (
    DbApiBatchInsertLoader,
    MysqlServerInfileAdapter,
)
from ptde.adapters.mysql.bulkload import MysqlBulkLoadAdapter
from ptde.generator.worker import FieldSpec
from ptde.loader.checkpoint import DbCheckpointStore
from ptde.validator import validate_identifier
from server.offline_pipeline import EntityMapping, OfflinePipeline
from server.ptde_repo import PtdeDatasetRepository

__all__ = [
    "REALDB_URL_ENV",
    "RealDbPipelineError",
    "build_realdb_pipeline",
    "entity_mappings_from_fields",
    "mysql_conn_factory",
    "parse_mysql_dsn",
    "realdb_url",
]

REALDB_URL_ENV = "PTDE_REALDB_URL"

# FieldSpec.type_name → MySQL 列类型（§9.2 语义映射；未知类型显式失败）
_MYSQL_TYPES: dict[str, str] = {
    "BIGINT": "BIGINT",
    "INT": "INT",
    "INTEGER": "INT",
    "SMALLINT": "SMALLINT",
    "DECIMAL": "DECIMAL(18,2)",
    "NUMERIC": "DECIMAL(18,2)",
    "FLOAT": "DOUBLE",
    "DOUBLE": "DOUBLE",
    "VARCHAR": "VARCHAR(255)",
    "CHAR": "CHAR(32)",
    "TEXT": "TEXT",
    "DATETIME": "DATETIME",
    "TIMESTAMP": "DATETIME",
    "DATE": "DATE",
    "TIME": "TIME",
    "TINYINT": "TINYINT",
    "BOOLEAN": "TINYINT(1)",
    "BOOL": "TINYINT(1)",
    "BIT": "TINYINT(1)",
    "YEAR": "SMALLINT",
    "JSON": "JSON",
    "BLOB": "BLOB",
    "VARBINARY": "BLOB",
    "ENUM": "VARCHAR(255)",
    "SET": "VARCHAR(255)",
}


class RealDbPipelineError(Exception):
    """真库通道装配违规（显式失败，不伪装可用）。"""


def realdb_url() -> str | None:
    """真库 DSN 环境变量读取（缺省 None——未配置由调用方显式处置）。"""
    return os.environ.get(REALDB_URL_ENV) or None


def parse_mysql_dsn(dsn: str) -> dict[str, object]:
    """``mysql://`` / ``mysql+pymysql://`` DSN → 连接参数（显式校验）。"""
    if not dsn or "://" not in dsn:
        raise RealDbPipelineError(f"DSN 形态非法：{dsn!r}（须为 mysql://user:pwd@host:port/db）")
    parts = urlsplit(dsn)
    if parts.scheme not in ("mysql", "mysql+pymysql"):
        raise RealDbPipelineError(
            f"DSN 方言非法：{parts.scheme!r}（真库通道仅支持 mysql / mysql+pymysql）")
    if not parts.hostname or not parts.path.strip("/"):
        raise RealDbPipelineError(
            f"DSN 缺少 host 或 database：{dsn!r}（显式失败，不伪装可用）")
    return {
        "host": parts.hostname,
        "port": parts.port or 3306,
        "user": unquote(parts.username) if parts.username else "",
        "password": unquote(parts.password) if parts.password else "",
        "database": parts.path.strip("/"),
    }


def mysql_conn_factory(dsn: str | None) -> Callable[[], object]:
    """DSN → pymysql 连接工厂（local_infile=True 供原生装载档）。"""
    params = parse_mysql_dsn(dsn or realdb_url() or "")
    import pymysql  # 懒加载（A2 起 server 组自带驱动）

    def factory() -> object:
        return pymysql.connect(
            host=str(params["host"]),
            port=int(params["port"]),  # type: ignore[arg-type]
            user=str(params["user"]),
            password=str(params["password"]),
            database=str(params["database"]),
            charset="utf8mb4",
            autocommit=False,
            local_infile=True,
        )

    return factory


def _field_spec_of(f: object) -> FieldSpec:
    """字段规格归一化：FieldSpec 直用；dict（JSON 原生）→ FieldSpec，
    distribution 同步归一化（dict → DistributionFit，采样器需模型形态）。"""
    if isinstance(f, FieldSpec):
        return f
    data = dict(f)  # type: ignore[arg-type]
    dist = data.get("distribution")
    if isinstance(dist, dict):
        from ptde.models.profile import DistributionFit

        data["distribution"] = DistributionFit.model_validate(dist)
    return FieldSpec(**data)


def _mysql_column_def(field: FieldSpec) -> str:
    t = (field.type_name or "").strip().upper()
    if t not in _MYSQL_TYPES:
        raise RealDbPipelineError(
            f"字段 {field.name!r} 类型 {field.type_name!r} 无 MySQL 类型映射"
            "（映射产出器显式失败，防口径漂移；支持："
            f"{', '.join(sorted(_MYSQL_TYPES))}）")
    return f"{field.name} {_MYSQL_TYPES[t]}"


def entity_mappings_from_fields(
    fields_by_entity: Mapping[str, Sequence[FieldSpec]],
    pk_by_entity: Mapping[str, str] | None = None,
) -> dict[str, EntityMapping]:
    """字段规格 → EntityMapping（A2 最小版映射产出器）。

    - DDL 由 FieldSpec.type_name 经 ``_MYSQL_TYPES`` 映射生成
      （``CREATE TABLE IF NOT EXISTS`` 形态，对齐 EntityMapping 校验口径）；
    - 主键列承载 planned_range 区间语义（worker 主键自预分配），落
      NOT NULL PRIMARY KEY；列名经 ``pk_by_entity``（缺省 ``id``，
      非 id 名主键由调用方按 Schema 显式给出）；
    - 未知类型显式失败（防口径漂移）。
    """
    mappings: dict[str, EntityMapping] = {}
    for entity, fields in dict(fields_by_entity).items():
        validate_identifier(entity)
        specs = [_field_spec_of(f) for f in fields]
        if not specs:
            raise RealDbPipelineError(f"实体 {entity} 字段规格为空（显式失败）")
        pk = (pk_by_entity or {}).get(entity, "id")
        cols = []
        for f in specs:
            col = _mysql_column_def(f)
            if f.name == pk:
                col += " NOT NULL PRIMARY KEY"
            cols.append(col)
        ddl = (f"CREATE TABLE IF NOT EXISTS {entity} (\n  "
               + ",\n  ".join(cols)
               + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
        mappings[entity] = EntityMapping(
            entity=entity, fields=tuple(specs), ddl=ddl)
    return mappings


def build_realdb_pipeline(
    repo: PtdeDatasetRepository,
    dsn: str | None,
    fields_by_entity: Mapping[str, Sequence[FieldSpec]],
    *,
    batch_size: int = 500,
    rows_per_shard: int = 250_000,
    staging_base: str | None = None,
    workers: int = 1,
    env_class: str = "dedicated_test_db",
    endpoint_probe: Callable[[], dict[str, int]] | None = None,
    conn_factory: Callable[[], object] | None = None,
    relations: Sequence[Mapping] | None = None,
    existing_max: Mapping[str, int] | None = None,
    pk_columns: Mapping[str, str] | None = None,
) -> OfflinePipeline:
    """MySQL 真库通道执行体装配（编排协议不变，换三处注入）。

    - ``dsn`` 未传读 ``PTDE_REALDB_URL``；仍缺 → 显式失败；
    - ``conn_factory`` 显式注入优先（测试注入 FakeConn / 已有工厂复用）；
    - 装载链按 §10.3 链序组装，末档 DbApiBatchInsertLoader 用调用方
      ``batch_size``（Pilot Run 校准项注入口径）；
    - ``relations``：表间关系四元组（JSON 原生 dict，如
      ``{"child_table": ..., "child_column": ..., "parent_table": ...,
      "parent_column": ..., "relation_kind": "db_fk"}``）——归一化与
      映射一致性校验在 ``OfflinePipeline``（normalize_relations）；
    - ``existing_max``：既有数据每实体最大 ID（追加灌数续接区间，
      T4.5 既有数据检查；缺省 None = 空表构建口径）；
    - ``pk_columns``：各实体 planned_range 接管的主键列名（缺省 ``id``；
      非 id 名主键由调用方按内省显式给出）。
    - ``register_builtin_adapters()`` 宿主显式注册（幂等，overwrite=True）。
    """
    from ptde.adapters import register_builtin_adapters

    register_builtin_adapters()
    factory = conn_factory if conn_factory is not None else mysql_conn_factory(dsn)
    chain = [
        MysqlBulkLoadAdapter(),
        MysqlServerInfileAdapter(),
        DbApiBatchInsertLoader(max(1, batch_size), product="mysql"),
    ]
    return OfflinePipeline(
        repo,
        factory,
        entity_mappings_from_fields(fields_by_entity, pk_by_entity=pk_columns),
        product="mysql",
        batch_size=max(1, batch_size),
        loader_chain=chain,
        rows_per_shard=rows_per_shard,
        staging_base=staging_base,
        workers=workers,
        env_class=env_class,
        checkpoint_store_factory=lambda conn: DbCheckpointStore(conn),
        endpoint_probe=endpoint_probe,
        relations=relations,
        existing_max=existing_max,
        pk_columns=pk_columns,
    )
