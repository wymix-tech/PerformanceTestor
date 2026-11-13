"""压测结果模型：逐字段对齐 StressTaskResult / Sample / StagePoint / StepStats。

序列化时输出 camelCase（by_alias=True），与 Java 侧 resultJson 的 JSON 结构
完全一致，便于 Java 侧直接导入（阶段 2 允许「先落工件、Java 导入」的路径）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """snake_case 字段 ↔ camelCase JSON 别名（Java 契约对齐）。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class Sample(CamelModel):
    """周期采样点（实时滚动图表）。"""

    sample_time: int = 0
    concurrency: int = 0
    qps: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    error_count: int = 0


class StagePoint(CamelModel):
    """分档采样点（容量评估曲线）。"""

    concurrency: int = 0
    qps: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    error_rate: float = 0.0
    total_requests: int = 0


class StepStats(CamelModel):
    """分步骤统计（多接口混合场景）。"""

    step_name: str = ""
    total_requests: int = 0
    error_count: int = 0
    error_rate: float = 0.0
    qps: float = 0.0
    avg_ms: float = 0.0
    max_ms: float = 0.0
    render_miss: int = 0


class StressTaskResult(CamelModel):
    """压测任务结果（对齐 com.perfserver.stress.StressTaskResult）。"""

    task_id: int = 0
    engine_type: str = ""
    target_url: str = ""
    concurrency: int = 0
    duration_sec: int = 0
    warmup_sec: int = 0
    ramp_up_sec: int = 0
    total_requests: int = 0
    error_count: int = 0
    error_rate: float = 0.0
    qps: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0
    avg_ms: float = 0.0
    samples: list[Sample] = Field(default_factory=list)
    stage_points: list[StagePoint] = Field(default_factory=list)
    step_stats: list[StepStats] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def to_java_json(self) -> dict:
        """输出 camelCase JSON（与 Java resultJson 结构一致）。"""
        return self.model_dump(by_alias=True)
