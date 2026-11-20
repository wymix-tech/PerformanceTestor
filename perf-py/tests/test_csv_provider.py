"""CSV 参数化数据源（stress/csv_provider）+ 请求校验桥接测试。

覆盖：解析（列名/空行/引号/补齐/限制拒绝）、sequential 游标循环、
random 取值域、from_spec 空缺、请求层校验（mode 白名单/无数据行/
超限文案），以及 to_scenario 桥接（单接口与步骤）。
"""

import pytest

from scenario.model import CsvSpec
from server.stress_request import (
    parse_stress_request,
    to_scenario,
    validate_stress_request,
)
from stress.csv_provider import CsvDataset, CsvParseError, parse_csv

CSV = "username,password\nu1,p1\nu2,p2\nu3,p3\n"


# ---------------------------------------------------------------- 解析


def test_parse_csv_basic_and_fill():
    columns, rows = parse_csv(CSV)
    assert columns == ["username", "password"]
    assert rows == [["u1", "p1"], ["u2", "p2"], ["u3", "p3"]]
    # 短行补空串；空行跳过
    cols, rows2 = parse_csv("a,b\nx\n\ny,2")
    assert cols == ["a", "b"]
    assert rows2 == [["x", ""], ["y", "2"]]
    # 空文本
    assert parse_csv("") == ([], [])
    assert parse_csv("  \n ") == ([], [])


def test_parse_csv_quoted_values():
    _, rows = parse_csv('a,b\n"1,2",x')
    assert rows == [["1,2", "x"]]


def test_parse_csv_rejections():
    with pytest.raises(CsvParseError, match="首行必须为列名"):
        parse_csv("a,\nx,1")
    with pytest.raises(CsvParseError, match="列名重复"):
        parse_csv("a,a\n1,2")
    with pytest.raises(CsvParseError, match="列数不能超过"):
        parse_csv(",".join(f"c{i}" for i in range(21)) + "\n" + ",".join(["1"] * 21))
    with pytest.raises(CsvParseError, match="数据行数不能超过"):
        parse_csv("a\n" + "\n".join(f"v{i}" for i in range(10001)))
    with pytest.raises(CsvParseError, match="数据过大"):
        parse_csv("a\n" + "x" * (1024 * 1024))


# ---------------------------------------------------------------- 抽样


def test_sequential_cursor_cycles():
    ds = CsvDataset(["u", "p"], [["1", "a"], ["2", "b"], ["3", "c"]], "sequential")
    seq = [ds.next_row()["u"] for _ in range(7)]
    assert seq == ["1", "2", "3", "1", "2", "3", "1"]


def test_random_stays_within_domain():
    ds = CsvDataset(["u"], [[str(i)] for i in range(10)], "random")
    picks = {ds.next_row()["u"] for _ in range(200)}
    assert picks <= {str(i) for i in range(10)}
    assert len(picks) > 1, "200 次随机应覆盖多个不同行"


def test_from_spec_semantics():
    assert CsvDataset.from_spec(None) is None
    assert CsvDataset.from_spec(CsvSpec(text="")) is None
    ds = CsvDataset.from_spec(CsvSpec(mode="random", text=CSV))
    assert ds is not None and ds.mode == "random"
    row = ds.next_row()
    assert set(row) == {"username", "password"}
    with pytest.raises(CsvParseError, match="没有数据行"):
        CsvDataset.from_spec(CsvSpec(text="a,b"))


# ---------------------------------------------------------------- 请求校验桥接


def test_validate_single_mode_csv():
    ok = parse_stress_request({
        "targetUrl": "http://h:8081/x", "concurrency": 1, "durationSec": 10,
        "csv": {"mode": "sequential", "text": CSV}})
    validate_stress_request(ok)  # 不抛

    bad_mode = parse_stress_request({
        "targetUrl": "http://h:8081/x", "concurrency": 1, "durationSec": 10,
        "csv": {"mode": "shuffle", "text": CSV}})
    with pytest.raises(ValueError, match="取值模式不合法"):
        validate_stress_request(bad_mode)

    no_rows = parse_stress_request({
        "targetUrl": "http://h:8081/x", "concurrency": 1, "durationSec": 10,
        "csv": {"text": "a,b"}})
    with pytest.raises(ValueError, match="没有数据行"):
        validate_stress_request(no_rows)

    dup = parse_stress_request({
        "targetUrl": "http://h:8081/x", "concurrency": 1, "durationSec": 10,
        "csv": {"text": "a,a\n1,2"}})
    with pytest.raises(ValueError, match="压测请求的 CSV 列名重复"):
        validate_stress_request(dup)


def test_validate_scenario_step_csv():
    step = {"name": "login", "url": "http://h:8081/login", "method": "POST",
            "weight": 1, "csv": {"mode": "random", "text": CSV}}
    req = parse_stress_request({
        "concurrency": 1, "durationSec": 10, "scenario": [step]})
    validate_stress_request(req)  # 不抛

    bad = parse_stress_request({
        "concurrency": 1, "durationSec": 10,
        "scenario": [{**step, "csv": {"mode": "bad", "text": CSV}}]})
    with pytest.raises(ValueError, match="CSV 取值模式不合法"):
        validate_stress_request(bad)


def test_to_scenario_carries_csv():
    # 单接口模式
    scn = to_scenario(parse_stress_request({
        "targetUrl": "http://h:8081/x", "concurrency": 1, "durationSec": 10,
        "csv": {"mode": "random", "text": CSV}}))
    assert scn.request is not None
    assert scn.request.csv is not None
    assert scn.request.csv.mode == "random"

    # 场景模式（request 与 scenario 互斥）
    scn2 = to_scenario(parse_stress_request({
        "concurrency": 1, "durationSec": 10,
        "scenario": [{"name": "s", "url": "http://h:8081/y", "weight": 1,
                      "csv": {"mode": "sequential", "text": CSV}}]}))
    assert scn2.steps[0].csv is not None
    assert scn2.steps[0].csv.mode == "sequential"
    # to_payload 回传 csv（JMX 导出等桥接路径字段不丢失）
    assert scn2.steps[0].to_payload()["csv"] == {
        "mode": "sequential", "text": CSV}
