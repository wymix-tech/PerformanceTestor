#!/usr/bin/env bash
# =====================================================================
# perf-datagen PostgreSQL 方言端到端验收脚本
#
# 前置条件：
#   1) 平台已启动（Python perf-py server 默认 8000；原 Java perf-server 8080 已归档 tag v-java-final）
#   2) PostgreSQL 可达，且已建好 perf_user / perf_order 两张表（见下方建表语句）
#
# 用法：
#   DEMO_DB=jdbc:postgresql://127.0.0.1:5432/demo DB_USER=postgres DB_PASS=postgres \
#     ./verify-datagen-postgres.sh
#
# 与 MySQL 版（verify-datagen-mysql.sh）的差异：
#   - JDBC URL 前缀 jdbc:postgresql:，方言自动追加
#     reWriteBatchedInserts=true 与 stringtype=unspecified
#   - status 列在 PG 中用原生枚举类型 e2e_status，用于验证 pg_enum 枚举发现链路
#   - 布尔列用 boolean 而非 tinyint(1)
#
# 建表语句（首次执行前手动跑一次）：
#   CREATE TABLE perf_user (
#       id         BIGSERIAL PRIMARY KEY,
#       username   VARCHAR(64)  NOT NULL,
#       email      VARCHAR(128),
#       created_at TIMESTAMP DEFAULT now()
#   );
#   CREATE TYPE e2e_status AS ENUM ('PENDING','PAID','SHIPPED','DONE');
#   CREATE TABLE perf_order (
#       id         BIGSERIAL PRIMARY KEY,
#       user_id    BIGINT NOT NULL REFERENCES perf_user(id),
#       status     e2e_status NOT NULL,
#       amount     NUMERIC(10,2),
#       paid       BOOLEAN DEFAULT false,
#       created_at TIMESTAMP DEFAULT now()
#   );
# =====================================================================
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
DEMO_DB="${DEMO_DB:-jdbc:postgresql://127.0.0.1:5432/demo}"
DB_USER="${DB_USER:-postgres}"
DB_PASS="${DB_PASS:-postgres}"

pass=0; fail=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  [PASS] $1"; pass=$((pass+1));
  else echo "  [FAIL] $1"; fail=$((fail+1)); fi; }
post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; }
get()  { curl -s "$BASE$1"; }
task_field() { get "/api/datagen/tasks/$1" | grep -o "\"$2\":[^,}]*" | head -1 | cut -d: -f2; }

echo "== 环境自检 =="
echo "  平台: $BASE / 数据库: $DEMO_DB"
if ! curl -s -o /dev/null -w '%{http_code}' "$BASE/api/datagen/tasks?limit=1" | grep -q '200\|401\|403'; then
  echo "  [FATAL] 平台不可达（$BASE），请先启动 perf-server"
  exit 2
fi
if ! python3 -c 'import sys; sys.exit(0)' 2>/dev/null; then
  echo "  [FATAL] 需要 python3"
  exit 2
fi

# 生成创建任务请求体（fields 为字符串型规则 JSON，与前端 dataGenApi JSON.stringify 行为一致）
gen_body() {
  python3 - "$1" "$2" "$DEMO_DB" "$DB_USER" "$DB_PASS" <<'PY'
import json, sys
task, dry, db, user, pwd = sys.argv[1], sys.argv[2] == "dry", sys.argv[3], sys.argv[4], sys.argv[5]
fields = {
    "user_id": {"type": "refSkew", "table": "perf_user", "column": "id", "hotPercent": 20, "hotShare": 80},
    # PG 原生枚举列：显式给枚举值，验证字符串参数写入 e2e_status 时不报类型不匹配
    "status": {"type": "enum", "values": ["PENDING", "PAID", "SHIPPED", "DONE"], "weights": [10, 80, 5, 5]},
}
body = {"taskName": task, "dbUrl": db, "dbUser": user, "dbPassword": pwd, "repoId": 1,
        "dryRun": dry,
        "tables": [{"tableName": "perf_user", "rowCount": 1000},
                   {"tableName": "perf_order", "rowCount": 5000, "fields": json.dumps(fields)}]}
print(json.dumps(body))
PY
}

wait_status() {
  local id=$1 t=${2:-30} st=""
  for _ in $(seq 1 "$t"); do
    st=$(task_field "$id" status | tr -d '"')
    case "$st" in finished|failed) echo "$st"; return;; esac
    sleep 1
  done
  echo "$st"
}

echo "== 1. Dry-Run 拦截错误表名 =="
R=$(post /api/datagen/tasks '{"taskName":"pg-v-bad","dbUrl":"'"$DEMO_DB"'","dbUser":"'"$DB_USER"'",
  "dbPassword":"'"$DB_PASS"'","tableName":"no_such_table_xyz","rowCount":10,"dryRun":true}')
TID=$(echo "$R" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')
ST=$(wait_status "$TID" 20)
check "错误表名任务 failed" "[ \"$ST\" = \"failed\" ]"

echo "== 2. Dry-Run 试运行（联表 + refSkew + enum weights） =="
R=$(post /api/datagen/tasks "$(gen_body pg-v-dry dry)")
TID=$(echo "$R" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')
ST=$(wait_status "$TID" 30)
check "试运行 finished" "[ \"$ST\" = \"finished\" ]"
check "详情含 dryRunReport" "get /api/datagen/tasks/$TID | grep -q dryRunReport"

echo "== 3. 正式灌数（FK 有效 / refSkew / 原生枚举写入） =="
R=$(post /api/datagen/tasks "$(gen_body pg-v-real real)")
TID=$(echo "$R" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')
ST=$(wait_status "$TID" 120)
check "灌数 finished" "[ \"$ST\" = \"finished\" ]"
check "详情含自检报告 selfCheck" "get /api/datagen/tasks/$TID | grep -q selfCheck"

echo "== 4. 元数据发现（PG：DatabaseMetaData + information_schema + pg_enum） =="
check "columns 接口返回 perf_order 列元数据" \
  "post /api/datagen/columns '{\"dbUrl\":\"$DEMO_DB\",\"dbUser\":\"$DB_USER\",\"dbPassword\":\"$DB_PASS\",\"tableName\":\"perf_order\"}' | grep -q 'columnName\|name'"
check "status 列识别为枚举并给出建议值" \
  "post /api/datagen/columns '{\"dbUrl\":\"$DEMO_DB\",\"dbUser\":\"$DB_USER\",\"dbPassword\":\"$DB_PASS\",\"tableName\":\"perf_order\"}' | grep -q 'suggestedValues'"
check "BIGSERIAL 主键未被当作待填充列" \
  "post /api/datagen/columns '{\"dbUrl\":\"$DEMO_DB\",\"dbUser\":\"$DB_USER\",\"dbPassword\":\"$DB_PASS\",\"tableName\":\"perf_order\"}' | grep -q 'autoIncrement'"

echo "== 5. 语义建议接口（与 MySQL 版共用） =="
check "建议接口有 relation 类提示" \
  "get '/api/datagen/suggestions?tableName=perf_order&hintType=relation' | grep -q 'hintType'"

echo ""
echo "结果: PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
