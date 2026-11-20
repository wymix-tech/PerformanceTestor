"""直连方言与批量执行 SPI（自 datagen/spi.py 原样收编终态）。

移植自 spi/DatabaseDialect.java + spi/BatchExecutor.java。

元数据读取、枚举值查询、SQL 构建与标识符引用全部下沉到方言，
核心引擎不硬依赖 MySQL；新增库种仅需注册实现类。

Java→Python 适配说明：JDBC DriverManager 不可用，连接建立由方言的
connect() 承担（测试方言可自行实现；生产方言包装对应驱动）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ptde.models.wire import SchemaMetadata


class DatabaseDialect(ABC):

    @abstractmethod
    def supports(self, jdbc_url: str | None) -> bool:
        """是否支持该 JDBC URL（如 jdbc:mysql:// 前缀）。"""

    @abstractmethod
    def enrich_jdbc_url(self, jdbc_url: str) -> str:
        """URL 增强（如 MySQL 追加 rewriteBatchedStatements=true；其他方言原样返回）。"""

    @abstractmethod
    def fetch_metadata(self, conn, table_name: str) -> SchemaMetadata:
        """读取表元数据：列（含类型/注释/自增）、主键、声明外键、当前行数。

        表名/列名内部统一小写（由 SchemaMetadata 构造完成）。
        """

    @abstractmethod
    def get_enum_values(self, conn, table_name: str | None, column_name: str | None) -> list[str]:
        """枚举值查询：MySQL 列内 ENUM（解析 COLUMN_TYPE）；PG 独立对象（查 pg_enum）。

        统一返回候选值列表；非枚举列返回空列表。
        """

    @abstractmethod
    def quote_identifier(self, name: str | None) -> str:
        """标识符引用：MySQL 反引号 `name` / PG 双引号 "name"。"""

    @abstractmethod
    def build_insert_sql(self, table_name: str, columns: list[str]) -> str:
        """构建 INSERT 语句（方言化列引用；值一律占位符）。"""

    def table_row_count(self, conn, table_name: str) -> int:
        """精确行数或估算行数（-1 表示无法获取）。"""
        return -1

    def connect(self, jdbc_url: str, user: str, password: str):
        """建立连接（Java 端由 DriverManager 承担，Python 端下沉到方言）。

        生产方言包装对应驱动；返回 DBAPI 连接。
        """
        raise NotImplementedError(f"方言 {type(self).__name__} 未实现 connect")


class BatchExecutor(ABC):

    @abstractmethod
    def get_batch_size(self) -> int:
        """单批行数。"""

    @abstractmethod
    def execute_batch(self, conn, sql: str, rows: list[list]) -> int:
        """批量写入一批行；返回实际写入行数（以 rows.size() 为权威计数）。"""
