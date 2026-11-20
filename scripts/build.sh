#!/usr/bin/env bash
# =====================================================================
# PerformanceTestor 全量构建脚本
# 用法：scripts/build.sh [--skip-frontend] [--skip-backend] [--skip-python]
# 产物：
#   perf-agent/target/perf-agent.jar     监控探针（Java Agent）
#   perf-demo/target/perf-demo.jar       示例被测应用
#   perf-py（uv sync 安装可编辑包）       Python 平台层（CLI / server / PTDE）
#   perf-web/dist/                       前端静态资源
#
# 注：Java 版 perf-server / perf-datagen 已由 Python perf-py 重构取代
#     （归档于 tag v-java-final），故不再构建 perf-server.jar。
# =====================================================================
set -e
cd "$(dirname "$0")/.."

SKIP_FRONTEND=0
SKIP_BACKEND=0
SKIP_PYTHON=0
for arg in "$@"; do
  case "$arg" in
    --skip-frontend) SKIP_FRONTEND=1 ;;
    --skip-backend)  SKIP_BACKEND=1 ;;
    --skip-python)   SKIP_PYTHON=1 ;;
    *) echo "未知参数: $arg" >&2; exit 1 ;;
  esac
done

echo "==> [1/4] 构建 Java Maven 模块（perf-agent / perf-demo）"
if [ "$SKIP_BACKEND" -eq 0 ]; then
  mvn -q clean package -DskipTests
  echo "    完成："
  ls -lh perf-agent/target/perf-agent.jar perf-demo/target/perf-demo.jar 2>/dev/null || true
else
  echo "    已跳过"
fi

echo "==> [2/4] 安装 Python 工程层 perf-py（uv sync）"
if [ "$SKIP_PYTHON" -eq 0 ]; then
  if command -v uv >/dev/null 2>&1; then
    (cd perf-py && uv sync --all-extras)
  else
    echo "    [WARN] 未找到 uv，回退 pip install -e（可选 extras 请按需安装）"
    (cd perf-py && pip install -e .)
  fi
  echo "    完成：perf 命令可用（uv run perf version 验证）"
else
  echo "    已跳过"
fi

echo "==> [3/4] 构建前端 perf-web"
if [ "$SKIP_FRONTEND" -eq 0 ]; then
  (cd perf-web && npm install --registry=https://registry.npmmirror.com && npm run build)
  echo "    完成：perf-web/dist/"
else
  echo "    已跳过"
fi

echo "==> [4/4] 构建完成 ✅"
echo "    提示：单机演示执行 scripts/start-all.sh；多机部署分别执行 start-server.sh / start-agent.sh"
