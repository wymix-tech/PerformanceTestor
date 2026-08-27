#!/usr/bin/env bash
# =====================================================================
# PerformanceTestor 全量构建脚本
# 用法：scripts/build.sh [--skip-frontend] [--skip-backend]
# 产物：
#   perf-agent/target/perf-agent.jar     监控探针（Java Agent）
#   perf-server/target/perf-server.jar   后端服务
#   perf-demo/target/perf-demo.jar       示例被测应用
#   perf-web/dist/                       前端静态资源
# =====================================================================
set -e
cd "$(dirname "$0")/.."

SKIP_FRONTEND=0
SKIP_BACKEND=0
for arg in "$@"; do
  case "$arg" in
    --skip-frontend) SKIP_FRONTEND=1 ;;
    --skip-backend)  SKIP_BACKEND=1 ;;
    *) echo "未知参数: $arg" >&2; exit 1 ;;
  esac
done

echo "==> [1/3] 构建后端 Maven 模块（perf-agent / perf-server / perf-demo）"
if [ "$SKIP_BACKEND" -eq 0 ]; then
  mvn -q clean package -DskipTests
  echo "    完成："
  ls -lh perf-agent/target/perf-agent.jar perf-server/target/perf-server.jar perf-demo/target/perf-demo.jar 2>/dev/null || true
else
  echo "    已跳过"
fi

echo "==> [2/3] 构建前端 perf-web"
if [ "$SKIP_FRONTEND" -eq 0 ]; then
  (cd perf-web && npm install --registry=https://registry.npmmirror.com && npm run build)
  echo "    完成：perf-web/dist/"
else
  echo "    已跳过"
fi

echo "==> [3/3] 构建完成 ✅"
echo "    提示：单机演示执行 scripts/start-all.sh；多机部署分别执行 start-server.sh / start-agent.sh"
