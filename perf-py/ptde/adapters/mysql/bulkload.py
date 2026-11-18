"""MySQL 原生批量装载适配器（R5 T5.3，实施方案 §8.2；总方案 §9.3/§9.4）。

通道：``LOAD DATA LOCAL INFILE``（QW4 PoC tests/poc_load_data_infile.py 实测口径）。

关键实测结论（勿另起一套）：
- **secure_file_priv**：LOCAL 通道由客户端读文件直传，不经服务端文件白名单；
  ``secure_file_priv`` 仅约束 SERVER 端 ``LOAD DATA INFILE``（探测值记录进
  Capability 快照供审计，不影响 LOCAL 通道可用性）；
- **LOCAL 权限**：需服务端 ``@@local_infile=ON`` **且** 客户端连接启用
  （pymysql ``connect(local_infile=True)``）——两者任一不满足显式失败，
  降级决策交 T5.5 降级链（本方法只判定不降级，A3）；
- **错误/警告计数**：LOAD DATA 对非法行默认跳行继续（非严格 sql_mode），
  靠 ``cur.rowcount`` 与期望行数显式比对 + ``SHOW WARNINGS`` 样例回传；
  **短装即 BulkLoadError，不静默丢弃错误记录**（QW4 结论）；
- **触发器/分区影响**：装载前查询 information_schema 预检，说明随结果
  回传（触发器逐行触发拉低吞吐；分区表装载正常但需关注锁与日志增长）。

staging 分片文件（file_format.csv_v1）装载约定：``IGNORE 2 LINES`` 跳过
首行格式声明 + 列头；``\\N``（NULL_TOKEN）与 MySQL ESCAPED BY '\\' 默认
语义天然对齐（裸 ``\\N`` → NULL；带引号空串 → 空串，NULL/空串区分保留）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ptde.adapters.base import (
    BulkLoadAdapter,
    BulkLoadError,
    BulkLoadResult,
    BulkLoadUnsupported,
)
from ptde.adapters.mysql.statistics import validate_identifier

__all__ = ["MysqlBulkLoadAdapter"]

IGNORE_LINES = 2  # 首行格式声明 + 列头行（csv_v1 格式约定，勿另起一套）


class MysqlBulkLoadAdapter(BulkLoadAdapter):
    """LOAD DATA LOCAL INFILE 装载器（行数核对锚点 + 错误样例可见）。"""

    product = "mysql"
    strategy = "load_data_local"

    # ------------------------------------------------------------ 能力判定

    def ensure_capable(self, conn) -> list[str]:
        """原生通道可用性判定（不降级；不可用 → BulkLoadUnsupported 带原因）。"""
        server_flag = self._scalar(conn, "SELECT @@local_infile")
        if server_flag is None:
            raise BulkLoadUnsupported(
                "无法读取 @@local_infile（探测失败，显式失败——不得默认可用）"
            )
        if not int(server_flag):
            raise BulkLoadUnsupported(
                "服务端 @@local_infile=OFF：LOAD DATA LOCAL INFILE 不可用"
                "（需 SET GLOBAL local_infile=ON 后重试；QW4 实测口径）"
            )
        client_flag = getattr(conn, "local_infile", None)
        if client_flag is False:
            raise BulkLoadUnsupported(
                "客户端未启用 LOCAL INFILE：连接需 local_infile=True"
                "（pymysql connect 参数，QW4 口径）"
            )
        notes = self.precheck_notes(conn, None)
        secure = self._scalar(conn, "SELECT @@secure_file_priv")
        notes.append(
            f"secure_file_priv={secure!r}（LOCAL 通道不经服务端文件白名单，"
            "仅 SERVER 端 INFILE 受限）"
        )
        return notes

    # ------------------------------------------------------------ 预检

    def precheck_notes(self, conn, table: str | None) -> list[str]:
        """触发器/分区影响预检（说明可见，不静默；table=None 时只查环境项）。"""
        notes: list[str] = []
        if table:
            validate_identifier(table)
            rows = self._query(
                conn,
                "SELECT TRIGGER_NAME, ACTION_TIMING, EVENT_MANIPULATION "
                "FROM information_schema.TRIGGERS "
                "WHERE EVENT_OBJECT_SCHEMA = DATABASE() AND EVENT_OBJECT_TABLE = %s "
                "ORDER BY TRIGGER_NAME",
                (table,),
            )
            if rows:
                names = ", ".join(f"{r[0]}({r[1]} {r[2]})" for r in rows)
                notes.append(
                    f"目标表存在触发器 {names}：LOAD DATA 逐行触发，"
                    "吞吐受影响且注意外部副作用（总方案 §13.3）"
                )
            part_rows = self._query(
                conn,
                "SELECT PARTITION_NAME FROM information_schema.PARTITIONS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
                "AND PARTITION_NAME IS NOT NULL",
                (table,),
            )
            if part_rows:
                notes.append(
                    f"目标表为分区表（{len(part_rows)} 个分区）：装载正常，"
                    "关注锁等待与事务日志增长（总方案 §9.5）"
                )
        return notes

    # ------------------------------------------------------------ 装载

    def load_shard(
        self,
        conn,
        path: str,
        table: str,
        columns,
        rows_expected: int,
        *,
        shard_id: str = "",
        entity: str = "",
    ) -> BulkLoadResult:
        result = BulkLoadResult(
            shard_id=shard_id,
            entity=entity,
            table=table,
            strategy=self.strategy,
            rows_expected=rows_expected,
            collected_at=datetime.now(UTC),
        )
        self._verify_shard_header(path)
        for col in columns:
            validate_identifier(col)
        validate_identifier(table)
        notes = self.precheck_notes(conn, table)
        notes.extend(self.ensure_capable(conn))
        result.precheck_notes = notes

        sql = self._load_statement(path, table, columns)
        cur = conn.cursor()
        try:
            try:
                cur.execute(sql)
            except Exception as exc:  # A3：语句级失败包装为装载错误（显式，不静默）
                raise BulkLoadError(
                    f"LOAD DATA 执行失败（分片 {shard_id or path}）：{exc}",
                    rows_expected=rows_expected,
                    cleanup_hint=None,
                ) from exc
            rows_loaded = int(cur.rowcount or 0)
            warnings_count, samples = self._collect_warnings(conn)
        finally:
            cur.close()
        conn.commit()

        result.rows_loaded = rows_loaded
        result.warnings_count = warnings_count
        result.warning_samples = samples
        if rows_loaded != rows_expected:
            raise BulkLoadError(
                f"分片 {shard_id or path} 装载行数不符（LOAD DATA 跳行/丢行，"
                "错误记录不静默丢弃——QW4 结论）",
                rows_expected=rows_expected,
                rows_loaded=rows_loaded,
                error_samples=samples,
                warnings_count=warnings_count,
            )
        result.rows_verified = True
        result.seal()
        return result

    # ------------------------------------------------------------ 内部

    def _load_statement(self, path: str, table: str, columns) -> str:
        """LOAD DATA 语句构造（子类可覆盖：官方替代档为服务端 INFILE）。"""
        col_list = ", ".join(columns)
        return (
            f"LOAD DATA LOCAL INFILE '{path}' INTO TABLE {table} "
            "FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"' "
            f"LINES TERMINATED BY '\\n' IGNORE {IGNORE_LINES} LINES ({col_list})"
        )

    def _collect_warnings(self, conn) -> tuple[int, list[str]]:
        """SHOW COUNT(*) WARNINGS + SHOW WARNINGS 样例（可见不吞）。"""
        count_row = self._query(conn, "SHOW COUNT(*) WARNINGS", ())
        count = int(count_row[0][0]) if count_row and count_row[0][0] is not None else 0
        samples: list[str] = []
        if count:
            rows = self._query(conn, "SHOW WARNINGS", ())
            samples = [f"[{r[0]} {r[1]}] {r[2]}" for r in rows]
        return count, samples

    @staticmethod
    def _verify_shard_header(path: str) -> None:
        """装载前核对首行格式声明（csv_v1 契约，显式失败不误装）。"""
        p = Path(path)
        if not p.exists():
            raise BulkLoadError(f"分片文件缺失：{path}（staging 不完整，显式失败）")
        with p.open(encoding="utf-8", newline="") as f:
            first = f.readline()
        if not first:
            raise BulkLoadError(f"分片文件为空：{path}（显式失败）")
        if "format_version=csv_v1" not in first:
            raise BulkLoadError(
                f"分片文件 {path} 首行非 csv_v1 格式声明（{first.strip()!r}）"
                "——staging 契约不符，显式失败"
            )

    def _scalar(self, conn, sql: str):
        rows = self._query(conn, sql, ())
        return rows[0][0] if rows else None

    @staticmethod
    def _query(conn, sql: str, params) -> list[tuple]:
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            return list(cur.fetchall())
        finally:
            cur.close()
