"""工作负载目标分支（R3 T3.5）：datagen/plan_pipeline.py 的函数级收编终态。

收编范围（F3 函数级迁移，实施方案 §6.2 T3.5）：
- estimate / compose / complete_parents / edge_confidence / refresh_seq_starts /
  resolve_rt_baseline（含各自依赖的 dataclass、常量与辅助函数）；
- B7 续批次 11（T7.9 退役收编补全）：parse_cycle / remove_ref_edge /
  break_weakest_cycle_edge（环降级三函数，原遗留于 legacy 本体）；
- llm_enhance 段收编于 ``ptde.planner.llm_enhance``（LLM 可选增强保持
  建议定位，不进确定性规划路径——planner.py 不消费任何 LLM 输出）。

模型居所：``Op/Predicate/SqlAccess`` 取自
``ptde.analyzer.lang_java._legacy_model``（分析链模型唯一居所，与
lang_java SQL/ORM 解析器返回对象同类——compose/llm_enhance 直接消费
其产物，枚举/数据类必须同类可比）。

逐语义对齐 Java perf-server plan 域：
- RowCountEstimator     行数推导（峰压公式/权重分摊/上下限保护/全局缩减）
- PlanRuleComposer      访问事实 → 字段规则 JSON（enum+weights/range/ref/refSkew/seq）
- PlanIntegrityChecker  补父表 + 环依赖降级
- SeqStartResolver      执行时探测 seq 起始值
- RtBaselineResolver    RT 基线三级取值（history/sample/default）
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

from ptde.analyzer.lang_java._legacy_model import Op, Predicate, SqlAccess

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 常量（对齐 Java 类头）


MAX_TOTAL_ROWS = 1_000_000
MAX_TABLE_ROWS = 200_000
HIT_RATIO = 0.1
WRITE_RATIO = 1.0
AVG_CHILDREN = 10.0
PAGE_MULTIPLIER = 20
DEFAULT_PAGE_SIZE = 20
MIN_MAIN_ROWS = 1_000
MIN_PARENT_ROWS = 100

MAX_ENUM_VALUES = 64
HIT_WEIGHT_PERCENT = 80
HOT_PERCENT = 20
HOT_SHARE = 80

DEFAULT_EDGE_CONFIDENCE = 1.0
MAX_CYCLE_RETRY = 1

DEFAULT_RT_MS = 50.0
MIN_RT_MS = 5.0
MAX_RT_MS = 5000.0
SAMPLE_LOOKBACK = 20
FALLBACK_START = 1


# ================================================================ RowCountEstimator


@dataclass
class Stage:
    """并发分档（int 基本类型，缺省 1/60）。"""

    concurrency: int = 1
    duration_sec: int = 60


@dataclass
class StressParams:
    """压测参数快照（单档；分档由 stages 承载）。"""

    concurrency: int = 1
    duration_sec: int = 60
    qps_limit: float = 0.0
    assumed_rt_ms: float = DEFAULT_RT_MS
    stages: list[Stage] = field(default_factory=list)


@dataclass
class TableDemand:
    """单表行数请求：表名 + 读写类型 + 分页大小 + 所属接口下标。"""

    table: str
    write: bool
    page_size: int = DEFAULT_PAGE_SIZE
    endpoint_index: int = 0

    def __post_init__(self) -> None:
        self.table = (self.table or "").lower()
        self.page_size = self.page_size if self.page_size > 0 else DEFAULT_PAGE_SIZE
        self.endpoint_index = max(0, self.endpoint_index)


@dataclass
class Estimate:
    rows_by_table: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    total_requests: int = 0
    peak_qps: float = 0.0


def parent_rows(child_rows: int) -> int:
    """父表行数：子表行数 / 平均子记录数，受上下限保护。"""
    v = round(child_rows / AVG_CHILDREN)
    return int(max(MIN_PARENT_ROWS, min(MAX_TABLE_ROWS, v)))


def _peak_qps(params: StressParams, concurrency: int | None = None) -> float:
    if params.qps_limit > 0:
        return params.qps_limit
    rt = params.assumed_rt_ms if params.assumed_rt_ms > 0 else DEFAULT_RT_MS
    return max(0.01, params.concurrency if concurrency is None else concurrency) * (1000.0 / rt)


def _clamp_requests(v: float) -> int:
    if math.isnan(v) or math.isinf(v):
        return 1
    return int(max(1, min(1e9, v)))


def _total_requests(params: StressParams, peak: float) -> int:
    if not params.stages:
        return _clamp_requests(peak * params.duration_sec)
    return _clamp_requests(sum(_peak_qps(params, s.concurrency) * s.duration_sec
                               for s in params.stages))


def _split_by_weight(total: int, weights: list[int], count: int) -> list[float]:
    out: list[float] = []
    if count <= 0:
        return out
    usable = len(weights) == count
    wsum = sum(w for w in weights if usable and w is not None and w >= 0)
    if not usable or wsum <= 0:
        return [total / count] * count
    return [total * (w if w and w > 0 else 0) / wsum for w in weights]


def _main_rows(raw_rows: float, page_size: int) -> int:
    v = math.ceil(raw_rows)
    page_floor = max(1, page_size) * PAGE_MULTIPLIER
    return int(max(max(MIN_MAIN_ROWS, page_floor), min(MAX_TABLE_ROWS, v)))


def estimate(params: StressParams, demands: list[TableDemand],
             weights: list[int] | None = None) -> Estimate:
    """推导各表行数（同表取 max，全局超限按比例缩减并告警）。"""
    out = Estimate()
    if not demands:
        return out
    out.peak_qps = _peak_qps(params)
    out.total_requests = _total_requests(params, out.peak_qps)

    # 接口级权重 → 表访问级权重：按 demand 的 endpointIndex 取值，
    # 保证 splitByWeight 的「权重数 == 分摊对象数」前提成立（否则恒退化为均分）
    per_demand: list[int] = []
    for d in demands:
        w = None
        if weights is not None and d.endpoint_index < len(weights):
            w = weights[d.endpoint_index]
        per_demand.append(w if w is not None and w >= 0 else 0)

    step_requests = _split_by_weight(out.total_requests, per_demand, len(demands))
    for i, d in enumerate(demands):
        req = step_requests[i]
        rows = _main_rows(req * (WRITE_RATIO if d.write else HIT_RATIO), d.page_size)
        prev = out.rows_by_table.get(d.table, 0)
        out.rows_by_table[d.table] = max(prev, rows)
    _shrink_to_limit(out)
    return out


def _shrink_to_limit(out: Estimate) -> None:
    total = sum(out.rows_by_table.values())
    if total <= MAX_TOTAL_ROWS:
        return
    scale = MAX_TOTAL_ROWS / total
    reduced = 0
    for table, v in list(out.rows_by_table.items()):
        nv = int(max(1, math.floor(v * scale)))
        out.rows_by_table[table] = nv
        reduced += nv
    out.warnings.append(f"方案总行数 {total} 超过上限 {MAX_TOTAL_ROWS}，已按比例缩减至 {reduced} 行")


# ================================================================ EndpointTableFact


@dataclass
class JoinRef:
    """外键引用边（JOIN ON 或外键命名）。"""

    child_column: str
    parent_table: str
    parent_column: str
    confidence: float


@dataclass
class EndpointTableFact:
    """接口对单表的访问事实（多接口/多步骤合并：行数取 max、谓词值集 union）。"""

    table: str
    access: SqlAccess
    source_refs: list[str] = field(default_factory=list)
    predicates: list[Predicate] = field(default_factory=list)
    join_tables: list[str] = field(default_factory=list)
    join_refs: list[JoinRef] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    page_size: int = 0
    detail_lookup: bool = False
    confidence: float = 1.0


# ================================================================ PlanRuleComposer


@dataclass
class TableContext:
    """表上下文：外键引用边 + 唯一索引列 + 已有枚举建议值 + 是否详情类查询。"""

    join_refs: list[JoinRef] = field(default_factory=list)
    unique_columns: list[str] = field(default_factory=list)
    enum_values: dict[str, list[Any]] = field(default_factory=dict)
    detail_lookup: bool = False


@dataclass
class Composed:
    fields_by_table: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _dedupe(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    out: list[Any] = []
    for v in values or []:
        if v is None:
            continue
        k = str(v)
        if k not in seen:
            seen.add(k)
            out.append(v)
    return out


def _contains(values: list[Any], v: Any) -> bool:
    return any(str(o) == str(v) for o in values)


def _put_value(v: Any) -> Any:
    """枚举值序列化形态：数值→float、布尔→bool、其余→str（对齐 Java putValue）。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    return str(v)


def even_sample(values: list[Any]) -> list[Any]:
    """均匀采样（保留首尾，保证区间边界不丢）。"""
    n = len(values)
    step = (n - 1) / (MAX_ENUM_VALUES - 1)
    out: list[Any] = []
    last = -1
    for i in range(MAX_ENUM_VALUES):
        idx = min(n - 1, round(i * step))
        if idx != last:
            out.append(values[idx])
            last = idx
    return out


def apply_value_cap(values: list[Any], column: str, warnings: list[str]
                    ) -> tuple[list[Any], float | None, float | None]:
    """值集上限处理：超限后纯数值转 range，混合类型均匀采样保留 64 个。

    返回 (values, range_min, range_max)，二选一。
    """
    if len(values) <= MAX_ENUM_VALUES:
        return values, None, None
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        mn = min(float(v) for v in values)
        mx = max(float(v) for v in values)
        warnings.append(f"列 {column} 谓词值 {len(values)} 个超过上限 {MAX_ENUM_VALUES}，"
                        f"已改为范围规则 [{mn}, {mx}]")
        return [], mn, mx
    sampled = even_sample(values)
    warnings.append(f"列 {column} 谓词值 {len(values)} 个超过上限 {MAX_ENUM_VALUES}，"
                    f"已均匀采样保留 {len(sampled)} 个")
    return sampled, None, None


def with_weights(rule: dict, values: list[Any], hit_count: int) -> None:
    """权重：前 hitCount 个命中值占 80%，其余补充值均分剩余（和为 100%）。"""
    rule["type"] = "enum"
    rule["values"] = [_put_value(v) for v in values]
    n = len(values)
    if n == 0:
        rule["weights"] = []
        return
    hit = max(0, min(hit_count, n))
    rest = n - hit
    if rest > 0 and hit > 0:
        per_hit = HIT_WEIGHT_PERCENT / hit
        per_rest = (100 - HIT_WEIGHT_PERCENT) / rest
    else:
        per_hit = 100.0 / n
        per_rest = per_hit
    rule["weights"] = [per_hit if i < hit else per_rest for i in range(n)]


def ref_rule(parent_table: str, parent_column: str, detail: bool,
             confidence: float) -> dict:
    """关联引用规则：confidence 必须落进 JSON（环依赖降级运行期要读回）。"""
    rule: dict[str, Any] = {
        "type": "refSkew" if detail else "ref",
        "table": parent_table,
        "column": parent_column,
        "confidence": confidence,
    }
    if detail:
        rule["hotPercent"] = HOT_PERCENT
        rule["hotShare"] = HOT_SHARE
    return rule


def seq_prefix(column: str | None) -> str:
    """seq 前缀：列名下划线分段首字母大写拼接（order_no → ORD_）。"""
    if not column or not column.strip():
        return "S_"
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", column) if p]
    sb = "".join(p[0].upper() for p in parts)
    if not sb:
        return "S_"
    return sb[:6] + "_" if len(sb) > 6 else sb + "_"


def _compose_table(fact: EndpointTableFact, ctx: TableContext,
                   fields: dict, warnings: list[str]) -> None:
    hit_count_by_column: dict[str, int] = {}
    values_by_column: dict[str, list[Any]] = {}

    # 1. 谓词 → 枚举 / 范围
    for p in fact.predicates:
        if p is None or not p.column:
            continue
        if p.op == Op.RANGE:
            rule: dict[str, Any] = {"type": "range"}
            if p.min is not None:
                rule["min"] = p.min
            if p.max is not None:
                rule["max"] = p.max
            fields[p.column] = rule
            continue
        values, mn, mx = apply_value_cap(_dedupe(p.values), p.column, warnings)
        if mn is not None or mx is not None:
            fields[p.column] = {"type": "range", "min": mn, "max": mx}
            continue
        if values:
            values_by_column[p.column] = values
            hit_count_by_column[p.column] = len(values)

    # 2. 枚举建议值：列已有谓词值时作为补充值追加，否则单独成规则
    for col, suggested_raw in ctx.enum_values.items():
        if not col or not suggested_raw:
            continue
        suggested = _dedupe(suggested_raw)
        if not suggested:
            continue
        existing = values_by_column.get(col)
        if existing is None:
            values, mn, mx = apply_value_cap(suggested, col, warnings)
            if mn is not None or mx is not None:
                fields[col] = {"type": "range", "min": mn, "max": mx}
                continue
            if values:
                values_by_column[col] = values
                hit_count_by_column[col] = 0
            continue
        # 补充值：只追加不在命中值中的，且总数不超上限
        for v in suggested:
            if len(existing) >= MAX_ENUM_VALUES:
                break
            if not _contains(existing, v):
                existing.append(v)

    # 3. 外键引用边 → ref/refSkew（该列已有更具体规则时不覆盖——谓词优先级更高）
    for ref in ctx.join_refs:
        if ref is None or not ref.child_column or not ref.parent_table:
            continue
        if ref.child_column in values_by_column or ref.child_column in fields:
            continue
        fields[ref.child_column] = ref_rule(
            ref.parent_table, ref.parent_column, ctx.detail_lookup, ref.confidence)

    # 4. 唯一索引/业务编码列 → seq（只落 prefix）；已有规则的列不覆盖
    for col in ctx.unique_columns:
        if not col or col in values_by_column or col in fields:
            continue
        fields[col] = {"type": "seq", "seqPrefix": seq_prefix(col)}

    # 5. 统一写出枚举规则与权重
    for col, values in values_by_column.items():
        rule: dict[str, Any] = {}
        with_weights(rule, values, hit_count_by_column.get(col, 0))
        fields[col] = rule


def compose(facts: list[EndpointTableFact],
            contexts: dict[str, TableContext]) -> Composed:
    """合成各表规则（表 → 字段规则 JSON 字符串）。"""
    out = Composed()
    for fact in facts or []:
        if fact is None or not fact.table:
            continue
        ctx = contexts.get(fact.table) if contexts else None
        fields: dict = {}
        _compose_table(fact, ctx or TableContext(), fields, out.warnings)
        out.fields_by_table[fact.table] = json.dumps(fields, ensure_ascii=False)
    return out


# ================================================================ PlanIntegrityChecker


@dataclass
class PlanTable:
    """方案中的一张表。"""

    table: str | None = None
    row_count: int = MIN_MAIN_ROWS
    fields: str | None = None
    access: str = "READ"
    evidence: list[str] = field(default_factory=list)


@dataclass
class CheckResult:
    tables: list[PlanTable]
    warnings: list[str] = field(default_factory=list)


def _ref_targets(fields_json: str | None) -> list[str]:
    """提取字段规则中所有 ref/refSkew 的目标表名（全小写）。"""
    out: list[str] = []
    if not fields_json or not fields_json.strip():
        return out
    try:
        root = json.loads(fields_json)
    except (ValueError, TypeError):
        return out
    if not isinstance(root, dict):
        return out
    for rule in root.values():
        if not isinstance(rule, dict):
            continue
        rtype = str(rule.get("type") or "")
        if rtype.lower() not in ("ref", "refskew"):
            continue
        parent = str(rule.get("table") or "").lower()
        if parent:
            out.append(parent)
    return out


def complete_parents(tables: list[PlanTable]) -> CheckResult:
    """预检：把所有 ref/refSkew 引用到的父表并入表清单（缺失会导致外键退化随机数）。"""
    out = CheckResult(tables=list(tables or []))
    if not out.tables:
        return out
    present = {t.table.lower() for t in out.tables if t.table}
    added: list[PlanTable] = []
    for t in list(out.tables):
        for parent in _ref_targets(t.fields):
            if parent in present:
                continue
            present.add(parent)
            p = PlanTable(table=parent, row_count=parent_rows(t.row_count),
                          fields=None, access="READ")
            p.evidence.append(f"由表 {t.table} 的外键引用自动补入（保证 ref 池非空）")
            added.append(p)
            out.warnings.append(
                f"父表 {parent} 不在方案中，已自动补入（{p.row_count} 行）"
                "——缺失会导致子表外键值退化为随机数且无任何报错")
    out.tables.extend(added)
    return out


def edge_confidence(tables: list[PlanTable]) -> dict[str, float]:
    """提取「子表->父表」边置信度（同对表多条 ref 列取最高）。"""
    out: dict[str, float] = {}
    for t in tables or []:
        child = (t.table or "").lower()
        if not child or not t.fields or not t.fields.strip():
            continue
        try:
            root = json.loads(t.fields)
        except (ValueError, TypeError):
            continue
        if not isinstance(root, dict):
            continue
        for rule in root.values():
            if not isinstance(rule, dict):
                continue
            rtype = str(rule.get("type") or "").lower()
            if rtype not in ("ref", "refskew"):
                continue
            parent = str(rule.get("table") or "").lower()
            if not parent:
                continue
            conf = rule.get("confidence")
            conf = float(conf) if conf is not None else DEFAULT_EDGE_CONFIDENCE
            key = f"{child}->{parent}"
            out[key] = max(out.get(key, 0.0), conf)
    return out


def parse_cycle(message: str) -> list[str]:
    """从环消息解析节点：支持 "a -> b -> a"，去消息前缀与非法字符。"""
    nodes: list[str] = []
    for part in re.split(r"\s*->\s*", message or ""):
        n = re.sub(r"[^A-Za-z0-9_]", "", part.strip())
        if n:
            nodes.append(n.lower())
    if len(nodes) > 2 and nodes[0] == nodes[-1]:
        nodes.pop()
    return nodes


def remove_ref_edge(tables: list[PlanTable], child: str, parent: str) -> bool:
    """移除子表中指向指定父表的 ref/refSkew 规则。"""
    removed = False
    for t in tables:
        if not t.table or t.table.lower() != child.lower() \
                or not t.fields or not t.fields.strip():
            continue
        try:
            root = json.loads(t.fields)
        except (ValueError, TypeError):
            continue
        if not isinstance(root, dict):
            continue
        to_remove = [col for col, rule in root.items()
                     if isinstance(rule, dict)
                     and str(rule.get("type") or "").lower() in ("ref", "refskew")
                     and str(rule.get("table") or "").lower() == parent.lower()]
        if to_remove:
            for col in to_remove:
                root.pop(col, None)
            t.fields = json.dumps(root, ensure_ascii=False)
            removed = True
            log.warning("cycle detected: removed ref edge %s -> %s (column %s)",
                        child, parent, to_remove)
    return removed


def break_weakest_cycle_edge(tables: list[PlanTable], cycle_message: str | None,
                             confidences: dict[str, float] | None) -> bool:
    """环依赖降级：剔除环中置信度最低的一条 ref 边（返回是否已剔除）。"""
    if not tables or not cycle_message:
        return False
    nodes = parse_cycle(cycle_message)
    if len(nodes) < 2:
        return False
    victim: str | None = None
    lowest = math.inf
    for i, child in enumerate(nodes):
        parent = nodes[(i + 1) % len(nodes)]
        key = f"{child}->{parent}"
        conf = (confidences or {}).get(key, DEFAULT_EDGE_CONFIDENCE)
        if conf < lowest:
            lowest = conf
            victim = key
    if victim is None:
        return False
    child, parent = victim.split("->", 1)
    return remove_ref_edge(tables, child, parent)


# ================================================================ SeqStartResolver


def _quote(ident: str, pg: bool) -> str:
    return '"' + ident.replace('"', '""') + '"' if pg else "`" + ident.replace("`", "``") + "`"


def _detect_start(conn, table: str, column: str, prefix: str, pg: bool) -> int:
    """探测起始值：先纯数值 MAX，失败再按「剥前缀转数值」MAX（返回 0 = 失败）。"""
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT MAX({_quote(column, pg)}) FROM {_quote(table, pg)}")
        row = cur.fetchone()
    except Exception:  # noqa: BLE001 — 对齐 queryMax：失败返回 0
        row = None
    finally:
        cur.close()
    numeric = _as_long(row[0] if row else None)
    if numeric > 0:
        return numeric + 1
    if not prefix or not prefix.strip():
        return 0
    start = len(prefix) + 1
    col_ref, from_ref = _quote(column, pg), _quote(table, pg)
    sql = (f"SELECT MAX(substring({col_ref} from {start})::numeric) FROM {from_ref}" if pg
           else f"SELECT MAX(CAST(SUBSTRING({col_ref}, {start}) AS DECIMAL(38,0))) FROM {from_ref}")
    cur = conn.cursor()
    try:
        cur.execute(sql)
        row = cur.fetchone()
    except Exception:  # noqa: BLE001
        row = None
    finally:
        cur.close()
    stripped = _as_long(row[0] if row else None)
    return stripped + 1 if stripped > 0 else 0


def _as_long(v: Any) -> int:
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v)
    return int(s) if s.isdigit() else 0


def refresh_seq_starts(conn, db_url: str, table: str, fields_json: str | None
                       ) -> tuple[str | None, list[str]]:
    """刷新字段规则 JSON 中所有 seq 规则的 seqStart（探测失败回退 1 并告警）。"""
    warnings: list[str] = []
    if not fields_json or not fields_json.strip() or conn is None:
        return fields_json, warnings
    try:
        root = json.loads(fields_json)
    except (ValueError, TypeError):
        return fields_json, warnings
    if not isinstance(root, dict):
        return fields_json, warnings
    pg = "postgresql" in (db_url or "").lower()
    changed = False
    for col, rule in list(root.items()):
        if not isinstance(rule, dict) or str(rule.get("type") or "").lower() != "seq":
            continue
        prefix = str(rule.get("seqPrefix") or "")
        start = _detect_start(conn, table, col, prefix, pg)
        if start <= 0:
            start = FALLBACK_START
            warnings.append(f"列 {table}.{col} 序号起始值探测失败，已回退为 {FALLBACK_START}"
                            "（若库内已有数据可能撞唯一键）")
        root[col] = {"type": "seq", "seqPrefix": prefix, "seqStart": start}
        changed = True
    return (json.dumps(root, ensure_ascii=False) if changed else fields_json), warnings


# ================================================================ RtBaselineResolver


@dataclass
class Baseline:
    rt_ms: float
    source: str  # history / sample / default


def _clamp_rt(rt: float) -> float:
    if math.isnan(rt) or math.isinf(rt):
        return DEFAULT_RT_MS
    return max(MIN_RT_MS, min(MAX_RT_MS, rt))


def _first_positive(node: dict, field_name: str) -> float:
    v = node.get(field_name)
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return float(v)
    for wrapper in ("stats", "summary", "result"):
        child = node.get(wrapper)
        if isinstance(child, dict):
            v = child.get(field_name)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                return float(v)
    return 0.0


def _parse_rt(result_json: str | None) -> float:
    """解析 resultJson 的 avgMs，缺失/异常时退化取 p95Ms（含嵌套结构兼容）。"""
    if not result_json or not result_json.strip():
        return 0
    try:
        root = json.loads(result_json)
    except (ValueError, TypeError):
        return 0
    if not isinstance(root, dict):
        return 0
    avg = _first_positive(root, "avgMs")
    if avg > 0:
        return avg
    return _first_positive(root, "p95Ms")


def resolve_rt_baseline(stress_repo, app_name: str | None, target_url: str | None,
                        task_id: int | None) -> Baseline:
    """RT 基线三级取值：taskId 采样点 → 同场景历史 → 默认 50ms（任一级失败静默降级）。"""
    if task_id is not None:
        try:
            samples = stress_repo.query_by_task(task_id, SAMPLE_LOOKBACK)
            total = 0.0
            n = 0
            for s in samples or []:
                v = s.p50_ms if (s.p50_ms is not None and s.p50_ms > 0) else s.p95_ms
                if v is not None and v > 0:
                    total += v
                    n += 1
            if n > 0:
                return Baseline(_clamp_rt(total / n), "sample")
        except Exception as exc:  # noqa: BLE001 — 对齐 Java：静默降级
            log.debug("rt baseline from samples failed, taskId=%s: %s", task_id, exc)
    if app_name and app_name.strip() and target_url and target_url.strip():
        try:
            history = stress_repo.query_by_scene(app_name, target_url, "finished", 1)
            if history:
                rt = _parse_rt(history[0].result_json)
                if rt > 0:
                    return Baseline(_clamp_rt(rt), "history")
        except Exception as exc:  # noqa: BLE001
            log.debug("rt baseline from history failed: %s", exc)
    return Baseline(DEFAULT_RT_MS, "default")


def describe_rt_source(source: str | None) -> str:
    """来源标签的中文说明（前端方案卡展示）。"""
    if source == "history":
        return "历史压测"
    if source == "sample":
        return "采样点"
    return "默认 50ms"
