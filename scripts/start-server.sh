#!/usr/bin/env bash
# =====================================================================
# PerformanceTestor 多机分离部署 - 平台服务器启动脚本
# 在【平台服务器】上启动 Python server(perf-py) + perf-web
# 网络要求：平台 → 被测应用（压测端口）；被测 → 平台（本机上报端口 /api/agent/report）
#
# 注：Java 版 perf-server 已由 Python perf-py 重构取代（归档于 tag v-java-final）。
# =====================================================================
set -e
cd "$(dirname "$0")/.."
mkdir -p logs

SERVER_PORT=${SERVER_PORT:-8000}
WEB_PORT=${WEB_PORT:-5173}
MYSQL_HOST=${MYSQL_HOST:-127.0.0.1}
MYSQL_PORT=${MYSQL_PORT:-3306}
MYSQL_USER=${MYSQL_USER:-root}
MYSQL_PASSWORD=${MYSQL_PASSWORD:-root123}
# Python server 数据库（SQLAlchemy URL，生产口径 mysql+pymysql）
PERF_PY_DATABASE_URL=${PERF_PY_DATABASE_URL:-"mysql+pymysql://${MYSQL_USER}:${MYSQL_PASSWORD}@${MYSQL_HOST}:${MYSQL_PORT}/performance_testor?charset=utf8mb4"}
# 建库脚本：默认 init_empty.sql（纯净空表，无演示数据）
# 需要演示数据时改用：SQL_FILE=sql/init.sql bash scripts/start-server.sh
SQL_FILE=${SQL_FILE:-sql/init_empty.sql}

echo "==> [1/3] 初始化平台库（脚本 ${SQL_FILE}）"
if command -v mysql >/dev/null 2>&1; then
  mysql --default-character-set=utf8mb4 -h"${MYSQL_HOST}" -P"${MYSQL_PORT}" \
        -u"${MYSQL_USER}" -p"${MYSQL_PASSWORD}" < "${SQL_FILE}"
else
  echo "    [WARN] 未找到 mysql 客户端，请手工执行 ${SQL_FILE}"
fi

echo "==> [2/3] 启动 Python server perf-py（端口 ${SERVER_PORT}，上报接口 /api/agent/report）"
if [ ! -d perf-py/.venv ] && ! command -v uv >/dev/null 2>&1; then
  echo "    [WARN] 未安装 uv 且无 perf-py/.venv，请先执行 scripts/build.sh --skip-frontend --skip-backend"
fi
(cd perf-py && nohup uv run perf serve --host 0.0.0.0 --port "${SERVER_PORT}" \
  > ../logs/server.log 2>&1 &)
echo "    日志: logs/server.log"

echo "==> [3/3] 启动 perf-web 前端（端口 ${WEB_PORT}）"
if [ ! -d perf-web/node_modules ]; then
  echo "    未安装依赖，执行 npm install ..." && (cd perf-web && npm install --registry=https://registry.npmmirror.com)
fi
(cd perf-web && nohup VITE_API_TARGET="http://localhost:${SERVER_PORT}" npm run dev > ../logs/web.log 2>&1 &)
echo "    日志: logs/web.log（/api 已代理到 Python server :${SERVER_PORT}，可用 VITE_API_TARGET 覆盖）"

sleep 3
echo "==> 平台启动完成 🎉"
echo "    Web 后台:   http://<平台服务器IP>:${WEB_PORT}"
echo "    API 服务:   http://<平台服务器IP>:${SERVER_PORT}/api"
echo "    被测端 agent 上报地址: http://<平台服务器IP>:${SERVER_PORT}/api/agent/report"
echo "    请在被测服务器执行 scripts/start-agent.sh 并配置 REPORT_URL 指向本机"
