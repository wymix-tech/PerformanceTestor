"""Java 栈解析器收编包（T1.2，第一个 Language Adapter 的解析器集合）。

自 `datagen/` 迁入的副本（legacy 本体保留供 datagen_service 使用，R7 统一退役）：

- `java_ast.py`                tree-sitter-java AST 封装
- `java_source_hints.py`       Java 枚举与常量收集
- `orm_source_parser.py`       ORM 解析抽象基座（ScanResult）
- `mybatis_source_parser.py`   MyBatis XML/注解扫描
- `jpa_source_parser.py`       JPA 注解扫描
- `call_graph_analyzer.py`     Controller → Mapper 调用链分析
- `endpoint_index_parser.py`   端点索引（供 R3 工作负载目标使用）
- `endpoint_path.py`           URL 路径工具
- `mybatis_sql_resolver.py`    Mapper 方法 → XML/注解 SQL 解析
- `sql_literal_extractor.py`   SQL 字面量/关联提取（支撑模块，T1.4 包装为 AccessPattern）
- `_legacy_model.py`           datagen.model 收编子集副本（R7 退役）

迁移期对照保证：同输入同输出（见 tests/test_ptde_lang_java_migrate.py）。
"""

from ptde.analyzer.lang_java.call_graph_analyzer import (
    CallGraphAnalyzer,
    CallGraphResult,
)
from ptde.analyzer.lang_java.call_graph_analyzer import (
    MapperInvocation as CallGraphMapperInvocation,
)
from ptde.analyzer.lang_java.endpoint_index_parser import EndpointIndexParser
from ptde.analyzer.lang_java.jpa_source_parser import JpaSourceParser
from ptde.analyzer.lang_java.layout import detect_migration_and_test_files
from ptde.analyzer.lang_java.mybatis_source_parser import MyBatisSourceParser
from ptde.analyzer.lang_java.mybatis_sql_resolver import MyBatisSqlResolver
from ptde.analyzer.lang_java.orm_source_parser import OrmSourceParser, ScanResult

__all__ = [
    "CallGraphAnalyzer",
    "CallGraphMapperInvocation",
    "CallGraphResult",
    "EndpointIndexParser",
    "JpaSourceParser",
    "MyBatisSourceParser",
    "MyBatisSqlResolver",
    "OrmSourceParser",
    "ScanResult",
    "detect_migration_and_test_files",
]
