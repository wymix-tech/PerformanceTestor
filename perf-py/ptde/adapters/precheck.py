"""装载前目标表检查（R5 T5.6，实施方案 §8.2；总方案 §9.4 逐项）。

检查项全集（§9.4，逐项报告，缺项显式标注不静默通过）：
  触发器 / 生成列 / 约束（FK/CHECK/唯一）/ 分区 / LOB·JSON / 自增·Sequence /
  RLS / 复制·日志·事务限制。

口径约束（勿另起一套）：
  - B5 前段 mysql/postgres bulkload 的 ``precheck_notes``（触发器/分区/RLS/
    权限）为起点口径，本模块补齐生成列/自增/复制限制等项并结构化；
  - **默认保留全部约束/触发器**（总方案 §9.4：不能为吞吐默认关闭全部约束）
    ——关闭类特殊策略需显式批准 + 全量验证（§19）；
  - RLS / 只读 / 备库等不可装载项 → ``blocked``（显式拒绝，对齐 T5.4 口径
    ——不由 Loader 静默放行，A3）。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from ptde.adapters.mysql.statistics import validate_identifier
from ptde_common.model_base import VersionedModel

__all__ = [
    "MYSQL_PRECHECK_ROUTES",
    "PG_PRECHECK_ROUTES",
    "PrecheckFinding",
    "PrecheckReport",
    "PrecheckStatus",
    "precheck_table",
]

DEFAULT_KEEP_CONSTRAINTS_NOTE = (
    "默认保留全部约束/触发器（总方案 §9.4：不得为吞吐默认关闭）；"
    "关闭类特殊策略需显式批准 + 全量验证（总方案 §19）"
)


class PrecheckStatus(StrEnum):
    """单项检查结论：ok（无影响）/ note（有影响需关注）/ blocked（禁止装载）。"""

    OK = "ok"
    NOTE = "note"
    BLOCKED = "blocked"


class PrecheckFinding(VersionedModel):
    """§9.4 单项检查结论（逐项结构化，可见不吞）。"""

    item: str  # triggers / generated_columns / constraints / partitioning /
    # lob_json_types / auto_increment_sequence / rls / replication_limits
    status: str = PrecheckStatus.OK.value
    detail: str = ""

    def blocked(self) -> bool:
        return self.status == PrecheckStatus.BLOCKED.value


class PrecheckReport(VersionedModel):
    """目标表预检报告（§8.5 装载策略选择输入；随 ExecutionManifest 可审计）。"""

    product: str
    table: str
    findings: list[PrecheckFinding] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def __init__(self, **data) -> None:
        super().__init__(**data)
        if DEFAULT_KEEP_CONSTRAINTS_NOTE not in self.notes:
            self.notes.append(DEFAULT_KEEP_CONSTRAINTS_NOTE)

    def blocked_items(self) -> list[str]:
        return [f.item for f in self.findings if f.blocked()]

    def is_blocked(self) -> bool:
        return bool(self.blocked_items())

    def finding(self, item: str) -> PrecheckFinding:
        for f in self.findings:
            if f.item == item:
                return f
        raise ValueError(
            f"预检报告缺少 §9.4 检查项 {item!r}（逐项全集，缺项显式失败）"
        )

    def require_loadable(self) -> None:
        """存在 blocked 项时显式失败（不得静默装载）。"""
        if self.is_blocked():
            raise ValueError(
                f"目标表 {self.table} 预检不通过（blocked: {self.blocked_items()}）"
                "——显式拒绝装载，处置建议见对应 finding detail"
            )


# ============================================================ MySQL


class MysqlTablePrecheck:
    """MySQL 逐项检查（information_schema + 会话变量实测；复用 T5.3 查询口径）。"""

    product = "mysql"

    def check(self, conn, table: str) -> PrecheckReport:
        validate_identifier(table)
        report = PrecheckReport(product=self.product, table=table)
        report.findings.append(self._triggers(conn, table))
        report.findings.append(self._generated_columns(conn, table))
        report.findings.append(self._constraints(conn, table))
        report.findings.append(self._partitioning(conn, table))
        report.findings.append(self._lob_json(conn, table))
        report.findings.append(self._auto_increment(conn, table))
        report.findings.append(self._rls(conn, table))
        report.findings.append(self._replication_limits(conn, table))
        return report

    def _triggers(self, conn, table: str) -> PrecheckFinding:
        rows = _query(conn, (
            "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
            "WHERE EVENT_OBJECT_SCHEMA = DATABASE() AND EVENT_OBJECT_TABLE = %s "
            "ORDER BY TRIGGER_NAME"
        ), (table,))
        if rows:
            return PrecheckFinding(
                item="triggers", status=PrecheckStatus.NOTE.value,
                detail=(
                    f"目标表存在触发器 {', '.join(r[0] for r in rows)}：装载逐行触发，"
                    "吞吐受影响；触发器外部副作用（消息/邮件/支付）默认隔离——"
                    "须确认后装载（总方案 §13.3）"
                ),
            )
        return PrecheckFinding(item="triggers", detail="无触发器")

    def _generated_columns(self, conn, table: str) -> PrecheckFinding:
        rows = _query(conn, (
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND GENERATION_EXPRESSION <> ''"
        ), (table,))
        if rows:
            return PrecheckFinding(
                item="generated_columns", status=PrecheckStatus.NOTE.value,
                detail=(
                    f"存在生成列 {', '.join(r[0] for r in rows)}：生成列不可直接装载，"
                    "装载列清单须排除（DB 端自动计算）"
                ),
            )
        return PrecheckFinding(item="generated_columns", detail="无生成列")

    def _constraints(self, conn, table: str) -> PrecheckFinding:
        fk = _query(conn, (
            "SELECT COUNT(*) FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND REFERENCED_TABLE_NAME IS NOT NULL"
        ), (table,))
        fk_count = int(fk[0][0]) if fk else 0
        return PrecheckFinding(
            item="constraints",
            detail=(
                f"FK 约束 {fk_count} 个（CHECK/唯一随列定义）：装载中照常校验，"
                "默认保留全部约束（总方案 §9.4）"
            ),
        )

    def _partitioning(self, conn, table: str) -> PrecheckFinding:
        rows = _query(conn, (
            "SELECT PARTITION_NAME FROM information_schema.PARTITIONS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND PARTITION_NAME IS NOT NULL"
        ), (table,))
        if rows:
            return PrecheckFinding(
                item="partitioning", status=PrecheckStatus.NOTE.value,
                detail=(
                    f"分区表（{len(rows)} 个分区）：装载正常，关注锁等待与"
                    "事务日志增长（总方案 §9.5）"
                ),
            )
        return PrecheckFinding(item="partitioning", detail="非分区表")

    def _lob_json(self, conn, table: str) -> PrecheckFinding:
        rows = _query(conn, (
            "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND DATA_TYPE IN ('blob','text','json','longblob','longtext')"
        ), (table,))
        if rows:
            cols = ", ".join(f"{r[0]}({r[1]})" for r in rows)
            return PrecheckFinding(
                item="lob_json_types", status=PrecheckStatus.NOTE.value,
                detail=(
                    f"LOB/JSON 列 {cols}：csv_v1 分片为文本直传，大对象注意"
                    "分片体积与 max_allowed_packet"
                ),
            )
        return PrecheckFinding(item="lob_json_types", detail="无 LOB/JSON 列")

    def _auto_increment(self, conn, table: str) -> PrecheckFinding:
        rows = _query(conn, (
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND EXTRA LIKE '%auto_increment%'"
        ), (table,))
        if rows:
            return PrecheckFinding(
                item="auto_increment_sequence", status=PrecheckStatus.NOTE.value,
                detail=(
                    f"自增列 {', '.join(r[0] for r in rows)}：装载列清单不含该列时"
                    "DB 自动分配；显式提供 id 时注意 AUTO_INCREMENT 计数推进"
                ),
            )
        return PrecheckFinding(item="auto_increment_sequence", detail="无自增列")

    def _rls(self, conn, table: str) -> PrecheckFinding:  # noqa: ARG002
        return PrecheckFinding(
            item="rls", detail="MySQL 无行级安全特性（不适用）"
        )

    def _replication_limits(self, conn, table: str) -> PrecheckFinding:  # noqa: ARG002
        read_only = _scalar(conn, "SELECT @@read_only")
        super_read_only = _scalar(conn, "SELECT @@super_read_only")
        if super_read_only and int(super_read_only):
            return PrecheckFinding(
                item="replication_limits", status=PrecheckStatus.BLOCKED.value,
                detail="@@super_read_only=ON：实例只读（复制/归档角色），禁止装载"
            )
        if read_only and int(read_only):
            return PrecheckFinding(
                item="replication_limits", status=PrecheckStatus.BLOCKED.value,
                detail="@@read_only=ON：实例只读，禁止装载（需显式解除）"
            )
        return PrecheckFinding(
            item="replication_limits",
            detail="实例可写；binlog 日志增长与锁等待纳入限流观测（总方案 §9.5）",
        )


# ============================================================ PostgreSQL


class PostgresTablePrecheck:
    """PG 逐项检查（pg_catalog 实测；复用 T5.4 RLS/触发器口径）。"""

    product = "postgres"

    def check(self, conn, table: str) -> PrecheckReport:
        report = PrecheckReport(product=self.product, table=table)
        report.findings.append(self._triggers(conn, table))
        report.findings.append(self._generated_columns(conn, table))
        report.findings.append(self._constraints(conn, table))
        report.findings.append(self._partitioning(conn, table))
        report.findings.append(self._lob_json(conn, table))
        report.findings.append(self._identity_sequence(conn, table))
        report.findings.append(self._rls(conn, table))
        report.findings.append(self._replication_limits(conn, table))
        return report

    def _triggers(self, conn, table: str) -> PrecheckFinding:
        rows = _query(conn, (
            "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE c.relname = %s AND NOT t.tgisinternal ORDER BY t.tgname"
        ), (table,))
        if rows:
            return PrecheckFinding(
                item="triggers", status=PrecheckStatus.NOTE.value,
                detail=(
                    f"目标表存在触发器 {', '.join(r[0] for r in rows)}：COPY 逐行触发，"
                    "吞吐受影响；触发器外部副作用默认隔离（总方案 §13.3）"
                ),
            )
        return PrecheckFinding(item="triggers", detail="无触发器")

    def _generated_columns(self, conn, table: str) -> PrecheckFinding:
        rows = _query(conn, (
            "SELECT a.attname FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "WHERE c.relname = %s AND a.attgenerated <> ''"
        ), (table,))
        if rows:
            return PrecheckFinding(
                item="generated_columns", status=PrecheckStatus.NOTE.value,
                detail=(
                    f"存在生成列 {', '.join(r[0] for r in rows)}：COPY 装载列清单"
                    "须排除（PG12+ GENERATED ALWAYS 不可直接插入）"
                ),
            )
        return PrecheckFinding(item="generated_columns", detail="无生成列")

    def _constraints(self, conn, table: str) -> PrecheckFinding:
        rows = _query(conn, (
            "SELECT contype, COUNT(*) FROM pg_catalog.pg_constraint "
            "WHERE conrelid = %s::regclass GROUP BY contype ORDER BY contype"
        ), (table,))
        if rows:
            kinds = ", ".join(f"{r[0]}×{r[1]}" for r in rows)
            return PrecheckFinding(
                item="constraints",
                detail=(
                    f"表级约束 {kinds}（f=FK/c=CHECK/u=唯一/p=PK）：COPY 中照常校验，"
                    "默认保留全部约束（总方案 §9.4）"
                ),
            )
        return PrecheckFinding(item="constraints", detail="无表级约束记录")

    def _partitioning(self, conn, table: str) -> PrecheckFinding:
        rows = _query(conn, (
            "SELECT relkind FROM pg_catalog.pg_class WHERE relname = %s "
            "AND relkind IN ('p', 'r')"
        ), (table,))
        if rows and rows[0][0] == "p":
            return PrecheckFinding(
                item="partitioning", status=PrecheckStatus.NOTE.value,
                detail="分区父表：装载走父表路由，关注分区锁与日志增长（总方案 §9.5）",
            )
        return PrecheckFinding(item="partitioning", detail="非分区表")

    def _lob_json(self, conn, table: str) -> PrecheckFinding:
        rows = _query(conn, (
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = %s AND data_type IN ('json','jsonb','bytea')"
        ), (table,))
        if rows:
            cols = ", ".join(f"{r[0]}({r[1]})" for r in rows)
            return PrecheckFinding(
                item="lob_json_types", status=PrecheckStatus.NOTE.value,
                detail=f"LOB/JSON 列 {cols}：csv_v1 文本直传，注意分片体积",
            )
        return PrecheckFinding(item="lob_json_types", detail="无 LOB/JSON 列")

    def _identity_sequence(self, conn, table: str) -> PrecheckFinding:
        nextval = _query(conn, (
            "SELECT a.attname FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_attrdef ad ON ad.adrelid = a.attrelid "
            "AND ad.adnum = a.attnum "
            "WHERE c.relname = %s AND pg_get_expr(ad.adbin, ad.adrelid) LIKE 'nextval%'"
        ), (table,))
        identity = _query(conn, (
            "SELECT a.attname FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "WHERE c.relname = %s AND a.attidentity <> ''"
        ), (table,))
        names = sorted({r[0] for r in nextval} | {r[0] for r in identity})
        if names:
            return PrecheckFinding(
                item="auto_increment_sequence", status=PrecheckStatus.NOTE.value,
                detail=(
                    f"IDENTITY/Sequence 列 {', '.join(names)}：装载列清单不含该列时"
                    "由序列分配；显式提供值时注意序列同步（setval）"
                ),
            )
        return PrecheckFinding(item="auto_increment_sequence", detail="无 IDENTITY/Sequence 列")

    def _rls(self, conn, table: str) -> PrecheckFinding:
        row = _scalar_row(conn, (
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_catalog.pg_class "
            "WHERE relname = %s AND relkind = 'r'"
        ), (table,))
        if row is None:
            return PrecheckFinding(
                item="rls", status=PrecheckStatus.BLOCKED.value,
                detail=f"目标表 {table} 不存在或非普通表（显式失败）",
            )
        rls, force = bool(row[0]), bool(row[1])
        if rls or force:
            return PrecheckFinding(
                item="rls", status=PrecheckStatus.BLOCKED.value,
                detail=(
                    f"行级安全开启（RLS={'on' if rls else 'off'}, "
                    f"FORCE={'on' if force else 'off'}）：COPY 插入受策略约束，"
                    "装载结果无法保证可见/完整——走业务通道或显式批准 RLS 策略豁免"
                    "（T5.4 口径，A3 不静默放行）"
                ),
            )
        return PrecheckFinding(item="rls", detail="RLS 未启用")

    def _replication_limits(self, conn, table: str) -> PrecheckFinding:  # noqa: ARG002
        in_recovery = _scalar(conn, "SELECT pg_is_in_recovery()")
        if in_recovery:
            return PrecheckFinding(
                item="replication_limits", status=PrecheckStatus.BLOCKED.value,
                detail="pg_is_in_recovery()=on：备库（只读复制角色），禁止装载",
            )
        return PrecheckFinding(
            item="replication_limits",
            detail="主库可写；WAL 增长与锁等待纳入限流观测（总方案 §9.5）",
        )


# ============================================================ 分发


_ALIASES = {"postgresql": "postgres", "pg": "postgres", "mariadb": "mysql"}
_PRECHECKERS = {"mysql": MysqlTablePrecheck, "postgres": PostgresTablePrecheck}


def precheck_table(conn, product: str, table: str) -> PrecheckReport:
    """按产品逐项预检目标表（未知产品显式失败，不伪装通过）。"""
    normalized = _ALIASES.get((product or "").lower(), (product or "").lower())
    cls = _PRECHECKERS.get(normalized)
    if cls is None:
        raise ValueError(
            f"产品 {product!r} 无目标表预检器（已支持: {sorted(_PRECHECKERS)}；"
            "显式失败，不得视为通过）"
        )
    return cls().check(conn, table)


# ============================================================ 工具


def _query(conn, sql: str, params: tuple = ()) -> list[tuple]:
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        return list(cur.fetchall())
    finally:
        cur.close()


def _scalar(conn, sql: str):
    rows = _query(conn, sql, ())
    return rows[0][0] if rows else None


def _scalar_row(conn, sql: str, params: tuple = ()) -> tuple | None:
    rows = _query(conn, sql, params)
    return rows[0] if rows else None


# 假连接路由样本（测试与文档口径对齐；marker 防包含冲突——B5 §4 第 4 条）
MYSQL_PRECHECK_ROUTES = {
    "EVENT_OBJECT_TABLE": [("trg_audit",)],
    "GENERATION_EXPRESSION <> ''": [("virtual_note",)],
    "KEY_COLUMN_USAGE": [(2,)],
    "information_schema.PARTITIONS": [("p2026",)],
    "DATA_TYPE IN": [("note", "json")],
    "EXTRA LIKE": [("id",)],
    "SELECT @@read_only": [(0,)],
    "SELECT @@super_read_only": [(0,)],
}

PG_PRECHECK_ROUTES = {
    "tgisinternal": [("trg_audit",)],
    "attgenerated <> ''": [],
    "FROM pg_catalog.pg_constraint": [("f", 1)],
    "relkind IN ('p', 'r')": [("r",)],
    "data_type IN ('json','jsonb','bytea')": [("payload", "jsonb")],
    "attidentity <> ''": [("id",)],
    "relrowsecurity": [(False, False)],
    "pg_is_in_recovery()": [(False,)],
}
