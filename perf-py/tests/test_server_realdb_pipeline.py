"""B7 续批次 7：MySQL 真库通道装配测试（FakeConn 协议级，不连真库）。

覆盖：
- DSN 解析（mysql:// / mysql+pymysql:// 显式校验，方言/形态非法显式失败）；
- A2 最小映射产出器（FieldSpec → MySQL DDL EntityMapping；未知类型显式失败）；
- build_realdb_pipeline 装配（product/链序/检查点仓/DSN 环境回退）；
- 真库通道协议端到端（FakeConn + DbCheckpointStore + 降级链 → READY；
  原生档不可用降级到批量 INSERT——降级链语义在协议级验证）；
- 装载段取消安全点（入口 + 实体步骤间；已装载实体经 DB 检查点保留）；
- manifest 能力快照如实记录不可用档（不崩溃、不伪装）。

真库端到端（原生 LOAD DATA LOCAL INFILE + 装载基准 + 双跑对照）由
``PTDE_REALDB_URL`` 环境门控，见 test_ptde_realdb_benchmark.py /
test_ptde_legacy_ptde_crosscheck.py。
"""

import re
import threading

import pytest
from sqlalchemy.orm import sessionmaker

from ptde.loader.checkpoint import DbCheckpointStore
from ptde.models.dataset import LayerKind
from server.dataset_service import DatasetCancelled, DatasetLifecycleService
from server.db import make_engine
from server.offline_pipeline import OfflinePipeline, OfflinePipelineError
from server.ptde_repo import PtdeDatasetRepository
from server.realdb_pipeline import (
    REALDB_URL_ENV,
    RealDbPipelineError,
    build_realdb_pipeline,
    entity_mappings_from_fields,
    mysql_conn_factory,
    parse_mysql_dsn,
)

# ---------------------------------------------------------------- 夹具

ROWS_SPEC = {
    "name": "订单数据集",
    "targets": {"rows": {"t_order": 100}},
    "reproducibility": {"seed": 42},
}

FIELDS = {
    "t_order": [
        {"name": "id", "type_name": "BIGINT"},
        {"name": "amount", "type_name": "DECIMAL"},
        {"name": "status", "type_name": "VARCHAR"},
    ]
}


class FakeCursor:
    """``%s`` paramstyle 假游标（MySQL paramstyle 口径，不需 _QmarkConn）。"""

    def __init__(self, conn) -> None:
        self._conn = conn
        self.rowcount = 0
        self._rows: list[tuple] = []

    def execute(self, sql, params=None) -> None:
        self._conn.executed.append(" ".join(sql.split()))
        s = sql.strip()
        self._rows = []
        if s.startswith("SELECT @@local_infile"):
            self._rows = [(0,)]  # 服务端 LOCAL INFILE OFF → 原生档显式不可用
        elif s.startswith("SELECT @@secure_file_priv"):
            self._rows = [(None,)]  # SERVER INFILE 无白名单 → 替代档不可用
        elif s.startswith("SELECT COUNT(*) FROM (SELECT"):
            # 关系层零孤儿核对（全量 LEFT JOIN 子查询形态）：按子表返回
            # 预置孤儿数（fk_orphans，缺省 0 = 零孤儿）
            m = re.search(r"FROM \(SELECT \w+ FROM (\w+)", s)
            child = m.group(1) if m else "?"
            self._rows = [(self._conn.fk_orphans.get(child, 0),)]
        elif s.startswith("SELECT COUNT(*) FROM"):
            table = s.rsplit(" ", 1)[1]
            self._rows = [(self._conn.row_counts.get(table, 0),)]

    def executemany(self, sql, batch) -> None:
        self._conn.executed.append(" ".join(sql.split()))
        s = sql.strip()
        self.rowcount = len(batch)
        if s.startswith("INSERT INTO"):
            table = s.split()[2]
            self._conn.row_counts[table] = \
                self._conn.row_counts.get(table, 0) + len(batch)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self) -> None:
        pass


class FakeConn:
    """MySQL paramstyle 假连接（真库通道协议验证；行数记账供 count 层核对）。"""

    def __init__(self) -> None:
        self.row_counts: dict[str, int] = {}
        self.fk_orphans: dict[str, int] = {}  # 子表 → 预置孤儿数（关系层协议）
        self.executed: list[str] = []
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass


def _repo() -> PtdeDatasetRepository:
    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    import server.ptde_repo  # noqa: F401  注册模型
    from server.db import Base

    Base.metadata.create_all(factory.kw["bind"])
    return PtdeDatasetRepository(factory)


def _conn_holder() -> tuple:
    """共享单连接持有器（同一 FakeConn 跨段复用，行数记账可核对）。"""
    holder = {"conn": FakeConn()}
    return holder, (lambda: holder["conn"])


def _make_service(tmp_path, *, conn_factory=None, rows_per_shard: int = 40):
    repo = _repo()
    holder, factory = _conn_holder()
    pipeline = build_realdb_pipeline(
        repo, None, FIELDS,
        conn_factory=conn_factory or factory,
        staging_base=tmp_path / "staging", rows_per_shard=rows_per_shard)
    svc = DatasetLifecycleService(
        repo, pipeline=pipeline,
        degradable_layers=frozenset({LayerKind.DISTRIBUTION, LayerKind.DB_READY}))
    svc.register_definition("orders", "1", "订单数据集", ROWS_SPEC,
                            profile_fingerprint="prof-fp-1")
    inst = svc.create_instance("orders", "1", environment_id="dev-mysql")
    return svc, inst, holder["conn"]


def _wait_done(svc, instance_id: str, *, timeout: float = 10.0) -> dict:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not svc.task_status(instance_id)["running"]:
            return svc.get_instance(instance_id)
        time.sleep(0.02)
    raise AssertionError("异步任务超时未完成")


# ---------------------------------------------------------------- DSN 解析


def test_parse_mysql_dsn_accepts_mysql_and_pymysql_schemes():
    p = parse_mysql_dsn("mysql://u:p@db.host:3307/perf")
    assert p == {"host": "db.host", "port": 3307, "user": "u",
                 "password": "p", "database": "perf"}
    p2 = parse_mysql_dsn("mysql+pymysql://u:p@db.host/perf")
    assert p2["port"] == 3306 and p2["database"] == "perf"  # 缺省端口


def test_parse_mysql_dsn_rejects_other_dialects_and_incomplete():
    with pytest.raises(RealDbPipelineError, match="方言非法"):
        parse_mysql_dsn("postgres://u:p@h/db")
    with pytest.raises(RealDbPipelineError, match="缺少 host 或 database"):
        parse_mysql_dsn("mysql://u:p@db.host")
    with pytest.raises(RealDbPipelineError, match="形态非法"):
        parse_mysql_dsn("not-a-dsn")


def test_mysql_conn_factory_rejects_missing_dsn(monkeypatch):
    monkeypatch.delenv(REALDB_URL_ENV, raising=False)
    with pytest.raises(RealDbPipelineError):
        mysql_conn_factory(None)


# ---------------------------------------------------------------- A2 映射产出器


def test_entity_mappings_from_fields_generates_mysql_ddl():
    mappings = entity_mappings_from_fields(FIELDS)
    m = mappings["t_order"]
    assert m.columns == ["id", "amount", "status"]
    assert "id BIGINT NOT NULL PRIMARY KEY" in m.ddl
    assert "amount DECIMAL(18,2)" in m.ddl
    assert "status VARCHAR(255)" in m.ddl
    assert m.ddl.strip().startswith("CREATE TABLE IF NOT EXISTS t_order")
    assert "ENGINE=InnoDB" in m.ddl


def test_entity_mappings_unknown_type_fails_explicitly():
    with pytest.raises(RealDbPipelineError, match="无 MySQL 类型映射"):
        entity_mappings_from_fields({"t_order": [{"name": "id", "type_name": "JSONB"}]})


def test_entity_mappings_empty_fields_fails():
    with pytest.raises(RealDbPipelineError, match="字段规格为空"):
        entity_mappings_from_fields({"t_order": []})


# ---------------------------------------------------------------- 装配


def test_build_realdb_pipeline_requires_dsn(monkeypatch, tmp_path):
    monkeypatch.delenv(REALDB_URL_ENV, raising=False)
    repo = _repo()
    with pytest.raises(RealDbPipelineError):
        build_realdb_pipeline(repo, None, FIELDS, staging_base=tmp_path / "s")


def test_build_realdb_pipeline_assembles_mysql_channel(monkeypatch, tmp_path):
    monkeypatch.setenv(REALDB_URL_ENV, "mysql://u:p@db.host:3306/perf")
    repo = _repo()
    pipeline = build_realdb_pipeline(
        repo, None, FIELDS, batch_size=128, staging_base=tmp_path / "s")
    assert pipeline._product == "mysql"
    # §10.3 链序：原生 LOCAL → SERVER INFILE → 批量 INSERT（校准批次）
    assert [a.strategy for a in pipeline._loader_chain] == [
        "load_data_local", "load_data_server", "dbapi_batch_insert"]
    assert pipeline._loader_chain[2].batch_size == 128
    store = pipeline._store_factory(FakeConn())
    assert isinstance(store, DbCheckpointStore)  # 检查点仓 MySQL 方言实现


# ---------------------------------------------------------------- 真库通道协议端到端


def test_realdb_channel_protocol_reaches_ready_with_degradation(tmp_path):
    """FakeConn（原生档不可用）→ 降级链逐档探测 → 批量 INSERT 档装载 → READY。

    降级语义（§10.3）协议级验证：ensure_capable 显式拒绝（不伪装可用），
    留痕降下一档，装载行数以分片行数为权威并显式核对。
    """
    svc, inst, conn = _make_service(tmp_path)
    accepted = svc.build(inst["id"])
    assert accepted["accepted"] is True
    final = _wait_done(svc, inst["id"])
    assert final["status"] == "READY"
    assert final["validationSummary"]["ready"] is True
    # 目标库实际行数 = 目标行数（count 层全量核对通过的物理证据）
    assert conn.row_counts["t_order"] == 100
    # 检查点仓落 MySQL 方言表（DbCheckpointStore 建表语句已执行）
    assert any("ptde_shard_checkpoint" in s for s in conn.executed)
    # manifest 能力快照：降级链全链在册
    summary = svc.instance_manifest(inst["id"])
    assert summary["adapterCapabilitySnapshot"]["chain"] == [
        "load_data_local", "load_data_server", "dbapi_batch_insert"]


def test_realdb_channel_manifest_records_unsupported_native_tier(tmp_path):
    """原生档不可用 → 能力快照如实记录 unsupported 原因（不崩溃、不伪装）。"""
    svc, inst, _ = _make_service(tmp_path)
    svc.build(inst["id"])
    _wait_done(svc, inst["id"])
    repo = svc._repo
    rows = repo.manifests_for_instance(int(inst["id"]))
    manifest = repo.manifest_to_model(rows[-1])
    caps = manifest.adapter_capability_snapshot["capabilities"]
    assert any("load_data_local: unsupported" in c for c in caps)
    assert any("批量 INSERT" in c for c in caps)  # 降级档能力说明在册


# ---------------------------------------------------------------- 装载段取消安全点


def test_realdb_channel_load_cancel_at_entry(tmp_path):
    repo = _repo()
    holder, factory = _conn_holder()
    pipeline = build_realdb_pipeline(
        repo, None, FIELDS, conn_factory=factory,
        staging_base=tmp_path / "staging")
    event = threading.Event()
    event.set()
    with pytest.raises(DatasetCancelled, match="load 入口"):
        pipeline.load({"instance": {"id": "1"}, "cancel_event": event})


def test_realdb_channel_load_cancel_between_entities_keeps_loaded_rows(tmp_path):
    """实体步骤间安全点：已装载实体保留（DB 行可核查），余下实体不装载。"""
    repo = _repo()
    holder, factory = _conn_holder()
    spec = {"name": "双表数据集",
            "targets": {"rows": {"t_item": 40, "t_order": 40}},
            "reproducibility": {"seed": 1}}
    fields = {
        "t_item": [{"name": "id", "type_name": "BIGINT"}],
        "t_order": [{"name": "id", "type_name": "BIGINT"}],
    }
    pipeline = build_realdb_pipeline(
        repo, None, fields, conn_factory=factory,
        staging_base=tmp_path / "staging", rows_per_shard=20)
    DatasetLifecycleService(repo).register_definition(
        "two", "1", "双表数据集", spec, profile_fingerprint="prof-fp-1")
    pipeline.generate({"instance": {"id": "1", "definition_id": "two@1"},
                       "params": {}})
    # is_set() 第 3 次调用起置位：load 入口(1) → 实体 t_item 前(2) →
    # t_order 前(3) → 取消
    event = _CountingEvent(trigger_after=3)
    with pytest.raises(DatasetCancelled, match="装载步骤间"):
        pipeline.load({"instance": {"id": "1"}, "cancel_event": event})
    conn = holder["conn"]
    assert conn.row_counts["t_item"] == 40  # 已装载实体保留
    assert conn.row_counts.get("t_order", 0) == 0  # 余下实体未装载


class _CountingEvent(threading.Event):
    """第 n 次 is_set() 起返回 True（模拟实体步骤间收到取消信号）。"""

    def __init__(self, trigger_after: int):
        super().__init__()
        self._calls = 0
        self._trigger_after = trigger_after

    def is_set(self) -> bool:
        self._calls += 1
        if self._calls >= self._trigger_after:
            return True
        return super().is_set()


# ---------------------------------------------------------------- 关系映射接线（B7 续批次 8）


REL_FIELDS = {
    "t_item": [{"name": "id", "type_name": "BIGINT"}],
    "t_order": [
        {"name": "id", "type_name": "BIGINT"},
        {"name": "user_id", "type_name": "BIGINT"},
    ],
}

RELATIONS = [{
    "child_table": "t_order", "child_column": "user_id",
    "parent_table": "t_item", "parent_column": "id",
    "relation_kind": "db_fk",
}]


def _rel_pipeline(tmp_path, *, holder=None):
    repo = _repo()
    if holder is None:
        holder, factory = _conn_holder()
    else:
        factory = lambda: holder["conn"]  # noqa: E731  测试注入
    spec = {"name": "关系数据集",
            "targets": {"rows": {"t_item": 40, "t_order": 40}},
            "reproducibility": {"seed": 1}}
    pipeline = build_realdb_pipeline(
        repo, None, REL_FIELDS, conn_factory=factory, relations=RELATIONS,
        staging_base=tmp_path / "staging", rows_per_shard=40)
    DatasetLifecycleService(repo).register_definition(
        "rel", "1", "关系数据集", spec, profile_fingerprint="prof-fp-1")
    ctx = {"instance": {"id": "1", "definition_id": "rel@1"}, "params": {}}
    return pipeline, ctx, holder


def _relationship_layer(report: dict) -> dict:
    return next(
        ly for ly in report["layers"] if ly["layer"] == "relationship")


def test_realdb_channel_relations_wire_fk_and_verify_layer(tmp_path):
    """关系接线端到端：生成侧外键列按父实体主键区间注入采样，
    verify 关系层实映射全量零孤儿核对（DB_FK，PASS + metrics 留痕）。"""
    pipeline, ctx, _ = _rel_pipeline(tmp_path)
    pipeline.generate(ctx)
    pipeline.load({"instance": {"id": "1"}})
    # 生成侧：子表外键列无显式分布 → 父实体 planned_range 区间 [1, 40]
    tasks = pipeline._runtime[1]["tasks"]
    order = next(t for t in tasks if t.entity == "t_order")
    fk = next(f for f in order.fields if f.name == "user_id")
    assert fk.distribution is not None
    assert fk.distribution.type == "uniform"
    assert fk.distribution.params == {"min": 1, "max": 40}
    # verify 关系层：实映射（此前显式空集）
    report = pipeline.verify(ctx)["report"]
    rel = _relationship_layer(report)
    assert rel["status"] == "PASS"
    assert rel["metrics"]["relations_checked"] == 1
    assert rel["metrics"]["orphan_free_relations"] == 1
    assert any("t_order.user_id" in c["name"] for c in rel["checks"])


def test_realdb_channel_load_order_parent_before_child(tmp_path):
    """依赖序装载回归：子表字母序在前（perf_order/perf_user 场景）时，
    装载仍须父先于子——子表先装载会在 FK 校验处 1452 失败
    （此前 OfflinePipeline.load 按 sorted 字母序逐实体装载的缺陷）。"""

    class _FkEnforcingCursor(FakeCursor):
        """子表 INSERT 时父表行数不足 → 模拟 MySQL 1452（FK 即时校验）。"""

        def executemany(self, sql, batch) -> None:
            s = sql.strip()
            if s.startswith("INSERT INTO"):
                table = s.split()[2]
                req = self._conn.fk_requires.get(table)
                if req:
                    parent, need = req
                    if self._conn.row_counts.get(parent, 0) < need:
                        raise AssertionError(
                            f"(1452, 'Cannot add or update a child row: a foreign "
                            f"key constraint fails (`{table}`)）——父表 {parent} "
                            "尚未装载")
            super().executemany(sql, batch)

    class _FkEnforcingConn(FakeConn):
        """FK 即时校验假连接（child → parent 装载序回归测试）。"""

        def __init__(self) -> None:
            super().__init__()
            self.fk_requires: dict[str, tuple[str, int]] = {}

        def cursor(self):
            cur = _FkEnforcingCursor(self)
            cur._conn = self
            return cur

    conn = _FkEnforcingConn()
    conn.fk_requires["t_order"] = ("t_item", 40)
    holder = {"conn": conn}
    pipeline, ctx, _ = _rel_pipeline(tmp_path, holder=holder)
    pipeline.generate(ctx)
    load = pipeline.load({"instance": {"id": "1"}})
    assert load["loaded_rows"] == 80  # 父 40 + 子 40 全量装载
    # 装载顺序留痕：t_item（父）的 INSERT 先于 t_order（子）
    first_insert: dict[str, int] = {}
    for s in conn.executed:
        if s.startswith("INSERT INTO"):
            first_insert.setdefault(s.split()[2], len(first_insert))
    assert first_insert["t_item"] < first_insert["t_order"]


def test_realdb_channel_relations_orphan_fail_marks_layer(tmp_path):
    """孤儿违规（DB_FK 声明关系）→ 关系层 FAIL（零孤儿要求，不降级放行）。"""
    holder = {"conn": FakeConn()}
    holder["conn"].fk_orphans["t_order"] = 3
    pipeline, ctx, _ = _rel_pipeline(tmp_path, holder=holder)
    pipeline.generate(ctx)
    pipeline.load({"instance": {"id": "1"}})
    report = pipeline.verify(ctx)["report"]
    rel = _relationship_layer(report)
    assert rel["status"] == "FAIL"
    assert rel["metrics"]["relations_checked"] == 1
    assert rel["metrics"]["orphan_free_relations"] == 0
    assert "3 条孤儿" in rel["checks"][0]["detail"]


def test_relations_without_wiring_keeps_empty_layer(tmp_path):
    """缺省 relations=None → 关系层空集退回既有口径（零改动兼容确认）。"""
    repo = _repo()
    holder, factory = _conn_holder()
    pipeline = build_realdb_pipeline(
        repo, None, FIELDS, conn_factory=factory,
        staging_base=tmp_path / "staging")
    DatasetLifecycleService(repo).register_definition(
        "orders", "1", "订单数据集", ROWS_SPEC, profile_fingerprint="prof-fp-1")
    ctx = {"instance": {"id": "1", "definition_id": "orders@1"}, "params": {}}
    pipeline.generate(ctx)
    pipeline.load({"instance": {"id": "1"}})
    rel = _relationship_layer(pipeline.verify(ctx)["report"])
    assert rel["status"] == "PASS"
    assert rel["metrics"]["relations_checked"] == 0


def test_relations_explicit_failure_cases():
    """关系规格校验显式失败：未映射实体 / 列缺失 / 子列为 id / 四元组不完整。"""
    repo = _repo()
    base = {
        "t_item": [{"name": "id", "type_name": "BIGINT"}],
        "t_order": [
            {"name": "id", "type_name": "BIGINT"},
            {"name": "user_id", "type_name": "BIGINT"},
        ],
    }
    specs = [
        ("未映射实体", [{"child_table": "t_order", "child_column": "user_id",
                        "parent_table": "t_ghost", "parent_column": "id"}]),
        ("不在实体映射字段中", [{"child_table": "t_order",
                               "child_column": "ghost_col",
                               "parent_table": "t_item", "parent_column": "id"}]),
        ("主键区间语义列", [{"child_table": "t_order", "child_column": "id",
                          "parent_table": "t_item", "parent_column": "id"}]),
        ("四元组不完整", [{"child_table": "t_order", "child_column": "user_id",
                         "parent_table": "t_item", "parent_column": ""}]),
    ]
    for match, relations in specs:
        with pytest.raises(OfflinePipelineError, match=match):
            OfflinePipeline(repo, FakeConn, entity_mappings_from_fields(base),
                            relations=relations)


def test_relations_explicit_distribution_not_overridden(tmp_path):
    """子表外键列已有显式分布 → 不静默覆盖（调用方口径优先）。"""
    repo = _repo()
    holder, factory = _conn_holder()
    fields = {
        "t_item": [{"name": "id", "type_name": "BIGINT"}],
        "t_order": [
            {"name": "id", "type_name": "BIGINT"},
            {"name": "user_id", "type_name": "BIGINT",
             "distribution": {"type": "uniform", "params": {"min": 1, "max": 20}}},
        ],
    }
    spec = {"name": "关系数据集",
            "targets": {"rows": {"t_item": 40, "t_order": 40}},
            "reproducibility": {"seed": 1}}
    pipeline = build_realdb_pipeline(
        repo, None, fields, conn_factory=factory, relations=RELATIONS,
        staging_base=tmp_path / "staging", rows_per_shard=40)
    DatasetLifecycleService(repo).register_definition(
        "rel", "1", "关系数据集", spec, profile_fingerprint="prof-fp-1")
    pipeline.generate({"instance": {"id": "1", "definition_id": "rel@1"},
                       "params": {}})
    tasks = pipeline._runtime[1]["tasks"]
    order = next(t for t in tasks if t.entity == "t_order")
    fk = next(f for f in order.fields if f.name == "user_id")
    assert fk.distribution.params == {"min": 1, "max": 20}  # 调用方显式分布保留


# ---------------------------------------------------------------- 规则语义对齐（B7 续批次 9：ref 分布补全 + 故意孤儿门禁）

ORPHAN_FIELDS = {
    "t_item": [{"name": "id", "type_name": "BIGINT"}],
    "t_order": [
        {"name": "id", "type_name": "BIGINT"},
        {"name": "user_id", "type_name": "BIGINT",
         "distribution": {"type": "ref", "params": {"orphan_rate": 0.05}}},
    ],
}

LOGICAL_RELATIONS = [{
    "child_table": "t_order", "child_column": "user_id",
    "parent_table": "t_item", "parent_column": "id",
    "relation_kind": "logical",
}]


def _register_and_generate(tmp_path, *, fields, relations, holder):
    repo = _repo()
    factory = lambda: holder["conn"]  # noqa: E731  测试注入
    spec = {"name": "关系数据集",
            "targets": {"rows": {"t_item": 40, "t_order": 40}},
            "reproducibility": {"seed": 1}}
    pipeline = build_realdb_pipeline(
        repo, None, fields, conn_factory=factory, relations=relations,
        staging_base=tmp_path / "staging", rows_per_shard=40)
    DatasetLifecycleService(repo).register_definition(
        "rel", "1", "关系数据集", spec, profile_fingerprint="prof-fp-1")
    pipeline.generate({"instance": {"id": "1", "definition_id": "rel@1"},
                       "params": {}})
    return pipeline


def test_ref_fk_distribution_gets_parent_domain_injected(tmp_path):
    """显式 ref 分布缺父主键区间 → 管线补全 min/max（热行参数保留，非覆盖）+
    热行告警（HOT_ROW_SHARE_WARN 同口径）。"""
    fields = {
        "t_item": [{"name": "id", "type_name": "BIGINT"}],
        "t_order": [
            {"name": "id", "type_name": "BIGINT"},
            {"name": "user_id", "type_name": "BIGINT",
             "distribution": {"type": "ref",
                              "params": {"hot_percent": 20, "hot_share": 80}}},
        ],
    }
    pipeline = _register_and_generate(
        tmp_path, fields=fields, relations=RELATIONS, holder=_conn_holder()[0])
    order = next(t for t in pipeline._runtime[1]["tasks"]
                 if t.entity == "t_order")
    fk = next(f for f in order.fields if f.name == "user_id")
    assert fk.distribution.type == "ref"
    assert fk.distribution.params == {"hot_percent": 20, "hot_share": 80,
                                      "min": 1, "max": 40}


def test_ref_fk_distribution_partial_domain_fails_explicitly(tmp_path):
    """ref 分布父区间参数单边缺（仅 min 或仅 max）→ 显式失败（不静默补全）。"""
    fields = {
        "t_item": [{"name": "id", "type_name": "BIGINT"}],
        "t_order": [
            {"name": "id", "type_name": "BIGINT"},
            {"name": "user_id", "type_name": "BIGINT",
             "distribution": {"type": "ref", "params": {"min": 1}}},
        ],
    }
    holder, _ = _conn_holder()
    with pytest.raises(OfflinePipelineError, match="min/max 须同缺或同给"):
        _register_and_generate(tmp_path, fields=fields, relations=RELATIONS,
                               holder=holder)


def test_declared_fk_with_deliberate_orphans_fails_at_construction(tmp_path):
    """db_fk 声明关系 × 故意孤儿（ref orphan_rate>0）→ 构造期显式失败
    （零孤儿硬核对必 FAIL，不伪装通过）。"""
    holder, _ = _conn_holder()
    with pytest.raises(OfflinePipelineError, match="故意孤儿"):
        _register_and_generate(tmp_path, fields=ORPHAN_FIELDS,
                               relations=RELATIONS, holder=holder)


def test_logical_relation_allows_deliberate_orphans_and_verify_warns(tmp_path):
    """logical 关系 × 故意孤儿 → 构造通过；verify 孤儿如实 WARN（不 FAIL）。"""
    holder = {"conn": FakeConn()}
    holder["conn"].fk_orphans["t_order"] = 3
    pipeline = _register_and_generate(tmp_path, fields=ORPHAN_FIELDS,
                                      relations=LOGICAL_RELATIONS,
                                      holder=holder)
    # 生成侧：ref 分布补全父区间（故意孤儿参数保留）
    order = next(t for t in pipeline._runtime[1]["tasks"]
                 if t.entity == "t_order")
    fk = next(f for f in order.fields if f.name == "user_id")
    assert fk.distribution.params == {"orphan_rate": 0.05, "min": 1, "max": 40}
    pipeline.load({"instance": {"id": "1"}})
    report = pipeline.verify({"instance": {"id": "1", "definition_id": "rel@1"},
                              "params": {}})["report"]
    rel = _relationship_layer(report)
    assert rel["status"] == "WARN"
    assert rel["metrics"]["relations_checked"] == 1
    assert "3 条孤儿" in rel["checks"][0]["detail"]
