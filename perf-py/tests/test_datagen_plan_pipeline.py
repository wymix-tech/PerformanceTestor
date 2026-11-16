"""datagen plans 管线单测（阶段 5 · 步骤 8）：行数推导 / 规则合成 / 完整性 / seq 刷新。

断言取自 Java RowCountEstimator / PlanRuleComposer / PlanIntegrityChecker 源码行为，
含关键边界（clamp、值集上限转 range、环降级最低置信边、seq 探测回退）。
"""

import json

from datagen.model import Op, Predicate, SqlAccess
from datagen.plan_pipeline import (
    MAX_ENUM_VALUES,
    MAX_TABLE_ROWS,
    MIN_MAIN_ROWS,
    EndpointTableFact,
    PlanTable,
    Stage,
    StressParams,
    TableContext,
    TableDemand,
    break_weakest_cycle_edge,
    complete_parents,
    compose,
    edge_confidence,
    estimate,
    even_sample,
    parent_rows,
    parse_cycle,
    ref_rule,
    seq_prefix,
)

# ---------------------------------------------------------------- 行数推导

def _params(**kw):
    return StressParams(**kw)


def test_estimate_default_params_min_rows():
    """默认参数：concurrency=1, duration=60, rt=50 → totalRequests=1200 →
    READ 主表 clamp(120, 1000, 200000) = 1000（MIN_MAIN_ROWS 兜底）。"""
    est = estimate(_params(), [TableDemand("perf_user", False)])
    assert est.rows_by_table == {"perf_user": MIN_MAIN_ROWS}


def test_estimate_write_ratio_and_page_floor():
    """WRITE×1.0：qpsLimit=100, 60s → 6000 请求 → 6000 行；分页下限 pageSize×20 生效。"""
    est = estimate(_params(qps_limit=100.0), [TableDemand("t", True, page_size=20)])
    assert est.rows_by_table["t"] == 6000
    # 小请求量 + 大分页：pageFloor 20×100=2000 > 请求量
    est2 = estimate(_params(qps_limit=5.0, duration_sec=10),
                    [TableDemand("t", True, page_size=100)])
    assert est2.rows_by_table["t"] == 2000


def test_estimate_same_table_takes_max_not_sum():
    """同表取 max：两个 demand 均分请求量（各 3000），取 max 不求和。"""
    est = estimate(_params(qps_limit=100.0),
                   [TableDemand("t", True), TableDemand("t", True)])
    assert est.rows_by_table["t"] == 3000


def test_estimate_weights_split_by_endpoint_index():
    """接口级权重按 endpointIndex 折算：两接口 weight 1:3 → 请求量 1:3。"""
    demands = [TableDemand("a", True, endpoint_index=0),
               TableDemand("b", True, endpoint_index=1)]
    est = estimate(_params(qps_limit=100.0), demands, weights=[1, 3])
    # WRITE×1.0：a: 6000×0.25=1500, b: 6000×0.75=4500（READ 会被 MIN_MAIN_ROWS 兜底）
    assert est.rows_by_table["a"] == 1500 and est.rows_by_table["b"] == 4500


def test_estimate_stages_sum_and_shrink_warning():
    """分档求和 + 全局超限按比例缩减并告警（不静默截断）。"""
    params = _params(qps_limit=100.0, stages=[Stage(10, 60), Stage(10, 60)])
    est = estimate(params, [TableDemand("t", True, page_size=10)])
    # peak=100（qpsLimit 优先）；total=100×60+100×60=12000 → clamp
    assert est.total_requests == 12000
    # 12 个表各 clamp 到 200000 行 → Σ=2.4e6 超限 → 缩减告警
    demands = [TableDemand(f"t{i}", True, page_size=10) for i in range(12)]
    est2 = estimate(_params(qps_limit=100000.0), demands)
    assert any("已按比例缩减" in w for w in est2.warnings)


def test_parent_rows_clamp():
    assert parent_rows(1000) == 100  # 1000/10=100 → MIN_PARENT_ROWS
    assert parent_rows(500000) == 50000
    assert parent_rows(2_500_000) == MAX_TABLE_ROWS
    assert parent_rows(5000) == 500


# ---------------------------------------------------------------- 规则合成


def _fact(table="perf_order", predicates=None, join_refs=None):
    f = EndpointTableFact(table, SqlAccess.READ)
    f.predicates = predicates or []
    f.join_refs = join_refs or []
    return f


def test_compose_enum_with_hit_weights():
    """等值谓词 → enum：命中值 80%（单命中值 → 80/20 权重）。"""
    fact = _fact(predicates=[Predicate(column="status", op=Op.EQ, values=["PAID"])])
    composed = compose([fact], {"perf_order": TableContext()})
    rule = json.loads(composed.fields_by_table["perf_order"])["status"]
    assert rule["type"] == "enum" and rule["values"] == ["PAID"]
    assert rule["weights"] == [100.0], "无补充值时归一化到 100（Java else 分支）"


def test_compose_enum_values_capped_to_range():
    """纯数值谓词超 64 → 转 range 并写告警；混合类型均匀采样保留首尾。"""
    values = [float(i) for i in range(100)]
    fact = _fact(predicates=[Predicate(column="id", op=Op.IN, values=values)])
    composed = compose([fact], {})
    rule = json.loads(composed.fields_by_table["perf_order"])["id"]
    assert rule["type"] == "range" and rule["min"] == 0.0 and rule["max"] == 99.0
    assert any("已改为范围规则" in w for w in composed.warnings)

    mixed = [f"id{i}" for i in range(100)]
    fact2 = _fact(predicates=[Predicate(column="code", op=Op.IN, values=mixed)])
    composed2 = compose([fact2], {})
    rule2 = json.loads(composed2.fields_by_table["perf_order"])["code"]
    assert len(rule2["values"]) == MAX_ENUM_VALUES
    assert rule2["values"][0] == "id0" and rule2["values"][-1] == "id99", "保留首尾"
    assert any("已均匀采样" in w for w in composed2.warnings)


def test_even_sample_keeps_endpoints():
    out = even_sample(list(range(200)))
    assert out[0] == 0 and out[-1] == 199 and len(out) == MAX_ENUM_VALUES


def test_compose_ref_and_seq_priority():
    """ref：已有谓词值的列不覆盖（谓词优先级更高）；seq 只落 prefix。"""
    fact = _fact(
        predicates=[Predicate(column="status", op=Op.EQ, values=["PAID"])],
        join_refs=[__import__("datagen.plan_pipeline", fromlist=["JoinRef"]).JoinRef(
            "user_id", "perf_user", "id", 0.9)])
    ctx = TableContext()
    ctx.join_refs = fact.join_refs
    ctx.unique_columns = ["order_no", "status"]
    composed = compose([fact], {"perf_order": ctx})
    fields = json.loads(composed.fields_by_table["perf_order"])
    assert fields["user_id"] == {"type": "ref", "table": "perf_user", "column": "id",
                                 "confidence": 0.9}
    assert fields["order_no"] == {"type": "seq", "seqPrefix": "ON_"}
    assert fields["status"]["type"] == "enum", "已有谓词的列不被 ref/seq 覆盖"


def test_ref_rule_detail_skew():
    rule = ref_rule("perf_user", "id", detail=True, confidence=0.7)
    assert rule["type"] == "refSkew" and rule["hotPercent"] == 20 and rule["hotShare"] == 80


def test_seq_prefix():
    assert seq_prefix("order_no") == "ON_"
    assert seq_prefix("couponCode") == "C_", "Java 正则无驼峰分段，整列名取首字母"
    assert seq_prefix("very_long_column_name_here") == "VLCNH_", "分段首字母拼接"
    assert seq_prefix(None) == "S_"


# ---------------------------------------------------------------- 完整性检查


def test_complete_parents_adds_missing_parent():
    """父表缺失 → 自动补入（行数=子表/10 clamp[100,200000]）+ 告警。"""
    fields = json.dumps({"user_id": {"type": "ref", "table": "perf_user", "column": "id"}})
    tables = [PlanTable(table="perf_order", row_count=5000, fields=fields, access="WRITE")]
    checked = complete_parents(tables)
    assert [t.table for t in checked.tables] == ["perf_order", "perf_user"]
    assert checked.tables[1].row_count == 500
    assert any("自动补入" in w for w in checked.warnings)


def test_complete_parents_no_duplicate_for_present_parent():
    fields = json.dumps({"user_id": {"type": "ref", "table": "perf_user", "column": "id"}})
    tables = [PlanTable(table="perf_order", row_count=1000, fields=fields, access="WRITE"),
              PlanTable(table="perf_user", row_count=100, fields=None, access="READ")]
    checked = complete_parents(tables)
    assert [t.table for t in checked.tables] == ["perf_order", "perf_user"]
    assert not checked.warnings


def test_cycle_parsing_and_weakest_edge_removal():
    msg = "表组存在循环依赖，无法确定生成顺序: a -> b -> a"
    assert parse_cycle(msg) == ["a", "b"]
    fields_a = json.dumps({"b_id": {"type": "ref", "table": "b", "column": "id",
                                    "confidence": 0.6}})
    fields_b = json.dumps({"a_id": {"type": "ref", "table": "a", "column": "id",
                                    "confidence": 0.9}})
    tables = [PlanTable(table="a", row_count=100, fields=fields_a, access="WRITE"),
              PlanTable(table="b", row_count=100, fields=fields_b, access="WRITE")]
    broke = break_weakest_cycle_edge(tables, msg, edge_confidence(tables))
    assert broke is True
    out_fields = json.loads(tables[0].fields)
    assert "b_id" not in out_fields, "剔除置信度最低的边（0.6 < 0.9）"
    assert json.loads(tables[1].fields)["a_id"]["confidence"] == 0.9, "另一方向的边保留"


def test_cycle_unbreakable_returns_false():
    tables = [PlanTable(table="a", row_count=100, fields=None, access="WRITE")]
    assert break_weakest_cycle_edge(tables, "x -> y -> x", {}) is False
    assert break_weakest_cycle_edge(tables, None, {}) is False


# ---------------------------------------------------------------- seq 刷新


def test_refresh_seq_starts_probe_and_fallback():
    from datagen.plan_pipeline import refresh_seq_starts

    class FakeCursor:
        def __init__(self, results):
            self._results = list(results)

        def execute(self, sql, *a):
            self._last = sql

        def fetchone(self):
            return (self._results.pop(0) if self._results else None,)

        def close(self):
            pass

    class FakeConn:
        def __init__(self, results):
            self._cursor = FakeCursor(results)

        def cursor(self):
            return self._cursor

        def close(self):
            pass

    fields = json.dumps({"order_no": {"type": "seq", "seqPrefix": "ON_"}})
    # 探测成功：MAX(col)=41 → start=42（先纯数值，无前缀剥离）
    out, warns = refresh_seq_starts(FakeConn([41]), "mysql://", "perf_order", fields)
    rule = json.loads(out)["order_no"]
    assert rule["seqStart"] == 42 and not warns
    # 探测失败 → 回退 1 + 告警
    out2, warns2 = refresh_seq_starts(FakeConn([None]), "mysql://", "perf_order", fields)
    assert json.loads(out2)["order_no"]["seqStart"] == 1
    assert any("已回退为 1" in w for w in warns2)


# ---------------------------------------------------------------- RT 基线


def test_rt_baseline_three_levels():
    from datagen.plan_pipeline import resolve_rt_baseline

    class Repo:
        def __init__(self, samples=None, history=None):
            self._samples, self._history = samples or [], history or []

        def query_by_task(self, tid, limit):
            return self._samples

        def query_by_scene(self, app, url, status, limit):
            return self._history

    class Sample:
        def __init__(self, p50, p95):
            self.p50_ms, self.p95_ms = p50, p95

    class Task:
        def __init__(self, result_json):
            self.result_json = result_json

    # ① 采样点：p50 均值
    b = resolve_rt_baseline(Repo(samples=[Sample(120.0, 300.0), Sample(80.0, 0)]),
                            None, None, task_id=7)
    assert b.source == "sample" and b.rt_ms == 100.0
    # ② 历史：avgMs（缺省退化 p95Ms，含嵌套结构）
    b2 = resolve_rt_baseline(
        Repo(history=[Task(json.dumps({"avgMs": 250.0}))]), "app", "/x", None)
    assert b2.source == "history" and b2.rt_ms == 250.0
    b3 = resolve_rt_baseline(
        Repo(history=[Task(json.dumps({"stats": {"p95Ms": 480.0}}))]), "app", "/x", None)
    assert b3.source == "history" and b3.rt_ms == 480.0
    # ③ 缺省
    b4 = resolve_rt_baseline(Repo(), None, None, None)
    assert b4.source == "default" and b4.rt_ms == 50.0


def test_rt_baseline_clamp():
    from datagen.plan_pipeline import resolve_rt_baseline

    class Repo:
        def query_by_task(self, tid, limit):
            class S:
                p50_ms, p95_ms = 999999.0, 999999.0

            return [S()]

        def query_by_scene(self, *a):
            return []

    b = resolve_rt_baseline(Repo(), None, None, task_id=1)
    assert b.rt_ms == 5000.0, "clamp 上限 5000"
