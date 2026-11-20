"""StressTaskRequest 解析层（阶段 5 · 写路径）：camelCase 请求体 → 内部模型 + 场景桥接。

对齐 com.perfserver.stress.StressTaskRequest（Jackson 反序列化语义）：
- 字段缺省 → Java 字段初始化默认值（concurrency=1 / durationSec=60 / warmupSec=10 / …）；
- 字段显式 null → Java 基本类型收 null 为 0（concurrency=0 → 校验拒绝）、引用类型为 null；
- 未知字段忽略（Spring Boot 默认关闭 FAIL_ON_UNKNOWN_PROPERTIES）；
- 校验规则与错误文案逐字对齐 StressTaskService.validate / validateCommon / validateScenario。

to_scenario()：扁平请求 → engine.locust_engine 可执行的 Scenario（model_construct
绕过 CLI 场景校验——CLI 的 ramp≤warmup 等约束在 HTTP 写路径不适用，Java 侧同为建议项）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scenario.model import (
    AssertionSpec,
    CsvSpec,
    GateSpec,
    LoadSpec,
    RequestSpec,
    Scenario,
    StepSpec,
    VarExtractSpec,
)
from stress.csv_provider import CSV_MODES, parse_csv

# 引擎类型映射：Python 侧无手写线程发压引擎（Issue #23 风险表决策），
# 平台 http 引擎由 locust 引擎等价承担；jmeter 引擎保持同名。
HTTP_ENGINE = "http"
EXECUTE_ENGINE_OF_HTTP = "locust"


@dataclass
class StageData:
    """对齐 StressTaskRequest.Stage（int 基本类型，缺省 0）。"""

    concurrency: int = 0
    duration_sec: int = 0


@dataclass
class StressTaskRequestData:
    """对齐 StressTaskRequest 的内部模型（camelCase JSON → snake_case 字段）。"""

    task_name: str | None = None
    engine_type: str = "http"
    target_url: str | None = None
    app_name: str | None = None
    method: str = "GET"
    concurrency: int = 1
    duration_sec: int = 60
    qps_limit: int = 0
    warmup_sec: int = 10
    ramp_up_sec: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    assertions: list[dict] = field(default_factory=list)
    body: str | None = None
    stages: list[StageData] | None = None
    stage_concurrencies: list[int] | None = None
    sample_interval_sec: int = 5
    scenario: list[dict] | None = None
    variables: dict[str, str] = field(default_factory=dict)
    csv: dict | None = None

    @property
    def is_scenario_mode(self) -> bool:
        """对齐 isScenarioMode：scenario 非空列表即为场景模式。"""
        return bool(self.scenario)

    def resolve_stages(self) -> list[StageData] | None:
        """对齐 resolveStages：stages 优先；stageConcurrencies 简写按档数均分时长
        （per = max(1, durationSec / n)），过滤非正值；两者皆空返回 None。"""
        if self.stages:
            return self.stages
        if not self.stage_concurrencies:
            return None
        valid = [c for c in self.stage_concurrencies if c is not None and c > 0]
        if not valid:
            return None
        per = max(1, self.duration_sec // len(valid))
        return [StageData(concurrency=c, duration_sec=per) for c in valid]


def _opt_str(payload: dict, key: str) -> str | None:
    """缺省 → None（取 Java 字段默认）；显式 null → None。"""
    v = payload.get(key)
    return None if v is None else str(v)


def _opt_int(payload: dict, key: str, default: int) -> int:
    """缺省 → Java 字段默认；显式 null → 0（Jackson 基本类型 null 收敛语义）。"""
    v = payload.get(key)
    if v is None:
        return 0 if key in payload else default
    try:
        return int(v)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"字段 {key} 必须是整数: {v!r}") from exc


def _str_map(payload: dict, key: str) -> dict[str, str]:
    v = payload.get(key)
    if not v:
        return {}
    return {str(k): str(val) for k, val in v.items()}


def parse_stress_request(payload: dict) -> StressTaskRequestData:
    """camelCase 请求体 → StressTaskRequestData（未知字段忽略，null 语义见上）。"""
    stages = None
    if isinstance(payload.get("stages"), list) and payload["stages"]:
        stages = [StageData(
            concurrency=_opt_int(s, "concurrency", 0),
            duration_sec=_opt_int(s, "durationSec", 0),
        ) for s in payload["stages"] if isinstance(s, dict)]
    concs = payload.get("stageConcurrencies")
    scenario = payload.get("scenario")
    return StressTaskRequestData(
        task_name=_opt_str(payload, "taskName"),
        engine_type=_opt_str(payload, "engineType") or "http",
        target_url=_opt_str(payload, "targetUrl"),
        app_name=_opt_str(payload, "appName"),
        method=_opt_str(payload, "method") or "GET",
        concurrency=_opt_int(payload, "concurrency", 1),
        duration_sec=_opt_int(payload, "durationSec", 60),
        qps_limit=_opt_int(payload, "qpsLimit", 0),
        warmup_sec=_opt_int(payload, "warmupSec", 10),
        ramp_up_sec=_opt_int(payload, "rampUpSec", 0),
        headers=_str_map(payload, "headers"),
        assertions=[a for a in (payload.get("assertions") or []) if isinstance(a, dict)],
        body=_opt_str(payload, "body"),
        stages=stages,
        stage_concurrencies=[int(c) for c in concs] if isinstance(concs, list) else None,
        sample_interval_sec=_opt_int(payload, "sampleIntervalSec", 5),
        scenario=[s for s in scenario if isinstance(s, dict)]
        if isinstance(scenario, list) and scenario else None,
        variables=_str_map(payload, "variables"),
        csv=payload.get("csv") if isinstance(payload.get("csv"), dict) else None,
    )


# ------------------------------------------------------------- 参数校验（文案逐字对齐）


def validate_stress_request(request: StressTaskRequestData) -> None:
    """对齐 StressTaskService.validate：场景校验 → URL 校验（非场景）→ 公共参数校验。"""
    _validate_scenario(request)
    if request.is_scenario_mode:
        for _i, step in enumerate(request.scenario or []):
            _validate_csv(step.get("csv"), f"场景步骤「{step.get('name')}」")
        _validate_common(request)
        return
    url = request.target_url
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("压测目标 URL 必须以 http:// 或 https:// 开头")
    _validate_csv(request.csv, "压测请求")
    _validate_common(request)


def _validate_csv(raw, label: str) -> None:
    """CSV 参数化校验：mode 白名单 + 完整解析（列名/行数/大小限制即建数据集口径）。"""
    if not isinstance(raw, dict) or not str(raw.get("text") or "").strip():
        return
    mode = str(raw.get("mode") or "sequential")
    if mode not in CSV_MODES:
        raise ValueError(f"{label}的 CSV 取值模式不合法（允许 sequential / random）: {mode}")
    try:
        parse_csv(str(raw.get("text") or ""))
    except ValueError as exc:
        raise ValueError(f"{label}的 {exc}") from exc
    if not str(raw.get("text") or "").strip().count("\n"):
        raise ValueError(f"{label}的 CSV 没有数据行（首行列名之后至少 1 行数据）")


def _validate_common(request: StressTaskRequestData) -> None:
    """对齐 validateCommon：并发/时长/限流/预热/爬坡/分档/引擎可用性。"""
    if request.concurrency < 1 or request.concurrency > 1000:
        raise ValueError("并发数必须在 1-1000 之间")
    if request.duration_sec < 1 or request.duration_sec > 3600:
        raise ValueError("压测时长必须在 1-3600 秒之间")
    if request.qps_limit < 0 or request.qps_limit > 100_000:
        raise ValueError("QPS 上限必须在 0-100000 之间")
    if request.warmup_sec < 0 or request.warmup_sec > 300:
        raise ValueError("预热时长必须在 0-300 秒之间")
    if request.ramp_up_sec < 0 or request.ramp_up_sec > 600:
        raise ValueError("并发爬坡时长必须在 0-600 秒之间")
    stages = request.resolve_stages()
    if stages is not None:
        if len(stages) > 10:
            raise ValueError("分档数量不能超过 10")
        for stage in stages:
            if stage.concurrency < 1 or stage.concurrency > 1000:
                raise ValueError("分档并发数必须在 1-1000 之间")
            if stage.duration_sec < 1 or stage.duration_sec > 600:
                raise ValueError("分档时长必须在 1-600 秒之间")
    # 引擎类型归一（空白 → http）；可用性校验由 StressTaskService._validate 注入执行
    # （保持与 Java validateCommon 相同的判定顺序：分档规则之后做引擎前置校验）


def _validate_scenario(request: StressTaskRequestData) -> None:
    """对齐 validateScenario：步骤数上限、每步 URL 与权重、变量提取规则。"""
    if not request.is_scenario_mode:
        return
    steps = request.scenario or []
    if len(steps) > 20:
        raise ValueError("场景步骤数量不能超过 20")
    positive_weight = 0
    for _i, step in enumerate(steps):
        name = step.get("name")
        # Java 字符串拼接 null 字面量 → "null"（场景步骤「null」的…）
        name_text = "null" if name is None else str(name)
        url = step.get("url")
        if not url or not (str(url).startswith("http://") or str(url).startswith("https://")):
            raise ValueError(f"场景步骤「{name_text}」的 URL 必须以 http:// 或 https:// 开头")
        if int(step.get("weight", 1) or 0) > 0:
            positive_weight += 1
        for e in step.get("extract") or []:
            if not isinstance(e, dict) or not (e.get("var") or "").strip() \
                    or not (e.get("path") or "").strip():
                raise ValueError(
                    f"场景步骤「{name_text}」的变量提取规则必须同时提供 var 与 path")
    if positive_weight == 0:
        raise ValueError("场景至少需要 1 个权重大于 0 的步骤")


# ------------------------------------------------------------- 场景桥接（供引擎执行）


def _assertion_specs(raw: list[dict] | None) -> list[AssertionSpec]:
    specs = []
    for a in raw or []:
        specs.append(AssertionSpec.model_construct(
            source=a.get("source") or "status",
            key=a.get("key"),
            op=a.get("op") or "eq",
            expected=a.get("expected") or "",
        ))
    return specs


def _csv_spec(raw) -> CsvSpec | None:
    """步骤/单接口的 csv dict → CsvSpec（空缺/空文本返回 None）。"""
    if not isinstance(raw, dict) or not str(raw.get("text") or "").strip():
        return None
    mode = str(raw.get("mode") or "sequential")
    return CsvSpec.model_construct(
        mode=mode if mode in CSV_MODES else "sequential", text=str(raw.get("text")))


def _step_spec(step: dict) -> StepSpec:
    extract = [VarExtractSpec.model_construct(
        var=e.get("var"), source=e.get("source") or "body", path=e.get("path"))
        for e in (step.get("extract") or []) if isinstance(e, dict)]
    return StepSpec.model_construct(
        name=step.get("name"),
        alias=step.get("alias"),
        url=str(step.get("url")),
        method=str(step.get("method") or "GET"),
        headers=_str_map(step, "headers"),
        body=step.get("body"),
        # 权重：缺省 → Java 字段默认 1；显式 null → Jackson 基本类型 0
        weight=1 if "weight" not in step else int(step.get("weight") or 0),
        extract=extract,
        assertions=_assertion_specs(step.get("assertions")),
        csv=_csv_spec(step.get("csv")),
    )


def _load_spec(request: StressTaskRequestData, *, concurrency: int | None = None,
               duration_sec: int | None = None, warmup_sec: int | None = None) -> LoadSpec:
    return LoadSpec.model_construct(
        concurrency=request.concurrency if concurrency is None else concurrency,
        duration_sec=request.duration_sec if duration_sec is None else duration_sec,
        warmup_sec=request.warmup_sec if warmup_sec is None else warmup_sec,
        ramp_up_sec=request.ramp_up_sec,
        qps_limit=request.qps_limit,
        sample_interval_sec=max(1, request.sample_interval_sec),
    )


def _request_spec(request: StressTaskRequestData) -> RequestSpec:
    return RequestSpec.model_construct(
        url=str(request.target_url),
        method=request.method,
        headers=request.headers,
        body=request.body,
        assertions=_assertion_specs(request.assertions),
        csv=_csv_spec(request.csv),
    )


def to_scenario(request: StressTaskRequestData) -> Scenario:
    """扁平请求 → 引擎可执行 Scenario。

    - 步骤与 request 互斥优先级与 Java 引擎一致（scenario 非空走步骤）；
    - model_construct 绕过 CLI 场景校验（ramp≤warmup 等在 HTTP 契约中是建议项非约束）；
    - engine 固定 locust（平台 http 引擎的 Python 等价承担者）。
    """
    scenario_mode = request.is_scenario_mode
    steps = [_step_spec(s) for s in (request.scenario or [])] if scenario_mode else []
    return Scenario.model_construct(
        name=request.task_name or request.target_url or "stress-task",
        description="",
        engine=EXECUTE_ENGINE_OF_HTTP,
        app=request.app_name or "-",
        task_name=request.task_name,
        request=None if scenario_mode else _request_spec(request),
        steps=steps,
        stages=[],
        stage_concurrencies=[],
        load=_load_spec(request),
        variables=dict(request.variables),
        gate=GateSpec.model_construct(baseline_task_id=None, timeout_sec=0,
                                      fail_on_no_baseline=False, webhook=""),
        labels=[],
    )


def stage_scenario(request: StressTaskRequestData, stage: StageData,
                   with_warmup: bool) -> Scenario:
    """分档子场景：对齐 HttpStressEngine.runStage——并发/时长取档值，
    仅首档执行 warmup（后续档连接池/JIT 已热），rampUp 与 QPS 上限逐档生效。"""
    base = to_scenario(request)
    return Scenario.model_construct(
        **{**base.__dict__,
           "load": _load_spec(request, concurrency=stage.concurrency,
                              duration_sec=stage.duration_sec,
                              warmup_sec=request.warmup_sec if with_warmup else 0)},
    )
