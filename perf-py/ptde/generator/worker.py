"""多进程生成 Worker（R4 T4.11，实施方案 §7.2；协议 §7.5 语言无关分片协议）。

职责边界（验收口径，勿另起一套）：
- Worker **只产数据，不持数据库连接**（生成与装载解耦，装载在 R5 Adapter）；
- **分片间无共享可变状态**：每个分片的随机源由 seed.py 派生
  （root_seed + entity + shard_id + field → 独立 random.Random），
  分片内字段级 rng 逐行复用（B4.1 seed 语义），跨 Worker/跨进程复现一致；
- 协议 §7.5：ShardTask/ShardResult 均为 JSON 原生（snake_case 字段、
  无 Python 对象引用），未来可替换 Go/Rust worker；
- 默认行工厂 ``plan_row_factory``：主键自预分配区间顺序生成（planned_range），
  其余字段按 FieldSpec 分布经 ``samplers.sample`` 采样（与 B3 产出形态
  同 fit 同 rng 口径），无分布时按列类型默认生成（primitive 收编口径）。

输出双模式（与 T4.9/T4.10 共用格式定义与检查点协议）：
- ``file``：经 file_format 写 staging 分片文件（sha256 + 行数校验和）；
- ``stream``：经 BoundedRowStream 有界缓冲直传（仅进程内模式；
  多进程传流对象无语义——显式失败不静默降级）。

多进程 + staging：子进程只回传结果 dict，staging 状态迁移统一在父进程
合并（避免多进程并发写 manifest 的检查点竞争）。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any

from ptde.generator.file_format import (
    FORMAT_VERSION,
    ShardFileMeta,
    write_shard_file,
)
from ptde.generator.seed import derive_seed
from ptde.models.profile import DistributionFit

PROTOCOL_VERSION = 1  # 协议 §7.5：版本变更需 bump 并记录 ExecutionManifest
MODE_FILE = "file"
MODE_STREAM = "stream"

RowFactory = Callable[["ShardTask"], Iterator[list]]


@dataclass(frozen=True)
class FieldSpec:
    """分片任务字段规格（JSON 原生：name/type_name/distribution 三键）。"""

    name: str
    type_name: str | None = None
    distribution: DistributionFit | None = None

    def to_manifest(self) -> dict:
        return {
            "name": self.name,
            "type_name": self.type_name,
            "distribution": (
                {"type": self.distribution.type,
                 "params": self.distribution.params,
                 "fitted": self.distribution.fitted}
                if self.distribution is not None else None
            ),
        }

    @classmethod
    def from_manifest(cls, data: dict) -> FieldSpec:
        dist = data.get("distribution")
        return cls(
            name=data["name"],
            type_name=data.get("type_name"),
            distribution=DistributionFit(**dist) if dist else None,
        )


@dataclass(frozen=True)
class ShardTask:
    """分片任务（协议 §7.5 计划侧：版本化 JSON，语言无关）。"""

    shard_id: str
    entity: str
    rows: int
    id_start: int | None = None  # planned_range 语义（hash_bucket 为 None）
    id_end: int | None = None
    strategy: str = "planned_range"
    fields: tuple[FieldSpec, ...] = ()
    root_seed: int | None = None  # None = 显式不可复现路径（A3，不伪装）
    plan_fingerprint: str | None = None
    mode: str = MODE_FILE
    format: str = FORMAT_VERSION
    path: str | None = None  # file 模式输出路径（缺省由 staging 分配）

    def to_dict(self) -> dict:
        """协议序列化：全 JSON 原生值（跨语言 worker 可消费）。"""
        return {
            "protocol_version": PROTOCOL_VERSION,
            "shard_id": self.shard_id,
            "entity": self.entity,
            "rows": self.rows,
            "id_range": {
                "start": self.id_start, "end": self.id_end,
                "strategy": self.strategy,
            },
            "plan_fingerprint": self.plan_fingerprint,
            "fields": [f.to_manifest() for f in self.fields],
            "root_seed": self.root_seed,
            "output": {"mode": self.mode, "format": self.format, "path": self.path},
        }

    @classmethod
    def from_dict(cls, data: dict) -> ShardTask:
        if data.get("protocol_version") != PROTOCOL_VERSION:
            raise ValueError(
                f"分片协议版本不符：{data.get('protocol_version')} ≠ {PROTOCOL_VERSION}"
                "（协议变更须 bump partition_algorithm_version 并记录 ExecutionManifest）"
            )
        id_range = data.get("id_range") or {}
        output = data.get("output") or {}
        return cls(
            shard_id=data["shard_id"],
            entity=data["entity"],
            rows=int(data["rows"]),
            id_start=id_range.get("start"),
            id_end=id_range.get("end"),
            strategy=id_range.get("strategy", "planned_range"),
            fields=tuple(FieldSpec.from_manifest(d) for d in data.get("fields", [])),
            root_seed=data.get("root_seed"),
            plan_fingerprint=data.get("plan_fingerprint"),
            mode=output.get("mode", MODE_FILE),
            format=output.get("format", FORMAT_VERSION),
            path=output.get("path"),
        )


@dataclass
class ShardResult:
    """分片执行结果（协议 §7.5 结果侧：对齐 ShardSummary 语义）。"""

    shard_id: str
    entity: str
    generated_rows: int = 0
    checksum: str | None = None
    duration_ms: int = 0
    status: str = "pending"
    path: str | None = None
    stream: Any = None  # 仅 stream 模式进程内持有（不入协议序列化）

    def to_dict(self) -> dict:
        return {
            "shard_id": self.shard_id,
            "entity": self.entity,
            "generated_rows": self.generated_rows,
            "checksum": self.checksum,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "path": self.path,
        }


def plan_row_factory(task: ShardTask) -> Iterator[list]:
    """默认行工厂（模块级可 pickle，多进程安全）。

    确定性：主键自 [id_start, id_end] 顺序生成；hash 策略主键由
    derive_seed(root_seed, entity, shard_id, f"pk#{row_index}") 派生；
    每字段独立 rng（seed.py 派生）逐行复用——同任务跨进程逐行一致。
    root_seed=None → 显式不可复现路径（field_rng 发 UserWarning）。
    """
    if task.rows < 0:
        raise ValueError(f"分片行数必须非负，收到 {task.rows}（显式失败）")
    if task.strategy == "planned_range" and (
        task.id_start is None or task.id_end is None
    ):
        raise ValueError(
            f"分片 {task.shard_id} planned_range 缺少预分配区间"
            "（id_start/id_end，显式失败）"
        )
    if task.id_start is not None and task.id_end is not None \
            and task.id_end - task.id_start + 1 < task.rows:
        raise ValueError(
            f"分片 {task.shard_id} 行数 {task.rows} 超出预分配区间 "
            f"[{task.id_start}, {task.id_end}]（区间不足，显式失败）"
        )

    # 延迟导入：worker 顶层只依赖 ptde（收编边界保持显式）
    from ptde.generator.primitive import generate_default
    from ptde.generator.samplers import sample as sample_distribution
    from ptde.generator.seed import field_rng
    from ptde.models.wire import ColumnMeta

    rngs = {
        f.name: field_rng(task.root_seed, task.entity, task.shard_id, f.name)
        for f in task.fields
    }
    for row_index in range(task.rows):
        row: list[Any] = []
        for spec in task.fields:
            if spec.name == "id":
                if task.id_start is not None:
                    row.append(task.id_start + row_index)  # 区间顺序主键
                else:
                    row.append(derive_seed(task.root_seed or 0, task.entity,
                                           task.shard_id, f"pk#{row_index}"))
                continue
            rng = rngs[spec.name]
            if spec.distribution is not None:
                # row_index 上下文注入（B7 续批次 10）：sequential 等行号敏感
                # 分布按分片内行号推进（缺省行工厂此前恒传 None → 恒取起始值）
                row.append(sample_distribution(
                    spec.distribution, rng, {"row_index": row_index}))
            else:
                col = ColumnMeta(name=spec.name, type_name=spec.type_name)
                row.append(generate_default(col, rng))
        yield row


def run_shard(
    task: ShardTask,
    row_factory: RowFactory = plan_row_factory,
    staging: Any = None,
) -> ShardResult:
    """执行单分片：产数据 → 输出（file 写 staging / stream 有界缓冲直传）。

    staging（StagingArea）可选：传入时登记 pending → generated 并 checkpoint
    （检查点协议共用）。Worker 全程不持有数据库连接。
    """
    if task.mode not in (MODE_FILE, MODE_STREAM):
        raise ValueError(
            f"分片 {task.shard_id} 未知输出模式 {task.mode!r}"
            "（支持 file/stream，显式失败不静默降级）"
        )
    started = time.monotonic()
    result = ShardResult(shard_id=task.shard_id, entity=task.entity)

    if task.mode == MODE_STREAM:
        from ptde.generator.stream import BoundedRowStream

        if staging is not None:
            raise ValueError(
                "stream 模式不落 staging 文件（检查点走消费端回填，显式失败不混用）"
            )
        result.stream = BoundedRowStream(row_factory(task))
        result.status = "streaming"
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    path = task.path
    if path is None:
        raise ValueError(
            f"分片 {task.shard_id} file 模式缺少输出路径"
            "（staging 分配或 task.path，显式失败）"
        )
    if staging is not None:
        staging.register(task.entity, task.shard_id)
    meta = write_shard_file(path, [f.name for f in task.fields],
                            row_factory(task), delimiter=",")
    result.generated_rows = meta.rows
    result.checksum = meta.checksum
    result.path = meta.path
    result.status = "generated"
    result.duration_ms = int((time.monotonic() - started) * 1000)
    if staging is not None:
        staging.mark_generated(task.shard_id, meta)
        staging.checkpoint()
    return result


def run_shards(
    tasks: Sequence[ShardTask],
    row_factory: RowFactory = plan_row_factory,
    workers: int = 1,
    staging: Any = None,
) -> list[ShardResult]:
    """多进程分派分片（workers>1 走 ProcessPoolExecutor；跨 Worker 复现一致）。

    - 任务按 shard_id 排序分派（确定性；重复 shard_id 显式失败）；
    - Worker 只消费 ShardTask.to_dict()（JSON 原生），无共享可变状态；
    - stream 模式仅在 workers=1 时支持（跨进程传输流对象显式失败）；
    - 多进程 + staging：状态迁移在父进程合并（检查点无并发写竞争）。
    """
    if workers < 1:
        raise ValueError(f"workers 必须为正，收到 {workers}（显式失败）")
    ids = [t.shard_id for t in tasks]
    if len(ids) != len(set(ids)):
        dup = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"分片任务 ID 重复：{dup}（分片 ID 全局唯一，显式失败）")
    ordered = sorted(tasks, key=lambda t: t.shard_id)

    if workers == 1:
        return [run_shard(t, row_factory, staging) for t in ordered]

    streams = [t for t in ordered if t.mode == MODE_STREAM]
    if streams:
        raise ValueError(
            f"多进程模式不支持 stream 输出（分片 {streams[0].shard_id}）："
            "流对象不可跨进程传递——显式失败，请改用 file 模式"
        )
    factory_name = _factory_name(row_factory)  # 不可按名称还原 → 显式失败
    payload = [t.to_dict() for t in ordered]

    results: list[ShardResult] = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_run_shard_remote, task_dict, factory_name)
                   for task_dict in payload]
        for fut in futures:
            results.append(ShardResult(**fut.result()))
    if staging is not None:  # 父进程合并状态迁移（子进程不写检查点）
        for r in results:
            staging.register(r.entity, r.shard_id)
            staging.mark_generated(
                r.shard_id,
                ShardFileMeta(path=r.path, rows=r.generated_rows,
                              checksum=r.checksum),
            )
        staging.checkpoint()
    return results


def _factory_name(row_factory: RowFactory) -> str:
    """行工厂必须为模块级函数（可 pickle；闭包/lambda 显式失败）。"""
    module = getattr(row_factory, "__module__", None)
    name = getattr(row_factory, "__name__", None)
    if module is None or name is None or name == "<lambda>":
        raise ValueError(
            "row_factory 必须为模块级函数（多进程 pickle 传输；闭包/lambda 显式失败）"
        )
    import importlib

    if getattr(importlib.import_module(module), name, None) is not row_factory:
        raise ValueError(
            f"row_factory {module}.{name} 不可按名称还原"
            "（多进程传输要求模块级函数，显式失败）"
        )
    return f"{module}:{name}"


def _run_shard_remote(task_dict: dict, factory_name: str) -> dict:
    """子进程入口：JSON 原生任务 → 结果 dict（无 DB 连接、无共享状态）。"""
    import importlib

    module_name, func_name = factory_name.split(":")
    factory = getattr(importlib.import_module(module_name), func_name)
    return run_shard(ShardTask.from_dict(task_dict), factory).to_dict()


def shard_tasks_from_plan(
    sharding_plan: Any,
    fields_by_entity: Mapping[str, Sequence[FieldSpec]],
    root_seed: int | None,
    plan_fingerprint: str | None = None,
    staging: Any = None,
) -> list[ShardTask]:
    """ShardingPlan → ShardTask 列表（区间/字段/seed 装配，确定性排序）。"""
    tasks: list[ShardTask] = []
    for shard in sorted(sharding_plan.shards, key=lambda s: s.shard_id):
        path = (str(staging.shard_path(shard.entity, shard.shard_id))
                if staging is not None else None)
        tasks.append(
            ShardTask(
                shard_id=shard.shard_id,
                entity=shard.entity,
                rows=shard.rows or 0,
                id_start=shard.id_start,
                id_end=shard.id_end,
                strategy=shard.strategy,
                fields=tuple(fields_by_entity.get(shard.entity, ())),
                root_seed=root_seed,
                plan_fingerprint=plan_fingerprint,
                mode=MODE_FILE,
                path=path,
            )
        )
    return tasks
