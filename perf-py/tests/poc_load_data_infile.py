"""QW4：MySQL 原生装载 PoC（随 R2 并行，实施方案 §11 执行卡）。

独立 PoC 脚本：不进生产代码、不改 legacy `datagen/` 与 `ptde/`。
对照基准与 legacy 装载通道对齐：
  - 通道 A：`executemany`，batch=500（对齐 datagen/executor.py 的
    DbApiBatchExecutor：DEFAULT_BATCH_SIZE=500，逐批 executemany + commit）；
  - 通道 B：`LOAD DATA LOCAL INFILE`（pymysql，连接需 local_infile=True）。
并验证 `secure_file_priv` / LOCAL 权限（@@local_infile）/
rewriteBatchedStatements（JDBC 侧参数，pymysql 场景为对照说明）的交互。
约束开启/关闭对照：同一数据分别装载入无约束表与含唯一键+外键表。

运行（真库可用时）：
    uv run python tests/poc_load_data_infile.py --rows 100000
环境变量：POC_MYSQL_HOST / POC_MYSQL_PORT / POC_MYSQL_USER /
POC_MYSQL_PASSWORD / POC_MYSQL_DATABASE
真库不可用（环境变量缺失或连接失败）时打印跳过说明并以退出码 0 结束；
产出报告以本脚本标准输出归档（测试输出）。
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import time
from datetime import datetime, timedelta

ROWS_DEFAULT = 100_000
BATCH_SIZE = 500  # 对齐 datagen.executor.DbApiBatchExecutor.DEFAULT_BATCH_SIZE

STATUS_VALUES = ["PAID", "COMPLETED", "PENDING", "CANCELLED"]

DDL_PARENT = """
CREATE TABLE poc_parent (
  id BIGINT NOT NULL PRIMARY KEY
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# 变体 1：无约束（除主键）
DDL_PLAIN = """
CREATE TABLE poc_orders_plain (
  id BIGINT NOT NULL PRIMARY KEY,
  code VARCHAR(32) NOT NULL,
  status VARCHAR(16) NOT NULL,
  amount DECIMAL(18,2) NOT NULL,
  created_at DATETIME NOT NULL,
  remark VARCHAR(64) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# 变体 2：约束开启（唯一键 + 外键；主键列兼任外键列，约束开销最大化）
DDL_CONSTRAINED = """
CREATE TABLE poc_orders_child (
  id BIGINT NOT NULL PRIMARY KEY,
  code VARCHAR(32) NOT NULL,
  status VARCHAR(16) NOT NULL,
  amount DECIMAL(18,2) NOT NULL,
  created_at DATETIME NOT NULL,
  remark VARCHAR(64) NULL,
  UNIQUE KEY uk_code (code),
  CONSTRAINT fk_poc_parent FOREIGN KEY (id) REFERENCES poc_parent(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def _connect():
    """按环境变量建连（local_infile=True 以启用通道 B）。"""
    import pymysql

    return pymysql.connect(
        host=os.environ.get("POC_MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("POC_MYSQL_PORT", "3306")),
        user=os.environ.get("POC_MYSQL_USER", "root"),
        password=os.environ.get("POC_MYSQL_PASSWORD", ""),
        database=os.environ.get("POC_MYSQL_DATABASE", "performance_testor"),
        charset="utf8mb4",
        local_infile=True,
        autocommit=False,
    )


def _generate_csv(path: str, rows: int) -> None:
    """生成对照数据（确定性：固定随机种子；两通道写同一份 CSV/同批内存行）。"""
    rng = random.Random(20261117)
    base = datetime(2026, 1, 1)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        for i in range(1, rows + 1):
            writer.writerow([
                i,
                f"POC{i:09d}",
                rng.choice(STATUS_VALUES),
                f"{rng.uniform(1, 9999):.2f}",
                (base + timedelta(seconds=rng.randrange(31_536_000))).isoformat(sep=" "),
                f"remark-{i % 997}",
            ])


def _load_rows(path: str) -> list[list]:
    """同数据内存形态（通道 A 与 CSV 同源，保证对照公平）。"""
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.reader(f))


def _server_flags(cur) -> dict:
    flags = {}
    for name in ("version", "secure_file_priv", "local_infile", "max_allowed_packet"):
        cur.execute(f"SELECT @@{name}")
        flags[name] = cur.fetchone()[0]
    return flags


def _run_executemany(conn, table: str, rows: list[list]) -> float:
    """通道 A：分批 executemany（batch=500）+ 每批 commit（对齐 legacy 装载通道）。"""
    cur = conn.cursor()
    sql = (
        f"INSERT INTO {table} (id, code, status, amount, created_at, remark) "
        "VALUES (%s, %s, %s, %s, %s, %s)"
    )
    start = time.perf_counter()
    batch = []
    try:
        for row in rows:
            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                cur.executemany(sql, batch)
                conn.commit()
                batch = []
        if batch:
            cur.executemany(sql, batch)
            conn.commit()
    finally:
        cur.close()
    return time.perf_counter() - start


def _run_load_data_infile(conn, table: str, csv_path: str) -> float:
    """通道 B：LOAD DATA LOCAL INFILE（CSV 同数据）。"""
    cur = conn.cursor()
    sql = (
        f"LOAD DATA LOCAL INFILE '{csv_path}' INTO TABLE {table} "
        "FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"' "
        "LINES TERMINATED BY '\n' "
        "(id, code, status, amount, created_at, remark)"
    )
    start = time.perf_counter()
    try:
        cur.execute(sql)
        conn.commit()
    finally:
        cur.close()
    return time.perf_counter() - start


def _prepare_tables(conn, rows: int) -> dict[str, str]:
    """两张对照目标表：无约束（除主键）/ 约束开启（唯一键 + 外键）。"""
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS poc_orders_child")
    cur.execute("DROP TABLE IF EXISTS poc_orders_plain")
    cur.execute("DROP TABLE IF EXISTS poc_parent")
    cur.execute(DDL_PARENT)
    cur.execute(DDL_PLAIN)
    cur.execute(DDL_CONSTRAINED)
    # 预置父表行（父键覆盖子表全部 id，外键全部可命中）
    cur.executemany(
        "INSERT INTO poc_parent (id) VALUES (%s)",
        [(i,) for i in range(1, rows + 1)],
    )
    conn.commit()
    cur.close()
    return {
        "plain（无约束）": "poc_orders_plain",
        "constrained（唯一键+外键）": "poc_orders_child",
    }


def _verify_row_count(conn, table: str, expected: int) -> int:
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    got = int(cur.fetchone()[0])
    cur.close()
    if got != expected:
        raise RuntimeError(f"{table} 行数核对失败：期望 {expected}，实际 {got}")
    return got


def _truncate(conn, table: str) -> None:
    cur = conn.cursor()
    cur.execute(f"TRUNCATE TABLE {table}")
    cur.close()
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QW4 MySQL 原生装载 PoC")
    parser.add_argument("--rows", type=int, default=ROWS_DEFAULT)
    args = parser.parse_args(argv)
    rows_total = args.rows

    missing = [
        k for k in ("POC_MYSQL_HOST", "POC_MYSQL_USER", "POC_MYSQL_DATABASE")
        if k not in os.environ
    ]
    if missing:
        print(
            "[QW4 PoC] 跳过：真库不可用——缺少环境变量 "
            + ", ".join(missing)
            + "（POC_MYSQL_HOST/POC_MYSQL_PORT/POC_MYSQL_USER/POC_MYSQL_PASSWORD/"
            "POC_MYSQL_DATABASE）。"
            "本 PoC 不依赖真库即可合入；配置后重跑即可产出实测报告。"
        )
        return 0

    import tempfile

    try:
        conn = _connect()
    except Exception as exc:  # noqa: BLE001 真库不可用即跳过
        print(f"[QW4 PoC] 跳过：真库连接失败（{exc}）；配置后重跑产出实测报告。")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "poc_load_rows.csv")
        _generate_csv(csv_path, rows_total)
        rows = _load_rows(csv_path)

        cur = conn.cursor()
        flags = _server_flags(cur)
        cur.close()

        local_infile_on = str(flags["local_infile"]).lower() in {"1", "on", "true"}
        print("== QW4 MySQL 原生装载 PoC 报告 ==")
        print(f"server_version={flags['version']}")
        print(
            f"secure_file_priv={flags['secure_file_priv']!r}"
            "（LOCAL 通道不经服务端文件系统白名单；SERVER 端 INFILE 受此限制）"
        )
        print(f"local_infile={flags['local_infile']}")
        print(
            "rewriteBatchedStatements=JDBC（Connector/J）侧参数：pymysql 的 "
            "executemany 内部已聚合多值 INSERT（对齐 datagen.executor 现行为）；"
            "R5 若切换 Connector/J 需将其=true 作为前置校验项"
        )
        print(f"rows={rows_total} batch_size={BATCH_SIZE}")
        if not local_infile_on:
            print(
                "[QW4 PoC] 跳过通道 B：服务端 @@local_infile=OFF，"
                "需 SET GLOBAL local_infile=ON 后重跑"
            )
        tables = _prepare_tables(conn, rows_total)

        results = []
        for variant, table in tables.items():
            # 通道 A：executemany(batch=500)
            seconds = _run_executemany(conn, table, rows)
            _verify_row_count(conn, table, rows_total)
            results.append(("executemany(batch=500)", variant, seconds,
                            rows_total / seconds))
            _truncate(conn, table)
            if local_infile_on:
                # 通道 B：LOAD DATA LOCAL INFILE（同数据）
                seconds = _run_load_data_infile(conn, table, csv_path)
                _verify_row_count(conn, table, rows_total)
                results.append(("LOAD DATA LOCAL INFILE", variant, seconds,
                                rows_total / seconds))
                _truncate(conn, table)

        print()
        print("| 通道 | 表约束 | 耗时(s) | 行/秒 |")
        print("|---|---|---|---|")
        for channel, variant, seconds, rps in results:
            print(f"| {channel} | {variant} | {seconds:.3f} | {rps:,.0f} |")
        print()
        print(
            "结论要点：R5 T5.3 设计输入——原生 LOAD DATA 通道收益需结合 "
            "secure_file_priv/LOCAL 权限约束评估；约束开启对照见上表。"
        )

        # 清理 PoC 表
        cur = conn.cursor()
        for t in ("poc_orders_child", "poc_orders_plain", "poc_parent"):
            cur.execute(f"DROP TABLE IF EXISTS {t}")
        cur.close()
        conn.commit()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
