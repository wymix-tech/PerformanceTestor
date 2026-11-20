"""B7 续批次 11：真库端到端门控——切换引擎（PtdeDatagenEngine）真库灌数核对。

沿革：实施方案 §13.1 门禁 2 为「legacy vs PTDE 双跑对照」（原
test_ptde_legacy_ptde_crosscheck.py，报告 benchmarks/gate2_crosscheck_report.json
已归档）；T7.9 退役删除后 legacy 引擎不存在，本文件承接其真库门控职责，
改为切换引擎生产路径端到端核对：
- 行数精确（父表 + 子表 = N_PARENT + N）；
- 分布统计容差内（status 加权类目频率；amount 均匀域均值）；
- 主键区间 [1, N]（seq 规则落 id 列 → planned_range 接管，追加语义 existing_max=0）；
- 外键零孤儿 + 域 [1, N_PARENT]（任务内父表 → 关系声明 → planned span 注入）。

环境门控：``PTDE_REALDB_URL`` 未设置 → skip（真库批次口径）。
"""

import json
import os
import time
from pathlib import Path

import pytest

from server.realdb_pipeline import REALDB_URL_ENV, realdb_url

pytestmark = pytest.mark.skipif(
    not os.environ.get(REALDB_URL_ENV),
    reason=f"真库批次：需 {REALDB_URL_ENV}（mysql://user:pwd@host:port/db）",
)

TABLE = "ptde_xcheck_orders"
PARENT = "ptde_xcheck_users"
N = int(os.environ.get("PTDE_XCHECK_ROWS", "10000"))
N_PARENT = int(os.environ.get("PTDE_XCHECK_PARENT_ROWS", "1000"))
STATUSES = ["PENDING", "PAID", "CANCELLED"]
WEIGHTS = [0.3, 0.6, 0.1]
AMOUNT_MIN, AMOUNT_MAX = 1, 10000
DDL = (
    f"CREATE TABLE IF NOT EXISTS {PARENT} (\n"
    "  id BIGINT NOT NULL PRIMARY KEY,\n"
    "  name VARCHAR(255)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
    f"CREATE TABLE IF NOT EXISTS {TABLE} (\n"
    "  id BIGINT NOT NULL PRIMARY KEY,\n"
    "  user_id BIGINT,\n"
    "  amount DECIMAL(18,2),\n"
    "  status VARCHAR(255)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
)


def _conn():
    from server.realdb_pipeline import mysql_conn_factory

    return mysql_conn_factory(realdb_url())()


def _scalar(conn, sql: str):
    cur = conn.cursor()
    try:
        cur.execute(sql)
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()


def _rows(conn, sql: str) -> list[tuple]:
    cur = conn.cursor()
    try:
        cur.execute(sql)
        return list(cur.fetchall())
    finally:
        cur.close()


def _ptde_engine_run(conn, tmp_path) -> dict:
    """切换引擎真库灌数（同规格：seq 主键 + ref 外键 + 均匀 amount + 加权 enum status）。

    经 perf_cli.ptde_datagen_engine 生产路径（information_schema 内省 →
    translate_fields → build_realdb_pipeline → generate/load）；
    id 列分布由 worker planned_range 主键语义接管；任务内父表 →
    关系声明 + 零孤儿 planned span。
    """
    from perf_cli.ptde_datagen_engine import PtdeDatagenEngine
    from ptde.models.wire import DataGenSpec, FieldRule, TableSpec
    from server.realdb_pipeline import parse_mysql_dsn

    p = parse_mysql_dsn(realdb_url() or "")
    cur = conn.cursor()
    try:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")  # 复跑不残留上批数据
        cur.execute(f"DROP TABLE IF EXISTS {PARENT}")
        cur.execute("DROP TABLE IF EXISTS ptde_shard_checkpoint")
        for ddl in DDL:
            cur.execute(ddl)
    finally:
        cur.close()
    conn.commit()

    spec = DataGenSpec(
        jdbc_url=f"jdbc:mysql://{p['host']}:{p['port']}/{p['database']}",
        db_user=str(p["user"]), db_password=str(p["password"]),
        tables=[
            TableSpec(table_name=PARENT, row_count=N_PARENT),
            TableSpec(
                table_name=TABLE, row_count=N,
                fields={
                    "id": FieldRule(
                        type=FieldRule.TYPE_SEQ, seq_prefix="", seq_start=1),
                    "user_id": FieldRule(
                        type=FieldRule.TYPE_REF, table=PARENT, column="id"),
                    "amount": FieldRule(
                        type=FieldRule.TYPE_RANGE, min=AMOUNT_MIN, max=AMOUNT_MAX),
                    "status": FieldRule(
                        type=FieldRule.TYPE_ENUM, values=STATUSES, weights=WEIGHTS),
                },
            ),
        ],
        seed=7,
    )
    engine = PtdeDatagenEngine(conn_factory=_conn,
                               staging_base=tmp_path / "staging",
                               metadata_url="sqlite:///:memory:")
    t0 = time.monotonic()
    result = engine.execute(spec)
    assert result.success, result.error_msg
    return {"rows": int(result.inserted_rows),
            "ms": int((time.monotonic() - t0) * 1000),
            "warnings": list(result.warnings)}


def _stats(conn) -> dict:
    freq = {s: 0 for s in STATUSES}
    for (status, cnt) in _rows(
            conn, f"SELECT status, COUNT(*) FROM {TABLE} GROUP BY status"):
        freq[str(status)] = int(cnt)
    return {
        "rows": int(_scalar(conn, f"SELECT COUNT(*) FROM {TABLE}")),
        "status_freq": freq,
        "amount_min": float(_scalar(conn, f"SELECT MIN(amount) FROM {TABLE}") or 0),
        "amount_max": float(_scalar(conn, f"SELECT MAX(amount) FROM {TABLE}") or 0),
        "amount_mean": float(_scalar(conn, f"SELECT AVG(amount) FROM {TABLE}") or 0),
        "id_min": int(_scalar(conn, f"SELECT MIN(id) FROM {TABLE}") or 0),
        "id_max": int(_scalar(conn, f"SELECT MAX(id) FROM {TABLE}") or 0),
    }


def _fk_stats(conn) -> dict:
    """外键有效率（全量非抽样口径）：valid = (非空外键 - 孤儿) / 非空外键。"""
    non_null = int(_scalar(
        conn, f"SELECT COUNT(*) FROM {TABLE} WHERE user_id IS NOT NULL") or 0)
    orphans = int(_scalar(
        conn,
        f"SELECT COUNT(*) FROM (SELECT user_id FROM {TABLE} "
        f"WHERE user_id IS NOT NULL) s LEFT JOIN {PARENT} p "
        "ON s.user_id = p.id WHERE p.id IS NULL") or 0)
    return {
        "non_null_fk": non_null,
        "orphans": orphans,
        "fk_valid_rate": (non_null - orphans) / non_null if non_null else None,
        "fk_min": int(_scalar(conn, f"SELECT MIN(user_id) FROM {TABLE}") or 0),
        "fk_max": int(_scalar(conn, f"SELECT MAX(user_id) FROM {TABLE}") or 0),
    }


def test_realdb_gate_ptde_engine_e2e(tmp_path):
    """真库门控：行数精确 + 分布容差内 + 主键区间 + 零孤儿外键；报告归档。"""
    conn = _conn()
    run = _ptde_engine_run(conn, tmp_path)
    stats = _stats(conn)
    fk = _fk_stats(conn)

    # 1. 行数精确（含父表行数核对）
    assert run["rows"] == N_PARENT + N
    assert stats["rows"] == N
    assert int(_scalar(conn, f"SELECT COUNT(*) FROM {PARENT}")) == N_PARENT

    # 2. 分布统计在容差内（加权类目频率 vs 权重，绝对容差 0.03；
    #    N=10000 时二项采样标准误 ≈ 0.0045）
    tolerance = 0.03
    verdicts: dict[str, object] = {}
    for s, w in zip(STATUSES, WEIGHTS, strict=True):
        freq = stats["status_freq"][s] / stats["rows"]
        assert abs(freq - w) <= tolerance, (s, freq, w)
    verdicts["status_freq_within_tolerance"] = True
    # amount 均匀域：min/max 在域内、均值在均值 ± 5% 内（均匀采样中心极限）
    assert AMOUNT_MIN - 0.01 <= stats["amount_min"] <= AMOUNT_MAX
    assert AMOUNT_MIN <= stats["amount_max"] <= AMOUNT_MAX + 0.01
    mean = (AMOUNT_MIN + AMOUNT_MAX) / 2
    assert abs(stats["amount_mean"] - mean) <= mean * 0.05, stats
    verdicts["amount_uniform_domain"] = True

    # 3. 主键区间 [1, N]（seq 规则落 id 列 → planned_range 接管）
    assert stats["id_min"] == 1 and stats["id_max"] == N
    verdicts["id_range_planned"] = True

    # 4. 外键零孤儿 + 域 [1, N_PARENT]（任务内父表 → 关系声明 → planned span）
    assert fk["orphans"] == 0, fk
    assert fk["fk_valid_rate"] == 1.0, fk
    assert 1 <= fk["fk_min"] and fk["fk_max"] <= N_PARENT, fk
    verdicts["relationship_check"] = {
        "relation": f"{TABLE}.user_id -> {PARENT}.id",
        "fk_valid_rate": fk["fk_valid_rate"],
        "orphans": fk["orphans"],
        "fk_domain": [1, N_PARENT],
    }

    report = {
        "gate": "真库端到端门控（原 §13.1 门禁 2 双跑对照的 PTDE 终态承接）",
        "path": "PtdeDatagenEngine → build_realdb_pipeline → generate/load",
        "table": TABLE,
        "parent_table": PARENT,
        "rows": N,
        "parent_rows": N_PARENT,
        "tolerance": tolerance,
        "run": run,
        "stats": stats,
        "fk": fk,
        "verdicts": verdicts,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    out = Path(__file__).resolve().parents[1] / "benchmarks"
    out.mkdir(exist_ok=True)
    (out / "ptde_realdb_gate_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
