"""PTDE 数据集参数样本导出（P1-3）：READY 实例 → 压测参数化 CSV。

从数据集主表按「热/温/冷」三段主键分层采样（低/中/高 ID 段各取 1/3 预算，
覆盖递增主键写入造成的取值分布差异），上限默认 10,000 行（对齐
stress/csv_provider 的 1 万行上限与 1MB 体积红线），写入样本目录白名单内。

安全约束：
- 目录白名单：仅允许写入 sample_dir（app 装配指定，如 ~/.perftestor/samples）；
  文件名固定 ``<instance_id>-sample.csv``，杜绝路径穿越；
- 行数上限：limit ∈ [1, MAX_SAMPLE_ROWS]；
- 连接工厂可注入（测试替身），缺省经 ``mysql_conn_factory`` 懒加载 pymysql。
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from server.dataset_pipeline_factory import resolve_target_dsn
from server.realdb_pipeline import mysql_conn_factory

log = logging.getLogger(__name__)

DEFAULT_SAMPLE_ROWS = 10_000
MAX_SAMPLE_ROWS = 10_000
# 分层段数（热/温/冷）
_SEGMENTS = 3


def _pick_primary_entity(spec: dict) -> str | None:
    """主表选择：targets.rows 中计划行数最大的实体（并列/无法解析时取字典序首个）。"""
    rows_cfg = (spec or {}).get("targets", {}).get("rows") or {}
    if not rows_cfg:
        return None

    def _count_of(cfg) -> int:
        if isinstance(cfg, int):
            return cfg
        if isinstance(cfg, dict):
            for k in ("count", "rows", "total_rows"):
                v = cfg.get(k)
                if isinstance(v, int):
                    return v
        return 0

    return max(sorted(rows_cfg), key=lambda k: _count_of(rows_cfg[k]))


def _probe_columns(conn, database: str, table: str) -> tuple[list[str], str | None]:
    """列名清单 + 主键列（information_schema 实探；无主键回退首列）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME, COLUMN_KEY FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
            [database, table])
        rows = cur.fetchall()
    names = [r[0] for r in rows]
    pk = next((r[0] for r in rows if r[1] == "PRI"), None)
    return names, pk or (names[0] if names else None)


def _fetch_rows(conn, table: str, key_col: str, names: list[str],
                limit: int) -> list[list]:
    """按主键三段分层采样：低/中/高 ID 段各取 ceil(limit/3)，合并去重。

    P0 修复：主键为 UUID / VARCHAR / 复合非数值列时 int() 强转抛
    TypeError/ValueError → 优雅降级为按主键顺序取前 limit 行（不中断导出）。
    """
    with conn.cursor() as cur:
        cur.execute(f"SELECT MIN(`{key_col}`), MAX(`{key_col}`) FROM `{table}`")  # noqa: S608 — 表名来自数据集定义非用户输入
        bounds = cur.fetchone()
        if not bounds or bounds[0] is None:
            return []
        cols = ", ".join(f"`{n}`" for n in names)
        pk_idx = names.index(key_col) if key_col in names else 0
        try:
            lo, hi = int(bounds[0]), int(bounds[1])
        except (TypeError, ValueError):
            # 非数值主键（UUID/字符串/复合）：无法分段采样，降级为按主键顺序取前 limit 行
            cur.execute(  # noqa: S608 — 同上，标识符来自 schema 实探
                f"SELECT {cols} FROM `{table}` ORDER BY `{key_col}` ASC LIMIT %s",
                [limit])
            return [list(r) for r in cur.fetchall()]
        span = max(1, (hi - lo) // _SEGMENTS)
        per_seg = max(1, -(-limit // _SEGMENTS))  # ceil
        collected: dict[tuple, list] = {}
        for i in range(_SEGMENTS):
            seg_lo = lo + i * span
            seg_hi = lo + (i + 1) * span - 1 if i < _SEGMENTS - 1 else hi
            cur.execute(  # noqa: S608 — 同上，标识符来自 schema 实探
                f"SELECT {cols} FROM `{table}` WHERE `{key_col}` BETWEEN %s AND %s "
                f"ORDER BY `{key_col}` ASC LIMIT %s",
                [seg_lo, seg_hi, per_seg])
            for row in cur.fetchall():
                collected.setdefault(tuple(row), list(row))
        # 数值主键按数值顺序（非字典序）
        return sorted(collected.values(), key=lambda r: r[pk_idx])[:limit]


def export_sample_csv(repo, instance_row, out_dir: str | Path, *,
                      limit: int = DEFAULT_SAMPLE_ROWS,
                      conn_factory=None) -> dict | None:
    """导出参数样本 CSV；目标库不可达/表为空时返回 None（尽力而为不阻断门禁）。"""
    limit = max(1, min(int(limit), MAX_SAMPLE_ROWS))
    definition = repo.find_definition_by_id(instance_row.definition_id)
    if definition is None:
        return None
    entity = _pick_primary_entity(json.loads(definition.spec_json))
    if entity is None:
        return None
    base = Path(out_dir)
    base.mkdir(parents=True, exist_ok=True)
    out_path = base / f"{instance_row.id}-sample.csv"  # 白名单目录 + 固定文件名

    dsn = resolve_target_dsn(repo)
    factory = conn_factory if conn_factory is not None else mysql_conn_factory(dsn)
    conn = factory()
    try:
        database = dsn.rsplit("/", 1)[-1].split("?")[0]
        names, key_col = _probe_columns(conn, database, entity)
        if not names or key_col is None:
            return None
        rows = _fetch_rows(conn, entity, key_col, names, limit)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — 关闭失败不影响导出结果
            pass
    if not rows:
        return None
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(names)
        for row in rows:
            writer.writerow(["" if v is None else v for v in row])
    log.info("dataset sample csv exported, instance=%s rows=%d path=%s",
             instance_row.id, len(rows), out_path)
    return {"path": str(out_path), "rows": len(rows), "entity": entity,
            "columns": names}
