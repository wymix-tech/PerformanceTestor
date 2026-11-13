#!/usr/bin/env bash
# =====================================================================
# perf-datagen Python 引擎（perf-py）MySQL 端到端冒烟脚本
#
# 与 Java 版 verify-datagen-mysql.sh 的差异：不经 perf-server REST，
# perf datagen run 直连数据库执行（阶段 3 步骤 5 CLI 接线验收）。
#
# 前置条件：
#   1) perf-py 已安装驱动组：pip install -e 'perf-py[datagen-drivers]'
#   2) perf CLI 可用（PERF 环境变量可覆盖，如 "uv run perf"）
#   3) MySQL 可达；脚本幂等建 perf_user / perf_order 表并清库
#
# 用法：
#   DEMO_DB=jdbc:mysql://127.0.0.1:3306/demo DB_USER=root DB_PASS=root \
#     ./verify-datagen-py-mysql.sh
# =====================================================================
set -euo pipefail

DEMO_DB="${DEMO_DB:-jdbc:mysql://127.0.0.1:3306/demo}"
DB_USER="${DB_USER:-root}"
DB_PASS="${DB_PASS:-root}"
PERF="${PERF:-perf}"
# Python 解释器（需与驱动安装环境一致；uv 环境可 PY="uv run python"）
PY="${PY:-python3}"
export DEMO_DB DB_USER DB_PASS
# 源码根：保证 python3 可 import datagen / perf_cli，uv run 在 perf-py 根执行
PY_ROOT="${PY_ROOT:-$(cd "$(dirname "$0")/../perf-py" && pwd)}"
export PYTHONPATH="$PY_ROOT${PYTHONPATH:+:$PYTHONPATH}"
cd "$PY_ROOT"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

pass=0; fail=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  [PASS] $1"; pass=$((pass+1));
  else echo "  [FAIL] $1"; fail=$((fail+1)); fi; }

echo "== 环境自检 =="
echo "  CLI: $PERF / 数据库: $DEMO_DB"
if ! $PERF version >/dev/null 2>&1; then
  echo "  [FATAL] perf CLI 不可用（可 PERF='uv run perf' 指定）"
  exit 2
fi
if ! $PY -c 'import pymysql' 2>/dev/null; then
  echo "  [FATAL] pymysql 未安装（pip install -e 'perf-py[datagen-drivers]'）"
  exit 2
fi

# SQL 执行器：优先 mysql 客户端，缺失时走 pymysql（统一在 perf-py 源码根执行）
sql_exec() {
  $PY - "$1" <<'PY'
import os, sys
import pymysql
from datagen.dialects import parse_jdbc_url
h, p, d, _ = parse_jdbc_url(os.environ["DEMO_DB"], "mysql")
conn = pymysql.connect(host=h, port=p or 3306, user=os.environ["DB_USER"],
                       password=os.environ["DB_PASS"], database=d or "demo")
try:
    with conn.cursor() as cur:
        for stmt in sys.argv[1].split(";"):
            if stmt.strip():
                cur.execute(stmt)
                try:
                    row = cur.fetchone()
                    if row is not None:
                        print(row[0])
                except pymysql.err.ProgrammingError:
                    pass
    conn.commit()
finally:
    conn.close()
PY
}

echo "== 0. 建表（幂等）与清库 =="
sql_exec "
CREATE TABLE IF NOT EXISTS perf_user (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    username   VARCHAR(64)  NOT NULL,
    email      VARCHAR(128),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS perf_order (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    status     VARCHAR(32) NOT NULL,
    amount     DECIMAL(10,2),
    paid       TINYINT(1) DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_order_user FOREIGN KEY (user_id) REFERENCES perf_user(id)
) ENGINE=InnoDB;
SET FOREIGN_KEY_CHECKS=0;
TRUNCATE TABLE perf_order;
TRUNCATE TABLE perf_user;
SET FOREIGN_KEY_CHECKS=1;" >/dev/null
echo "  [OK] perf_user / perf_order 就绪"

# spec 生成（fields 为规则 JSON，与 Java 验收脚本同构）
gen_spec() {
  $PY - "$1" "$2" "$3" > "$WORKDIR/$1.json" <<'PY'
import json, os, sys
name, dry, bad = sys.argv[1], sys.argv[2] == "dry", sys.argv[3] == "bad"
fields = {
    "user_id": {"type": "refSkew", "table": "perf_user", "column": "id",
                "hotPercent": 20, "hotShare": 80},
    "status": {"type": "enum", "values": ["PENDING", "PAID", "SHIPPED", "DONE"],
               "weights": [10, 80, 5, 5]},
}
body = {"taskName": name, "dbUrl": os.environ["DEMO_DB"], "dbUser": os.environ["DB_USER"],
        "dbPassword": os.environ["DB_PASS"], "dryRun": dry,
        "tables": [{"tableName": "no_such_table_xyz" if bad else "perf_user", "rowCount": 1000},
                   {"tableName": "no_such_table_xyz" if bad else "perf_order", "rowCount": 5000,
                    "fields": fields}]}
print(json.dumps(body))
PY
}

echo "== 1. Dry-Run 拦截错误表名 =="
gen_spec bad dry bad
if $PERF datagen run "$WORKDIR/bad.json" --output "$WORKDIR/bad.out" >/dev/null 2>&1; then
  echo "  [FAIL] 错误表名应失败"; fail=$((fail+1))
else
  echo "  [PASS] 错误表名任务 failed"; pass=$((pass+1))
fi
check "错误信息含表不存在" "grep -q '不存在' '$WORKDIR/bad.out'"

echo "== 2. Dry-Run 试运行（联表+refSkew+enum weights） =="
gen_spec dry dry ok
check "试运行退出码 0" "$PERF datagen run '$WORKDIR/dry.json' --output '$WORKDIR/dry.out'"
check "结果含 dryRun 标记" "grep -q '\"dryRun\": true' '$WORKDIR/dry.out'"
check "结果含事务时长估算" "grep -q 'estimatedMaxTxnMs' '$WORKDIR/dry.out'"
check "逐表统计齐备" "grep -q 'perf_order' '$WORKDIR/dry.out'"

echo "== 3. 正式灌数（FK 有效 / refSkew / weights） =="
gen_spec real real ok
check "灌数退出码 0" "$PERF datagen run '$WORKDIR/real.json' --output '$WORKDIR/real.out'"
check "插入行数 6000" "grep -q '\"insertedRows\": 6000' '$WORKDIR/real.out'"
check "perf_order 行数 5000" "[ \"\$(sql_exec 'SELECT COUNT(*) FROM perf_order' | tail -1)\" = '5000' ]"
ORPHANS=$(sql_exec "SELECT COUNT(*) FROM perf_order o LEFT JOIN perf_user u ON o.user_id=u.id WHERE u.id IS NULL" | tail -1)
check "FK 孤儿行 0" "[ \"$ORPHANS\" = '0' ]"

echo ""
echo "结果: PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
