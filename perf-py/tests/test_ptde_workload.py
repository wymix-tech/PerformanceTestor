"""T3.5 验收：workload 收编对照（同输入同输出；plan_pipeline 本体保留不动，A4a 终态退役）。

对照基准：datagen/plan_pipeline.py 与其单测 tests/test_datagen_plan_pipeline.py
（语义逐条迁入本文件，legacy 与收编副本双跑对照）。
"""

from __future__ import annotations

import json

import pytest

from datagen import plan_pipeline as legacy
from datagen.model import Op, Predicate, SqlAccess
from ptde.planner import workload as wl

# ---------------------------------------------------------------- 对照：同输入同输出


def test_module_shape_parity():
    """收编副本含全部 6 个收编函数与配套 dataclass/常量；llm_enhance 不进副本。"""
    for name in ("estimate", "compose", "complete_parents", "edge_confidence",
                 "refresh_seq_starts", "resolve_rt_baseline", "parent_rows",
                 "StressParams", "TableDemand", "EndpointTableFact", "TableContext",
                 "PlanTable", "JoinRef"):
        assert hasattr(wl, name), name
    assert not hasattr(wl, "llm_enhance"), "LLM 增强保持 legacy 建议定位，不进确定性路径"


@pytest.mark.parametrize("params,demands,weights", [
    (legacy.StressParams(), [legacy.TableDemand("perf_user", False)], None),
    (legacy.StressParams(qps_limit=100.0), [legacy.TableDemand("t", True)], None),
    (legacy.StressParams(qps_limit=100.0),
     [legacy.TableDemand("t", True), legacy.TableDemand("t", True)], None),
    (legacy.StressParams(qps_limit=100.0),
     [legacy.TableDemand("a", True, endpoint_index=0),
      legacy.TableDemand("b", True, endpoint_index=1)], [1, 3]),
    (legacy.StressParams(qps_limit=100.0,
                         stages=[legacy.Stage(10, 60), legacy.Stage(10, 60)]),
     [legacy.TableDemand("t", True, page_size=10)], None),
    (legacy.StressParams(qps_limit=5.0, duration_sec=10),
     [legacy.TableDemand("t", True, page_size=100)], None),
])
def test_estimate_same_input_same_output(params, demands, weights):
    """同输入同输出：行数、告警、峰值 QPS、总请求逐项一致（含钳制缩减语义副本）。"""
    wl_params = wl.StressParams(
        concurrency=params.concurrency, duration_sec=params.duration_sec,
        qps_limit=params.qps_limit, assumed_rt_ms=params.assumed_rt_ms,
        stages=[wl.Stage(s.concurrency, s.duration_sec) for s in params.stages])
    wl_demands = [wl.TableDemand(d.table, d.write, d.page_size, d.endpoint_index)
                  for d in demands]
    a = legacy.estimate(params, demands, weights)
    b = wl.estimate(wl_params, wl_demands, weights)
    assert a.rows_by_table == b.rows_by_table
    assert a.warnings == b.warnings
    assert a.total_requests == b.total_requests
    assert a.peak_qps == b.peak_qps


def test_estimate_max_table_rows_parity():
    """12 表各 clamp 到 200000 → 超限按比例缩减并告警（副本语义一致）。"""
    demands = [legacy.TableDemand(f"t{i}", True, page_size=10) for i in range(12)]
    wl_demands = [wl.TableDemand(d.table, d.write, d.page_size) for d in demands]
    a = legacy.estimate(legacy.StressParams(qps_limit=100000.0), demands)
    b = wl.estimate(wl.StressParams(qps_limit=100000.0), wl_demands)
    assert a.rows_by_table == b.rows_by_table
    assert a.warnings == b.warnings
    assert any("已按比例缩减" in w for w in b.warnings)


@pytest.mark.parametrize("child", [1000, 5000, 500_000, 2_500_000])
def test_parent_rows_parity(child):
    assert legacy.parent_rows(child) == wl.parent_rows(child)


def test_compose_same_input_same_output():
    """规则合成对照：谓词/枚举建议/外键边/唯一列组合全量一致。"""
    def build(mod):
        fact = mod.EndpointTableFact("perf_order", SqlAccess.READ)
        fact.predicates = [Predicate(column="status", op=Op.EQ, values=["PAID"]),
                           Predicate(column="amount", op=Op.RANGE, min=10.0, max=99.0)]
        fact.join_refs = [mod.JoinRef("user_id", "perf_user", "id", 0.9)]
        ctx = mod.TableContext()
        ctx.join_refs = fact.join_refs
        ctx.unique_columns = ["order_no", "status"]
        ctx.enum_values = {"channel": ["APP", "WEB"]}
        return mod.compose([fact], {"perf_order": ctx})

    a = build(legacy)
    b = build(wl)
    assert a.fields_by_table == b.fields_by_table
    assert a.warnings == b.warnings


def test_compose_value_cap_parity():
    """值集超 64 上限：纯数值转 range、混合类型均匀采样（对照一致）。"""
    values = [float(i) for i in range(100)]
    mixed = [f"id{i}" for i in range(100)]

    def build(mod, vals):
        fact = mod.EndpointTableFact("perf_order", SqlAccess.READ)
        fact.predicates = [Predicate(column="c", op=Op.IN, values=vals)]
        return mod.compose([fact], {})

    a = build(legacy, values)
    b = build(wl, values)
    assert a.fields_by_table == b.fields_by_table and a.warnings == b.warnings
    a2 = build(legacy, mixed)
    b2 = build(wl, mixed)
    assert a2.fields_by_table == b2.fields_by_table and a2.warnings == b2.warnings


def test_complete_parents_parity():
    fields = json.dumps({"user_id": {"type": "ref", "table": "perf_user",
                                     "column": "id"}})
    a = legacy.complete_parents([legacy.PlanTable(table="perf_order", row_count=5000,
                                                  fields=fields, access="WRITE")])
    b = wl.complete_parents([wl.PlanTable(table="perf_order", row_count=5000,
                                          fields=fields, access="WRITE")])
    assert [(t.table, t.row_count, t.access, t.evidence) for t in a.tables] \
        == [(t.table, t.row_count, t.access, t.evidence) for t in b.tables]
    assert a.warnings == b.warnings


def test_edge_confidence_parity():
    fields_a = json.dumps({"b_id": {"type": "ref", "table": "b", "column": "id",
                                    "confidence": 0.6}})
    fields_b = json.dumps({"a_id": {"type": "refSkew", "table": "a", "column": "id",
                                    "confidence": 0.9}})
    ta = [legacy.PlanTable(table="a", row_count=100, fields=fields_a),
          legacy.PlanTable(table="b", row_count=100, fields=fields_b)]
    tb = [wl.PlanTable(table="a", row_count=100, fields=fields_a),
          wl.PlanTable(table="b", row_count=100, fields=fields_b)]
    assert legacy.edge_confidence(ta) == wl.edge_confidence(tb)
    assert wl.edge_confidence(tb) == {"a->b": 0.6, "b->a": 0.9}


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


def test_refresh_seq_starts_parity():
    fields = json.dumps({"order_no": {"type": "seq", "seqPrefix": "ON_"}})
    a = legacy.refresh_seq_starts(FakeConn([41]), "mysql://", "perf_order", fields)
    b = wl.refresh_seq_starts(FakeConn([41]), "mysql://", "perf_order", fields)
    assert a == b
    a2 = legacy.refresh_seq_starts(FakeConn([None]), "mysql://", "perf_order", fields)
    b2 = wl.refresh_seq_starts(FakeConn([None]), "mysql://", "perf_order", fields)
    assert a2 == b2
    assert any("已回退为 1" in w for w in b2[1])


class _Sample:
    def __init__(self, p50, p95):
        self.p50_ms, self.p95_ms = p50, p95


class _Task:
    def __init__(self, result_json):
        self.result_json = result_json


class FakeRepo:
    def __init__(self, samples=None, history=None):
        self._samples, self._history = samples or [], history or []

    def query_by_task(self, tid, limit):
        return self._samples

    def query_by_scene(self, app, url, status, limit):
        return self._history


@pytest.mark.parametrize("repo,app,url,task,expected", [
    (FakeRepo(samples=[_Sample(120.0, 300.0), _Sample(80.0, 0)]), None, None, 7,
     ("sample", 100.0)),
    (FakeRepo(history=[_Task(json.dumps({"avgMs": 250.0}))]), "app", "/x", None,
     ("history", 250.0)),
    (FakeRepo(history=[_Task(json.dumps({"stats": {"p95Ms": 480.0}}))]), "app", "/x",
     None, ("history", 480.0)),
    (FakeRepo(), None, None, None, ("default", 50.0)),
])
def test_resolve_rt_baseline_parity(repo, app, url, task, expected):
    a = legacy.resolve_rt_baseline(repo, app, url, task_id=task)
    b = wl.resolve_rt_baseline(repo, app, url, task_id=task)
    assert (a.source, a.rt_ms) == (b.source, b.rt_ms) == expected


def test_legacy_module_untouched():
    """plan_pipeline 本体保留（A4a 才退役）：legacy 侧符号不受收编影响。"""
    assert legacy.LLM_NO_KEY_REASON
    assert hasattr(legacy, "llm_enhance")
    assert legacy.MAX_TOTAL_ROWS == wl.MAX_TOTAL_ROWS
