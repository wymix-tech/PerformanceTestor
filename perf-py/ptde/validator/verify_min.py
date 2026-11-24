"""生成后验证最小集 QW3（自 datagen/verify_min.py 原样收编终态）。

三项最小验证（落地方案 §6.1 / 实施方案 §11 QW3）：
1. 行数核对：默认逐表 SELECT COUNT(*) 对比目标行数（精确一致）；
   宿主提供 ``baseline`` 时改为增量核对：``COUNT(*) - baseline`` 对比
   本次期望新增行数（避免目标库已有存量数据时把总行数误判为本次灌数结果）；
2. 外键孤儿抽检：声明式外键逐条抽检（非空外键采样 ≤ sampleLimit 行，
   LEFT JOIN 父表计孤儿，孤儿 = 0 通过）；
3. 枚举合法率：内联 ENUM/SET 类型列（或注释枚举）逐列统计
   非空值命中候选集的比例（≥ ENUM_LEGAL_MIN 通过）。

输出简化 ValidationReport（dict 形态），宿主落 data_gen_task.detail_json
（R6 迁 ptde_validation_report）。验证不通过不代表灌数失败，宿主据此显式标注
任务状态（不伪装成功）。

连接用鸭子类型（DBAPI conn + dialect），测试可用 FakeConn / sqlite 直跑。
"""

from __future__ import annotations

from ptde.models.wire import SchemaMetadata

# 外键孤儿抽检采样上限（每条外键非空外键值采样行数）
DEFAULT_SAMPLE_LIMIT = 200
# 枚举合法率通过阈值（≥ 0.99 通过；容许个别历史/手工数据）
ENUM_LEGAL_MIN = 0.99

CHECK_ROW_COUNT = "row_count"
CHECK_FK_ORPHAN = "fk_orphan"
CHECK_ENUM_LEGAL = "enum_legal"


def verify_min(conn, dialect, tables: list[dict],
               sample_limit: int = DEFAULT_SAMPLE_LIMIT) -> dict:
    """灌数后最小验证。

    tables 元素：{"table": 表名, "expected": 目标行数, "baseline": 灌数前行数}。
    expected 缺省不核对行数；baseline 缺省时 expected 按总行数核对，
    提供 baseline 时 expected 按本次新增行数核对（COUNT(*) - baseline）。
    返回 {"passed": bool, "checks": [...], "sampleLimit": n}；
    单项执行异常记为该检查失败（不伪装成功），不阻断其余检查。
    """
    checks: list[dict] = []
    for t in tables or []:
        table = (t.get("table") or "").strip()
        if not table:
            continue
        expected = t.get("expected")
        meta = _fetch_meta(conn, dialect, table)
        if meta is None or not meta.columns:
            checks.append({"kind": CHECK_ROW_COUNT, "table": table,
                           "expected": expected, "ok": False, "error": "表不存在或无可见列"})
            continue
        # 1. 行数核对（baseline 提供时按本次新增行数核对）
        checks.append(_check_row_count(conn, dialect, table, expected,
                                       baseline=t.get("baseline")))
        # 2. 外键孤儿抽检（声明式外键逐条）
        seen: set[tuple] = set()
        for fk in meta.foreign_keys:
            key = (fk.fk_column, fk.pk_table, fk.pk_column)
            if key in seen or not fk.fk_column or not fk.pk_table:
                continue
            seen.add(key)
            checks.append(_check_fk_orphan(conn, dialect, table, fk.fk_column,
                                           fk.pk_table, fk.pk_column, sample_limit))
        # 3. 枚举合法率（内联 ENUM/SET / 注释枚举；有主键水位时按本次新增行核对）
        wm = t.get("pk_watermark") or {}
        for col in meta.columns:
            values = _enum_values(col)
            if values:
                checks.append(_check_enum_legal(conn, dialect, table, col, values,
                                                pk_watermark=wm))
    return {
        "passed": all(c.get("ok") for c in checks) if checks else True,
        "checks": checks,
        "sampleLimit": sample_limit,
    }


# ---------------------------------------------------------------- 各项检查


def _check_row_count(conn, dialect, table: str, expected, baseline=None) -> dict:
    check: dict = {"kind": CHECK_ROW_COUNT, "table": table, "expected": expected}
    if baseline is not None:
        check["mode"] = "increment"
        check["baseline"] = int(baseline)
    else:
        check["mode"] = "total"
    try:
        actual_total = _scalar(conn, f"SELECT COUNT(*) FROM {dialect.quote_identifier(table)}")
        actual_total = int(actual_total) if actual_total is not None else 0
        if baseline is None:
            check["actual"] = actual_total
            check["ok"] = (expected is None) or (check["actual"] == expected)
        else:
            actual_delta = max(0, actual_total - int(baseline))
            check["actual"] = actual_delta
            check["actualTotal"] = actual_total
            check["ok"] = (expected is None) or (check["actual"] == expected)
    except Exception as exc:  # noqa: BLE001 — 单项失败不阻断
        check["ok"] = False
        check["error"] = str(exc)
    return check


def _check_fk_orphan(conn, dialect, table: str, fk_column: str,
                     parent_table: str, parent_column: str, sample_limit: int) -> dict:
    """抽检：非空外键采样 ≤ limit 行，LEFT JOIN 父表计孤儿（孤儿 = 0 通过）。"""
    check: dict = {"kind": CHECK_FK_ORPHAN, "table": table, "column": fk_column,
                   "parent": f"{parent_table}.{parent_column}", "sampled": None}
    q = dialect.quote_identifier
    try:
        sampled = _scalar(conn,
                          f"SELECT COUNT(*) FROM (SELECT {q(fk_column)} FROM {q(table)} "
                          f"WHERE {q(fk_column)} IS NOT NULL LIMIT {int(sample_limit)}) s")
        orphans = _scalar(conn,
                          f"SELECT COUNT(*) FROM (SELECT {q(fk_column)} FROM {q(table)} "
                          f"WHERE {q(fk_column)} IS NOT NULL LIMIT {int(sample_limit)}) s "
                          f"LEFT JOIN {q(parent_table)} p "
                          f"ON s.{q(fk_column)} = p.{q(parent_column)} "
                          f"WHERE p.{q(parent_column)} IS NULL")
        check["sampled"] = int(sampled) if sampled is not None else 0
        check["orphans"] = int(orphans) if orphans is not None else 0
        check["ok"] = check["orphans"] == 0
    except Exception as exc:  # noqa: BLE001 — 单项失败不阻断
        check["ok"] = False
        check["error"] = str(exc)
    return check


def _check_enum_legal(conn, dialect, table: str, col, values: list,
                      pk_watermark: dict | None = None) -> dict:
    """非空值命中候选集比例 legalRate = 1 - violations / nonNull；≥ ENUM_LEGAL_MIN 通过。

    提供 ``pk_watermark={"column": ..., "max": ...}`` 时按本次新增行核对
    （主键 > 灌数前水位），历史存量数据不参与本次校验。
    """
    check: dict = {"kind": CHECK_ENUM_LEGAL, "table": table, "column": col.name,
                   "values": values}
    q = dialect.quote_identifier
    ph = getattr(dialect, "param", "?")
    wm_col = None
    wm_max = None
    if pk_watermark and pk_watermark.get("column") and pk_watermark.get("max") is not None:
        wm_col = pk_watermark["column"]
        wm_max = int(pk_watermark["max"])
        check["pkWatermark"] = {"column": wm_col, "max": wm_max}
    try:
        scope = f" AND {q(wm_col)} > {wm_max}" if wm_col is not None else ""
        total = _scalar(conn,
                        f"SELECT COUNT(*) FROM {q(table)} "
                        f"WHERE {q(col.name)} IS NOT NULL{scope}")
        placeholders = ", ".join([ph] * len(values))
        violations = _scalar(conn,
                             f"SELECT COUNT(*) FROM {q(table)} "
                             f"WHERE {q(col.name)} IS NOT NULL "
                             f"AND {q(col.name)} NOT IN ({placeholders}){scope}",
                             list(values))
        non_null = int(total) if total is not None else 0
        bad = int(violations) if violations is not None else 0
        check["legalRate"] = (1.0 - bad / non_null) if non_null else 1.0
        check["ok"] = check["legalRate"] >= ENUM_LEGAL_MIN
    except Exception as exc:  # noqa: BLE001 — 单项失败不阻断
        check["ok"] = False
        check["error"] = str(exc)
    return check


# ---------------------------------------------------------------- 元数据/工具


def _fetch_meta(conn, dialect, table: str) -> SchemaMetadata | None:
    try:
        return dialect.fetch_metadata(conn, table)
    except Exception:  # noqa: BLE001 — 表不存在等归一为 None
        return None


def _enum_values(col) -> list:
    """候选枚举值：内联 ENUM/SET 类型名 + 注释枚举（四级发现的无 DB 子集）。"""
    from ptde_common.enum_value_discoverer import discover

    try:
        return discover(col) or []
    except Exception:  # noqa: BLE001
        return []


def _scalar(conn, sql: str, params: list | None = None):
    """单值查询（第一行第一列；无行返回 None）。"""
    cur = conn.cursor()
    try:
        cur.execute(sql, params or [])
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()
