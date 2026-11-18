"""灌数宿主装配与编排：Python 侧宿主层（对齐 Java perf-server）。

对应 Java 侧三个宿主组件（本文件不移植 DataGenService 全部业务，仅承接
CLI 直连引擎所需的最小编排，阶段 5 server 域迁移时再补齐任务化/持久化）：
- DataGenEngineConfig：方言/执行器/ORM 解析器注册（新增库种/ORM 仅在此追加）
- DataGenSpecAdapter.toSpec：请求 JSON → DataGenSpec（字段规则解析逐键对齐）
- DataGenService 的进度回调与结果序列化（EngineResult → camelCase JSON）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from datagen.default_engine import DefaultDataGenEngine
from datagen.dialects.mysql import MysqlDialect
from datagen.dialects.postgres import PostgresDialect
from datagen.executor import DbApiBatchExecutor
from datagen.jpa_source_parser import JpaSourceParser
from datagen.model import (
    DataGenSpec,
    EngineListener,
    EngineResult,
    FieldRule,
    OverThreshold,
    SourceFile,
    TableSpec,
)
from datagen.mybatis_source_parser import MyBatisSourceParser

# 源码扫描上限（防误传大目录；Java 侧 SourceFileProvider 走 DB 亦有等价约束）
MAX_SOURCE_FILES = 5000


def build_default_engine() -> DefaultDataGenEngine:
    """宿主装配：对齐 Java DataGenEngineConfig。

    多方言 + 多 ORM 解析器注册，由引擎按 supports(jdbc_url) 与
    supports(files)+order() 运行时路由；新增库种/ORM 仅需在此追加。
    """
    return DefaultDataGenEngine(
        [MysqlDialect(), PostgresDialect()],
        DbApiBatchExecutor(),
        [MyBatisSourceParser(), JpaSourceParser()],
    )


# ===== 请求 JSON → DataGenSpec（对齐 DataGenSpecAdapter） =====


def _as_text(x) -> str:
    """非数值节点的文本化（对齐 Jackson asText：bool→true/false、null→"null"）。"""
    if x is None:
        return "null"
    if isinstance(x, bool):
        return "true" if x else "false"
    return str(x)


def rule_from_json(node) -> FieldRule | None:
    """规则节点 → FieldRule（逐键对齐 DataGenSpecAdapter#toRule）。"""
    if not isinstance(node, dict):
        return None
    r = FieldRule()
    r.type = node.get("type", "")
    v = node.get("value")
    if isinstance(v, bool):
        r.value = _as_text(v)  # Java 布尔值走 asText → "true"/"false"
    elif isinstance(v, (int, float)):
        r.value = v
    elif v is not None:
        r.value = str(v)
    if node.get("min") is not None:
        r.min = _as_double(node["min"])
    if node.get("max") is not None:
        r.max = _as_double(node["max"])
    values = node.get("values")
    if isinstance(values, list):
        r.values = [x if isinstance(x, (int, float)) and not isinstance(x, bool) else _as_text(x)
                    for x in values]
    weights = node.get("weights")
    if isinstance(weights, list):
        r.weights = [_as_double(w) for w in weights]
    if node.get("table") is not None:
        r.table = str(node["table"])
    if node.get("column") is not None:
        r.column = str(node["column"])
    if node.get("hotPercent") is not None:
        r.hot_percent = _as_double(node["hotPercent"])
    if node.get("hotShare") is not None:
        r.hot_share = _as_double(node["hotShare"])
    ot = node.get("overThreshold")
    if isinstance(ot, dict):
        t = OverThreshold()
        if ot.get("op") is not None:
            t.op = str(ot["op"]) or ">"
        t.value = _as_double(ot.get("value"), 0.0)
        t.share = _as_double(ot.get("share"), 30.0)
        r.over_threshold = t
    if node.get("seqPrefix") is not None:
        r.seq_prefix = str(node["seqPrefix"])
    if node.get("seqStart") is not None:
        # 由宿主执行时刷新；spec JSON 里不固化起始值（对齐 Java 注释）
        r.seq_start = int(node["seqStart"])
    return r


def parse_fields(fields) -> dict[str, FieldRule]:
    """fields（dict 或 JSON 字符串）→ {小写列名: FieldRule}（对齐 parseFields）。"""
    out: dict[str, FieldRule] = {}
    if fields is None:
        return out
    if isinstance(fields, str):
        if not fields.strip():
            return out
        fields = json.loads(fields)
    if not isinstance(fields, dict):
        return out
    for key, node in fields.items():
        r = rule_from_json(node)
        if r is not None:
            out[key.lower()] = r
    return out


def spec_from_request(req: dict, source_files: list[SourceFile] | None = None) -> DataGenSpec:
    """请求 JSON（DataGenRequest 形状）→ DataGenSpec。

    支持表组形式（tables）与旧单表形式（tableName+rowCount+ruleJson）。
    """
    spec = DataGenSpec()
    spec.jdbc_url = req.get("dbUrl")
    spec.db_user = req.get("dbUser")
    spec.db_password = req.get("dbPassword")
    spec.orphan_rate = req.get("orphanRate")
    spec.dry_run = bool(req.get("dryRun"))
    spec.source_files = source_files
    # QW1：seed 可选字段（缺省不出现，不破坏契约）；非法值显式报错
    seed = req.get("seed")
    if seed is None or (isinstance(seed, str) and not seed.strip()):
        spec.seed = None
    else:
        try:
            spec.seed = int(seed)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"seed 必须是整数: {seed!r}") from exc

    tables: list[TableSpec] = []
    req_tables = req.get("tables")
    if req_tables:
        for dto in req_tables:
            ts = TableSpec(dto.get("tableName"), int(dto.get("rowCount", 1000)))
            ts.fields = parse_fields(dto.get("fields"))
            ts.orphan_rate = dto.get("orphanRate")
            tables.append(ts)
    else:
        ts = TableSpec(req.get("tableName"), int(req.get("rowCount", 1000)))
        ts.fields = parse_fields_json(req.get("ruleJson"))
        tables.append(ts)
    spec.tables = tables
    return spec


def parse_fields_json(rule_json: str | None) -> dict[str, FieldRule]:
    """旧单表 ruleJson（{\"fields\":{...}}）→ 规则 Map（对齐 parseFieldsJson）。"""
    out: dict[str, FieldRule] = {}
    if rule_json is None or not rule_json.strip():
        return out
    fields = json.loads(rule_json).get("fields")
    if isinstance(fields, dict):
        for key, node in fields.items():
            r = rule_from_json(node)
            if r is not None:
                out[key.lower()] = r
    return out


def collect_source_files(source_dir: Path) -> list[SourceFile]:
    """源码目录扫描 → SourceFile 列表（.java/.xml，路径排序保证确定性）。"""
    files: list[SourceFile] = []
    for p in sorted(source_dir.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in (".java", ".xml"):
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files.append(SourceFile(path=str(p), content=content))
        if len(files) >= MAX_SOURCE_FILES:
            break
    return files


def load_spec(spec_file: Path, source_dir: Path | None = None) -> DataGenSpec:
    """spec JSON 文件 → DataGenSpec（文件读取/JSON 解析异常直接上抛）。"""
    req = json.loads(spec_file.read_text(encoding="utf-8"))
    source_files = collect_source_files(source_dir) if source_dir else None
    return spec_from_request(req, source_files)


class ConsoleListener(EngineListener):
    """进度回调 → 控制台输出（对齐 DataGenService 的 onProgress 语义）。"""

    def __init__(self, write=None) -> None:
        self._write = write or (lambda s: print(s, file=sys.stderr, flush=True))
        self._last_table: str | None = None

    def on_progress(self, total_rows: int, inserted_rows: int, current_table: str | None) -> None:
        self._write(f"进度: {inserted_rows}/{total_rows} 当前表: {current_table or '-'}")

    def on_hints(self, magic_hints: list, relation_hints: list) -> None:
        if magic_hints or relation_hints:
            self._write(f"语义提示: 魔法值 {len(magic_hints)} 条 / 关联 {len(relation_hints)} 条")

    def on_warn(self, message: str) -> None:
        self._write(f"[警告] {message}")


def result_to_camel_json(result: EngineResult) -> str:
    """EngineResult → camelCase JSON（对齐 Java Jackson bean 序列化字段名）。"""
    return json.dumps({
        "success": result.success,
        "dryRun": result.dry_run,
        "totalRows": result.total_rows,
        "insertedRows": result.inserted_rows,
        "estimatedMaxTxnMs": result.estimated_max_txn_ms,
        "warnings": result.warnings,
        "tableStats": [{"table": t.table, "rows": t.rows} for t in result.table_stats],
        "errorMsg": result.error_msg,
    }, ensure_ascii=False)


def _as_double(v, default: float | None = None) -> float:
    if v is None or isinstance(v, bool):
        return default if default is not None else 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return default if default is not None else 0.0
