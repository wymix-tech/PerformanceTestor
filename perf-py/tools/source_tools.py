"""源码分析工具：MyBatis/JPA 源码语义扫描（移植自 datagen 源码解析层，tree-sitter）。"""

from __future__ import annotations

from pathlib import Path

from datagen.jpa_source_parser import JpaSourceParser
from datagen.mybatis_source_parser import MyBatisSourceParser
from perf_cli.datagen_runner import collect_source_files
from tools.registry import Tool

SOURCE_SCAN_SCHEMA = {
    "type": "object",
    "properties": {
        "source_dir": {"type": "string", "description": "源码目录（.java/.xml 递归扫描）"},
        "tables": {
            "type": "array",
            "items": {"type": "string"},
            "description": "关注的表名清单（作为语义扫描的已知表过滤集；来自 DB 元数据或灌数 spec）",
        },
    },
    "required": ["source_dir", "tables"],
}


def _hint_dict(h) -> dict:
    return {
        "table": h.table,
        "column": h.column,
        "kind": h.kind,
        "values": h.values,
        "threshold": ({"op": h.threshold.op, "value": h.threshold.value, "share": h.threshold.share}
                      if h.threshold is not None else None),
        "sourceRefs": h.source_refs,
    }


def _relation_dict(h) -> dict:
    return {
        "childTable": h.child_table,
        "childColumn": h.child_column,
        "parentTable": h.parent_table,
        "parentColumn": h.parent_column,
        "source": h.source,
        "confidence": h.confidence,
        "sourceRefs": h.source_refs,
    }


def source_scan(args: dict) -> dict:
    """源码语义扫描：魔法值候选（表列→候选值）+ 表间关联提示（四级语义）。"""
    source_dir = args.get("source_dir")
    tables = args.get("tables")
    if not source_dir:
        return {"error": "缺少 source_dir 参数"}
    d = Path(source_dir)
    if not d.is_dir():
        return {"error": f"目录不存在: {source_dir}"}
    if not tables:
        return {"error": "缺少 tables 参数（提供关注的表名清单，如 DB 元数据中的表）"}
    files = collect_source_files(d)
    if not files:
        return {"error": "目录中未发现 .java/.xml 源码文件"}
    known = {t.lower().strip() for t in tables}

    result = MyBatisSourceParser().scan(files, known)  # 失败文件静默跳过（引擎同策略）
    jpa = JpaSourceParser().scan(files, known)
    result.magic_hints.extend(jpa.magic_hints)
    result.relation_hints.extend(jpa.relation_hints)
    return {
        "scannedFiles": len(files),
        "magicHints": [_hint_dict(h) for h in result.magic_hints],
        "relationHints": [_relation_dict(h) for h in result.relation_hints],
    }


SOURCE_TOOLS = [
    Tool(
        name="source_scan",
        description="扫描 Java/MyBatis 源码产出语义提示：SQL 字面量/枚举常量/范围谓词"
                    "（表列→候选值）与 ORM 关联（resultMap/JOIN），用于解释业务热点与数据依赖。",
        parameters=SOURCE_SCAN_SCHEMA,
        handler=source_scan,
    ),
]
