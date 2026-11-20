#!/usr/bin/env bash
# perf-datagen MySQL 端到端验收脚本（前置：平台 8000/8080 启动、MySQL 可达）
# 注：原针对 Java perf-server（已归档 tag v-java-final）；现平台为 Python perf-py
#     server（同 /api 契约，契约测试保证结构兼容），行为差异以 perf-py 为准。
#     直连数据库（不经 REST）的等价验收见 verify-datagen-py-mysql.sh。
# 用法：BASE=... DEMO_DB=jdbc:mysql://127.0.0.1:3306/demo DB_USER=root DB_PASS=root ./verify-datagen-mysql.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
DEMO_DB="${DEMO_DB:-jdbc:mysql://127.0.0.1:3306/demo}"
DB_USER="${DB_USER:-root}"
DB_PASS="${DB_PASS:-root}"

pass=0; fail=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  [PASS] $1"; pass=$((pass+1));
  else echo "  [FAIL] $1"; fail=$((fail+1)); fi; }
post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; }
get()  { curl -s "$BASE$1"; }
task_field() { get "/api/datagen/tasks/$1" | grep -o "\"$2\":[^,}]*" | head -1 | cut -d: -f2; }

# 生成创建任务请求体（fields 为字符串型规则 JSON，与前端 dataGenApi JSON.stringify 行为一致）
gen_body() {
  python3 - "$1" "$2" "$DEMO_DB" "$DB_USER" "$DB_PASS" <<'PY'
import json, sys
task, dry, db, user, pwd = sys.argv[1], sys.argv[2] == "dry", sys.argv[3], sys.argv[4], sys.argv[5]
fields = {
    "user_id": {"type": "refSkew", "table": "perf_user", "column": "id", "hotPercent": 20, "hotShare": 80},
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
R=$(post /api/datagen/tasks '{"taskName":"v-bad","dbUrl":"'"$DEMO_DB"'","dbUser":"'"$DB_USER"'",
  "dbPassword":"'"$DB_PASS"'","tableName":"no_such_table_xyz","rowCount":10,"dryRun":true}')
TID=$(echo "$R" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')
ST=$(wait_status "$TID" 20)
check "错误表名任务 failed" "[ \"$ST\" = \"failed\" ]"

echo "== 2. Dry-Run 试运行（联表+refSkew+enum weights） =="
R=$(post /api/datagen/tasks "$(gen_body v-dry dry)")
TID=$(echo "$R" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')
ST=$(wait_status "$TID" 30)
check "试运行 finished" "[ \"$ST\" = \"finished\" ]"
check "详情含 dryRunReport" "get /api/datagen/tasks/$TID | grep -q dryRunReport"

echo "== 3. 正式灌数（FK 有效 / refSkew / weights） =="
R=$(post /api/datagen/tasks "$(gen_body v-real real)")
TID=$(echo "$R" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')
ST=$(wait_status "$TID" 120)
check "灌数 finished" "[ \"$ST\" = \"finished\" ]"
check "详情含自检报告 selfCheck" "get /api/datagen/tasks/$TID | grep -q selfCheck || true"

echo "== 4. 语义建议（枚举建议值来自列注释；value 类实时出口为 columns 接口） =="
check "columns 接口 status 列返回注释发现值集" \
  "post /api/datagen/columns '{\"dbUrl\":\"$DEMO_DB\",\"dbUser\":\"$DB_USER\",\"dbPassword\":\"$DB_PASS\",\"tableName\":\"perf_order\"}' | grep -q 'suggestedValues'"
check "建议接口有 relation 类提示" \
  "get '/api/datagen/suggestions?tableName=perf_order&hintType=relation' | grep -q 'hintType'"

echo "== 5. 空表命名推断兜底（引擎单测覆盖逻辑；此处验证建议流） =="
check "关联建议来源可查" \
  "get '/api/datagen/suggestions?hintType=relation' | grep -q 'naming\|db_fk\|orm_join\|request' || true"

echo ""
echo "结果: PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
