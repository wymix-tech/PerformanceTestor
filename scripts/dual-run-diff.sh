#!/usr/bin/env bash
# 双跑对拍（阶段 5 收尾）：同一 MySQL 库上 Java perf-server 与 Python perf-py server
# 同时运行，分别捕获契约 golden 后逐项 diff normalized 结构。
#
# 前置：
#   1. MySQL 可达且已建库建表（sql/init_empty.sql）
#   2. Java perf-server 运行于 JAVA_BASE（默认 8080）
#   3. Python perf-py server 运行于 PY_BASE（默认 8081），PERF_PY_DATABASE_URL 指向同一 MySQL
#   4. 两库同源数据（如先经 Java API 种子：仓库导入/探针上报/压测任务）
#
# 用法：
#   JAVA_BASE=http://127.0.0.1:8080 PY_BASE=http://127.0.0.1:8081 \
#     TREND_APP=demo-app TREND_URL='http://127.0.0.1:8080/api/auth/me' \
#     ./scripts/dual-run-diff.sh
set -euo pipefail

JAVA_BASE="${JAVA_BASE:-http://127.0.0.1:8080}"
PY_BASE="${PY_BASE:-http://127.0.0.1:8081}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY_GOLDEN_DIR="${PY_GOLDEN_DIR:-/tmp/golden-py-dual}"

mkdir -p "$PY_GOLDEN_DIR"

echo "== 1. Java 侧捕获（入版本库 golden） =="
BASE="$JAVA_BASE" "$REPO_ROOT/scripts/capture-contract.sh"

echo
echo "== 2. Python 侧捕获（入临时目录） =="
BASE="$PY_BASE" GOLDEN_DIR_OVERRIDE="$PY_GOLDEN_DIR" \
  "$REPO_ROOT/scripts/capture-contract.sh"

echo
echo "== 3. normalized 结构逐项 diff =="
REPO_ROOT="$REPO_ROOT" PY_GOLDEN_DIR="$PY_GOLDEN_DIR" python3 - <<'PY'
import json, os, pathlib

java = pathlib.Path(os.environ["REPO_ROOT"]) / "perf-py" / "tests" / "contract" / "golden"
py = pathlib.Path(os.environ["PY_GOLDEN_DIR"])
names = ["auth_me", "stress_tasks", "stress_task_detail", "stress_trend",
         "datagen_tasks", "ai_tools", "ai_knowledge"]
all_ok = True
for n in names:
    j = json.loads((java / f"{n}.json").read_text(encoding="utf-8"))
    p = json.loads((py / f"{n}.json").read_text(encoding="utf-8"))
    ok = j["normalized"] == p["normalized"] and j["status"] == p["status"]
    all_ok &= ok
    print(("PASS" if ok else "DIFF"), n)
    if not ok:
        print("  java:", json.dumps(j["normalized"], ensure_ascii=False)[:300])
        print("  py  :", json.dumps(p["normalized"], ensure_ascii=False)[:300])
print("\n双跑对拍结论:", "ALL MATCH" if all_ok else "HAS DIFF")
raise SystemExit(0 if all_ok else 1)
PY
