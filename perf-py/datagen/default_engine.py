"""引擎默认实现：移植自 DefaultDataGenEngine.java。

编排 关联识别 → 拓扑排序 → Dry-Run 校验/正式写入。
Dry-Run 优先：完整执行元数据读取、规则解析、关联识别、拓扑排序、SQL 预检与
外键池预检，但不 INSERT；报告预计行数/事务时长估算/缺失警告。

无状态契约：RelationResolver/枚举缓存均为 execute 内局部对象，
同一引擎实例可并发执行多任务。

Java→Python 适配：JDBC 事务控制（setAutoCommit/commit/rollback）改为 DBAPI
隐式事务 + commit()/rollback()（语义等价：每批一个事务，失败回滚当前表）。
"""

from __future__ import annotations

import random
import re

from datagen.dialects.mysql import MysqlDialect  # noqa: F401  (re-export convenience)
from datagen.enum_value_discoverer import discover_with_db, suspect_enum_column
from datagen.executor import DbApiBatchExecutor
from datagen.id_pool import IdPool
from datagen.model import (
    DataGenSpec,
    EngineListener,
    EngineResult,
    FieldRule,
    RelationHint,
    SchemaMetadata,
    SourceFile,
    TableSpec,
    TableStat,
)
from datagen.relation_resolver import RelationResolver
from datagen.rule_engine import apply_rule, generate_default
from datagen.spi import BatchExecutor, DatabaseDialect
from datagen.topo_sorter import sort as topo_sort

# 自引用表最大引用深度（parent_id 树层级上限，防爆栈）
SELF_REF_MAX_DEPTH = 5
# 父 ID 池填充批量游标步长
POOL_FETCH_BATCH = 50_000
# 高倾斜警告阈值：单热行承载子行占比
HOT_ROW_SHARE_WARN = 50.0

_TABLE_NAME_OK = re.compile(r"[A-Za-z0-9_]+")


def _rand(rng):
    """QW1 随机源：seed 路径为注入的 random.Random；None 时模块级 random（现行为）。"""
    return rng if rng is not None else random


class DefaultDataGenEngine:
    """灌数引擎（方言按 JDBC URL 路由；ORM 解析器按源码特征路由）。"""

    def __init__(self, dialects: DatabaseDialect | list[DatabaseDialect] | None,
                 executor: BatchExecutor | None = None,
                 orm_parsers=None) -> None:
        if dialects is None:
            self.dialects: list[DatabaseDialect] = []
        elif isinstance(dialects, (list, tuple)):
            self.dialects = list(dialects)
        else:
            self.dialects = [dialects]
        self.executor = executor if executor is not None else DbApiBatchExecutor()
        if orm_parsers is None:
            self.orm_parsers = []
        elif isinstance(orm_parsers, (list, tuple)):
            self.orm_parsers = list(orm_parsers)
        else:
            self.orm_parsers = [orm_parsers]

    def get_dialects(self) -> list[DatabaseDialect]:
        """已注册方言（供调用方做 URL 前置校验，保证与引擎内路由判定一致）。"""
        return self.dialects

    def resolve_dialect(self, jdbc_url: str | None) -> DatabaseDialect:
        """按 JDBC URL 选方言（supports 判别，扩展新库种无需改动引擎）。"""
        for d in self.dialects:
            if d.supports(jdbc_url):
                return d
        raise ValueError("不支持的 JDBC URL（未注册匹配方言）: " + (jdbc_url or ""))

    def _resolve_parsers(self, files: list[SourceFile]):
        """命中的 ORM 解析器（特征检测 + order 升序，多 ORM 共存时全部执行后合并）。"""
        return sorted((p for p in self.orm_parsers if p.supports(files)),
                      key=lambda p: p.order())

    def execute(self, spec: DataGenSpec, listener: EngineListener | None = None) -> EngineResult:
        if listener is None:
            listener = _NullListener()
        result = EngineResult()
        result.dry_run = spec.dry_run
        result.total_rows = sum(t.row_count for t in spec.tables)
        try:
            self._validate(spec)
            dialect = self.resolve_dialect(spec.jdbc_url)
            conn = dialect.connect(spec.jdbc_url or "", spec.db_user or "", spec.db_password or "")
            try:
                # 1. 元数据读取（缺失表立即失败）
                metas: dict[str, SchemaMetadata] = {}
                for ts in spec.tables:
                    m = dialect.fetch_metadata(conn, ts.table_name or "")
                    if not m.columns:
                        raise ValueError("表不存在或无可见列: " + (ts.table_name or ""))
                    metas[m.table_name] = m

                # 2. 源码语义扫描（可选；失败静默降级）
                magic_hints = []
                java_enums: dict[str, list[str]] = {}
                self._scan_source(spec, metas.keys(), magic_hints, java_enums)
                orm_hints: list[RelationHint] = []
                if spec.source_files:
                    for parser in self._resolve_parsers(spec.source_files):
                        try:
                            for h in parser.scan(spec.source_files, metas.keys()).relation_hints:
                                if h.child_table in metas and h.parent_table in metas:
                                    orm_hints.append(h)
                        except Exception:  # noqa: BLE001 单解析器失败静默降级
                            pass

                # 3. 关联识别（四级合并，orm 语义参数化传递）与枚举建议
                rel = RelationResolver().resolve(
                    list(metas.values()), spec, conn, dialect, orm_hints)
                if rel.pending_suggestions:
                    listener.on_warn("存在基于命名约定推断的关联（低置信度），请在建议面板确认后采纳")
                listener.on_hints(magic_hints, self._merge_hints(rel, magic_hints))

                # 4. 依赖图 + 拓扑排序（环检测）
                deps = self._build_dependencies(metas.keys(), rel.auto_edges)
                order = topo_sort(list(metas.keys()), deps)

                # 5. 枚举建议注入（未配置 enum 规则的列，用发现值生成建议提示）
                enum_values: dict[str, list] = {}
                self._collect_enum_suggestions(conn, spec, metas, java_enums, result,
                                               dialect, enum_values)

                # 6. Dry-Run：估算后返回；正式：执行写入
                result.estimated_max_txn_ms = self._estimate_max_txn_ms(spec)
                if spec.dry_run:
                    listener.on_progress(result.total_rows, 0,
                                         order[0] if order else "")
                    for ts in spec.tables:
                        result.table_stats.append(TableStat(ts.table_name, ts.row_count))
                    result.success = True
                    return result
                # QW1：seed 注入的可复现随机源（每 execute 独立实例，保证无状态契约）；
                # seed=None 时为 None，全部采样回落模块级 random，行为不变
                rng = random.Random(spec.seed) if spec.seed is not None else None
                result.inserted_rows = self._insert_all(
                    spec, conn, metas, rel, order, listener, result, dialect,
                    enum_values, rng)
                result.success = True
                return result
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001 引擎顶层容错：失败经 result 返回
            result.success = False
            result.error_msg = str(e) if str(e) else type(e).__name__
            return result

    # ===== 写入编排（正式执行） =====

    def _insert_all(self, spec: DataGenSpec, conn, metas, rel, order,
                    listener: EngineListener, result: EngineResult,
                    dialect: DatabaseDialect, enum_values: dict[str, list],
                    rng=None) -> int:
        total_inserted = 0
        total_rows = result.total_rows

        # 边索引：子表 → (子列 → 父表.父列)
        edges_by_table: dict[str, dict[str, RelationHint]] = {}
        for e in rel.auto_edges:
            edges_by_table.setdefault(e.child_table, {})[e.child_column] = e

        # 父 ID 池：表 → 列 → 池（自引用与组间引用共用）
        pools: dict[str, dict[str, IdPool]] = {}

        for table_name in order:
            ts = next(t for t in spec.tables
                      if (t.table_name or "").lower() == table_name)
            meta = metas[table_name]
            table_edges = edges_by_table.get(table_name, {})

            # 先填充依赖父表的 ID 池
            for edge in table_edges.values():
                self._ensure_pool(spec, conn, metas, pools,
                                  edge.parent_table or "", edge.parent_column or "",
                                  dialect, rng)

            inserted = self._insert_table(spec, conn, meta, ts, table_edges, pools, listener,
                                          total_inserted, total_rows, dialect, enum_values,
                                          rng)
            total_inserted += inserted
            result.table_stats.append(TableStat(table_name, inserted))

            # 自引用表：本表主键也进入池（后续行引用已插行）
            if any(e.parent_table == table_name for e in table_edges.values()):
                pk = next(iter(meta.primary_keys)) if meta.primary_keys else "id"
                self._ensure_pool(spec, conn, metas, pools, table_name, pk, dialect, rng)
        return total_inserted

    def _ensure_pool(self, spec: DataGenSpec, conn, metas, pools,
                     table: str, column: str, dialect: DatabaseDialect,
                     rng=None) -> None:
        """确保父表主键池已填充（分段游标拉取，上限 50 万；幂等）。"""
        by_col = pools.setdefault(table, {})
        if column in by_col:
            return
        pool = IdPool(rng)
        meta = metas.get(table)
        col = meta.find_column(column) if meta is not None else None
        auto_increment_pk = col is not None and col.auto_increment
        if auto_increment_pk:
            # 自增主键：游标分段拉取（id > lastId LIMIT batch）
            ph = getattr(dialect, "param", "?")
            sql = ("SELECT " + dialect.quote_identifier(column) + " FROM "
                   + dialect.quote_identifier(table) + " WHERE " + dialect.quote_identifier(column)
                   + " > " + ph + " ORDER BY " + dialect.quote_identifier(column)
                   + " LIMIT " + str(POOL_FETCH_BATCH))
            last_id = 0
            cur = conn.cursor()
            try:
                while pool.size() < IdPool.MAX_POOL_SIZE:
                    cur.execute(sql, (last_id,))
                    rows = cur.fetchall()
                    n = 0
                    for row in rows:
                        id_ = int(row[0])
                        pool.add(id_)
                        last_id = max(last_id, id_)
                        n += 1
                    if n < POOL_FETCH_BATCH:
                        break
            finally:
                cur.close()
        elif meta is not None:
            # 非自增主键：直接全量拉取（上限内）
            sql = ("SELECT " + dialect.quote_identifier(column) + " FROM "
                   + dialect.quote_identifier(table))
            cur = conn.cursor()
            try:
                cur.execute(sql)
                for row in cur.fetchall():
                    if pool.size() >= IdPool.MAX_POOL_SIZE:
                        break
                    pool.add(int(row[0]))
            finally:
                cur.close()
        by_col[column] = pool

    def _insert_table(self, spec: DataGenSpec, conn, meta: SchemaMetadata, ts: TableSpec,
                      table_edges, pools, listener: EngineListener, inserted_before: int,
                      total_rows: int, dialect: DatabaseDialect,
                      enum_values: dict[str, list], rng=None) -> int:
        """单表生成与批量插入（每表独立事务；ref/refSkew 采样 + orphanRate 独立池后置替换）。"""
        insert_columns: list[str] = []
        cols = []
        for c in meta.columns:
            if not c.auto_increment:
                insert_columns.append(c.name)
                cols.append(c)
        if not insert_columns:
            return 0
        sql = dialect.build_insert_sql(meta.table_name or "", insert_columns)
        orphan_rate = (ts.orphan_rate if ts.orphan_rate is not None
                       else (spec.orphan_rate if spec.orphan_rate is not None else 0))
        pk = next(iter(meta.primary_keys)) if meta.primary_keys else "id"
        self_ref = any(e.parent_table == meta.table_name for e in table_edges.values())

        # refSkew 权重预热
        for edge in table_edges.values():
            fr = ts.fields.get(edge.child_column or "")
            if fr is not None and fr.type == FieldRule.TYPE_REF_SKEW:
                parent_pools = pools.get(edge.parent_table)
                if parent_pools is not None:
                    pp = parent_pools.get(edge.parent_column)
                    if pp is not None:
                        hp = int(fr.hot_percent) if fr.hot_percent is not None else 20
                        pp.build_skew_weights(hp, int(fr.hot_share) if fr.hot_share is not None else 80)
                        # 高倾斜警告（单热行承载占比估算）
                        per_hot = 100.0 * (fr.hot_share if fr.hot_share is not None else 80) / max(1, hp)
                        if per_hot > HOT_ROW_SHARE_WARN and not spec.dry_run:
                            listener.on_warn(
                                f"表 {ts.table_name} 列 {edge.child_column} 热点倾斜极高"
                                f"（单热行约承载 {per_hot:.0f}% 子行），"
                                "建议降低 BATCH_SIZE 或为子表分区")

        inserted = 0
        batch_size = self.executor.get_batch_size()
        batch: list[list] = []
        try:
            for row in range(ts.row_count):
                batch.append(self._build_row(meta, ts, insert_columns, cols, table_edges,
                                             pools, pk, self_ref, orphan_rate,
                                             enum_values, row, rng))
                if len(batch) >= batch_size:
                    self.executor.execute_batch(conn, sql, batch)
                    inserted += len(batch)
                    batch = []
                    conn.commit()
                    listener.on_progress(total_rows, inserted_before + inserted,
                                         meta.table_name)
            if batch:
                self.executor.execute_batch(conn, sql, batch)
                inserted += len(batch)
                conn.commit()
                listener.on_progress(total_rows, inserted_before + inserted, meta.table_name)
        except Exception:
            conn.rollback()
            raise
        return inserted

    def _build_row(self, meta: SchemaMetadata, ts: TableSpec, insert_columns, cols,
                   table_edges, pools, pk: str, self_ref: bool, orphan_rate: float,
                   enum_values: dict[str, list], row_index: int, rng=None) -> list:
        """构造一行参数值：规则引擎 + DB 枚举发现 + ref/refSkew 池采样 + orphanRate 后置替换。"""
        r = _rand(rng)
        row: list = []
        orphan = r.random() < orphan_rate

        for i, col_name in enumerate(insert_columns):
            col = cols[i]
            fr = ts.fields.get(col_name)
            edge = table_edges.get(col_name)
            is_ref_rule = (fr is not None
                           and fr.type in (FieldRule.TYPE_REF, FieldRule.TYPE_REF_SKEW))
            if edge is not None or is_ref_rule:
                parent_table = (edge.parent_table if edge is not None
                                else (fr.table or "").lower())
                parent_column = (edge.parent_column if edge is not None
                                 else (fr.column if fr.column is not None else "id"))
                pool = (pools.get(parent_table) or {}).get(parent_column)
                value = self._sample_ref(pool, fr, orphan, col, rng)
            elif fr is not None:
                value = apply_rule(fr, col, row_index, rng)
            else:
                # 未配置规则：优先用 DB 发现的枚举值（PG 原生枚举 / MySQL ENUM / CHECK 约束），
                # 否则随机生成——否则枚举列会被填入随机长串，直接触发类型/约束错误。
                discovered = enum_values.get(enum_key(meta.table_name, col_name))
                if discovered:
                    value = discovered[r.randrange(len(discovered))]
                else:
                    value = generate_default(col, rng)
                if (orphan and col_name.endswith("_" + pk)
                        and not col.nullable):
                    # 不可空列的孤儿策略由池采样无效 ID 处理（见 _sample_ref），此处仅可空列置 NULL
                    value = None
            row.append(value)
        return row

    def _sample_ref(self, pool: IdPool | None, fr: FieldRule | None,
                    orphan: bool, col: SchemaMetadata.ColumnMeta, rng=None):
        """ref/refSkew 采样；orphan=true 时按可空性置 NULL 或无效 ID（独立于倾斜池，后置替换）。"""
        r = _rand(rng)
        skewed = fr is not None and fr.type == FieldRule.TYPE_REF_SKEW
        if orphan:
            if col.nullable and r.random() < 0.5:
                return None
            return 0 if pool is None else pool.sample_invalid_id()
        if pool is None or pool.is_empty():
            # 无池可用（父表组外/空表兜底未采纳）：退化随机数，交由 dry-run 警告提示
            return generate_default(col, rng)
        return pool.sample_skewed() if skewed else pool.sample_uniform()

    def _validate(self, spec: DataGenSpec) -> None:
        if not any(d.supports(spec.jdbc_url) for d in self.dialects):
            raise ValueError("不支持的 JDBC URL（未注册匹配方言）: " + (spec.jdbc_url or ""))
        if not spec.tables:
            raise ValueError("表组为空")
        total = sum(t.row_count for t in spec.tables)
        if total < 1 or total > spec.max_rows:
            raise ValueError(f"表组总行数必须在 1-{spec.max_rows} 之间")
        for ts in spec.tables:
            name = ts.table_name
            if name is None or not _TABLE_NAME_OK.fullmatch(name):
                raise ValueError("表名仅允许字母数字下划线: " + (name or ""))
            if ts.row_count < 0:
                raise ValueError("行数非法: " + (name or ""))

    def _scan_source(self, spec: DataGenSpec, tables, out_magic, out_enums) -> None:
        files = spec.source_files
        if not files:
            return
        # 多 ORM 共存：全部命中解析器按 order 顺序执行并合并结果
        for parser in self._resolve_parsers(files):
            try:
                sr = parser.scan(files, tables)
                out_magic.extend(sr.magic_hints)
                out_enums.update(sr.java_enums)
            except Exception:  # noqa: BLE001 单个解析器失败静默降级
                pass

    @staticmethod
    def _merge_hints(rel, magic) -> list[RelationHint]:
        all_hints = list(rel.auto_edges)
        all_hints.extend(rel.pending_suggestions)
        return all_hints

    @staticmethod
    def _build_dependencies(tables, edges: list[RelationHint]) -> dict[str, set[str]]:
        deps: dict[str, set[str]] = {t: set() for t in tables}
        for e in edges:
            if (e.child_table in deps and e.parent_table in deps
                    and e.child_table != e.parent_table):
                deps[e.child_table].add(e.parent_table)
        return deps

    def _collect_enum_suggestions(self, conn, spec: DataGenSpec, metas, java_enums,
                                  result: EngineResult, dialect: DatabaseDialect,
                                  enum_values: dict[str, list]) -> None:
        """枚举建议注入：DB 发现值写入 enum_values 缓存（表名.列名 → 候选值）。"""
        for ts in spec.tables:
            m = metas.get((ts.table_name or "").lower())
            if m is None:
                continue
            for col in m.columns:
                if ts.fields.get(col.name) is not None:
                    continue  # 已配置规则
                values = discover_with_db(conn, dialect, m.table_name, col, java_enums)
                if values:
                    # 缓存 DB 发现值：未配置规则时直接用于生成，避免枚举列被填入随机长串
                    enum_values[enum_key(m.table_name, col.name)] = values
                elif suspect_enum_column(col):
                    result.warnings.append(
                        "列 " + (m.table_name or "") + "." + (col.name or "")
                        + " 疑似枚举列（列名特征），建议人工配置 enum 规则")

    def _estimate_max_txn_ms(self, spec: DataGenSpec) -> int:
        batch_size = self.executor.get_batch_size()
        max_rows = max((t.row_count for t in spec.tables), default=0)
        # 经验估算：每批 5ms（rewriteBatchedStatements 多行插入），不含索引开销
        return (max_rows // max(1, batch_size)) * 5


def enum_key(table: str | None, column: str | None) -> str:
    """DB 枚举值缓存键（表名.列名，均小写）。"""
    return ((table or "").lower() + "." + (column or "").lower())


class _NullListener(EngineListener):
    """listener 缺省时的空实现（Java 侧为必传参数，Python 端容忍 None）。"""

    def on_progress(self, total_rows, inserted_rows, current_table) -> None:
        pass

    def on_hints(self, magic_hints, relation_hints) -> None:
        pass
