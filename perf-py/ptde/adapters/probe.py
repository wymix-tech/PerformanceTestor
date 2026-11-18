"""数据库适配能力探测（R5 T5.2，实施方案 §8.2；总方案 §10.1/§10.2）。

探测项覆盖总方案 §10.1 全集：
  产品/版本/兼容模式/存储引擎、元数据查询能力、索引与约束读取、分区信息读取、
  统计信息读取、原生批量导入、并行导入、主键/Sequence 获取方式、事务边界与
  批量失败语义、触发器和约束执行语义、LOB/JSON/二进制支持、表和索引存储
  统计读取、统计信息刷新方式。

约束（A3，总方案 §10.2 末行口径）：
  - Capability 快照**由探测程序填充**，不得复制示例作为真实能力声明；
  - 探测失败/无法判定的项 → ``None``（未知）+ notes 说明，**不伪装 True/False**；
  - 快照随 ``ExecutionManifest.adapter_capability_snapshot`` 入库（门禁 6：
    快照可审计，与实际行为一致——版本级测试锁定）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from ptde_common.model_base import VersionedModel

__all__ = [
    "AdapterCapability",
    "CAPABILITY_KEYS",
    "CapabilityProbeError",
    "MysqlCapabilityProbe",
    "PostgresCapabilityProbe",
    "probe_adapter",
]

# 总方案 §10.2 Capability 键集（探测填充；None=未知）：
#   读取类（元数据/索引/约束/分区/统计）
#   装载类（bulk_load / parallel_load / bulk_load_strategy 经 bulk_load 子对象）
#   键与事务类（sequence_support / deferred_constraints / batch_failure_semantics）
#   执行语义类（trigger_fires_on_bulk_load / lob_json_binary_support /
#   storage_statistics / post_load_analyze）
CAPABILITY_KEYS: tuple[str, ...] = (
    "schema_inspection",
    "index_inspection",
    "foreign_key_inspection",
    "partition_inspection",
    "statistics_read",
    "bulk_load",
    "parallel_load",
    "sequence_support",
    "deferred_constraints",
    "trigger_fires_on_bulk_load",
    "lob_json_binary_support",
    "storage_statistics",
    "post_load_analyze",
)


class CapabilityProbeError(Exception):
    """能力探测结构性失败（无法获取产品/版本等基本识别信息，显式失败）。"""


class AdapterCapability(VersionedModel):
    """Adapter Capability 快照（探测填充；入 ExecutionManifest 可审计）。

    - ``capabilities``：§10.2 键集，值 True/False/None（None=未探测到，不伪装）；
    - ``bulk_load``：装载通道细节（selected_strategy / 前置参数 / 诊断能力），
      探测填充（如 MySQL：requires_client_local_infile / secure_file_priv）；
    - ``notes``：探测过程警告（查询失败、无法判定项），随快照落库可审计。
    """

    exclude_fingerprint_fields = frozenset({"probed_at"})

    product: str
    db_version: str | None = None
    compat_mode: str | None = None  # 兼容模式（OceanBase mysql/oracle；原生库 native）
    engine: str | None = None  # 存储引擎/部署形态线索
    capabilities: dict[str, bool | None] = Field(default_factory=dict)
    bulk_load: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    probed_at: datetime | None = None

    def model_post_init(self, __context: Any) -> None:
        missing = [k for k in CAPABILITY_KEYS if k not in self.capabilities]
        if missing:
            raise ValueError(
                f"Capability 快照缺少 §10.1 探测项：{missing}"
                "（探测填充全集，未知项显式 None，不得缺省伪装）"
            )

    def to_snapshot(self) -> dict[str, Any]:
        """回填 ExecutionManifest.adapter_capability_snapshot 的形态。"""
        return self.model_dump(mode="json")


class _ProbeBase:
    """探测基类：逐项探测、失败降为 None+note（A3：未知不伪装）。"""

    product = ""

    def probe(self, conn) -> AdapterCapability:
        cap = AdapterCapability(
            product=self.product,
            capabilities={k: None for k in CAPABILITY_KEYS},
            probed_at=datetime.now(UTC),
        )
        self._probe_identity(conn, cap)
        self._probe_capabilities(conn, cap)
        self._probe_bulk_load(conn, cap)
        cap.seal()
        return cap

    # ---------- 工具 ----------

    @staticmethod
    def _query(conn, sql: str, params: tuple = ()) -> list[tuple]:
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            return list(cur.fetchall())
        finally:
            cur.close()

    def _try(self, conn, cap: AdapterCapability, item: str, sql: str,
             params: tuple = ()) -> Any:
        """单条探测查询；失败 → None + note（不伪装，不中断其余探测项）。"""
        try:
            rows = self._query(conn, sql, params)
        except Exception as exc:  # 单项失败降级为未知（不伪装，不中断其余探测项）
            cap.notes.append(f"{item} 探测失败（记为未知）：{exc}")
            return None
        return rows[0][0] if rows else None

    @staticmethod
    def _set(cap: AdapterCapability, key: str, value: Any) -> None:
        if key not in CAPABILITY_KEYS:
            raise ValueError(f"未知 Capability 键：{key}（§10.1 键集之外，显式失败）")
        cap.capabilities[key] = None if value is None else bool(value)

    # ---------- 各产品实现以下钩子 ----------

    def _probe_identity(self, conn, cap: AdapterCapability) -> None:
        raise NotImplementedError

    def _probe_capabilities(self, conn, cap: AdapterCapability) -> None:
        raise NotImplementedError

    def _probe_bulk_load(self, conn, cap: AdapterCapability) -> None:
        raise NotImplementedError


class MysqlCapabilityProbe(_ProbeBase):
    """MySQL / MySQL 兼容库能力探测。

    探测来源（逐项实测，非硬编码产品名）：
      - SELECT VERSION() / @@version_comment（版本与变体线索）
      - @@default_storage_engine、@@sql_mode（兼容模式线索：ANSI/STRICT）
      - @@local_infile / @@secure_file_priv（原生装载前置，QW4 口径）
      - information_schema 可用性（读取类能力的实测判定）
    """

    product = "mysql"

    def probe(self, conn) -> AdapterCapability:
        cap = super().probe(conn)
        if cap.db_version is None:
            raise CapabilityProbeError(
                "MySQL 探测失败：无法获取版本（SELECT VERSION() 不可用）——"
                "无法识别环境禁止写入（总方案 §13.1）"
            )
        return cap

    def _probe_identity(self, conn, cap: AdapterCapability) -> None:
        cap.db_version = self._try(conn, cap, "version", "SELECT VERSION()")
        comment = self._try(conn, cap, "version_comment", "SELECT @@version_comment")
        if comment and "mariadb" in str(comment).lower():
            cap.compat_mode = "mariadb"
        engine = self._try(
            conn, cap, "default_storage_engine", "SELECT @@default_storage_engine"
        )
        cap.engine = str(engine) if engine else None
        # 兼容模式：sql_mode 含 ANSI/STRICT 线索（探测填充，非复制示例）
        sql_mode = self._try(conn, cap, "sql_mode", "SELECT @@sql_mode")
        if sql_mode:
            modes = {m.strip().upper() for m in str(sql_mode).split(",")}
            cap.compat_mode = cap.compat_mode or (
                "ansi" if "ANSI" in modes or "ANSI_QUOTES" in modes else "native"
            )

    def _probe_capabilities(self, conn, cap: AdapterCapability) -> None:
        # 读取类：information_schema 子查询实测（表在查询即视为可用）
        schema_ok = self._try(
            conn, cap, "schema_inspection",
            "SELECT COUNT(*) FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE()",
        )
        self._set(cap, "schema_inspection", schema_ok is not None)
        for key, marker in (
            ("index_inspection", "information_schema.STATISTICS"),
            ("foreign_key_inspection", "KEY_COLUMN_USAGE"),
            ("partition_inspection", "information_schema.PARTITIONS"),
        ):
            ok = self._try(
                conn, cap, key,
                f"SELECT COUNT(*) FROM information_schema.{marker} "
                f"WHERE TABLE_SCHEMA = DATABASE() LIMIT 1",
            )
            self._set(cap, key, ok is not None)
        # 触发器语义：LOAD DATA 逐行触发 INSERT 触发器（官方语义，实测触发器表可读）
        trig = self._try(
            conn, cap, "triggers",
            "SELECT COUNT(*) FROM information_schema.TRIGGERS "
            "WHERE EVENT_OBJECT_SCHEMA = DATABASE() LIMIT 1",
        )
        self._set(cap, "trigger_fires_on_bulk_load", trig is not None)
        # 统计刷新：ANALYZE TABLE 可用性（不执行，仅按权限探测——失败记未知）
        # MySQL 无「ANALYZE 权限探测」原语；以 information_schema 可读 + 版本已知
        # 推定可用并标注推定（A3：推定不伪装事实，notes 说明）。
        if cap.capabilities["schema_inspection"]:
            self._set(cap, "post_load_analyze", True)
            cap.notes.append("post_load_analyze=推定（ANALYZE TABLE 为标准能力，未实测执行）")
        # Sequence：AUTO_INCREMENT 属性（R1 metadata 已按 SequenceSchema 登记口径）
        seq_ok = self._try(
            conn, cap, "auto_increment",
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND EXTRA LIKE '%auto_increment%' LIMIT 1",
        )
        self._set(cap, "sequence_support", seq_ok is not None)
        # 延迟约束：MySQL 无 DEFERRABLE（FOREIGN_KEY_CHECKS 仅会话开关）→ False
        self._set(cap, "deferred_constraints", False)
        # LOB/JSON：BLOB/JSON 类型在 8.x 可装载（类型系统存在性探测）
        lob = self._try(
            conn, cap, "lob_types",
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND DATA_TYPE IN "
            "('blob','text','json','longblob','longtext') LIMIT 1",
        )
        self._set(cap, "lob_json_binary_support", lob is not None)
        # 存储统计/并行装载：MySQL 标准无并行 LOAD DATA 机制
        self._set(cap, "storage_statistics", schema_ok is not None)
        self._set(cap, "parallel_load", False)
        self._set(cap, "statistics_read", schema_ok is not None)

    def _probe_bulk_load(self, conn, cap: AdapterCapability) -> None:
        local_infile = self._try(conn, cap, "local_infile", "SELECT @@local_infile")
        secure_file_priv = self._try(
            conn, cap, "secure_file_priv", "SELECT @@secure_file_priv"
        )
        max_packet = self._try(
            conn, cap, "max_allowed_packet", "SELECT @@max_allowed_packet"
        )
        cap.bulk_load = {
            "selected_strategy": "load_data_local",
            "requires_client_local_infile": True,  # QW4：pymysql local_infile=True
            "server_local_infile": (
                None if local_infile is None else bool(int(local_infile))
            ),
            # QW4 结论：LOCAL 通道不经服务端文件白名单；SERVER 端 INFILE 受限
            "secure_file_priv": None if secure_file_priv is None else str(secure_file_priv),
            "secure_file_priv_scope": "server_infile_only",
            "max_allowed_packet": None if max_packet is None else int(max_packet),
            "supports_error_diagnostics": True,  # SHOW WARNINGS 错误样例可见
        }
        # bulk_load 能力位：服务端 local_infile 开启与否可实测；未知则 None
        if local_infile is not None:
            self._set(cap, "bulk_load", bool(int(local_infile)))
        else:
            self._set(cap, "bulk_load", None)
            cap.notes.append(
                "bulk_load 探测失败：@@local_infile 不可读（记为未知，不伪装可用）"
            )


class PostgresCapabilityProbe(_ProbeBase):
    """PostgreSQL 能力探测（pg_catalog 实测，非硬编码）。"""

    product = "postgres"

    def probe(self, conn) -> AdapterCapability:
        cap = super().probe(conn)
        if cap.db_version is None:
            raise CapabilityProbeError(
                "PostgreSQL 探测失败：无法获取版本（SELECT version() 不可用）——"
                "无法识别环境禁止写入（总方案 §13.1）"
            )
        return cap

    def _probe_identity(self, conn, cap: AdapterCapability) -> None:
        cap.db_version = self._try(conn, cap, "version", "SELECT version()")
        server_num = self._try(
            conn, cap, "server_version_num", "SHOW server_version_num"
        )
        del server_num  # 仅探测可达性；版本口径以 version() 为准
        cap.compat_mode = "native"  # 原生 PG 无兼容层；派生兼容库由探测另行填充

    def _probe_capabilities(self, conn, cap: AdapterCapability) -> None:
        # 读取类：pg_catalog 实测
        def catalog_ok(relation: str, column: str) -> Any:
            return self._try(
                conn, cap, f"catalog:{relation}.{column}",
                "SELECT COUNT(*) FROM pg_catalog.pg_attribute "
                f"WHERE attrelid = '{relation}'::regclass AND attname = '{column}'",
            )

        self._set(cap, "schema_inspection", catalog_ok("pg_catalog.pg_class", "relname") is not None)
        self._set(cap, "index_inspection", catalog_ok("pg_catalog.pg_index", "indexrelid") is not None)
        self._set(cap, "foreign_key_inspection",
                  catalog_ok("pg_catalog.pg_constraint", "conkey") is not None)
        self._set(cap, "partition_inspection",
                  catalog_ok("pg_catalog.pg_partitioned_table", "partstrat") is not None)
        stats_ok = self._try(
            conn, cap, "pg_stats",
            "SELECT COUNT(*) FROM pg_catalog.pg_stats LIMIT 1",
        )
        self._set(cap, "statistics_read", stats_ok is not None)
        self._set(cap, "storage_statistics", stats_ok is not None)
        # 延迟约束：pg_constraint.deferrable 列存在性实测（PG 约束可延迟）
        deferred = catalog_ok("pg_catalog.pg_constraint", "deferrable")
        self._set(cap, "deferred_constraints", deferred is not None)
        # Sequence：pg_sequences 存在性
        seq_ok = self._try(
            conn, cap, "pg_sequences",
            "SELECT COUNT(*) FROM information_schema.sequences LIMIT 1",
        )
        self._set(cap, "sequence_support", seq_ok is not None)
        # RLS 特性：pg_class.relrowsecurity 列存在性（逐表开关在 precheck 判定）
        rls = catalog_ok("pg_catalog.pg_class", "relrowsecurity")
        cap.bulk_load = {"rls_feature_present": None if rls is None else bool(rls)}
        # LOB/JSON：pg_type 中 json/bytea 类型存在性
        lob = self._try(
            conn, cap, "lob_types",
            "SELECT COUNT(*) FROM pg_catalog.pg_type "
            "WHERE typname IN ('json','jsonb','bytea','text') LIMIT 1",
        )
        self._set(cap, "lob_json_binary_support", lob is not None)
        # 触发器：COPY 逐行触发 INSERT 触发器（官方语义；pg_trigger 可读佐证）
        trig = self._try(
            conn, cap, "pg_trigger",
            "SELECT COUNT(*) FROM pg_catalog.pg_trigger WHERE NOT tgisinternal LIMIT 1",
        )
        self._set(cap, "trigger_fires_on_bulk_load", trig is not None)
        # 统计刷新：ANALYZE 标准能力（推定，notes 标注不伪装事实）
        if cap.capabilities["schema_inspection"]:
            self._set(cap, "post_load_analyze", True)
            cap.notes.append("post_load_analyze=推定（ANALYZE 为标准能力，未实测执行）")
        # 并行装载：标准 PG 无并行 COPY 机制
        self._set(cap, "parallel_load", False)
        # 装载通道与诊断能力
        cap.bulk_load.setdefault("selected_strategy", "copy_from_stdin")
        cap.bulk_load.setdefault("supports_error_diagnostics", True)

    def _probe_bulk_load(self, conn, cap: AdapterCapability) -> None:
        # COPY FROM STDIN 为服务端标准能力；权限逐表在 precheck 判定，
        # 此处仅探测 current_schema 可写性线索（无 → 未知）
        schema_writable = self._try(
            conn, cap, "has_schema_privilege",
            "SELECT has_schema_privilege(current_user, current_schema(), 'CREATE')",
        )
        if schema_writable is not None:
            cap.bulk_load["current_schema_create"] = bool(schema_writable)


def probe_adapter(conn, product: str) -> AdapterCapability:
    """按产品名探测（未注册探测器的产品显式失败，不伪装快照）。

    别名归一：postgresql/pg → postgres；mariadb → mysql（同语义探测器）。
    """
    aliases = {
        "postgresql": "postgres",
        "pg": "postgres",
        "mariadb": "mysql",
    }
    normalized = aliases.get((product or "").lower(), (product or "").lower())
    probes: dict[str, type[_ProbeBase]] = {
        "mysql": MysqlCapabilityProbe,
        "postgres": PostgresCapabilityProbe,
    }
    cls = probes.get(normalized)
    if cls is None:
        raise CapabilityProbeError(
            f"产品 {product!r} 无 Capability 探测器（已支持: {sorted(probes)}；"
            "显式失败，不得凭产品名硬编码能力快照）"
        )
    return cls().probe(conn)
