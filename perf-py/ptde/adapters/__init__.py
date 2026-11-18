"""PTDE 数据库适配器分层（横切，总方案 §9.2）。

R1 先落 Metadata 子集（扩展采集）；R2 统计；R5（本批次）落：
  - base.py：T5.1 七大子接口 + SPI 注册（DatabaseDialect 语义映射）；
  - probe.py：T5.2 Capability Probe（探测填充，非复制示例）；
  - mysql/bulkload.py：T5.3 LOAD DATA LOCAL INFILE（QW4 口径）；
  - postgres/bulkload.py：T5.4 COPY FROM STDIN（流式 + RLS/约束预检）。

宿主装配约定（对齐 legacy datagen/dialects：宿主注册，不在包导入时隐式注册）：
  ``from ptde.adapters import register_builtin_adapters; register_builtin_adapters()``
"""

from ptde.adapters.base import register_adapter

__all__ = ["register_builtin_adapters"]


def register_builtin_adapters() -> None:
    """注册内置适配器（宿主装配入口；幂等需 overwrite 显式覆盖）。"""
    from ptde.adapters.mysql.bulkload import MysqlBulkLoadAdapter
    from ptde.adapters.postgres.bulkload import PostgresBulkLoadAdapter

    register_adapter(
        "mysql",
        MysqlBulkLoadAdapter,
        url_prefixes=("jdbc:mysql://", "mysql://"),
        overwrite=True,
    )
    register_adapter(
        "postgres",
        PostgresBulkLoadAdapter,
        url_prefixes=("jdbc:postgresql://", "jdbc:postgres://", "postgresql://", "postgres://"),
        overwrite=True,
    )
