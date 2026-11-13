import pytest

from engine.registry import EngineRegistry
from scenario.model import Scenario


class FakeEngine:
    def __init__(self, engine_type: str) -> None:
        self._type = engine_type

    @property
    def type(self) -> str:
        return self._type

    def run(self, scenario: Scenario, listener=None):  # noqa: ANN001
        return {"engine": self._type}


def test_registry_fallback_http_semantics():
    reg = EngineRegistry()
    locust = FakeEngine("locust")
    reg.register(locust)
    # 对齐 StressEngineRegistry.get：null/未知类型回退 http；
    # 本工程无 http 实现，最终落到唯一可用引擎（locust）
    assert reg.get(None) is locust
    assert reg.get("http") is locust
    assert reg.get("locust") is locust
    assert reg.get("jmeter") is locust


def test_registry_lookup_error_when_empty():
    with pytest.raises(LookupError):
        EngineRegistry().get("locust")


def test_registry_jmeter_registered_specifically():
    reg = EngineRegistry()
    locust, jmeter = FakeEngine("locust"), FakeEngine("jmeter")
    reg.register(locust)
    reg.register(jmeter)
    assert reg.get("jmeter") is jmeter
    assert reg.get("locust") is locust
    assert reg.get(None) is locust  # 回退 http → 唯一可用 → locust


def test_registry_overwrite_same_type():
    reg = EngineRegistry()
    first, second = FakeEngine("locust"), FakeEngine("locust")
    reg.register(first)
    reg.register(second)
    assert reg.get("locust") is second
