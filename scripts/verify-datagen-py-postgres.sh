#!/usr/bin/env bash
# =====================================================================
# perf-datagen Python 引擎（perf-py）PostgreSQL 端到端冒烟脚本
#
# 前置条件：
#   1) perf-py 已安装驱动组：pip install -e 'perf-py[datagen-drivers]'
#   2) perf CLI 可用（PERF 环境变量可覆盖，如 "uv run perf"）
#   3) PostgreSQL 可达；脚本幂等建 perf_user / perf_order 表并清库
#
# 与 MySQL 版（verify-datagen-py-mysql.sh）的差异：
#   - JDBC URL 前缀 jdbc:postgresql://，方言 connect() 走 psycopg2/psycopg3
#   - status 列用原生枚举类型 e2e_status，验证 pg_enum 枚举发现链路
#   - 布尔列用 boolean；主键 BIGSERIAL（nextval 默认值 → 自增识别）
#
# 用法：
#   DEMO_DB=jdbc:postgresql://127.0.0.1:5432/demo DB_USER=postgres DB_PASS=postgres \
#     ./verify-datagen-py-postgres.sh
# =====================================================================
set -euo pipefail

DEMO_DB="${DEMO_DB:-jdbc:postgresql://127.0.0.1:5432/demo}"
DB_USER="${DB_USER:-postgres}"
DB_PASS="${DB_PASS:-postgres}"
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
if ! $PY -c 'import psycopg2' 2>/dev/null && ! $PY -c 'import psycopg' 2>/dev/null; then
  echo "  [FATAL] psycopg 未安装（pip install -e 'perf-py[datagen-drivers]'）"
  exit 2
fi

# SQL 执行器：psycopg2 优先 / psycopg3 回退（统一在 perf-py 源码根执行）
sql_exec() {
  $PY - "$1" <<'PY'
import os, sys
from datagen.dialects import parse_jdbc_url
h, p, d, _ = parse_jdbc_url(os.environ["DEMO_DB"], "postgresql")
kwargs = {"host": h, "port": p or 5432, "user": os.environ["DB_USER"],
          "password": os.environ["DB_PASS"], "dbname": d or "demo"}
try:
    import psycopg2
    conn = psycopg2.connect(**kwargs)
except ImportError:
    import psycopg
    conn = psycopg.connect(**kwargs)
try:
    cur = conn.cursor()
    for stmt in sys.argv[1].split(";"):
        if stmt.strip():
            cur.execute(stmt)
            if cur.description is not None:
                row = cur.fetchone()
                if row is not None:
                    print(row[0])
    conn.commit()
    cur.close()
finally:
    conn.close()
PY
}

echo "== 0. 建表（幂等）与清库 =="
sql_exec "
DO \$\$ BEGIN
  CREATE TYPE e2e_status AS ENUM ('PENDING','PAID','SHIPPED','DONE');
EXCEPTION WHEN duplicate_object THEN NULL; END \$\$;
DROP TABLE IF EXISTS perf_order;
DROP TABLE IF EXISTS perf_user;
CREATE TABLE perf_user (
    id         BIGSERIAL PRIMARY KEY,
    username   VARCHAR(64)  NOT NULL,
    email      VARCHAR(128),
    created_at TIMESTAMP DEFAULT now()
);
CREATE TABLE perf_order (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES perf_user(id),
    status     e2e_status NOT NULL,
    amount     NUMERIC(10,2),
    paid       BOOLEAN DEFAULT false,
    created_at TIMESTAMP DEFAULT now()
);" >/dev/null
echo "  [OK] perf_user / perf_order 就绪"

# spec 生成（fields 为规则 JSON，与 Java PG 验收脚本同构）
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

echo "== 2. Dry-Run 试运行（联表+refSkew+enum weights+原生枚举列） =="
gen_spec dry dry ok
check "试运行退出码 0" "$PERF datagen run '$WORKDIR/dry.json' --output '$WORKDIR/dry.out'"
check "结果含 dryRun 标记" "grep -q '\"dryRun\": true' '$WORKDIR/dry.out'"
check "结果含事务时长估算" "grep -q 'estimatedMaxTxnMs' '$WORKDIR/dry.out'"
check "逐表统计齐备" "grep -q 'perf_order' '$WORKDIR/dry.out'"

echo "== 3. 正式灌数（FK 有效 / refSkew / weights / 原生枚举写入） =="
gen_spec real real ok
check "灌数退出码 0" "$PERF datagen run '$WORKDIR/real.json' --output '$WORKDIR/real.out'"
check "插入行数 6000" "grep -q '\"insertedRows\": 6000' '$WORKDIR/real.out'"
check "perf_order 行数 5000" "[ \"\$(sql_exec 'SELECT COUNT(*) FROM perf_order' | tail -1)\" = '5000' ]"
ORPHANS=$(sql_exec "SELECT COUNT(*) FROM perf_order o LEFT JOIN perf_user u ON o.user_id=u.id WHERE u.id IS NULL" | tail -1)
check "FK 孤儿行 0" "[ \"$ORPHANS\" = '0' ]"

echo ""
echo "结果: PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
