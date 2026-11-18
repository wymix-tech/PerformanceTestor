"""T5.5 装载降级链测试（实施方案 §8.2/§8.3；总方案 §9.4/§10.3）。

覆盖：链序与降级留痕（门禁 4）、运行时失败不静默换通道（A3）、预算执行前
显式失败（§10.3）、批量 INSERT 收编档（DEFAULT_BATCH_SIZE=500 + 热行警告）、
官方替代档 secure_file_priv 语义、B4.2 遗留问题 1 接入点、探测→策略接线
（deferred_constraints 快照 → resolve_cycle 调用方 + STAGED_LOAD 执行原语）。

全部走鸭子类型假连接/测试桩（不连真库）；分片文件用
file_format.write_shard_file 真实落盘（csv_v1 契约收编）。
"""

from __future__ import annotations

import pytest

from ptde.adapters.base import (
    BulkLoadAdapter,
    BulkLoadError,
    BulkLoadResult,
    BulkLoadUnsupported,
)
from ptde.adapters.bulkload.fallback import (
    DEFAULT_BATCH_SIZE,
    EXTERNAL_LOADER_REGISTRY,
    BudgetExceededError,
    DbApiBatchInsertLoader,
    FallbackLoader,
    LoadBudget,
    MysqlServerInfileAdapter,
    default_chain_for_product,
    deferred_constraints_of,
    hot_row_batch_warning,
    plan_cycle_strategy,
    register_external_loader,
    staged_load_guard,
)
from ptde.adapters.probe import CAPABILITY_KEYS, AdapterCapability
from ptde.generator.cycle_policy import CycleFkEdge, CyclePolicyError, CycleStrategy
from ptde.generator.file_format import write_shard_file

# ============================================================ 测试桩与假连接


class StubAdapter(BulkLoadAdapter):
    """降级链测试桩：可注入 unsupported / load_error 行为。"""

    def __init__(self, strategy: str, fail: str | None = None) -> None:
        self.product = "stub"
        self.strategy = strategy
        self._fail = fail
        self.ensure_calls = 0
        self.load_calls = 0

    def ensure_capable(self, conn) -> list[str]:  # noqa: ARG002
        self.ensure_calls += 1
        if self._fail == "unsupported":
            raise BulkLoadUnsupported(f"{self.strategy} 不可用（测试桩原因）")
        return [f"{self.strategy} 可用"]

    def load_shard(self, conn, path, table, columns, rows_expected, *,
                   shard_id="", entity=""):  # noqa: ARG002
        self.load_calls += 1
        if self._fail == "load_error":
            raise BulkLoadError(f"{self.strategy} 装载失败（测试桩）")
        return BulkLoadResult(
            shard_id=shard_id, entity=entity, table=table,
            strategy=self.strategy, rows_expected=rows_expected,
            rows_loaded=rows_expected, rows_verified=True,
        )


@pytest.fixture(autouse=True)
def _clean_external_registry():
    EXTERNAL_LOADER_REGISTRY.clear()
    yield
    EXTERNAL_LOADER_REGISTRY.clear()


class BatchFakeCursor:
    def __init__(self, conn) -> None:
        self._conn = conn

    def executemany(self, sql: str, rows: list) -> None:
        self._conn.executed.append((sql, rows))

    def close(self) -> None:
        pass


class BatchFakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, list]] = []
        self.committed = 0

    def cursor(self) -> BatchFakeCursor:
        return BatchFakeCursor(self)

    def commit(self) -> None:
        self.committed += 1


class RouteCursor:
    """按 marker 包含匹配路由（B5 §4 第 4 条：防 marker 包含冲突）。"""

    def __init__(self, conn) -> None:
        self._conn = conn
        self._result: list[tuple] = []
        self.rowcount = 0

    def execute(self, sql: str, params: tuple = ()) -> None:  # noqa: ARG002
        self._conn.executed_sql.append(sql)
        self._result = []
        if sql.startswith("LOAD DATA"):
            self.rowcount = self._conn.load_rowcount
            return
        for marker, rows in self._conn.routes.items():
            if marker in sql:
                self._result = rows
                return

    def fetchall(self) -> list[tuple]:
        return self._result

    def fetchone(self) -> tuple | None:
        return self._result[0] if self._result else None

    def close(self) -> None:
        pass


class RouteConn:
    def __init__(self, routes: dict[str, list[tuple]], load_rowcount: int = 0) -> None:
        self.routes = routes
        self.load_rowcount = load_rowcount
        self.executed_sql: list[str] = []
        self.committed = 0

    def cursor(self) -> RouteCursor:
        return RouteCursor(self)

    def commit(self) -> None:
        self.committed += 1


def _make_shard(tmp_path, rows=None):
    path = tmp_path / "shard_0.csv"
    meta = write_shard_file(
        path, ["id", "name"],
        rows if rows is not None else [[str(i), f"name_{i}"] for i in range(5)],
    )
    return str(path), meta.rows


# ============================================================ 链序与降级留痕（门禁 4）


def test_default_chain_for_product_mysql_three_tiers() -> None:
    chain = default_chain_for_product("mysql")
    assert [a.strategy for a in chain] == [
        "load_data_local", "load_data_server", "dbapi_batch_insert",
    ]
    assert isinstance(chain[2], DbApiBatchInsertLoader)
    assert chain[2].batch_size == DEFAULT_BATCH_SIZE == 500  # 收编口径


def test_default_chain_for_product_postgres_two_tiers() -> None:
    chain = default_chain_for_product("postgres")
    assert [a.strategy for a in chain] == ["copy_from_stdin", "dbapi_batch_insert"]


def test_default_chain_unknown_product_fails() -> None:
    with pytest.raises(ValueError, match="无内置降级链"):
        default_chain_for_product("oracle")


def test_native_success_not_degraded_with_external_trace() -> None:
    native = StubAdapter("stub_native")
    loader = FallbackLoader("mysql", chain=[native, StubAdapter("stub_fallback")])
    outcome = loader.load_shard(None, "/tmp/s.csv", "t", ["id"], 10, shard_id="s0")
    assert outcome.strategy_used == "stub_native"
    assert outcome.degraded is False
    assert outcome.result.rows_verified
    # B4.2 遗留问题 1：接入点预留留痕（未实现如实记录）
    assert outcome.degradation_trace[0]["tier"] == "external_kv_loader"
    assert outcome.degradation_trace[0]["status"] == "not_registered"
    assert "B4.2 遗留问题 1" in outcome.degradation_trace[0]["detail"]


def test_native_unsupported_degrades_with_visible_reasons() -> None:
    native = StubAdapter("stub_native", fail="unsupported")
    fallback = StubAdapter("stub_fallback")
    loader = FallbackLoader("mysql", chain=[native, fallback])
    outcome = loader.load_shard(None, "/tmp/s.csv", "t", ["id"], 10, shard_id="s0")
    assert outcome.strategy_used == "stub_fallback"
    assert outcome.degraded is True
    assert native.load_calls == 0  # 不可用档未执行装载
    assert fallback.load_calls == 1
    assert any("stub_native 不可用" in t["detail"] for t in outcome.degradation_trace)
    assert any("装载降级告警" in w for w in outcome.warnings)
    # 降级原因随结果回传（门禁 4：降级有说明）
    assert any("装载降级告警" in n for n in outcome.result.precheck_notes)


def test_chain_exhausted_raises_unsupported_with_all_reasons() -> None:
    loader = FallbackLoader(
        "mysql",
        chain=[StubAdapter("a", fail="unsupported"), StubAdapter("b", fail="unsupported")],
    )
    with pytest.raises(BulkLoadUnsupported) as exc:
        loader.load_shard(None, "/tmp/s.csv", "t", ["id"], 10, shard_id="s0")
    assert "降级链穷尽" in str(exc.value)
    assert "a 不可用（测试桩原因）" in str(exc.value)
    assert "b 不可用（测试桩原因）" in str(exc.value)


def test_runtime_error_propagates_not_degraded() -> None:
    """A3：运行时失败（BulkLoadError）不静默换通道——原样抛出。"""
    native = StubAdapter("stub_native", fail="load_error")
    fallback = StubAdapter("stub_fallback")
    loader = FallbackLoader("mysql", chain=[native, fallback])
    with pytest.raises(BulkLoadError, match="stub_native 装载失败"):
        loader.load_shard(None, "/tmp/s.csv", "t", ["id"], 10)
    assert fallback.load_calls == 0


# ============================================================ B4.2 遗留问题 1 接入点


def test_external_loader_registered_consulted_first() -> None:
    external = StubAdapter("external_kv")
    register_external_loader("mysql", lambda: external)
    native = StubAdapter("stub_native")
    loader = FallbackLoader("mysql", chain=[native])
    outcome = loader.load_shard(None, "/tmp/s.csv", "t", ["id"], 10, shard_id="s0")
    assert outcome.strategy_used == "external_kv"
    assert outcome.degraded is True  # 非原生档即降级
    assert native.load_calls == 0


def test_external_loader_duplicate_requires_overwrite() -> None:
    register_external_loader("mysql", StubAdapter)
    with pytest.raises(ValueError, match="overwrite"):
        register_external_loader("mysql", StubAdapter)


def test_external_loader_unsupported_falls_through_to_native() -> None:
    external = StubAdapter("external_kv", fail="unsupported")
    register_external_loader("mysql", lambda: external)
    native = StubAdapter("stub_native")
    loader = FallbackLoader("mysql", chain=[native])
    outcome = loader.load_shard(None, "/tmp/s.csv", "t", ["id"], 10)
    assert outcome.strategy_used == "stub_native"
    assert any(t["tier"] == "external_kv_loader" and t["status"] == "unsupported"
               for t in outcome.degradation_trace)


# ============================================================ 预算（总方案 §10.3）


def test_budget_exceeded_fails_before_execution() -> None:
    budget = LoadBudget(budget_seconds=10, calibrated_rows_per_second=1.0,
                        source="pilot:t")
    with pytest.raises(BudgetExceededError, match="执行前明确失败"):
        budget.ensure_feasible(100)


def test_budget_without_calibration_returns_note_not_raise() -> None:
    """无校准吞吐不做通用假设（§9.5）：返回说明，不伪装判定。"""
    note = LoadBudget(budget_seconds=10).ensure_feasible(100)
    assert "Pilot Run 校准" in note
    assert LoadBudget().ensure_feasible(100)  # 未配置预算 → 说明


def test_budget_from_pilot_calibrates_throughput() -> None:
    class FakeReport:
        entity = "pilot_x"
        rows_loaded = 1000
        load_ms = 1000  # 1000 rows/s

    budget = LoadBudget.from_pilot(FakeReport(), budget_seconds=2)
    assert budget.calibrated_rows_per_second == 1000.0
    assert budget.source == "pilot:pilot_x"
    assert "预算内" in budget.ensure_feasible(500)
    with pytest.raises(BudgetExceededError):
        budget.ensure_feasible(3000)


def test_budget_from_pilot_dry_run_rejected() -> None:
    class DryReport:
        entity = "pilot_x"
        rows_loaded = None  # 干跑
        load_ms = 0

    with pytest.raises(ValueError, match="不可校准"):
        LoadBudget.from_pilot(DryReport(), budget_seconds=2)


def test_fallback_loader_budget_precheck_note_visible() -> None:
    loader = FallbackLoader(
        "mysql", chain=[StubAdapter("stub_native")],
        budget=LoadBudget(budget_seconds=10),
    )
    outcome = loader.load_shard(None, "/tmp/s.csv", "t", ["id"], 10)
    assert any("Pilot Run 校准" in w for w in outcome.warnings)


# ============================================================ 收编档：批量 INSERT


def test_batch_insert_loader_loads_with_batch_commits(tmp_path) -> None:
    path, rows = _make_shard(tmp_path)
    conn = BatchFakeConn()
    on_batches: list[tuple[int, int, int]] = []
    result = DbApiBatchInsertLoader(batch_size=2).load_shard(
        conn, path, "orders", ["id", "name"], rows,
        shard_id="s0", entity="orders", on_batch=lambda c, n, total: on_batches.append((c, n, total)),
    )
    assert result.strategy == "dbapi_batch_insert"
    assert result.rows_loaded == rows and result.rows_verified
    assert len(conn.executed) == 3  # 5 行 / batch=2 → 3 批
    assert conn.committed == 3  # 逐批 commit（事务边界可控）
    assert on_batches == [(1, 2, 2), (2, 2, 4), (3, 1, 5)]
    sql = conn.executed[0][0]
    assert sql == "INSERT INTO orders (id, name) VALUES (%s, %s)"


def test_hot_row_warning_semantics_preserved(tmp_path) -> None:
    """refSkew>50% 热行 batch 调优警告语义保留（executor.py 收编口径）。"""
    assert hot_row_batch_warning(0.6, 500) == hot_row_batch_warning(0.6, 500)
    warning = hot_row_batch_warning(0.6, 500)
    assert "refSkew=60%" in warning and "500→250" in warning
    assert "子表分区" in warning
    # 阈值边界：>50% 才告警（≤50% 返回空串）
    assert hot_row_batch_warning(0.5, 500) == ""

    path, rows = _make_shard(tmp_path)
    conn = BatchFakeConn()
    result = DbApiBatchInsertLoader(batch_size=DEFAULT_BATCH_SIZE).load_shard(
        conn, path, "orders", ["id", "name"], rows, ref_skew_ratio=0.6,
    )
    assert any("refSkew=60%" in n for n in result.precheck_notes)


def test_fallback_loader_passes_skew_to_batch_tier(tmp_path) -> None:
    path, rows = _make_shard(tmp_path)
    loader = FallbackLoader(
        "mysql",
        chain=[StubAdapter("stub_native", fail="unsupported"),
               DbApiBatchInsertLoader(DEFAULT_BATCH_SIZE)],
    )
    conn = BatchFakeConn()
    outcome = loader.load_shard(
        conn, path, "orders", ["id", "name"], rows, ref_skew_ratio=0.7,
    )
    assert outcome.strategy_used == "dbapi_batch_insert"
    assert any("refSkew=70%" in n for n in outcome.result.precheck_notes)


# ============================================================ 官方替代档（服务端 INFILE）


def test_server_infile_unsupported_when_secure_file_priv_null() -> None:
    conn = RouteConn({"SELECT @@secure_file_priv": [("NULL",)]})
    with pytest.raises(BulkLoadUnsupported, match="secure_file_priv"):
        MysqlServerInfileAdapter().ensure_capable(conn)


def test_server_infile_path_outside_whitelist_unsupported(tmp_path) -> None:
    path, rows = _make_shard(tmp_path)  # staging 在 tmp，不在服务端白名单目录
    conn = RouteConn({"SELECT @@secure_file_priv": [("/var/lib/mysql-files/",)]})
    with pytest.raises(BulkLoadUnsupported, match="白名单"):
        MysqlServerInfileAdapter().load_shard(
            conn, path, "orders", ["id", "name"], rows, shard_id="s0"
        )


def test_server_infile_uses_server_sql_inside_whitelist(tmp_path) -> None:
    secure_dir = tmp_path / "secure"
    secure_dir.mkdir()
    path, rows = _make_shard(secure_dir)
    conn = RouteConn({
        "SELECT @@secure_file_priv": [(str(secure_dir),)],
        "EVENT_OBJECT_TABLE": [],
        "information_schema.PARTITIONS": [],
    }, load_rowcount=rows)
    result = MysqlServerInfileAdapter().load_shard(
        conn, path, "orders", ["id", "name"], rows, shard_id="s0"
    )
    assert result.strategy == "load_data_server"
    assert result.rows_verified
    load_sql = next(s for s in conn.executed_sql if s.startswith("LOAD DATA"))
    assert load_sql.startswith("LOAD DATA INFILE")  # 无 LOCAL（服务端通道）
    assert "IGNORE 2 LINES" in load_sql  # csv_v1 契约复用（勿另起一套）


# ============================================================ 探测→策略接线（B5 §7 衔接项）


def _capability(deferred: bool | None) -> AdapterCapability:
    return AdapterCapability(
        product="postgres",
        capabilities={k: (deferred if k == "deferred_constraints" else None)
                      for k in CAPABILITY_KEYS},
    )


def test_deferred_constraints_of_extracts_flag() -> None:
    assert deferred_constraints_of(_capability(True)) is True
    assert deferred_constraints_of(_capability(None)) is None  # 未知不伪装
    assert deferred_constraints_of(_capability(True).to_snapshot()) is True
    assert deferred_constraints_of(None) is None
    assert deferred_constraints_of({}) is None


def test_plan_cycle_strategy_wires_capability_snapshot() -> None:
    edges = [CycleFkEdge("a", "a_b_id", False), CycleFkEdge("b", "b_a_id", False)]
    res = plan_cycle_strategy(["a", "b"], edges, _capability(True))
    assert res.strategy is CycleStrategy.DEFERRED_CONSTRAINT
    # 未知能力位不得视为 True → 无延迟约束路径 → 显式失败（CyclePolicyError）
    with pytest.raises(CyclePolicyError, match="延迟约束"):
        plan_cycle_strategy(["a", "b"], edges, _capability(None))
    # 完全无探测快照 → 同样显式失败
    with pytest.raises(CyclePolicyError):
        plan_cycle_strategy(["a", "b"], edges, None)


def test_plan_cycle_strategy_staged_load_needs_approval() -> None:
    edges = [CycleFkEdge("a", "a_b_id", False), CycleFkEdge("b", "b_a_id", False)]
    with pytest.raises(CyclePolicyError, match="未批准关闭约束"):
        plan_cycle_strategy(["a", "b"], edges, _capability(False))
    res = plan_cycle_strategy(
        ["a", "b"], edges, _capability(False), allow_constraint_disabled=True,
    )
    assert res.strategy is CycleStrategy.STAGED_LOAD


# ============================================================ STAGED_LOAD 执行原语


class FakeConstraintAdapter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def disable_foreign_key_checks(self, conn) -> None:  # noqa: ARG002
        self.calls.append("disable")

    def enable_foreign_key_checks(self, conn) -> None:  # noqa: ARG002
        self.calls.append("enable")


class FakeTransactionAdapter:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self, conn) -> None:  # noqa: ARG002
        self.commits += 1


def test_staged_load_guard_requires_explicit_approval() -> None:
    """默认保留全部约束：关闭 FK 校验须显式批准（§9.4/§19）。"""
    with pytest.raises(ValueError, match="显式批准"):
        with staged_load_guard(None, FakeConstraintAdapter(), FakeTransactionAdapter()):
            pass


def test_staged_load_guard_disables_restores_and_commits() -> None:
    constraint = FakeConstraintAdapter()
    transaction = FakeTransactionAdapter()
    executed: list[str] = []
    with staged_load_guard(None, constraint, transaction, approved=True) as conn:
        executed.append("stage-load")
    assert conn is None
    assert executed == ["stage-load"]
    assert constraint.calls == ["disable", "enable"]  # 退出时恢复
    assert transaction.commits == 1
