"""T4.11 验收：多进程 Worker（实施方案 §7.2；协议 §7.5 语言无关分片协议）。

- Worker 只产数据不持 DB 连接（全程无连接对象）；
- 分片间无共享可变状态：随机源 seed.py 派生，跨 Worker/跨进程复现一致；
- 协议 JSON 原生：to_dict/from_dict 往返 + 版本校验显式失败；
- file/stream 双模式；多进程 + staging 检查点在父进程合并。
"""

from __future__ import annotations

import pytest

from ptde.generator.keys import IdRangeAllocator
from ptde.generator.partitioner import plan_shards
from ptde.generator.staging import ShardStatus, StagingArea
from ptde.generator.worker import (
    PROTOCOL_VERSION,
    FieldSpec,
    ShardResult,
    ShardTask,
    plan_row_factory,
    run_shard,
    run_shards,
    shard_tasks_from_plan,
)
from ptde.models.profile import DistributionFit

ROOT_SEED = 20261118


def _fields() -> tuple[FieldSpec, ...]:
    return (
        FieldSpec(name="id", type_name="BIGINT"),
        FieldSpec(name="amount", type_name="DECIMAL",
                  distribution=DistributionFit(type="uniform",
                                               params={"min": 0, "max": 10000})),
        FieldSpec(name="note", type_name="VARCHAR"),
    )


def _task(shard_id="orders-0000", rows=500, tmp=None, **kw) -> ShardTask:
    kw.setdefault("id_start", 1)
    kw.setdefault("id_end", rows)
    if tmp is not None and "path" not in kw:
        kw["path"] = str(tmp / f"{shard_id}.csv")
    return ShardTask(
        shard_id=shard_id, entity="orders", rows=rows,
        root_seed=ROOT_SEED, fields=_fields(), **kw,
    )


# ---------------------------------------------------------------- 协议序列化（§7.5）


def test_shard_task_protocol_roundtrip_json_native():
    task = _task(plan_fingerprint="fp-1")
    data = task.to_dict()
    # 协议字段全 JSON 原生（snake_case、无 Python 对象引用）
    assert data["protocol_version"] == PROTOCOL_VERSION == 1
    assert data["id_range"] == {"start": 1, "end": 500, "strategy": "planned_range"}
    assert data["output"]["mode"] == "file"
    assert data["fields"][1]["distribution"]["type"] == "uniform"
    restored = ShardTask.from_dict(data)
    assert restored == task


def test_shard_task_protocol_version_mismatch_explicit_failure():
    data = _task().to_dict()
    data["protocol_version"] = 99
    with pytest.raises(ValueError, match="协议版本不符"):
        ShardTask.from_dict(data)


# ---------------------------------------------------------------- 行工厂确定性


def test_plan_row_factory_deterministic_and_pk_sequential():
    task = _task(rows=100)
    rows1 = list(plan_row_factory(task))
    rows2 = list(plan_row_factory(task))
    assert rows1 == rows2  # 同任务两次逐行一致
    assert [r[0] for r in rows1] == list(range(1, 101))  # 主键区间顺序
    assert all(isinstance(r[1], int) and 0 <= r[1] <= 10000 for r in rows1)
    assert all(isinstance(r[2], str) and r[2] for r in rows1)  # VARCHAR 默认生成


def test_plan_row_factory_planned_range_missing_range_explicit_failure():
    task = ShardTask(shard_id="s", entity="e", rows=10, root_seed=ROOT_SEED,
                     strategy="planned_range", fields=_fields())
    with pytest.raises(ValueError, match="预分配区间"):
        list(plan_row_factory(task))


def test_plan_row_factory_rows_exceed_range_explicit_failure():
    task = _task(rows=100, id_start=1, id_end=50)
    with pytest.raises(ValueError, match="超出预分配区间"):
        list(plan_row_factory(task))


# ---------------------------------------------------------------- 单分片执行


def test_run_shard_file_mode_with_staging_checkpoint(tmp_path):
    staging = StagingArea(tmp_path / "staging", plan_fingerprint="fp-1")
    task = _task(rows=50, tmp=tmp_path)
    result = run_shard(task, staging=staging)
    assert result.status == "generated"
    assert result.generated_rows == 50
    assert result.checksum and len(result.checksum) == 64  # sha256 hex
    assert staging.records()[0].status == ShardStatus.GENERATED.value
    payload = staging.checkpoint()
    assert payload["shards"][0]["rows"] == 50

    # 结果可复核：文件行数与校验和一致
    from ptde.generator.file_format import ShardFileMeta, verify_shard_file

    verify_shard_file(result.path, ShardFileMeta(path=result.path, rows=50,
                                                 checksum=result.checksum))


def test_run_shard_stream_mode_in_process(tmp_path):
    task = _task(rows=30, mode="stream")
    result = run_shard(task)
    assert result.status == "streaming"
    rows = list(result.stream)
    assert [r[0] for r in rows] == list(range(1, 31))
    r = result.stream.result()
    assert r.completed and r.pushed_rows == 30


def test_run_shard_stream_with_staging_explicit_failure(tmp_path):
    staging = StagingArea(tmp_path / "staging")
    with pytest.raises(ValueError, match="不落 staging"):
        run_shard(_task(rows=1, mode="stream"), staging=staging)


def test_run_shard_unknown_mode_explicit_failure():
    with pytest.raises(ValueError, match="未知输出模式"):
        run_shard(_task(rows=1, mode="kafka"))


def test_run_shard_file_mode_missing_path_explicit_failure():
    with pytest.raises(ValueError, match="输出路径"):
        run_shard(_task(rows=1, path=None))


# ---------------------------------------------------------------- 多进程执行


def _tasks(tmp_path, rows=400, shards=4) -> list[ShardTask]:
    plan = plan_shards({"orders": rows}, rows_per_shard=rows // shards,
                       id_allocator=IdRangeAllocator())
    fields = {"orders": _fields()}
    return shard_tasks_from_plan(plan, fields, root_seed=ROOT_SEED,
                                 plan_fingerprint="fp-1",
                                 staging=StagingArea(tmp_path / "plan"))


def test_run_shards_multiprocess_reproducible_across_workers(tmp_path):
    """验收核心：跨 Worker/跨进程复现一致（同输入 → 同校验和）。"""
    tasks = _tasks(tmp_path)
    # 单进程（workers=1）基准
    single = StagingArea(tmp_path / "single")
    r1 = run_shards(tasks, workers=1, staging=single)
    # 4 进程：不同 Worker 执行不同分片
    multi = StagingArea(tmp_path / "multi")
    r4 = run_shards(tasks, workers=4, staging=multi)

    assert [x.checksum for x in r1] == [x.checksum for x in r4]
    assert sum(x.generated_rows for x in r1) == 400 == sum(
        x.generated_rows for x in r4)
    # 两次 staging 状态一致（检查点合并后）
    assert single.totals() == multi.totals()
    assert single.totals()["rows_generated"] == 400
    # 文件逐字节一致（跨进程逐行一致的强证据）
    for a, b in zip(r1, r4, strict=True):
        assert a.checksum == b.checksum and a.generated_rows == b.generated_rows


def test_run_shards_duplicate_shard_id_explicit_failure(tmp_path):
    t = _task(rows=1, tmp=tmp_path)
    with pytest.raises(ValueError, match="重复"):
        run_shards([t, t], workers=1)


def test_run_shards_stream_multiprocess_explicit_failure(tmp_path):
    task = _task(rows=1, mode="stream")
    with pytest.raises(ValueError, match="不支持 stream"):
        run_shards([task], workers=4)


def test_run_shards_lambda_factory_multiprocess_explicit_failure(tmp_path):
    with pytest.raises(ValueError, match="模块级函数"):
        run_shards([_task(rows=1, tmp=tmp_path)],
                   row_factory=lambda t: iter([]), workers=4)


def test_run_shards_invalid_workers():
    with pytest.raises(ValueError, match="workers 必须为正"):
        run_shards([], workers=0)


# ---------------------------------------------------------------- 计划装配


def test_shard_tasks_from_plan_wiring(tmp_path):
    tasks = _tasks(tmp_path, rows=1000, shards=4)
    assert [t.shard_id for t in tasks] == ["orders-0000", "orders-0001",
                                           "orders-0002", "orders-0003"]
    assert tasks[0].id_start == 1 and tasks[-1].id_end == 1000
    assert all(t.root_seed == ROOT_SEED for t in tasks)
    assert all(t.path and t.path.startswith(str(tmp_path)) for t in tasks)
    assert all([f.name for f in t.fields] == ["id", "amount", "note"]
               for t in tasks)
    # 技术债清理：实体行偏移按 shard_id 序累计（sequential 跨分片连续）
    assert [t.row_offset for t in tasks] == [0, 250, 500, 750]
    assert all(t.pk_column == "id" for t in tasks), "缺省主键列 = id（历史行为）"


def test_shard_tasks_from_plan_pk_by_entity(tmp_path):
    plan = plan_shards({"orders": 500}, rows_per_shard=250,
                       id_allocator=IdRangeAllocator())
    tasks = shard_tasks_from_plan(plan, {"orders": _fields()},
                                  root_seed=ROOT_SEED,
                                  pk_by_entity={"orders": "code"})
    assert all(t.pk_column == "code" for t in tasks)


def test_shard_result_roundtrip_dict():
    r = ShardResult(shard_id="s", entity="e", generated_rows=3, checksum="ab" * 32,
                    duration_ms=5, status="generated", path="p")
    assert ShardResult(**r.to_dict()) == r


# ---------------------------------------------------------------- 技术债清理行为


def test_sequential_continues_across_shards_via_row_offset():
    """>2M 多分片已知差异修复：sequential 列按实体全局行号连续推进。"""
    plan = plan_shards({"orders": 200}, rows_per_shard=100,
                       id_allocator=IdRangeAllocator())
    fields = (FieldSpec(name="id", type_name="BIGINT"),
              FieldSpec(name="code", type_name="VARCHAR",
                        distribution=DistributionFit(
                            type="sequential",
                            params={"seq_start": 1, "step": 1})))
    tasks = shard_tasks_from_plan(plan, {"orders": fields}, root_seed=ROOT_SEED)
    codes: list[str] = []
    for t in tasks:
        for row in plan_row_factory(t):
            codes.append(row[1])
    assert codes == [f"{i}" for i in range(1, 201)], \
        "多分片 sequential 跨分片连续（全局行偏移），不再分片内重启"


def test_non_id_pk_column_planned_range_takeover():
    """非 id 名主键 planned_range 泛化：pk_column 特判放宽。"""
    fields = (FieldSpec(name="code", type_name="BIGINT"),
              FieldSpec(name="amount", type_name="DECIMAL"))
    task = ShardTask(shard_id="orders-0000", entity="orders", rows=10,
                     id_start=101, id_end=110, root_seed=ROOT_SEED,
                     fields=fields, pk_column="code")
    rows = list(plan_row_factory(task))
    assert [r[0] for r in rows] == list(range(101, 111)), \
        "主键列 code 自预分配区间顺序生成（非 id 名接管）"


def test_pk_column_default_preserves_legacy_behavior():
    """缺省 pk_column="id"：非主键 id 命名外的列不受影响（历史行为冻结）。"""
    fields = (FieldSpec(name="id", type_name="BIGINT"),
              FieldSpec(name="code", type_name="BIGINT"))
    task = ShardTask(shard_id="s", entity="e", rows=5, id_start=1, id_end=5,
                     root_seed=ROOT_SEED, fields=fields)
    rows = list(plan_row_factory(task))
    assert [r[0] for r in rows] == [1, 2, 3, 4, 5]
    assert all(isinstance(r[1], int) and 0 <= r[1] <= 2**63 - 1 for r in rows), \
        "code 列走 BIGINT 缺省生成（不接管）"
