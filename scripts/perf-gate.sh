#!/usr/bin/env bash
# =====================================================================
# 性能回归门禁调用示例
#
# 用途：在流水线中触发一次压测并与基线比对，不达标则让构建失败。
#
# Python 替代入口（Issue #23 阶段 1）：perf-py 已提供等价命令，
# 环境变量与退出码语义完全一致，CI 可直接替换：
#   cd perf-py && uv run perf gate            # 参数走 GATE_* 环境变量
#   cd perf-py && uv run perf gate --scenario scenario/scenarios/demo_nplus1.yaml
#
# 用法：
#   PERF_API_TOKEN=xxx ./scripts/perf-gate.sh [参数覆盖]
#
# 环境变量（均可在命令行覆盖）：
#   PERF_SERVER        平台地址         默认 http://localhost:8000（Python perf-py server）
#   PERF_API_TOKEN     CI 静态令牌      需与平台 perf.auth.api-token 一致
#   GATE_URL           压测目标 URL     默认 http://localhost:8081/api/order/list（perf-demo 慢 SQL 热点）
#   GATE_APP           应用名           默认 demo-app（同时用于基线匹配）
#   GATE_METHOD        请求方法         默认 GET
#   GATE_BODY          请求体           默认空
#   GATE_CONCURRENCY   并发数           默认 20
#   GATE_DURATION      压测时长(秒)     默认 60
#   GATE_WARMUP        预热时长(秒)     默认 5
#   GATE_RAMP_UP       并发爬坡(秒)     默认 0
#   GATE_QPS_LIMIT     全局 QPS 上限    默认 0（不限）
#   GATE_BASELINE      基线任务 ID      默认空（自动取最近一次同应用同 URL 任务）
#   GATE_TIMEOUT       等待超时(秒)     默认 0（由服务端按压测时长自动放宽）
#   GATE_FAIL_NO_BASE  无基线是否失败   默认 false（首次执行放行，仅记录基线）
#   GATE_WEBHOOK       告警地址         默认空（不发告警）
#
# 退出码：0 = 通过；1 = 未通过（回归/超时/失败）；2 = 调用异常（参数或网络问题）
# =====================================================================
set -uo pipefail

PERF_SERVER="${PERF_SERVER:-http://localhost:8000}"
PERF_API_TOKEN="${PERF_API_TOKEN:-}"
GATE_URL="${GATE_URL:-http://localhost:8081/api/order/list}"
GATE_APP="${GATE_APP:-demo-app}"
GATE_METHOD="${GATE_METHOD:-GET}"
GATE_BODY="${GATE_BODY:-}"
GATE_CONCURRENCY="${GATE_CONCURRENCY:-20}"
GATE_DURATION="${GATE_DURATION:-60}"
GATE_WARMUP="${GATE_WARMUP:-5}"
GATE_RAMP_UP="${GATE_RAMP_UP:-0}"
GATE_QPS_LIMIT="${GATE_QPS_LIMIT:-0}"
GATE_BASELINE="${GATE_BASELINE:-}"
GATE_TIMEOUT="${GATE_TIMEOUT:-0}"
GATE_FAIL_NO_BASE="${GATE_FAIL_NO_BASE:-false}"
GATE_WEBHOOK="${GATE_WEBHOOK:-}"

if [ -z "${PERF_API_TOKEN}" ]; then
  echo "错误：未配置 PERF_API_TOKEN（需与平台 perf.auth.api-token 一致）" >&2
  exit 2
fi

command -v python3 >/dev/null 2>&1 || { echo "错误：需要 python3" >&2; exit 2; }

echo "==> 触发性能门禁"
echo "    目标: ${GATE_URL}"
echo "    并发: ${GATE_CONCURRENCY} / 时长: ${GATE_DURATION}s / 预热: ${GATE_WARMUP}s"

# 构造请求体（JSON 字符串由 python3 生成，避免手工转义出错）
BODY=$(GATE_URL="${GATE_URL}" GATE_APP="${GATE_APP}" GATE_METHOD="${GATE_METHOD}" \
  GATE_BODY="${GATE_BODY}" GATE_CONCURRENCY="${GATE_CONCURRENCY}" \
  GATE_DURATION="${GATE_DURATION}" GATE_WARMUP="${GATE_WARMUP}" \
  GATE_RAMP_UP="${GATE_RAMP_UP}" GATE_QPS_LIMIT="${GATE_QPS_LIMIT}" \
  python3 -c '
import json, os
body = os.environ.get("GATE_BODY", "")
print(json.dumps({
    "targetUrl": os.environ["GATE_URL"],
    "appName": os.environ["GATE_APP"],
    "method": os.environ["GATE_METHOD"],
    "body": body,
    "concurrency": int(os.environ["GATE_CONCURRENCY"]),
    "durationSec": int(os.environ["GATE_DURATION"]),
    "warmupSec": int(os.environ["GATE_WARMUP"]),
    "rampUpSec": int(os.environ["GATE_RAMP_UP"]),
    "qpsLimit": int(os.environ["GATE_QPS_LIMIT"]),
}))
')

# 拼接查询参数
QUERY="failOnNoBaseline=${GATE_FAIL_NO_BASE}"
[ -n "${GATE_BASELINE}" ] && QUERY="${QUERY}&baselineTaskId=${GATE_BASELINE}"
[ -n "${GATE_TIMEOUT}" ] && [ "${GATE_TIMEOUT}" != "0" ] && QUERY="${QUERY}&timeoutSec=${GATE_TIMEOUT}"
[ -n "${GATE_WEBHOOK}" ] && QUERY="${QUERY}&webhook=$(python3 -c "import urllib.parse,os;print(urllib.parse.quote(os.environ['GATE_WEBHOOK']))")"

RESP=$(curl -sS -w '\n%{http_code}' -X POST "${PERF_SERVER}/api/stress/gate?${QUERY}" \
  -H "Content-Type: application/json" \
  -H "X-Perf-Api-Token: ${PERF_API_TOKEN}" \
  --max-time 1800 \
  -d "${BODY}" 2>&1) || { echo "错误：调用门禁接口失败：${RESP}" >&2; exit 2; }

HTTP_CODE=$(printf '%s' "${RESP}" | tail -n1)
PAYLOAD=$(printf '%s' "${RESP}" | sed '$d')

if [ "${HTTP_CODE}" != "200" ]; then
  echo "错误：门禁接口返回 HTTP ${HTTP_CODE}" >&2
  echo "${PAYLOAD}" >&2
  exit 2
fi

# 解析结论（platform 统一返回 {code, message, data}）
read -r PASSED SUMMARY TASK_ID STATUS <<<"$(PAYLOAD="${PAYLOAD}" python3 -c '
import json, os
d = json.loads(os.environ["PAYLOAD"])
v = d.get("data") or {}
print(" ".join([
    "true" if v.get("passed") else "false",
    str(v.get("summary", "")).replace(" ", "_"),
    str(v.get("taskId", "")),
    str(v.get("status", "")),
]))
')"

echo "    任务 #${TASK_ID} 状态: ${STATUS}"
echo "    结论: ${SUMMARY//_/ }"

if [ "${PASSED}" = "true" ]; then
  echo "==> 性能门禁通过 ✅"
  exit 0
fi

echo "==> 性能门禁未通过 ❌" >&2
exit 1
