"""场景即代码：Scenario 数据模型（pydantic v2）。

字段语义逐项对齐 perf-server 的 StressTaskRequest
（perf-server/src/main/java/com/perfserver/stress/StressTaskRequest.java）：

- 单接口模式：request（url/method/headers/body/assertions）
- 分档模式：stages 或 stage_concurrencies（容量评估）
- 混合场景模式：steps（ScenarioStep，带权重/变量提取/断言）
- 负载参数：load（concurrency/duration/warmup/ramp_up/qps_limit/sample_interval）

YAML 中使用 snake_case，发给平台 API 时由 to_request_payload() 转为 camelCase。
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ASSERTION_SOURCES = {"status", "body", "header", "json"}
ASSERTION_OPS = {"eq", "ne", "contains", "notContains", "gt", "lt", "regex"}
EXTRACT_SOURCES = {"body", "header"}
HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
ENGINE_TYPES = {"http", "jmeter"}


class StrictModel(BaseModel):
    """拒绝未知字段：场景文件里的拼写错误应当立刻暴露，而不是被静默忽略。"""

    model_config = ConfigDict(extra="forbid")


class AssertionSpec(StrictModel):
    """对齐 StressTaskRequest.Assertion。"""

    source: Literal["status", "body", "header", "json"] = "status"
    key: str | None = None
    op: Literal["eq", "ne", "contains", "notContains", "gt", "lt", "regex"] = "eq"
    expected: str = ""

    @model_validator(mode="after")
    def _check_key(self) -> AssertionSpec:
        if self.source in {"header", "json"} and not (self.key or "").strip():
            raise ValueError(f"断言 source={self.source} 时必须提供 key")
        return self

    def to_payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {"source": self.source, "op": self.op, "expected": self.expected}
        if self.key is not None:
            data["key"] = self.key
        return data


class VarExtractSpec(StrictModel):
    """对齐 StressTaskRequest.VarExtract。"""

    var: str = Field(min_length=1)
    source: Literal["body", "header"] = "body"
    path: str = Field(min_length=1)

    def to_payload(self) -> dict[str, Any]:
        return {"var": self.var, "source": self.source, "path": self.path}


class CsvSpec(StrictModel):
    """步骤/单接口的 CSV 参数化数据集（首行为列名，列名即变量名）。

    mode：sequential（游标循环）/ random（每次迭代随机取行）；
    text：CSV 文本（首行列名）。模板中经 {{列名}} 引用，CSV 值优先于
    全局 variables（每次迭代按行合并进渲染池）。
    """

    mode: Literal["sequential", "random"] = "sequential"
    text: str = ""


class StepSpec(StrictModel):
    """对齐 StressTaskRequest.ScenarioStep。"""

    name: str | None = None
    alias: str | None = None
    url: str
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    weight: int = Field(default=1, ge=1)
    extract: list[VarExtractSpec] = Field(default_factory=list)
    assertions: list[AssertionSpec] = Field(default_factory=list)
    csv: CsvSpec | None = None

    @field_validator("method")
    @classmethod
    def _check_method(cls, v: str) -> str:
        m = v.strip().upper()
        if m not in HTTP_METHODS:
            raise ValueError(f"非法 HTTP 方法: {v}（允许 {sorted(HTTP_METHODS)}）")
        return m

    def to_payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "url": self.url,
            "method": self.method,
            "headers": self.headers,
            "weight": self.weight,
        }
        if self.name:
            data["name"] = self.name
        if self.alias:
            data["alias"] = self.alias
        if self.body is not None:
            data["body"] = self.body
        if self.extract:
            data["extract"] = [e.to_payload() for e in self.extract]
        if self.assertions:
            data["assertions"] = [a.to_payload() for a in self.assertions]
        if self.csv is not None:
            data["csv"] = {"mode": self.csv.mode, "text": self.csv.text}
        return data


class StageSpec(StrictModel):
    """对齐 StressTaskRequest.Stage（分档并发，容量评估）。"""

    concurrency: int = Field(ge=1)
    duration_sec: int = Field(ge=1)


class RequestSpec(StrictModel):
    """单接口模式请求定义（未配置 steps 时生效）。"""

    url: str = Field(min_length=1)
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    assertions: list[AssertionSpec] = Field(default_factory=list)
    csv: CsvSpec | None = None

    @field_validator("method")
    @classmethod
    def _check_method(cls, v: str) -> str:
        m = v.strip().upper()
        if m not in HTTP_METHODS:
            raise ValueError(f"非法 HTTP 方法: {v}（允许 {sorted(HTTP_METHODS)}）")
        return m


class LoadSpec(StrictModel):
    """负载参数，语义对齐 StressTaskRequest 的负载字段。"""

    concurrency: int = Field(default=20, ge=1, description="单档模式并发数")
    duration_sec: int = Field(default=60, ge=1, description="持续时长秒")
    warmup_sec: int = Field(default=10, ge=0, description="预热时长秒（不计入统计）")
    ramp_up_sec: int = Field(default=0, ge=0, description="并发爬坡秒")
    qps_limit: int = Field(default=0, ge=0, description="QPS 上限，0 不限")
    sample_interval_sec: int = Field(default=5, ge=1, description="采样周期秒")


class GateSpec(StrictModel):
    """场景级门禁参数（可与 GATE_* 环境变量互补）。"""

    baseline_task_id: int | None = None
    timeout_sec: int = Field(default=0, ge=0)
    fail_on_no_baseline: bool = False
    webhook: str = ""


class Scenario(StrictModel):
    """场景文件顶层模型。"""

    name: str = Field(min_length=1)
    description: str = ""
    engine: Literal["http", "jmeter"] = "http"
    app: str = Field(min_length=1, description="归属应用名（appName，用于基线匹配）")
    task_name: str | None = None
    request: RequestSpec | None = None
    steps: list[StepSpec] = Field(default_factory=list)
    stages: list[StageSpec] = Field(default_factory=list)
    stage_concurrencies: list[int] = Field(default_factory=list)
    load: LoadSpec = Field(default_factory=LoadSpec)
    variables: dict[str, str] = Field(default_factory=dict)
    gate: GateSpec = Field(default_factory=GateSpec)
    labels: list[str] = Field(default_factory=list, description="场景标签（仅元数据，可复现追踪用）")

    @model_validator(mode="after")
    def _check_shape(self) -> Scenario:
        if not self.request and not self.steps:
            raise ValueError("场景必须定义 request（单接口）或 steps（多接口混合）之一")
        if self.request and self.steps:
            raise ValueError("request 与 steps 互斥，一个场景只能选择一种模式")
        if self.steps and self.stages:
            raise ValueError("steps（混合场景）暂不支持与 stages（分档并发）组合")
        if self.stages and self.stage_concurrencies:
            raise ValueError("stages 与 stage_concurrencies 互斥（简写与完整形式只能选一个）")
        if self.load.ramp_up_sec > 0 and self.load.warmup_sec > 0 and self.load.ramp_up_sec > self.load.warmup_sec:
            # 对齐 Java 侧建议：爬坡应落在预热窗口内，否则爬坡尾巴会污染统计
            raise ValueError("ramp_up_sec 建议不超过 warmup_sec（爬坡尾巴会污染正式统计）；如需放宽请自行承担")
        return self

    # ------------------------------------------------------------- helpers
    @property
    def is_scenario_mode(self) -> bool:
        return bool(self.steps)

    def to_request_payload(self) -> dict[str, Any]:
        """转为 /api/stress/* 请求体（camelCase，字段与 StressTaskRequest 一致）。"""
        payload: dict[str, Any] = {
            "engineType": self.engine,
            "appName": self.app,
            "concurrency": self.load.concurrency,
            "durationSec": self.load.duration_sec,
            "warmupSec": self.load.warmup_sec,
            "rampUpSec": self.load.ramp_up_sec,
            "qpsLimit": self.load.qps_limit,
            "sampleIntervalSec": self.load.sample_interval_sec,
        }
        if self.task_name:
            payload["taskName"] = self.task_name
        if self.request:
            payload["targetUrl"] = self.request.url
            payload["method"] = self.request.method
            payload["headers"] = self.request.headers
            if self.request.body is not None:
                payload["body"] = self.request.body
            if self.request.assertions:
                payload["assertions"] = [a.to_payload() for a in self.request.assertions]
        if self.steps:
            payload["scenario"] = [s.to_payload() for s in self.steps]
        if self.variables:
            payload["variables"] = self.variables
        if self.stages:
            payload["stages"] = [
                {"concurrency": s.concurrency, "durationSec": s.duration_sec} for s in self.stages
            ]
        elif self.stage_concurrencies:
            payload["stageConcurrencies"] = self.stage_concurrencies
        return payload

    def summary_line(self) -> str:
        """单行摘要（CLI 输出用）。"""
        if self.steps:
            target = f"{len(self.steps)} 步混合场景"
        elif self.stages or self.stage_concurrencies:
            n = len(self.stages) or len(self.stage_concurrencies)
            target = f"{n} 档并发"
        else:
            assert self.request
            target = f"{self.request.method} {self.request.url}"
        return f"{self.name} [{self.app}] {target} 并发={self.load.concurrency} 时长={self.load.duration_sec}s"


def assertion_from_json(raw: str) -> AssertionSpec:
    """调试辅助：从 JSON 字符串构造断言。"""
    return AssertionSpec.model_validate(json.loads(raw))
