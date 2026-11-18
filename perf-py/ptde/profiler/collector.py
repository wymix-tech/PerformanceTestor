"""只读统计采集器（R2 T2.1/T2.2/T2.3/T2.5）。

职责与硬约束：
- A5 双 DSN（T2.1）：采集连接必须使用 `resolve_dsns().profile_dsn`；
  fallback_same_value=True 时 warning 进入采集执行清单（CollectionManifest，可断言）；
  采集清单只记录脱敏后的凭据身份（credential_label，不含密码）。
- 只读白名单（T2.2）：全部语句发送前过 `assert_readonly_sql`，白名单外直接拒绝；
  查询失败带对象名与原因显式上抛 StatisticsCollectionError（对齐 A3 / metadata 采集）。
- 采集安全（T2.3）：采样率/超时/行数上限来自 PtdeConfig（可配置）；敏感列（姓名/
  手机号/身份证形）只出 SQL 侧哈希分桶 Top-K / 分布参数，不取原始样本；
  小样本类别披露保护（低于 min_bucket_samples 的类别并入 OTHER）。
- 画像带 collected_at / sampling_rate / accuracy / source_type 元数据并随指纹入库。

T2.5：`collect(source_type="test_db_self")` 对目标测试库完整采集（R2 验收 +
R3 校准输入）。
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ptde.adapters.mysql.statistics import (
    MysqlStatisticsAdapter,
    StatisticsCollectionError,
    validate_identifier,
)
from ptde.adapters.postgres.statistics import PostgresStatisticsAdapter
from ptde.config import PtdeConfig
from ptde.models.profile import (
    ColumnProfile,
    DistributionFit,
    LengthStats,
    ProfileSource,
    RelationCardinalityProfile,
    StatisticsProfile,
    TableProfile,
    TopKEntry,
)

__all__ = [
    "CollectionManifest",
    "CollectorSettings",
    "SOURCE_TYPE_DB_READONLY",
    "SOURCE_TYPE_FILE_UPLOAD",
    "SOURCE_TYPE_TEST_DB_SELF",
    "StatisticsCollectionError",
    "StatisticsCollector",
    "adapter_for_dsn",
    "assert_readonly_sql",
    "credential_label",
    "fit_zipf",
    "is_sensitive_column",
]

SOURCE_TYPE_DB_READONLY = "db_readonly"
SOURCE_TYPE_FILE_UPLOAD = "file_upload"
SOURCE_TYPE_TEST_DB_SELF = "test_db_self"

# A3 精度分级词汇（与 row_count_accuracy / accuracy 字段对齐）
ACCURACY_EXACT = "exact"
ACCURACY_APPROXIMATE = "approximate"
ACCURACY_ESTIMATED = "estimated"
ACCURACY_FITTED = "fitted"

# ---------- 语句白名单（T2.2/T2.3 硬约束） ----------

_ALLOWED_START = re.compile(r"^select\b", re.I)
# 锁定子句 / 服务端导出：虽以 SELECT 开头但属白名单外，直接拒绝
_FORBIDDEN_CLAUSE = re.compile(
    r"\bfor\s+(update|share)\b|\binto\s+(outfile|dumpfile)\b", re.I
)


def assert_readonly_sql(sql: str) -> str:
    """发送前语句白名单校验：仅允许单条只读 SELECT；白名单外直接拒绝。"""
    text = (sql or "").strip()
    if not _ALLOWED_START.match(text):
        raise StatisticsCollectionError(
            f"语句白名单校验失败（仅允许只读 SELECT，拒绝执行）: {sql!r}"
        )
    without_trailing = text.rstrip().rstrip(";")
    if ";" in without_trailing:
        raise StatisticsCollectionError(
            f"语句白名单校验失败（禁止多语句）: {sql!r}"
        )
    if _FORBIDDEN_CLAUSE.search(without_trailing):
        raise StatisticsCollectionError(
            f"语句白名单校验失败（禁止锁定/导出子句）: {sql!r}"
        )
    return without_trailing


# ---------- 凭据脱敏（A5：采集日志只记录身份标签） ----------

_CREDENTIAL_RE = re.compile(
    r"^(?P<scheme>[A-Za-z0-9+]+)://(?P<user>[^:@/]*)(?::[^@/]*)?@(?P<rest>.+)$"
)


def credential_label(dsn: str | None) -> str | None:
    """DSN → 脱敏凭据身份：保留 scheme/user/host/db，剔除密码。"""
    if not dsn:
        return None
    m = _CREDENTIAL_RE.match(dsn)
    if m:
        return f"{m.group('scheme')}://{m.group('user')}@{m.group('rest')}"
    return dsn


# ---------- 敏感列识别（T2.3/T2.7） ----------

_SENSITIVE_NAME_RE = re.compile(
    r"(name|phone|mobile|msisdn|tel|id_?card|idcard|identity|email|address|birth"
    r"|姓名|手机|身份证|地址)",
    re.I,
)


def is_sensitive_column(column: str, data_type: str = "") -> bool:
    """按列名识别"姓名/手机号/身份证"形敏感字段（类型仅辅助判断，名字为主）。"""
    del data_type
    return bool(_SENSITIVE_NAME_RE.search(column or ""))


# ---------- 采集配置（T2.3：采样率/超时/行数上限/披露保护阈值可配置） ----------

class CollectorSettings(BaseModel):
    """采集安全参数（默认取自 PtdeConfig；可独立覆盖用于试验）。"""

    model_config = ConfigDict(extra="forbid")

    sampling_rate: float = 1.0  # 0 < rate <= 1
    timeout_seconds: int = 30  # > 0（采集窗口/超时）
    max_rows: int = 100_000  # 单次采样行数上限（> 0）
    top_k: int = 20  # Top-K 条目数（> 0）
    min_bucket_samples: int = 5  # 小样本类别披露保护阈值（>= 1）
    high_cardinality_ratio: float = 0.5  # distinct/rows 超过则视为高基数

    @field_validator("sampling_rate")
    @classmethod
    def _rate_range(cls, v: float) -> float:
        if not 0 < v <= 1:
            raise ValueError("sampling_rate 必须在 (0, 1] 区间")
        return v

    @field_validator("timeout_seconds", "max_rows", "top_k", "min_bucket_samples")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("超时/行数上限/Top-K/披露阈值必须为正整数")
        return v

    @classmethod
    def from_config(cls, config: PtdeConfig) -> CollectorSettings:
        return cls(
            sampling_rate=config.sampling_rate,
            timeout_seconds=config.collect_timeout_seconds,
            max_rows=config.collect_max_rows,
        )


# ---------- 采集执行清单（T2.1：A5 warning / 凭据身份 / 语句审计） ----------

class CollectionManifest(BaseModel):
    """采集执行清单：可断言的 A5 告警、脱敏凭据身份与已执行语句审计。"""

    warnings: list[str] = Field(default_factory=list)
    fallback_same_value: bool = False
    credential_label: str | None = None
    statements: list[str] = Field(default_factory=list)  # 全部实际发送的 SQL
    tables: list[str] = Field(default_factory=list)
    sampling_rate: float = 1.0
    timeout_seconds: int = 30
    max_rows: int = 100_000
    started_at: datetime | None = None
    finished_at: datetime | None = None


# ---------- 拟合（热点倾斜：Top-K 占比 + Zipf 拟合，复用 legacy 参数化思路） ----------

def fit_zipf(ratios: list[float]) -> tuple[float | None, float | None]:
    """对降序占比做 log f_r = c - s·log r 最小二乘拟合；返回 (exponent, r2)。

    点数不足（< 3）或无法求解时返回 (None, None)——不得伪装拟合成功。
    """
    pts = [(i + 1, r) for i, r in enumerate(ratios) if r and r > 0]
    if len(pts) < 3:
        return None, None
    xs = [math.log(float(rank)) for rank, _ in pts]
    ys = [math.log(float(freq)) for _, freq in pts]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    if sxx == 0:
        return None, None
    slope = sxy / sxx
    exponent = -slope
    if exponent <= 0:
        return None, None
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum(
        (y - (my + slope * (x - mx))) ** 2 for x, y in zip(xs, ys, strict=True)
    )
    quality = 1.0 - ss_res / ss_tot if ss_tot > 0 else None
    return exponent, quality


# ---------- 只读查询守卫 ----------

class _GuardedQuery:
    """采集器统一查询入口：白名单校验 → 执行 → 审计记录；失败带对象名显式上抛。"""

    def __init__(self, conn, manifest: CollectionManifest, product: str) -> None:
        self._conn = conn
        self._manifest = manifest
        self._product = product

    def __call__(self, sql: str, params: tuple = (), obj: str = "") -> list[tuple]:
        readonly = assert_readonly_sql(sql)  # 白名单外直接拒绝（不入审计）
        self._manifest.statements.append(readonly)
        cur = self._conn.cursor()
        try:
            cur.execute(readonly, params)
            return list(cur.fetchall())
        except StatisticsCollectionError:
            raise
        except Exception as exc:  # noqa: BLE001 A3 硬约束：显式失败，不静默降级
            raise StatisticsCollectionError(
                f"{self._product} statistics query failed for {obj}: {exc}"
            ) from exc
        finally:
            cur.close()


# ---------- 默认连接（生产路径；测试一律注入 connect 工厂，鸭子类型假连接） ----------

def _default_connect(dsn: str, timeout_seconds: int):
    parsed = urlsplit(dsn)
    scheme = (parsed.scheme or "").lower()
    if scheme == "mysql":
        try:
            import pymysql
        except ImportError as exc:  # pragma: no cover - 环境差异路径
            raise RuntimeError(
                "MySQL 采集需要 pymysql：uv add pymysql"
            ) from exc
        return pymysql.connect(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 3306,
            user=parsed.username or "",
            password=parsed.password or "",
            database=(parsed.path or "").lstrip("/") or None,
            charset="utf8mb4",
            read_timeout=timeout_seconds,
            write_timeout=timeout_seconds,
        )
    if scheme in {"postgres", "postgresql"}:
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - 环境差异路径
            try:
                import psycopg2 as psycopg  # type: ignore[import-not-found,no-redef]
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL 采集需要 psycopg/psycopg2：uv add psycopg[binary]"
                ) from exc
        return psycopg.connect(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 5432,
            user=parsed.username or "",
            password=parsed.password or "",
            dbname=(parsed.path or "").lstrip("/") or None,
            connect_timeout=timeout_seconds,
        )
    raise StatisticsCollectionError(
        f"不支持的采集 DSN scheme: {dsn!r}（仅支持 mysql/postgres）"
    )


_ADAPTER_REGISTRY: dict[str, type] = {
    "mysql": MysqlStatisticsAdapter,
    "postgres": PostgresStatisticsAdapter,
    "postgresql": PostgresStatisticsAdapter,
}


def adapter_for_dsn(dsn: str):
    """按 DSN scheme 路由统计子适配器；白名单外 scheme 直接拒绝。"""
    scheme = (urlsplit(dsn).scheme or "").lower()
    cls = _ADAPTER_REGISTRY.get(scheme)
    if cls is None:
        raise StatisticsCollectionError(
            f"不支持的采集 DSN scheme: {dsn!r}（仅支持 mysql/postgres）"
        )
    return cls()


# ---------- 采集器 ----------

_NUMERIC_TYPES = {
    "int", "integer", "bigint", "smallint", "mediumint", "tinyint",
    "decimal", "numeric", "float", "double", "real", "serial", "bigserial",
}
_TEMPORAL_TYPES = {"datetime", "date", "timestamp", "timestamptz", "time"}
_STRING_TYPES = {
    "char", "varchar", "bpchar", "text", "tinytext", "mediumtext",
    "longtext", "uuid", "citus_text",
}


def _percentile(sorted_vals: list, p: float) -> float | None:
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100.0
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(sorted_vals[int(k)])
    return float(sorted_vals[f]) * (c - k) + float(sorted_vals[c]) * (k - f)


def _temporal_buckets(values: list, now: datetime) -> dict[str, float]:
    """相对时间分桶：last_7_days/last_30_days/last_90_days/older 占比。"""
    parsed: list[datetime] = []
    for v in values:
        if isinstance(v, datetime):
            parsed.append(v)
        elif isinstance(v, str):
            try:
                parsed.append(datetime.fromisoformat(v.replace("T", " ")))
            except ValueError:
                continue
    if not parsed:
        return {}
    buckets = {"last_7_days": 0, "last_30_days": 0, "last_90_days": 0, "older": 0}
    for dt in parsed:
        age = now - dt if dt.tzinfo is None or now.tzinfo is None else (now - dt)
        days = age.days if hasattr(age, "days") else 0
        if days < 0:
            days = 0
        if days <= 7:
            buckets["last_7_days"] += 1
        elif days <= 30:
            buckets["last_30_days"] += 1
        elif days <= 90:
            buckets["last_90_days"] += 1
        else:
            buckets["older"] += 1
    total = sum(buckets.values())
    return {name: cnt / total for name, cnt in buckets.items()}


class StatisticsCollector:
    """只读统计采集器（orchestrator：方言子适配器产出 SQL 原语，这里组装画像）。

    用法：
        collector = StatisticsCollector(config, connect=fake_connect)
        profile, manifest = collector.collect(tables=["orders"], source_type="db_readonly")
    """

    def __init__(
        self,
        config: PtdeConfig,
        adapter=None,
        connect=None,
        settings: CollectorSettings | None = None,
    ) -> None:
        self._config = config
        self._settings = settings or CollectorSettings.from_config(config)
        self._adapter = adapter
        self._connect = connect

    # ---- 主入口 ----

    def collect(
        self,
        tables: list[str] | None = None,
        source_type: str = SOURCE_TYPE_DB_READONLY,
        profile_id: str = "",
    ) -> tuple[StatisticsProfile, CollectionManifest]:
        """完整采集：返回（画像, 执行清单）。

        A5：连接一律使用 resolve_dsns().profile_dsn；回退告警进清单（可断言）。
        T2.5：source_type="test_db_self" 即测试库自画像。
        """
        resolution = self._config.resolve_dsns()
        now = datetime.now(UTC)
        manifest = CollectionManifest(
            warnings=list(resolution.warnings),
            fallback_same_value=resolution.fallback_same_value,
            credential_label=credential_label(resolution.profile_dsn),
            sampling_rate=self._settings.sampling_rate,
            timeout_seconds=self._settings.timeout_seconds,
            max_rows=self._settings.max_rows,
            started_at=now,
        )
        conn = self._open(resolution.profile_dsn)
        adapter = self._adapter if self._adapter is not None else adapter_for_dsn(
            resolution.profile_dsn
        )
        q = _GuardedQuery(conn, manifest, adapter.product)
        try:
            if tables is None:
                tables = adapter.list_tables(q)
            tables = list(tables)
            manifest.tables = tables
            table_profiles = [self._collect_table(q, adapter, t) for t in tables]
        finally:
            manifest.finished_at = datetime.now(UTC)
        profile = StatisticsProfile(
            id=profile_id or f"{source_type}-{now.strftime('%Y%m%d%H%M%S')}",
            source=ProfileSource(
                type=source_type,
                database_type=adapter.product,
                collected_at=now,
                sampling_rate=self._settings.sampling_rate,
                accuracy=self._overall_accuracy(table_profiles),
                credential_label=manifest.credential_label,
            ),
            tables=table_profiles,
            collected_at=now,
        ).seal()
        return profile, manifest

    def collect_test_db_self(
        self, tables: list[str] | None = None, profile_id: str = ""
    ) -> tuple[StatisticsProfile, CollectionManifest]:
        """T2.5：目标测试库自画像（R2 验收 + R3 校准输入）。"""
        return self.collect(
            tables=tables,
            source_type=SOURCE_TYPE_TEST_DB_SELF,
            profile_id=profile_id,
        )

    # ---- 内部 ----

    def _open(self, dsn: str):
        if self._connect is not None:
            return self._connect(dsn)
        return _default_connect(dsn, self._settings.timeout_seconds)

    @staticmethod
    def _overall_accuracy(table_profiles: list[TableProfile]) -> str:
        levels = [t.row_count_accuracy for t in table_profiles]
        if levels and all(lv == ACCURACY_EXACT for lv in levels):
            return ACCURACY_EXACT
        if levels and all(lv in {ACCURACY_EXACT, ACCURACY_APPROXIMATE} for lv in levels):
            return ACCURACY_APPROXIMATE
        return ACCURACY_ESTIMATED

    def _sample_size(self, rows: int | None) -> int:
        """采样行数：ceil(rows × 采样率)，夹在 [1, max_rows]。"""
        base = rows if rows and rows > 0 else self._settings.max_rows
        return max(1, min(self._settings.max_rows, math.ceil(base * self._settings.sampling_rate)))

    def _collect_table(self, q, adapter, table: str) -> TableProfile:
        validate_identifier(table)
        tp = TableProfile(table=table)
        rows, accuracy = adapter.row_count(q, table)
        tp.approximate_rows = rows
        tp.row_count_accuracy = accuracy
        col_types = adapter.column_types(q, table)
        sample_n = self._sample_size(rows)
        for col, ctype in col_types.items():
            validate_identifier(col)
            tp.columns.append(
                self._profile_column(q, adapter, table, col, ctype, sample_n, rows)
            )
        parent_rows_cache: dict[str, int | None] = {}
        for child_col, parent_table, parent_col in adapter.foreign_keys(q, table):
            validate_identifier(child_col)
            validate_identifier(parent_table)
            validate_identifier(parent_col)
            if parent_table not in parent_rows_cache:
                parent_rows_cache[parent_table] = adapter.row_count(q, parent_table)[0]
            tp.relations.append(
                self._profile_relation(
                    q, adapter, table, child_col, parent_table, parent_col,
                    parent_rows_cache[parent_table],
                )
            )
        return tp

    def _profile_column(self, q, adapter, table: str, col: str, ctype: str,
                        sample_n: int, rows: int | None) -> ColumnProfile:
        sensitive = is_sensitive_column(col, ctype)
        is_numeric = ctype in _NUMERIC_TYPES
        is_temporal = ctype in _TEMPORAL_TYPES
        is_string = ctype in _STRING_TYPES or ctype.endswith("text")
        cp = ColumnProfile(column=col)

        # 敏感列全程走哈希变体（采样子查询内层即为 SQL 侧哈希，明文不出库）
        if sensitive:
            cp.distinct_count = adapter.sampled_distinct_hashed(q, table, col, sample_n)
            cp.null_ratio = adapter.sampled_null_ratio_hashed(q, table, col, sample_n)
            buckets = adapter.sampled_top_k_hashed(q, table, col, sample_n, self._settings.top_k)
        else:
            cp.distinct_count = adapter.sampled_distinct(q, table, col, sample_n)
            cp.null_ratio = adapter.sampled_null_ratio(q, table, col, sample_n)
            buckets = adapter.sampled_top_k(q, table, col, sample_n, self._settings.top_k)
        cp.top_k = self._top_k_with_suppression(buckets)

        # 分布拟合（分类 / 数值直方图 / 时间分桶 / 倾斜）
        ratios = [entry.ratio for entry in cp.top_k if entry.bucket != "OTHER"]
        if is_numeric and not sensitive:
            values = sorted(
                v for v in adapter.sample_column(q, table, col, sample_n) if v is not None
            )
            if values:
                p50 = _percentile(values, 50)
                p95 = _percentile(values, 95)
                cp.quantiles = {"p50": p50, "p95": p95}
                cp.distribution = DistributionFit(
                    type="numeric_histogram",
                    params={
                        "min": float(values[0]),
                        "max": float(values[-1]),
                        "p50": p50,
                        "p95": p95,
                    },
                    fitted=True,
                )
                cp.accuracy = ACCURACY_ESTIMATED
        elif is_temporal and not sensitive:
            values = adapter.sample_column(q, table, col, sample_n)
            now = datetime.now(UTC)
            buckets_ratio = _temporal_buckets(values, now)
            if buckets_ratio:
                cp.distribution = DistributionFit(
                    type="temporal", params={"buckets": buckets_ratio}, fitted=True
                )
                cp.accuracy = ACCURACY_ESTIMATED
        elif ratios:
            exponent, quality = fit_zipf(ratios)
            if exponent is not None:
                cp.distribution = DistributionFit(
                    type="skewed",
                    params={"fitted_model": "zipf", "fitted_parameter": round(exponent, 4)},
                    fitted=True,
                )
                cp.fit_quality = quality
                cp.accuracy = ACCURACY_FITTED
            else:
                cp.distribution = DistributionFit(
                    type="weighted_categorical",
                    params={entry.bucket: entry.ratio for entry in cp.top_k},
                    fitted=True,
                )
                cp.accuracy = ACCURACY_ESTIMATED

        # 字符长度分布（长度统计不泄露取值，敏感列同样安全）
        if is_string and not is_numeric and not is_temporal:
            stats = adapter.sampled_length_stats(q, table, col, sample_n)
            if stats and stats[0] is not None:
                cp.length_stats = LengthStats(min=stats[0], max=stats[1], avg=stats[2])
        return cp

    def _top_k_with_suppression(self, buckets: list[tuple]) -> list[TopKEntry]:
        """Top-K + 小样本类别披露保护：低于阈值的小类别并入 OTHER（防反推个体）。"""
        total = sum(cnt for _, cnt in buckets)
        if not buckets or total <= 0:
            return []
        entries: list[TopKEntry] = []
        suppressed = 0
        for bucket, cnt in buckets:
            if cnt < self._settings.min_bucket_samples:
                suppressed += cnt
                continue
            entries.append(TopKEntry(bucket=str(bucket), count=cnt, ratio=cnt / total))
        if suppressed:
            entries.append(
                TopKEntry(bucket="OTHER", count=suppressed, ratio=suppressed / total)
            )
        return entries

    def _profile_relation(self, q, adapter, child: str, child_col: str,
                          parent: str, parent_col: str,
                          parent_rows: int | None) -> RelationCardinalityProfile:
        counts = adapter.relation_counts(
            q, child, child_col, parent, parent_col, self._settings.top_k
        )
        rcp = RelationCardinalityProfile(child_table=child, parent_table=parent)
        total = sum(cnt for _, cnt in counts)
        if counts and total > 0:
            ratios = [cnt / total for _, cnt in counts]
            exponent, quality = fit_zipf(ratios)
            if exponent is not None:
                rcp.child_per_parent_distribution = DistributionFit(
                    type="skewed",
                    params={"fitted_model": "zipf", "fitted_parameter": round(exponent, 4)},
                    fitted=True,
                )
            else:
                rcp.child_per_parent_distribution = DistributionFit(
                    type="weighted_categorical",
                    params={bucket: cnt / total for bucket, cnt in counts},
                    fitted=True,
                )
        distinct_parents = adapter.distinct_keys(q, child, child_col)
        if distinct_parents is not None and parent_rows and parent_rows > 0:
            rcp.zero_child_ratio = max(0.0, 1.0 - distinct_parents / parent_rows)
        return rcp
