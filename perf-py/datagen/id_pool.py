"""父表主键池：共享层 re-export（A4 过渡）。

实现自 R0 起迁入 `ptde_common/id_pool.py`；本模块保留原 import 路径与符号集合，
legacy（default_engine 等）不改 import。R4 由 `ptde/generator/keys.py` 泛型化，
R7 随 datagen 包退役。
"""

from ptde_common.id_pool import *  # noqa: F401,F403
from ptde_common.id_pool import MAX_POOL_SIZE, IdPool  # noqa: F401 显式补齐星导可见性
