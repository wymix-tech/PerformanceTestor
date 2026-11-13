"""pytest 全局配置。

1) perf-py 根目录加入 sys.path（无安装环境下可直接跑测试）。

2) gevent 导入闸门（默认阻断）。

locust 引擎测试会在**收集阶段**导入 locust → gevent monkey.patch_all 全局生效，
父进程内所有后续阻塞 I/O（socket/threading/queue）被替换为 gevent 协作式实现，
导致 pytest 主线程上的网络型测试（FastAPI TestClient 等）永久挂起——
这是进程级污染，无法在运行中撤销。

因此默认阻断 locust/gevent 导入（相关用例 importorskip 自动跳过）；
需要运行引擎族用例时显式放开：

    PERF_PY_ENGINES=1 uv run pytest tests/test_locust_engine.py tests/test_jmeter_engine.py

注意：放开后不得在同一进程混跑网络型测试（TestClient/真 socket）。
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_ENGINES_ALLOWED = os.environ.get("PERF_PY_ENGINES") == "1"
_GEVENT_ROOTS = ("locust", "gevent")


class _GeventGate:
    """MetaPathFinder：阻断 locust/gevent 族导入（可经 PERF_PY_ENGINES=1 放行）。"""

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if root in _GEVENT_ROOTS and not _ENGINES_ALLOWED:
            raise ImportError(
                f"{fullname} 被测试闸门阻断：gevent/locust 导入会 monkey-patch 整个测试进程，"
                "污染后续网络型测试。运行引擎族用例请使用 "
                "PERF_PY_ENGINES=1 uv run pytest tests/test_locust_engine.py（勿与网络型测试混跑）"
            )
        return None


if not _ENGINES_ALLOWED:
    sys.meta_path.insert(0, _GeventGate())
