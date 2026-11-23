"""压测 worker 子进程入口（阶段 5 · 写路径）。

gevent monkey-patch 必须发生在进程最早时刻（任何 socket/ssl 导入之前），
故独立子进程执行：父进程（uvicorn/FastAPI）不做 patch，引擎隔离在 worker 内。

用法：python -m server.stress_worker <payload_json_path> <task_id>
数据库：环境变量 PERF_PY_DATABASE_URL（由 SubprocessRunner 注入）。

P1-3 运维控制：
- SIGTERM 钩子优雅退出（cancel 通道）：不再执行后续档位，退出码 143；
- payload 临时文件 finally 删除（正常结束 / 异常 / 被中止三路径均无残留）。
"""

from __future__ import annotations

# ruff: noqa: E402  —— monkey.patch_all() 必须先于其余 import（有意为之）
from gevent import monkey

monkey.patch_all()

import json
import signal
import sys


def _on_sigterm(signum, frame) -> None:  # noqa: ARG001
    """中止信号：立即退出（SystemExit 不被 run_task 的 except Exception 捕获）。"""
    raise SystemExit(143)


def main() -> int:
    payload_path, task_id = sys.argv[1], int(sys.argv[2])
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):  # pragma: no cover — 非 main 线程/平台限制
        pass
    try:
        with open(payload_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        import os

        from server.db import database_url, make_session_factory
        from server.stress_service import run_task

        factory = make_session_factory(os.environ.get("PERF_PY_DATABASE_URL") or database_url())
        run_task(task_id, payload, factory)
        return 0
    finally:
        try:
            import os

            os.unlink(payload_path)
        except (FileNotFoundError, OSError):
            pass


if __name__ == "__main__":
    sys.exit(main())
