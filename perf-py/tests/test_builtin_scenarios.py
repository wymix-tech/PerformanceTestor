"""入库场景回归测试：scenario/scenarios/*.yaml 必须全部通过校验。

这是「场景即代码」的 CI 兜底——场景文件拼写/结构错误在 PR 门禁期暴露，
而不是在压测执行现场才发现。
"""

from pathlib import Path

from scenario.loader import load_scenario

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenario" / "scenarios"


def test_builtin_scenarios_dir_exists():
    files = sorted(SCENARIO_DIR.glob("*.yaml"))
    assert files, f"入库场景目录为空: {SCENARIO_DIR}"


def test_all_builtin_scenarios_valid():
    for path in sorted(SCENARIO_DIR.glob("*.yaml")):
        scenario = load_scenario(path)  # 校验失败抛 ScenarioError 即用例失败
        assert scenario.name
        assert scenario.load.duration_sec >= 1
        # 默认目标必须是 perf-demo 真实端口（8081），防止再次引入过时的 18080
        urls = []
        if scenario.request:
            urls.append(scenario.request.url)
        urls.extend(s.url for s in scenario.steps)
        for url in urls:
            assert ":8081/" in url, f"{path.name} 的目标端口不是 perf-demo 的 8081: {url}"


def test_builtin_scenario_names_unique():
    names = [load_scenario(p).name for p in sorted(SCENARIO_DIR.glob("*.yaml"))]
    assert len(names) == len(set(names)), f"场景名重复: {names}"
