"""Adapter 分层接口与 SPI 注册（R5 T5.1，实施方案 §8.2；总方案 §9.2/§9.3）。

分层（总方案 §9.2，本批次落 T5.1 任务表点名七个子接口）：
  - MetadataAdapter        → R1 落位（metadata.py，采集器语义）；
  - StatisticsAdapter      → R2 落位（statistics.py，统计原语语义）；
  - CapabilityProbeAdapter → T5.2 probe.py（能力探测，非硬编码）；
  - BulkLoadAdapter        → T5.3/T5.4 原生批量装载（LOAD DATA / COPY）；
  - ConstraintAdapter      → 约束操作原语（环依赖两阶段装载的关闭/恢复/回填）；
  - TransactionAdapter     → 事务边界原语（延迟约束/分阶段装载提交点）；
  - StorageMetricsAdapter  → 存储度量（对齐 planner.pilot_run.SqlStorageProbe 语义）。

SPI 注册机制沿用 legacy ``datagen/spi.py`` 的 ``DatabaseDialect`` 语义映射
（A1 分轨：legacy 本体保留不动，仅取语义，不引其对象）：
  - ``DatabaseDialect.supports(jdbc_url)``      → ``url_prefixes`` 前缀匹配；
  - ``DatabaseDialect.connect``（驱动懒加载）   → ``factory`` 惰性实例化；
  - ``BatchExecutor`` 的注册替换                → ``register_adapter`` 覆盖注册。

约束（勿另起一套）：
  - 接口只认鸭子类型连接（不 import 数据库驱动——驱动由各适配器自行懒加载）；
  - Capability 一律探测填充（总方案 §9.3：不得仅凭产品名硬编码能力）；
  - 失败显式（A3）：BulkLoadError 携带行数核对与错误样例，不静默丢弃记录。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from typing import Any

from pydantic import Field

from ptde_common.model_base import VersionedModel

__all__ = [
    "BulkLoadAdapter",
    "BulkLoadError",
    "BulkLoadUnsupported",
    "BulkLoadResult",
    "CapabilityProbeAdapter",
    "ConstraintAdapter",
    "MetadataAdapter",
    "SPIRegistryError",
    "StatisticsAdapter",
    "StorageMetricsAdapter",
    "TransactionAdapter",
    "adapter_for_url",
    "get_adapter",
    "register_adapter",
    "registered_products",
    "unregister_all",
]


# ============================================================ 结果模型


class BulkLoadResult(VersionedModel):
    """单分片原生装载结果（协议结果侧：行数核对锚点 + 错误/警告可见）。"""

    exclude_fingerprint_fields = frozenset({"collected_at"})

    shard_id: str
    entity: str
    table: str
    strategy: str  # load_data_local / copy_from_stdin
    rows_expected: int = 0
    rows_loaded: int = 0
    rows_verified: bool = False  # rows_loaded == rows_expected（数量复核锚点）
    warnings_count: int = 0  # DB 返回警告计数（可见不吞）
    warning_samples: list[str] = Field(default_factory=list)  # SHOW WARNINGS 样例
    precheck_notes: list[str] = Field(default_factory=list)  # 触发器/分区/RLS 等预检说明
    collected_at: datetime | None = None  # 指纹排除（exclude_fingerprint_fields）

    def to_snapshot(self) -> dict[str, Any]:
        """协议 §7.5：JSON 原生快照（manifest/shard checkpoint 可直接落盘）。"""
        return self.model_dump(mode="json")


class BulkLoadError(Exception):
    """原生装载失败（A3：错误计数/样例可见，不静默丢弃错误记录）。

    QW4 实测口径（tests/poc_load_data_infile.py）：LOAD DATA 对非法行默认
    跳行继续——故行数核对（rows_loaded vs expected）必须显式比对，短装即失败。
    """

    def __init__(
        self,
        message: str,
        *,
        rows_expected: int | None = None,
        rows_loaded: int | None = None,
        error_samples: list[str] | None = None,
        warnings_count: int = 0,
        cleanup_hint: str | None = None,
    ) -> None:
        self.rows_expected = rows_expected
        self.rows_loaded = rows_loaded
        self.error_samples = list(error_samples or [])
        self.warnings_count = warnings_count
        self.cleanup_hint = cleanup_hint
        detail = message
        if rows_expected is not None and rows_loaded is not None:
            detail += f"（期望 {rows_expected} 行，实际装载 {rows_loaded} 行）"
        if error_samples:
            detail += f"；错误样例 {len(self.error_samples)} 条：" + "；".join(self.error_samples[:3])
        if cleanup_hint:
            detail += f"；{cleanup_hint}"
        super().__init__(detail)


class BulkLoadUnsupported(Exception):
    """原生通道在该目标环境不可用（降级决策输入，显式带原因，不静默降级）。"""


# ============================================================ 七大子接口


class MetadataAdapter(ABC):
    """元数据子接口（总方案 §9.2 Metadata 层）。

    语义映射 legacy ``DatabaseDialect.fetch_metadata``；ptde 侧消费
    DatabaseSchemaSnapshot（R1 采集器已落位，此处只固化接口形状）。
    """

    product: str = ""

    @abstractmethod
    def list_tables(self, conn) -> list[str]: ...

    @abstractmethod
    def db_version(self, conn) -> str | None: ...


class StatisticsAdapter(ABC):
    """统计子接口（总方案 §9.2 Statistics 层）。

    语义映射 legacy ``DatabaseDialect.table_row_count``（精确/估算）与
    R2 统计原语适配器（sampled_* 族）；适配器只提供 SQL 原语，经白名单
    查询回调执行（发送前校验）。
    """

    product: str = ""

    @abstractmethod
    def row_count(self, conn, table: str) -> tuple[int | None, str]:
        """返回 (行数, 口径)；口径 ∈ {"exact", "approximate"}；不可得 → (None, "unavailable")。"""


class CapabilityProbeAdapter(ABC):
    """能力探测子接口（总方案 §9.3/§10：能力探测非硬编码）。"""

    product: str = ""

    @abstractmethod
    def probe(self, conn) -> Any:
        """探测目标环境能力 → AdapterCapability 快照（探测填充，非复制示例）。"""


class BulkLoadAdapter(ABC):
    """原生批量装载子接口（总方案 §9 Bulk Load Engine）。

    契约：
    - 只装 staging 分片文件（file_format.FORMAT_VERSION=csv_v1：首行格式声明
      + 列头 + 数据行），跳过声明与列头、NULL 用 ``\\N`` 与 DB 端 NULL 语义
      天然对齐（勿另起一套行解析——错误记录核对靠 DB 行数回传）；
    - 装载行数 ≠ 期望行数 → BulkLoadError（行数核对锚点，不静默吞差异）；
    - 预检说明（触发器/分区/RLS/权限）随结果回传，可见不吞。
    """

    product: str = ""
    strategy: str = ""

    @abstractmethod
    def load_shard(
        self,
        conn,
        path: str,
        table: str,
        columns: Sequence[str],
        rows_expected: int,
        *,
        shard_id: str = "",
        entity: str = "",
    ) -> BulkLoadResult:
        """装载一个分片文件（流式；返回结果带行数核对与警告样例）。"""

    @abstractmethod
    def ensure_capable(self, conn) -> list[str]:
        """校验该连接上原生通道可用性；不可用 → BulkLoadUnsupported（带原因）。

        返回预检说明列表（如触发器/分区影响）；本方法只判定，不降级。
        """


class ConstraintAdapter(ABC):
    """约束操作原语子接口（总方案 §9.4；环依赖策略承载方）。

    语义来源：generator/cycle_policy.py 的 DEFERRED_CONSTRAINT / STAGED_LOAD
    两阶段装载（B4.2 遗留问题 3）——关闭/恢复约束是显式批准的特殊策略，
    默认保留全部约束，不做隐式开关。
    """

    product: str = ""

    @abstractmethod
    def disable_foreign_key_checks(self, conn) -> None: ...

    @abstractmethod
    def enable_foreign_key_checks(self, conn) -> None: ...

    @abstractmethod
    def list_trigger_names(self, conn, table: str) -> list[str]:
        """目标表触发器清单（预检：触发器外部副作用风险，总方案 §13.3）。"""


class TransactionAdapter(ABC):
    """事务边界原语子接口（总方案 §9.2 Transaction 层）。

    语义映射 legacy ``BatchExecutor`` 逐批 commit 的边界控制 + 协议 §9.6
    失败恢复的「部分提交」判定输入（分片级提交点由 Loader 检查点记录）。
    """

    product: str = ""

    @abstractmethod
    def commit(self, conn) -> None: ...

    @abstractmethod
    def rollback(self, conn) -> None: ...


class StorageMetricsAdapter(ABC):
    """存储度量子接口（总方案 §9.2 Storage Metrics 层）。

    语义对齐 planner.pilot_run.SqlStorageProbe.measure（R3 Pilot Run 已落位，
    勿另起一套口径）：返回 (table_bytes, index_bytes, row_count)。
    """

    product: str = ""

    @abstractmethod
    def measure(self, conn, table: str, include_indexes: bool = True
                ) -> tuple[int, int, int]: ...

    @abstractmethod
    def refresh_statistics(self, conn, table: str) -> None:
        """装载后统计刷新（ANALYZE；§10.1 探测项：数据库统计信息刷新方式）。"""


# ============================================================ SPI 注册机制


class SPIRegistryError(Exception):
    """SPI 注册表冲突/缺失（显式失败，不静默换用其他适配器）。"""


# 注册表：product → (工厂, url 前缀)。工厂惰性调用（驱动懒加载语义映射）。
_REGISTRY: dict[str, tuple[Callable[[], Any], tuple[str, ...]]] = {}


def register_adapter(
    product: str,
    factory: Callable[[], Any],
    *,
    url_prefixes: Sequence[str] = (),
    overwrite: bool = False,
) -> None:
    """注册适配器工厂（SPI 沿用 DatabaseDialect.supports 语义：URL 前缀匹配）。

    重复注册须显式 overwrite=True（防误覆盖——对齐 legacy 单一注册表语义）。
    """
    if not product or not product.strip():
        raise SPIRegistryError("product 不能为空（显式失败）")
    if not callable(factory):
        raise SPIRegistryError(f"{product} 的 factory 必须可调用（显式失败）")
    if product in _REGISTRY and not overwrite:
        raise SPIRegistryError(
            f"adapter '{product}' 已注册（重复注册须显式 overwrite=True）"
        )
    _REGISTRY[product.lower()] = (factory, tuple(url_prefixes))


def get_adapter(product: str) -> Any:
    """按产品名实例化适配器；未注册显式失败（不静默换用）。"""
    entry = _REGISTRY.get((product or "").lower())
    if entry is None:
        raise SPIRegistryError(
            f"未注册的 adapter product: {product!r}"
            f"（已注册: {registered_products()}；显式失败，不静默换用）"
        )
    return entry[0]()


def adapter_for_url(url: str | None) -> Any:
    """按 JDBC/连接 URL 匹配适配器（DatabaseDialect.supports 语义映射）。

    遍历注册表 url_prefixes 前缀匹配；多命中取最长前缀（确定性）；
    无命中显式失败。
    """
    if not url:
        raise SPIRegistryError("URL 为空，无法匹配 adapter（显式失败）")
    matches: list[tuple[int, str]] = []
    for product, (_factory, prefixes) in _REGISTRY.items():
        for prefix in prefixes:
            if url.startswith(prefix):
                matches.append((len(prefix), product))
    if not matches:
        raise SPIRegistryError(
            f"无 adapter 支持 URL: {url!r}（已注册前缀: "
            + ", ".join(f"{p}:{prefixes}" for p, (_f, prefixes) in sorted(_REGISTRY.items()))
            + "；显式失败）"
        )
    matches.sort(reverse=True)  # 最长前缀优先
    return get_adapter(matches[0][1])


def registered_products() -> list[str]:
    """已注册产品清单（确定性排序）。"""
    return sorted(_REGISTRY)


def unregister_all() -> None:
    """清空注册表（仅测试用途；生产代码不得调用）。"""
    _REGISTRY.clear()


def iter_registry() -> Iterator[str]:
    """注册表键迭代（确定性排序；诊断用）。"""
    yield from sorted(_REGISTRY)
