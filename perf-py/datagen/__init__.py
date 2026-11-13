"""perf-datagen 核心移植（Issue #23 阶段 3 步骤 4）。

模块职责与 Java 侧 com.perftestor.datagen 一致：
mock 数据生成引擎的纯逻辑层——规则引擎 / 关联解析（四级合并）/ 源码语义 /
Dry-Run。外部依赖（JDBC、tree-sitter）在后续子步骤接入。

移植原则：语义逐行对齐 Java 实现，行为等价优先于 Python 风格重构；
与 Java 有意保留的既有行为（如宿主反射差异）在测试中注明。
"""
