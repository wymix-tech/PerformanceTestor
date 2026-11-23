"""引擎注册表：按 type 选择引擎，未匹配回退 http（语义对齐 StressEngineRegistry SPI）。

与 Java 侧的对应：
- StressEngine SPI 接口       → Engine Protocol（type 属性 + run 方法）
- StressEngineRegistry        → register()/get()
- engineType: http / jmeter   → 这里新增 locust；http 保留给平台 Java 引擎，
  Python 侧不实现 http 引擎（避免 GIL 手写发压，见 Issue #23 风险表）
"""

from __future__ import annotations

from typing import Any, Protocol

from scenario.model import Scenario

DEFAULT_ENGINE_TYPE = "http"

# P2 预留接入点：k6 / Gatling 等高精度发压引擎（Locust Python 侧 P95/P99 长尾
# 系统性偏高）。接入时实现 Engine 协议（type="k6"）并在此注册；
# steps 多步场景若走 JMeter 补齐，优先在 generate_jmx 按 ThroughputController +
# JSONPostProcessor 映射权重与变量提取（当前 steps 执行仍由 locust 承担）。
RESERVED_ENGINE_TYPES = ("k6",)


class Engine(Protocol):
    """引擎协议（对齐 StressEngine SPI：type + run）。"""

    @property
    def type(self) -> str: ...

    def run(self, scenario: Scenario, listener: Any | None = None) -> Any:
        """执行压测并返回 engine.result.StressTaskResult。"""
        ...


class EngineRegistry:
    def __init__(self) -> None:
        self._engines: dict[str, Engine] = {}

    def register(self, engine: Engine) -> None:
        """注册引擎；同名重复注册时后者覆盖（Java 侧 merge 取先者，此处取后者便于测试桩替换）。"""
        self._engines[engine.type] = engine

    def get(self, engine_type: str | None) -> Engine:
        """按 type 取引擎，未匹配回退 http（再退 locust——本工程无 http 实现）。"""
        t = engine_type or DEFAULT_ENGINE_TYPE
        engine = self._engines.get(t)
        if engine is None:
            engine = self._engines.get(DEFAULT_ENGINE_TYPE)
        if engine is None and self._engines:
            engine = next(iter(self._engines.values()))
        if engine is None:
            raise LookupError("没有可用引擎（请安装 locust：uv sync --extra locust）")
        return engine
