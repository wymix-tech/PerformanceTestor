"""PostgreSQL 原生批量装载适配器（R5 T5.4，实施方案 §8.2；总方案 §9.3/§9.4）。

通道：``COPY ... FROM STDIN``（流式装载——不整文件驻内存，分片文件逐块
经流包装器剥离声明/列头后直送驱动）。

预检与失败语义（A3，显式不静默）：
- **INSERT 权限**：``has_table_privilege`` 实测，不足显式失败；
- **RLS 检查**：``pg_class.relrowsecurity`` 开启的表显式拒绝装载
  （行安全策略使插入行为依赖策略定义，装载结果无法保证可见/完整——
  需走业务通道或显式批准策略豁免，不由 Loader 静默放行）；
- **触发器**：COPY 逐行触发 INSERT 触发器（官方语义），预检说明回传；
- **约束检查**：FK/CHECK/唯一约束在 COPY 中照常校验（等效 INSERT），
  默认保留全部约束（总方案 §9.4：不得为吞吐默认关闭约束）；
- **失败空间清理提示**：COPY 中途失败回滚留下的死元组需 VACUUM 回收，
  BulkLoadError 携带 cleanup_hint 提示（§8.2 验收项）。

staging 分片文件（file_format.csv_v1）装载约定：``ShardTextStream`` 流式
跳过首行格式声明 + 列头；``NULL '\\N'`` 与 NULL_TOKEN 对齐；CSV 格式下
反斜杠非转义字符（与 MySQL ESCAPED BY 语义不同，勿混用）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from ptde.adapters.base import (
    BulkLoadAdapter,
    BulkLoadError,
    BulkLoadResult,
    BulkLoadUnsupported,
)

__all__ = ["PostgresBulkLoadAdapter", "ShardTextStream"]

VACUUM_HINT = (
    "COPY 失败回滚留下死元组：建议对目标表执行 VACUUM 回收空间"
    "（失败后空间清理，实施方案 §8.2）"
)


class ShardTextStream:
    """分片文件流包装器：跳过声明+列头后逐块透传（不整文件驻内存）。"""

    def __init__(self, path: str | Path, skip_lines: int = 2) -> None:
        self._f: TextIO = Path(path).open(encoding="utf-8", newline="")
        self._skip = skip_lines
        self._skipped = False

    def read(self, size: int = -1) -> str:
        if not self._skipped:
            for _ in range(self._skip):
                self._f.readline()
            self._skipped = True
        return self._f.read(size)

    def readline(self) -> str:
        return self._f.readline()

    @property
    def closed(self) -> bool:
        return self._f.closed

    def close(self) -> None:
        self._f.close()


class PostgresBulkLoadAdapter(BulkLoadAdapter):
    """COPY FROM STDIN 装载器（流式 + RLS/约束/权限显式预检）。"""

    product = "postgres"
    strategy = "copy_from_stdin"

    # ------------------------------------------------------------ 能力判定

    def ensure_capable(self, conn) -> list[str]:
        """COPY 通道为服务端标准能力；连接级预检（版本/schema 可达性）。"""
        cur = conn.cursor()
        try:
            cur.execute("SELECT current_schema() IS NOT NULL")
            row = cur.fetchone()
        finally:
            cur.close()
        if not row or not row[0]:
            raise BulkLoadUnsupported(
                "current_schema() 不可达（无法定位目标表，显式失败）"
            )
        return ["COPY FROM STDIN 为服务端标准能力；逐表权限/RLS 于装载前预检"]

    # ------------------------------------------------------------ 预检

    def precheck_table(self, conn, table: str) -> list[str]:
        """逐表预检：INSERT 权限 / RLS / 触发器（显式失败或说明回传）。"""
        notes: list[str] = []
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT has_table_privilege(current_user, %s, 'INSERT')",
                (table,),
            )
            granted = bool(cur.fetchone()[0])
            if not granted:
                raise BulkLoadUnsupported(
                    f"目标表 {table} 无 INSERT 权限（current_user 权限不足，显式失败）"
                )
            cur.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = %s AND relkind = 'r'",
                (table,),
            )
            row = cur.fetchone()
            if row is None:
                raise BulkLoadError(f"目标表 {table} 不存在或非普通表（显式失败）")
            rls, force_rls = bool(row[0]), bool(row[1])
            cur.execute(
                "SELECT t.tgname FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relname = %s AND NOT t.tgisinternal ORDER BY t.tgname",
                (table,),
            )
            triggers = [r[0] for r in cur.fetchall()]
        finally:
            cur.close()
        if rls or force_rls:
            raise BulkLoadError(
                f"目标表 {table} 启用行级安全（RLS={'on' if rls else 'off'}, "
                f"FORCE={'on' if force_rls else 'off'}）：COPY 插入受策略约束，"
                "装载结果无法保证可见/完整——显式拒绝（A3：不由 Loader 静默放行）",
                cleanup_hint="如需装载，走业务通道或显式批准 RLS 策略豁免",
            )
        if triggers:
            notes.append(
                f"目标表存在触发器 {', '.join(triggers)}：COPY 逐行触发，"
                "吞吐受影响且注意外部副作用（总方案 §13.3）"
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
        result.precheck_notes = self.precheck_table(conn, table)

        col_list = ", ".join(columns)
        sql = (
            f"COPY {table} ({col_list}) FROM STDIN WITH "
            "(FORMAT csv, NULL '\\N')"
        )
        stream = ShardTextStream(path, skip_lines=2)
        cur = conn.cursor()
        try:
            try:
                cur.copy_expert(sql, stream)
            except Exception as exc:  # A3：失败包装 + 空间清理提示（显式，不静默）
                raise BulkLoadError(
                    f"COPY 装载失败（分片 {shard_id or path}）：{exc}",
                    rows_expected=rows_expected,
                    cleanup_hint=VACUUM_HINT,
                ) from exc
            rows_loaded = int(getattr(cur, "rowcount", 0) or 0)
        finally:
            cur.close()
            stream.close()
        conn.commit()

        result.rows_loaded = rows_loaded
        if rows_loaded != rows_expected:
            raise BulkLoadError(
                f"分片 {shard_id or path} 装载行数不符（错误记录不静默丢弃）",
                rows_expected=rows_expected,
                rows_loaded=rows_loaded,
                cleanup_hint=VACUUM_HINT,
            )
        result.rows_verified = True
        result.seal()
        return result
