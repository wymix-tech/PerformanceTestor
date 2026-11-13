"""让 pytest 将 perf-py 根目录加入 sys.path（无安装环境下可直接跑测试）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
