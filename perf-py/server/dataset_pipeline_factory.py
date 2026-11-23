"""真库数据集执行体动态工厂：按数据集实例（ctx）按需装配 OfflinePipeline。

P0-2 接线（评审「PTDE 真库执行体未接入 perf serve」）：DatasetLifecycleService
注入 pipeline_factory 后，构建/验证/清理各段按实例目标库动态构造
build_realdb_pipeline，而非进程级静态单例：

- DSN 解析优先级：显式传入 → ``PTDE_REALDB_URL`` 环境变量 → ``db_conn`` 表
  默认连接（jdbc:mysql:// 转换为 SQLAlchemy/pymysql 口径）；
- 字段规格：目标库 ``information_schema.COLUMNS`` 实探（真库语义——列结构
  以真实表为准，CREATE TABLE IF NOT EXISTS 不改既有表）；
- 关系规格：``information_schema.KEY_COLUMN_USAGE`` 外键实探
  （父子实体自动接线 relation_kind=db_fk）；
- DSN 无法解析 / 目标表实探缺失 → 显式抛 DatasetServiceError
  （对齐 UnwiredPipeline「不伪装可用」口径）。

introspector 注入缝供测试替身（避免单测依赖真实 MySQL）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from server.crypto import decrypt
from server.realdb_pipeline import (
    REALDB_URL_ENV,
    build_realdb_pipeline,
    mysql_conn_factory,
)

# 实体探结果形态：（fields_by_entity, relations, pk_columns），
# 字段项与 FieldSpec JSON 原生协议一致（name/type_name/size）
Introspector = Callable[[str, list[str]],
                        tuple[dict[str, list[dict]], list[dict], dict[str, str]]]


class DatasetPipelineWireError(RuntimeError):
    """真库执行体装配失败（DSN 缺失 / 目标库不可达 / 目标表缺失）。"""


def jdbc_url_to_pymysql(url: str, username: str | None = None,
                        password: str | None = None) -> str:
    """jdbc:mysql://host:port/db?params → mysql+pymysql://host:port/db?params。

    db_conn 表生产口径存 JDBC URL（EXPLAIN/灌数复用）；真库管线 DSN 走
    pymysql 方言（parse_mysql_dsn 仅支持 mysql:// 与 mysql+pymysql://）。

    P0 修复：可选透传 username/password（db_conn 凭据与 URL 分列存储），
    凭据经 URL 编码后拼入 netloc——防止密码含 @ / : / # 等特殊字符破坏
    DSN 解析；URL 自身已带凭据时不覆盖。
    """
    prefix = "jdbc:mysql://"
    converted = "mysql+pymysql://" + url[len(prefix):] if url.startswith(prefix) else url
    if not username:
        return converted
    parts = urlsplit(converted)
    if parts.username:  # URL 自身已带凭据则不覆盖
        return converted
    user_q = quote(username, safe="")
    auth = f"{user_q}:{quote(password, safe='')}" if password else user_q
    netloc = f"{auth}@{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def resolve_target_dsn(repo, explicit: str | None = None) -> str:
    """目标库 DSN 解析：显式 → PTDE_REALDB_URL → db_conn 默认连接。

    db_conn 取 is_default=1 优先、id 最小兜底（对齐 matchOrder 语义），
    仅接受 jdbc:mysql:// 前缀（与 EXPLAIN 通道同一准入）。
    P0 修复：回退 db_conn 表时透传 username 与解密后的 password
    （decrypt 对存量明文原样透传；错误信息与日志严禁打印完整 dsn）。
    """
    if explicit and explicit.strip():
        return explicit.strip()
    env_url = os.environ.get(REALDB_URL_ENV)
    if env_url and env_url.strip():
        return env_url.strip()
    from sqlalchemy import select

    from server.collector_repo import DbConnModel

    with repo._sessions() as s:  # noqa: SLF001 — 同进程装配缝，仓储内部会话复用
        rows = list(s.scalars(
            select(DbConnModel)
            .where(DbConnModel.db_url.like("jdbc:mysql://%"))
            .order_by(DbConnModel.is_default.desc(), DbConnModel.id.asc())
            .limit(10)))
    for row in rows:  # is_default DESC 已排序；首个可转换连接即生效
        dsn = jdbc_url_to_pymysql(
            row.db_url,
            username=row.username,
            password=decrypt(row.password) if row.password else None,
        )
        if dsn.startswith(("mysql://", "mysql+pymysql://")):
            return dsn
    raise DatasetPipelineWireError(
        f"真库执行体目标库未配置：设 {REALDB_URL_ENV}（或平台数据库连接页"
        "登记一条 jdbc:mysql:// 默认连接）后重试")


def _fetch_columns(conn: Any, database: str, entities: list[str]) -> list[dict]:
    placeholders = ", ".join(["%s"] * len(entities))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, "
            "COLUMN_KEY FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN ({placeholders}) "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION",
            [database, *entities])
        return [
            {"table": r[0], "name": r[1], "type_name": str(r[2]).upper(),
             "size": int(r[3]) if r[3] is not None else 0, "pk": r[4] == "PRI"}
            for r in cur.fetchall()
        ]


def _fetch_relations(conn: Any, database: str, entities: list[str]) -> list[dict]:
    placeholders = ", ".join(["%s"] * len(entities))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, "
            "REFERENCED_COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = %s AND REFERENCED_TABLE_NAME IS NOT NULL "
            f"AND TABLE_NAME IN ({placeholders})",
            [database, *entities])
        return [
            {"child_table": r[0], "child_column": r[1],
             "parent_table": r[2], "parent_column": r[3],
             "relation_kind": "db_fk"}
            for r in cur.fetchall()
        ]


def default_introspector(dsn: str, entities: list[str]) -> tuple[dict[str, list[dict]], list[dict]]:
    """目标库实探（列结构 + 外键关系）；工厂缺省注入缝。"""
    conn = mysql_conn_factory(dsn)()
    try:
        database = dsn.rsplit("/", 1)[-1].split("?")[0]
        cols = _fetch_columns(conn, database, entities)
        rels = _fetch_relations(conn, database, entities)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — 关闭失败不影响主流程
            pass
    fields: dict[str, list[dict]] = {}
    pk_columns: dict[str, str] = {}
    for c in cols:
        fields.setdefault(c["table"], []).append(
            {"name": c["name"], "type_name": c["type_name"], "size": c["size"]})
        if c["pk"]:
            pk_columns[c["table"]] = c["name"]
    missing = [e for e in entities if e not in fields]
    if missing:
        raise DatasetPipelineWireError(
            f"目标库缺失数据集实体表：{', '.join(missing)}"
            "（请先建表或调整数据集 targets）")
    relations = [r for r in rels
                 if r["child_table"] in fields and r["parent_table"] in fields]
    return fields, relations, pk_columns


def realdb_pipeline_factory(
    repo,
    *,
    dsn: str | None = None,
    staging_base: str | None = None,
    batch_size: int = 500,
    workers: int = 1,
    introspector: Introspector | None = None,
) -> Callable[[dict], Any]:
    """构造 ctx → OfflinePipeline 动态工厂（DatasetLifecycleService 注入缝）。

    ctx 需含 ``instance.definition_id``（数字主键；DatasetLifecycleService
    转换后 model_dump 形态）。实体清单取 definition spec 的 targets.rows；
    DSN/字段/关系按上文优先级解析。
    """
    inspect = introspector or default_introspector

    def build(ctx: dict) -> Any:
        instance = ctx.get("instance") or {}
        definition = repo.find_definition_by_id(int(instance.get("definition_id") or 0))
        if definition is None:
            raise DatasetPipelineWireError(
                f"数据集定义不存在（definition_id={instance.get('definition_id')}）")
        spec = json.loads(definition.spec_json)
        entities = sorted((spec.get("targets", {}).get("rows") or {}).keys())
        if not entities:
            raise DatasetPipelineWireError(
                f"数据集定义 {definition.def_key}@{definition.version} 无目标实体")
        target_dsn = resolve_target_dsn(repo, dsn)
        fields, relations, pk_columns = inspect(target_dsn, entities)
        return build_realdb_pipeline(
            repo, target_dsn, fields,
            relations=relations or None, pk_columns=pk_columns or None,
            batch_size=batch_size, staging_base=staging_base, workers=workers,
        )

    return build
