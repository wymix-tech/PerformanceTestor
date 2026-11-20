#!/usr/bin/env bash
# =====================================================================
# PerformanceTestor 单机演示模式启动脚本
# 在同一台服务器启动：MySQL(需自行准备) + Python server(perf-py) + perf-web + perf-demo(挂载 agent)
# 说明：单机模式仅用于演示，生产环境请使用多机分离部署（start-server.sh / start-agent.sh）
#
# 注：Java 版 perf-server 已由 Python perf-py 重构取代（归档于 tag v-java-final）。
#     Python server 通过 FastAPI 承接 /api 契约（auth/agent/stress/datagen/source/dbconns/ai）。
# =====================================================================
set -e
cd "$(dirname "$0")/.."
ROOT_DIR=$(pwd)

# ---------- 可配置项（单机演示默认值） ----------
SERVER_PORT=${SERVER_PORT:-8000}
WEB_PORT=${WEB_PORT:-5173}
DEMO_PORT=${DEMO_PORT:-8081}
MYSQL_HOST=${MYSQL_HOST:-127.0.0.1}
MYSQL_PORT=${MYSQL_PORT:-3306}
MYSQL_USER=${MYSQL_USER:-root}
MYSQL_PASSWORD=${MYSQL_PASSWORD:-root123}
# Agent 上报地址（单机演示指向本机 Python server 的 /api/agent/report）
AGENT_REPORT_URL=${AGENT_REPORT_URL:-"http://127.0.0.1:${SERVER_PORT}/api/agent/report"}
# Python server 数据库（SQLAlchemy URL，生产口径 mysql+pymysql）
PERF_PY_DATABASE_URL=${PERF_PY_DATABASE_URL:-"mysql+pymysql://${MYSQL_USER}:${MYSQL_PASSWORD}@${MYSQL_HOST}:${MYSQL_PORT}/performance_testor?charset=utf8mb4"}
# 建库脚本：默认 init_empty.sql（纯净空表，无演示数据）
# 需要首屏演示数据时改用：SQL_FILE=sql/init.sql bash scripts/start-all.sh
SQL_FILE=${SQL_FILE:-sql/init_empty.sql}

mkdir -p logs
echo "==> PerformanceTestor 单机演示模式启动"
echo "    Python server 端口: ${SERVER_PORT} / 前端端口: ${WEB_PORT} / 被测应用端口: ${DEMO_PORT}"

# ---------- 1. 初始化数据库 ----------
echo "==> [1/5] 初始化平台库（${MYSQL_HOST}:${MYSQL_PORT}，脚本 ${SQL_FILE}）"
if command -v mysql >/dev/null 2>&1; then
  mysql --default-character-set=utf8mb4 -h"${MYSQL_HOST}" -P"${MYSQL_PORT}" \
        -u"${MYSQL_USER}" -p"${MYSQL_PASSWORD}" < "${SQL_FILE}"
  echo "    数据库初始化完成"
else
  echo "    [WARN] 未找到 mysql 客户端，请手工执行 ${SQL_FILE} 完成建表"
fi

# ---------- 2. 启动 Python server（perf-py） ----------
echo "==> [2/5] 启动 Python server perf-py（端口 ${SERVER_PORT}）"
if [ ! -d perf-py/.venv ] && ! command -v uv >/dev/null 2>&1; then
  echo "    [WARN] 未安装 uv 且无 perf-py/.venv，请先执行 scripts/build.sh --skip-frontend --skip-backend"
fi
(cd perf-py && nohup uv run perf serve --host 0.0.0.0 --port "${SERVER_PORT}" \
  > ../logs/server.log 2>&1 &)
echo "    日志: logs/server.log（LLM 未配置时 mode=rule 兜底，不影响启动）"

# ---------- 3. 启动 perf-demo（挂载 agent） ----------
echo "==> [3/5] 启动 perf-demo 示例应用（端口 ${DEMO_PORT}，挂载 perf-agent）"
if [ ! -f perf-demo/target/perf-demo.jar ] || [ ! -f perf-agent/target/perf-agent.jar ]; then
  echo "    未找到 jar，先构建后端..." && mvn -q -pl perf-agent,perf-demo package -DskipTests
fi
nohup java -javaagent:perf-agent/target/perf-agent.jar=reportUrl=${AGENT_REPORT_URL},appName=perf-demo,appPort=${DEMO_PORT},sampleRate=1.0 \
  -jar perf-demo/target/perf-demo.jar \
  --server.port=${DEMO_PORT} \
  --spring.datasource.url="jdbc:mysql://${MYSQL_HOST}:${MYSQL_PORT}/demo?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai&rewriteBatchedStatements=true" \
  --spring.datasource.username=${MYSQL_USER} \
  --spring.datasource.password=${MYSQL_PASSWORD} \
  > logs/demo.log 2>&1 &
echo "    日志: logs/demo.log (PID $!)"

# ---------- 4. 启动 perf-web ----------
echo "==> [4/5] 启动 perf-web 前端（端口 ${WEB_PORT}）"
if [ ! -d perf-web/node_modules ]; then
  echo "    未安装依赖，执行 npm install ..." && (cd perf-web && npm install --registry=https://registry.npmmirror.com)
fi
(cd perf-web && nohup VITE_API_TARGET="http://localhost:${SERVER_PORT}" npm run dev > ../logs/web.log 2>&1 &)
echo "    日志: logs/web.log（/api 已代理到 Python server :${SERVER_PORT}，可用 VITE_API_TARGET 覆盖）"

# ---------- 5. 输出访问地址 ----------
echo "==> [5/5] 启动完成 🎉"
sleep 3
echo "    Web 后台:   http://localhost:${WEB_PORT}"
echo "    API 服务:   http://localhost:${SERVER_PORT}/api（Python server，健康检查 GET /actuator/health 类比端点见 /api/auth/me）"
echo "    被测应用:   http://localhost:${DEMO_PORT}/api/order/list?status=PAID（慢 SQL 热点）"
echo "    查看日志:   tail -f logs/*.log"
