"""数据库方言实现包（MySQL / PostgreSQL）。

宿主装配与引擎路由约定见 perf_cli/datagen_runner.py 与 datagen.spi：
新增库种仅需追加方言实现类并在宿主注册，无需改动引擎与调用方。
"""

from __future__ import annotations

import re

# jdbc:mysql://host:3306/db?k=v 与 jdbc:postgresql://host/db 变体
_URL_RE = re.compile(
    r"^jdbc:(?P<scheme>[a-zA-Z0-9_]+)://(?P<host>[^/:?]+)(?::(?P<port>\d+))?"
    r"(?:/(?P<database>[^?]*))?(?:\?(?P<query>.*))?$"
)

# 驱动缺失提示（含可选依赖安装指引）
DRIVER_HINT = "驱动未安装，请安装 perf-py 数据库驱动组：pip install -e 'perf-py[datagen-drivers]'"


def parse_jdbc_url(jdbc_url: str, scheme: str) -> tuple[str, int, str | None, dict[str, str]]:
    """解析 JDBC URL → (host, port, database, params)。

    仅接受指定 scheme（mysql / postgresql），与方言 supports() 判定同源；
    解析失败抛 ValueError（引擎顶层容错会转为 result.failure）。
    """
    m = _URL_RE.match(jdbc_url or "")
    if m is None or m.group("scheme").lower() != scheme:
        raise ValueError(f"无法解析的 {scheme} JDBC URL: {jdbc_url}")
    params: dict[str, str] = {}
    query = m.group("query")
    if query:
        for pair in query.split("&"):
            if not pair:
                continue
            k, _, v = pair.partition("=")
            params[k] = v
    return (
        m.group("host"),
        int(m.group("port") or 0),
        m.group("database") or None,
        params,
    )
