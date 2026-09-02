#!/usr/bin/env bash
# 多接口场景「步骤上下文引用」端到端自验（前置：perf-server 8080、perf-demo 8081 已启动）
# 用法：export PERF_ADMIN_PASSWORD=<管理员密码>   # 必填，脚本不内置默认口令；账号默认 admin
#       BASE=http://127.0.0.1:8080 DEMO=http://127.0.0.1:8081 ./verify-scenario-context.sh
#
# 验证链路（零 extract 声明）：
#   步骤1 login → 步骤2 order（请求头引用 p1.result.data.token + p1.header.X-Request-Id，
#                            URL 引用 login.result.data.userId）
#              → 步骤3 detail（URL 引用 order.result.data.orderId）
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"
DEMO="${DEMO:-http://127.0.0.1:8081}"

pass=0; fail=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  [PASS] $1"; pass=$((pass+1));
  else echo "  [FAIL] $1"; fail=$((fail+1)); fi; }
# 平台默认开启 JWT 鉴权（/api/** 除 /api/auth/**、/api/agent/** 外均需登录），
# 故自验前必须先取 token。
#
# 密码必须由环境变量提供，脚本不内置默认值：application.yml 里的兜底弱口令
# （admin123）只应用于本地起服务时的便利，把它写进脚本等于把弱口令固化进仓库
# 并诱导人照抄到生产。用户名非机密，默认 admin，可用 PERF_ADMIN_USER 覆盖。
ADMIN_USER="${PERF_ADMIN_USER:-admin}"
ADMIN_PASS="${PERF_ADMIN_PASSWORD:-}"
if [ -z "$ADMIN_PASS" ]; then
  echo "错误：未设置 PERF_ADMIN_PASSWORD，脚本不内置默认口令。" >&2
  echo "  export PERF_ADMIN_PASSWORD=<你的管理员密码>   # 账号默认 admin，可用 PERF_ADMIN_USER 覆盖" >&2
  echo "  BASE=... DEMO=... ./scripts/verify-scenario-context.sh" >&2
  exit 1
fi

login() { curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}"; }
# 带 token 的请求（未取到 token 时降级为匿名，便于直接观察 401 现象）
post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -H "$AUTH_HEADER" -d "$2"; }
get()  { curl -s "$BASE$1" -H "$AUTH_HEADER"; }

echo "== 0. 前置检查 =="
LOGIN=$(login)
TOKEN=$(echo "$LOGIN" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
if [ -n "$TOKEN" ]; then
  AUTH_HEADER="Authorization: Bearer $TOKEN"
else
  AUTH_HEADER="Authorization: Bearer "
  echo "  [WARN] 登录未取到 token（响应：$LOGIN）——后续接口预期全部 401"
fi
check "登录成功并取得 token" "[ -n \"$TOKEN\" ]"
check "perf-demo 登录接口可用" "curl -s -o /dev/null -w '%{http_code}' -X POST $DEMO/api/scenario/login | grep -q 200"
check "无 token 时下单返回 401" \
  "curl -s -o /dev/null -w '%{http_code}' -X POST $DEMO/api/scenario/order | grep -q 401"

echo "== 1. 提交三步场景（零 extract 声明） =="
PAYLOAD=$(python3 - "$DEMO" <<'PY'
import json, sys
demo = sys.argv[1]
# 步骤 2 的请求头同时引用上游响应体（token）与响应头（X-Request-Id）；
# 步骤 3 的 URL 引用步骤 2 响应体里的 orderId —— 全程不声明任何 extract
json.dump({
    "taskName": "verify-scenario-context",
    "engineType": "http",
    "concurrency": 4,
    "durationSec": 8,
    "warmupSec": 3,
    "sampleIntervalSec": 2,
    "scenario": [
        {"alias": "login", "name": "登录", "url": demo + "/api/scenario/login",
         "method": "POST", "weight": 3},
        {"alias": "order", "name": "下单", "url": demo + "/api/scenario/order?u={{login.result.data.userId}}",
         "method": "POST", "weight": 5,
         "headers": {"Authorization": "Bearer {{p1.result.data.token}}",
                     "X-Trace-Id": "{{p1.header.X-Request-Id}}"}},
        {"alias": "detail", "name": "详情", "url": demo + "/api/scenario/order/{{order.result.data.orderId}}",
         "method": "GET", "weight": 2,
         "headers": {"Authorization": "Bearer {{p1.result.data.token}}"}}
    ]
}, sys.stdout)
PY
)
R=$(post /api/stress/tasks "$PAYLOAD")
TID=$(echo "$R" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')
check "任务创建成功（id=$TID）" "[ -n \"$TID\" ]"

echo "== 2. 等待任务完成 =="
ST=""
for _ in $(seq 1 60); do
  ST=$(get "/api/stress/tasks/$TID" | grep -o '"status":"[a-zA-Z]*"' | head -1 | cut -d'"' -f4)
  case "$ST" in finished|failed) break;; esac
  sleep 1
done
check "任务 finished（实际 $ST）" "[ \"$ST\" = \"finished\" ]"

REPORT=$(get "/api/stress/tasks/$TID/report")

# 报告接口返回 ApiResponse 包装：result 在 data.result 下（前端由 axios 拦截器解包，
# 脚本是裸 curl，必须自己多取一层 data）
result_json() { python3 -c 'import json,sys; json.dump(json.load(sys.stdin)["data"]["result"], sys.stdout)'; }

echo "== 3. 上下文引用生效性 =="
# 引用失效会表现为下游 100% 401；反之说明 token / X-Request-Id 确实取自上游响应
ORDER_ERR=$(echo "$REPORT" | result_json | python3 -c '
import json,sys
r = json.load(sys.stdin)
for s in r.get("stepStats", []):
    if s["stepName"] == "下单":
        print(int(s["errorRate"]))
        break
else:
    print(-1)
')
check "下单步骤错误率为 0（实际 ${ORDER_ERR}%）" "[ \"$ORDER_ERR\" = \"0\" ]"

DETAIL_ERR=$(echo "$REPORT" | result_json | python3 -c '
import json,sys
r = json.load(sys.stdin)
for s in r.get("stepStats", []):
    if s["stepName"] == "详情":
        print(int(s["errorRate"]))
        break
else:
    print(-1)
')
check "详情步骤错误率为 0（实际 ${DETAIL_ERR}%）" "[ \"$DETAIL_ERR\" = \"0\" ]"

echo "== 4. 未命中计数与配置告警 =="
MISS=$(echo "$REPORT" | result_json | python3 -c '
import json,sys
r = json.load(sys.stdin)
print(max((s.get("renderMiss", 0) for s in r.get("stepStats", [])), default=-1))
')
check "正式期 renderMiss 为 0（warmup 期未命中不得计入，实际 $MISS）" "[ \"$MISS\" = \"0\" ]"

WARN_COUNT=$(echo "$REPORT" | result_json | python3 -c '
import json,sys
print(len(json.load(sys.stdin).get("warnings", [])))
')
check "合法配置无 warnings（实际 $WARN_COUNT 条）" "[ \"$WARN_COUNT\" = \"0\" ]"

echo "== 5. 越界引用的降级行为（不中断压测，产出 warning） =="
BAD=$(python3 - "$DEMO" <<'PY'
import json, sys
demo = sys.argv[1]
json.dump({
    "taskName": "verify-scenario-bad-ref",
    "engineType": "http", "concurrency": 2, "durationSec": 3, "warmupSec": 0,
    "sampleIntervalSec": 1,
    "scenario": [
        # name 必须显式给出：stepName 只取 name，alias 仅用于上下文引用，
        # 未填 name 时 stepName 回落成 step-N，会让下面按名查找步骤的断言落空
        {"alias": "home", "name": "home", "url": demo + "/api/scenario/login", "method": "POST", "weight": 1},
        # p9 越界：启动期应产出 warning，运行期保留原文并计入 renderMiss
        {"alias": "bad", "name": "bad", "url": demo + "/api/scenario/order", "method": "POST", "weight": 1,
         "headers": {"Authorization": "Bearer {{p9.result.data.token}}"}}
    ]
}, sys.stdout)
PY
)
R2=$(post /api/stress/tasks "$BAD")
TID2=$(echo "$R2" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')
ST2=""
for _ in $(seq 1 40); do
  ST2=$(get "/api/stress/tasks/$TID2" | grep -o '"status":"[a-zA-Z]*"' | head -1 | cut -d'"' -f4)
  case "$ST2" in finished|failed) break;; esac
  sleep 1
done
check "越界引用任务照常完成（实际 $ST2）" "[ \"$ST2\" = \"finished\" ]"
REPORT2=$(get "/api/stress/tasks/$TID2/report")
WARN2=$(echo "$REPORT2" | result_json | python3 -c '
import json,sys
print(len(json.load(sys.stdin).get("warnings", [])))
')
check "越界引用产出 warning（实际 $WARN2 条）" "[ \"$WARN2\" -ge 1 ]"
MISS2=$(echo "$REPORT2" | result_json | python3 -c '
import json,sys
r = json.load(sys.stdin)
for s in r.get("stepStats", []):
    if s["stepName"] == "bad":
        print(int(s.get("renderMiss", 0)))
        break
else:
    print(-1)
')
check "越界引用计入 renderMiss（实际 $MISS2）" "[ \"$MISS2\" -ge 1 ]"

echo
echo "结果：PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
