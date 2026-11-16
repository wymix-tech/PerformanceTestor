#!/usr/bin/env python3
"""契约捕获脚本（阶段 5 · 步骤 1）：对运行中的 Java perf-server 抓取响应结构，
规范化后固化为 golden 文件，作为 Python 侧按域迁移的契约测试基线。

无 springdoc（本仓库未引入），故采用黑盒捕获替代 OpenAPI 导出：
- 规范化规则：dict 保留键名、list 取首元素、标量值 → 类型标记
  （string/number/boolean/null），结构与键名即契约。
- golden 同时保留原始响应样本（raw），便于人工核对与 diff。

用法：
    BASE=http://127.0.0.1:8080 ./scripts/capture-contract.sh
    # 或直接：uv run python scripts/capture_contract.py --base http://127.0.0.1:8080

前置：perf-server 已启动且 demo 库可达（管理员账号可登录）。
"""

from __future__ import annotations

import argparse
import json
import sys
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "perf-py"))
from tests.contract._utils import normalize  # noqa: E402  与契约测试共用实现（防规则漂移）

_GOLDEN_DIR_OVERRIDE = os.environ.get("GOLDEN_DIR_OVERRIDE")
GOLDEN_DIR = Path(_GOLDEN_DIR_OVERRIDE) if _GOLDEN_DIR_OVERRIDE \
    else REPO_ROOT / "perf-py" / "tests" / "contract" / "golden"

# trend 取数参数可由环境覆盖（须与库中实际压测场景一致，否则捕获到空列表）
TREND_APP = os.environ.get("TREND_APP", "demo-app")
TREND_URL = os.environ.get("TREND_URL", "http://localhost:8081/api/user/list?limit=2000")

# 捕获清单：只读/幂等端点（错误包装同样是契约的一部分）
ENDPOINTS = [
    {"name": "auth_me", "method": "GET", "path": "/api/auth/me"},
    {"name": "stress_tasks", "method": "GET", "path": "/api/stress/tasks?limit=5"},
    {"name": "stress_task_detail", "method": "GET", "path": "/api/stress/tasks/1"},
    # trend 首次捕获缺 appName/url 必填参数，拿到的是错误包装；改为带参重捕得真实契约
    {"name": "stress_trend", "method": "GET",
     "path": "/api/stress/trend?appName=" + urllib.parse.quote(TREND_APP)
             + "&url=" + urllib.parse.quote(TREND_URL, safe="") + "&limit=5"},
    {"name": "datagen_tasks", "method": "GET", "path": "/api/datagen/tasks?limit=5"},
    {"name": "ai_tools", "method": "GET", "path": "/api/ai/tools"},
    {"name": "ai_knowledge", "method": "GET", "path": "/api/ai/knowledge?limit=5"},
]


def request_json(base: str, method: str, path: str, token: str | None,
                 body: dict | None = None) -> tuple[int, dict | list]:
    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except json.JSONDecodeError:
            return e.code, {}


def login(base: str, username: str, password: str) -> str:
    status, body = request_json(base, "POST", "/api/auth/login", None,
                                {"username": username, "password": password})
    if status != 200 or not isinstance(body, dict) or "token" not in (body.get("data") or {}):
        print(f"[FATAL] 登录失败（{status}）：{json.dumps(body, ensure_ascii=False)[:200]}",
              file=sys.stderr)
        sys.exit(2)
    return body["data"]["token"]


def normalize(value) -> object:
    """值 → 类型标记；结构与键名即契约。"""
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(value[0])] if value else []
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return None
    if value == "":
        return "string"
    return "string"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", default="admin123")
    args = ap.parse_args()

    token = login(args.base, args.username, args.password)
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    ok = fail = 0
    for ep in ENDPOINTS:
        status, body = request_json(args.base, ep["method"], ep["path"], token)
        golden = {
            "endpoint": ep["path"],
            "method": ep["method"],
            "capturedAtSource": "java-perf-server",
            "status": status,
            "normalized": normalize(body),
            "raw": body,
        }
        out = GOLDEN_DIR / f"{ep['name']}.json"
        out.write_text(json.dumps(golden, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        mark = "OK" if status == 200 else f"HTTP {status}（错误包装也入库）"
        print(f"  [{mark}] {ep['method']} {ep['path']} -> {out.name}")
        if status == 200:
            ok += 1
        else:
            fail += 1
    print(f"\n捕获完成：{ok} 成功 / {fail} 非成功（均入 golden）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
