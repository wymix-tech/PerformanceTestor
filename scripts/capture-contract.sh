#!/usr/bin/env bash
# 契约捕获（阶段 5 · 步骤 1）：对运行中的 Java perf-server 抓取响应结构 → golden。
# 用法：BASE=http://127.0.0.1:8080 ./scripts/capture-contract.sh
set -euo pipefail
PY_ROOT="${PY_ROOT:-$(cd "$(dirname "$0")/../perf-py" && pwd)}"
export PYTHONPATH="$PY_ROOT${PYTHONPATH:+:$PYTHONPATH}"
cd "$PY_ROOT"
exec uv run python ../scripts/capture_contract.py "$@"
