"""JMX 导入解析（压测中心 · JMeter 脚本导入）：JMX → 场景步骤 + 负载参数。

把 JMeter 测试计划里的 HTTPSamplerProxy 解析为平台的多接口混合场景步骤
（与前端 StressForm 的 ScenarioStep 字段一一对应），解析 ThreadGroup 的
线程数/时长/爬坡预填负载参数，导入后用户可编辑再发起压测——而非黑盒直跑。

支持范围（逐项给出降级 warning，不阻断导入）：
- 采样器：method/domain/port/path、postBodyRaw 请求体、Argument 形式的 query 参数
- 请求头：采样器子树内的 HeaderManager
- 断言：ResponseAssertion 的 status eq / body eq|contains|regex
  （test_type 2=contains、8=equals、16=regex，与 jmeter_engine.generate_jmx 写入侧一致）
- 变量提取：JSONPostProcessor（JSON 路径）；RegexExtractor 无法映射，降级 warning
- 负载：首个 ThreadGroup 的 num_threads/duration/ramp_time
- 计划级断言：仅单采样器时附加，否则 warning（无法归属到具体步骤）
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from scenario.model import HTTP_METHODS

# 与 generate_jmx 写入侧一致的 ResponseAssertion test_type 位值
TEST_TYPE_OPS = {2: "contains", 8: "eq", 16: "regex"}
MAX_JMX_BYTES = 2 * 1024 * 1024
# 前端编辑下拉支持的方法（HEAD/OPTIONS 后端可用，仅提示）
UI_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}


def parse_jmx(content: str) -> dict:
    """JMX 文本 → {steps, load, warnings}（wire 与前端 ScenarioStep 对齐）。"""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"JMX 文件不是合法的 XML：{exc}") from exc
    if root.tag != "jmeterTestPlan":
        raise ValueError("不是 JMeter 测试计划文件（根元素应为 jmeterTestPlan）")

    warnings: list[str] = []
    steps: list[dict] = []
    plan_level_assertions = _collect_plan_assertions(root)

    for sampler in root.iter("HTTPSamplerProxy"):
        step = _parse_sampler(sampler, warnings)
        if step is not None:
            steps.append(step)

    if not steps:
        raise ValueError("未在 JMX 中找到 HTTP 采样器（HTTPSamplerProxy），无法导入")

    # 计划级断言归属：仅单采样器时可无歧义附加
    if plan_level_assertions:
        if len(steps) == 1:
            steps[0]["assertLines"] = plan_level_assertions + steps[0]["assertLines"]
        else:
            warnings.append(
                f"计划级断言 {len(plan_level_assertions)} 条无法归属到单个步骤（共 "
                f"{len(steps)} 个采样器），已跳过")

    return {
        "steps": [_step_wire(s) for s in steps],
        "load": _parse_load(root, warnings),
        "warnings": warnings,
    }


# ---------------------------------------------------------------- 采样器

def _parse_sampler(sampler: ET.Element, warnings: list[str]) -> dict | None:
    name = sampler.get("testname") or ""
    method = (_prop_text(sampler, "HTTPSampler.method") or "GET").strip().upper()
    if method not in HTTP_METHODS:
        warnings.append(f"采样器「{name}」方法 {method} 不受支持，已跳过")
        return None
    if method not in UI_METHODS:
        warnings.append(
            f"采样器「{name}」方法 {method} 不在前端编辑下拉中（后端仍可执行）")

    url, query_args, body = _parse_url_and_body(sampler, name, warnings)
    step: dict = {
        "name": name,
        "method": method,
        "url": url,
        "queryArgs": query_args,
        "body": body,
        "headers": _parse_headers(sampler),
        "assertLines": _parse_assertions(sampler, name, warnings),
        "extract": _parse_extract(sampler, name, warnings),
    }
    return step


def _parse_url_and_body(sampler: ET.Element, name: str,
                        warnings: list[str]) -> tuple[str, list[dict], str]:
    """采样器 → (url, query 参数列表, 原始 body)。

    domain 缺失或为 JMeter 变量（${...}）时 url 退化为 path 并 warning，
    由用户在表单中补全 host（提交时后端也会校验 URL 合法性）。
    """
    protocol = _prop_text(sampler, "HTTPSampler.protocol") or "http"
    domain = (_prop_text(sampler, "HTTPSampler.domain") or "").strip()
    path = _prop_text(sampler, "HTTPSampler.path") or ""
    if path.startswith("/http"):  # 兼容 JMeter 导出的非常规转义
        path = path.lstrip("/")
    query_args: list[dict] = []
    body = ""

    args_el = sampler.find(".//elementProp[@name='HTTPsampler.Arguments']")
    if args_el is not None:
        raw = (_prop_text(sampler, "HTTPSampler.postBodyRaw") or "").strip() == "true"
        for arg in args_el.findall("elementProp"):
            value = _prop_text(arg, "Argument.value") or ""
            if raw:
                body = value
            else:
                key = _prop_text(arg, "Argument.name") or ""
                if key.strip():
                    query_args.append({"name": key, "value": value})

    if not domain:
        warnings.append(
            f"采样器「{name}」未配置服务器域名（或为 JMeter 变量），URL 仅含路径，请补全 host")
        return path, query_args, body
    if "${" in domain:
        warnings.append(
            f"采样器「{name}」域名含 JMeter 变量（{domain}），请导入后改为实际地址")
        return path, query_args, body

    port_text = (_prop_text(sampler, "HTTPSampler.port") or "").strip()
    port = ""
    if port_text.isdigit():
        p = int(port_text)
        if (protocol == "https" and p != 443) or (protocol == "http" and p != 80):
            port = f":{p}"
    elif port_text:
        port = f":{port_text}"
    return f"{protocol}://{domain}{port}{path}", query_args, body


def _parse_headers(sampler: ET.Element) -> dict[str, str]:
    """采样器子树内 HeaderManager → 有序 {name: value}。"""
    headers: dict[str, str] = {}
    for hm in sampler.iter("elementProp"):
        if hm.get("elementType") != "HeaderManager":
            continue
        for h in hm.findall(".//elementProp"):
            key = _prop_text(h, "Header.name")
            if key:
                headers[key] = _prop_text(h, "Header.value") or ""
    return headers


def _parse_assertions(sampler: ET.Element, name: str, warnings: list[str]) -> list[str]:
    """采样器子树内 ResponseAssertion → 断言文本行（前端 assertText 语法）。"""
    lines: list[str] = []
    for ra in sampler.iter("ResponseAssertion"):
        field = _prop_text(ra, "Assertion.test_field") or ""
        op = TEST_TYPE_OPS.get(_int_prop(ra, "Assertion.test_type"))
        for expected in _assert_patterns(ra):
            if field == "Assertion.response_code" and op == "eq":
                lines.append(f"status eq {expected}")
            elif field == "Assertion.response_data" and op:
                lines.append(f"body {op} {expected}")
            else:
                warnings.append(
                    f"采样器「{name}」断言 {field or '?'}/{op or '?'}（期望 {expected}）"
                    "暂不支持映射，已跳过")
    return lines


def _collect_plan_assertions(root: ET.Element) -> list[str]:
    """TestPlan 直接子级的 ResponseAssertion（generate_jmx 的写入位置）。"""
    lines: list[str] = []
    for plan in root.iter("TestPlan"):
        for ra in list(plan):
            if ra.tag != "ResponseAssertion":
                continue
            field = _prop_text(ra, "Assertion.test_field") or ""
            op = TEST_TYPE_OPS.get(_int_prop(ra, "Assertion.test_type"))
            for expected in _assert_patterns(ra):
                if field == "Assertion.response_code" and op == "eq":
                    lines.append(f"status eq {expected}")
                elif field == "Assertion.response_data" and op:
                    lines.append(f"body {op} {expected}")
    return lines


def _parse_extract(sampler: ET.Element, name: str, warnings: list[str]) -> dict:
    """采样器子树内 JSONPostProcessor → {var, source: body, path}。

    RegexExtractor 等其他提取器无法等价映射为 JSON 路径，降级 warning。
    """
    for jp in sampler.iter("JSONPostProcessor"):
        var = (_prop_text(jp, "JSONPostProcessor.referenceName") or "").strip()
        paths = []
        for cp in jp.findall(".//collectionProp"):
            for sp in cp.findall("stringProp"):
                if sp.text and sp.text.strip():
                    paths.append(sp.text.strip())
        if var and paths:
            return {"var": var, "source": "body", "path": paths[0]}
        return {}
    for rx in sampler.iter("RegexExtractor"):
        ref = (_prop_text(rx, "RegexExtractor.refname") or "").strip()
        if ref:
            warnings.append(
                f"采样器「{name}」的 RegexExtractor（变量 {ref}）无法映射为 JSON 路径提取，"
                "已跳过，请导入后手动配置变量提取")
    return {}


def _parse_load(root: ET.Element, warnings: list[str]) -> dict:
    """首个 ThreadGroup 的线程数/时长/爬坡（duration 缺省 = 永久循环，不填）。"""
    groups = list(root.iter("ThreadGroup"))
    if not groups:
        return {}
    tg = groups[0]
    if len(groups) > 1:
        warnings.append(f"JMX 含 {len(groups)} 个线程组，负载参数取第一个")
    load: dict = {}
    n = _int_prop(tg, "ThreadGroup.num_threads")
    if n and n > 0:
        load["concurrency"] = n
    dur = _int_prop(tg, "ThreadGroup.duration")
    if dur and dur > 0:
        load["durationSec"] = dur
    ramp = _int_prop(tg, "ThreadGroup.ramp_time")
    if ramp is not None and ramp > 0:
        load["rampUpSec"] = ramp
    return load


# ---------------------------------------------------------------- wire

def _step_wire(s: dict) -> dict:
    """内部步骤结构 → 前端 ScenarioStep wire（headersText/assertText/extract 直填）。"""
    headers_text = "\n".join(f"{k}: {v}" for k, v in s["headers"].items())
    query = "&".join(
        f"{a['name']}={a['value']}" for a in s["queryArgs"] if a["value"] != "")
    url = s["url"]
    if query:
        url = url + ("&" if "?" in url else "?") + query
    return {
        "name": s["name"],
        "alias": "",
        "method": s["method"],
        "url": url,
        "headersText": headers_text,
        "body": s["body"],
        "weight": 1,
        "extractVar": s["extract"].get("var", ""),
        "extractSource": s["extract"].get("source", "body"),
        "extractPath": s["extract"].get("path", ""),
        "assertText": "\n".join(s["assertLines"]),
    }


# ---------------------------------------------------------------- 工具

def _prop_text(parent: ET.Element, name: str) -> str | None:
    el = parent.find(f"stringProp[@name='{name}']")
    return el.text if el is not None and el.text is not None else None


def _int_prop(parent: ET.Element, name: str) -> int | None:
    text = _prop_text(parent, name)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _assert_patterns(ra: ET.Element) -> list[str]:
    out: list[str] = []
    for cp in ra.findall("collectionProp[@name='Asserts.test_strings']"):
        for sp in cp.findall("stringProp"):
            if sp.text:
                out.append(sp.text)
    return out
