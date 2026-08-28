#!/usr/bin/env bash
# =====================================================================
# PerformanceTestor 多机分离部署 - 被测服务器启动脚本
# 在【被测服务器】上以 -javaagent 方式启动 perf-demo（或任意被测应用）
# 核心：REPORT_URL 必须指向平台服务器的上报接口
# =====================================================================
set -e
cd "$(dirname "$0")/.."
mkdir -p logs

# ---------- 关键配置（多机部署必须修改） ----------
# 平台服务器上报接口（请替换为真实平台服务器 IP）
REPORT_URL=${REPORT_URL:-"http://127.0.0.1:8080/api/agent/report"}
# 被测应用自身端口
DEMO_PORT=${DEMO_PORT:-8081}
# 被测应用数据库（灌数据/业务使用）
MYSQL_HOST=${MYSQL_HOST:-127.0.0.1}
MYSQL_PORT=${MYSQL_PORT:-3306}
MYSQL_USER=${MYSQL_USER:-root}
MYSQL_PASSWORD=${MYSQL_PASSWORD:-root123}
# agent 采样率（默认 1.0 全量采样便于演示，生产建议 0.1）
SAMPLE_RATE=${SAMPLE_RATE:-1.0}
# agent 慢 SQL 阈值 ms
SLOW_SQL_THRESHOLD_MS=${SLOW_SQL_THRESHOLD_MS:-200}

echo "==> PerformanceTestor 被测端启动"
echo "    被测应用端口: ${DEMO_PORT} / 上报地址: ${REPORT_URL}"

if [ ! -f perf-agent/target/perf-agent.jar ] || [ ! -f perf-demo/target/perf-demo.jar ]; then
  echo "==> 未找到 jar，先构建 ..." && mvn -q package -DskipTests
fi

# agent 参数（逗号分隔 k=v）；appPort 供平台压测表单自动预填服务地址
AGENT_ARGS="reportUrl=${REPORT_URL},appName=perf-demo,appPort=${DEMO_PORT},sampleRate=${SAMPLE_RATE},slowSqlThresholdMs=${SLOW_SQL_THRESHOLD_MS}"

nohup java \
  -javaagent:perf-agent/target/perf-agent.jar=${AGENT_ARGS} \
  -jar perf-demo/target/perf-demo.jar \
  --server.port=${DEMO_PORT} \
  --spring.datasource.url="jdbc:mysql://${MYSQL_HOST}:${MYSQL_PORT}/demo?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai&rewriteBatchedStatements=true" \
  --spring.datasource.username=${MYSQL_USER} \
  --spring.datasource.password=${MYSQL_PASSWORD} \
  > logs/demo.log 2>&1 &

sleep 3
echo "==> 被测应用启动完成 🎉"
echo "    应用地址: http://<被测服务器IP>:${DEMO_PORT}"
echo "    日志:     tail -f logs/demo.log"
echo "    验证:     访问应用若干次后，在平台 Web 后台 JVM 监控/调用链页面查看采集数据"
