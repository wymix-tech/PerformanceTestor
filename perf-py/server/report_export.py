"""压测报告 HTML 渲染与导出（P0-1c）。

数据源与 GET /api/stress/tasks/{id}/report 完全同源：task（wire camel）+
result（result_json 解析，含 stagePoints / capacity[advices, jvmAdvice]）。
导出形态（无第三方转换引擎的最小止血实现，见 report_api.py 路由注释）：
- format=html：完整独立 HTML（内联样式，可直接浏览器打开）；
- format=doc：同一 HTML 载荷 + application/msword（Word/WPS 可直接打开）；
- format=pdf / printMode=true：inline 打印视图（自动唤起浏览器打印，
  「另存为 PDF」即达导出语义；服务端 PDF 转换引擎列为后续增强）。
"""

from __future__ import annotations

import html
import re


def _e(value) -> str:
    return html.escape(str(value if value is not None else "-"))


def _metric_rows(result: dict) -> str:
    rows = ""
    for label, key in (("QPS", "qps"), ("P50 (ms)", "p50Ms"), ("P95 (ms)", "p95Ms"),
                       ("P99 (ms)", "p99Ms"), ("错误率 (%)", "errorRate"),
                       ("总请求数", "totalRequests")):
        rows += (f"<tr><th>{label}</th>"
                 f"<td>{_e(result.get(key) if result else None)}</td></tr>")
    return rows


def _stage_rows(stage_points: list) -> str:
    if not stage_points:
        return "<p>无分档采样数据。</p>"
    body = ""
    for p in stage_points:
        body += (f"<tr><td>{_e(p.get('concurrency'))}</td>"
                 f"<td>{_e(p.get('qps'))}</td>"
                 f"<td>{_e(p.get('p50Ms'))}</td>"
                 f"<td>{_e(p.get('p95Ms'))}</td>"
                 f"<td>{_e(p.get('p99Ms'))}</td>"
                 f"<td>{_e(p.get('errorRate'))}</td>"
                 f"<td>{_e(p.get('totalRequests'))}</td></tr>")
    return ("<table><thead><tr><th>并发</th><th>QPS</th><th>P50</th><th>P95</th>"
            "<th>P99</th><th>错误率</th><th>总请求</th></tr></thead>"
            f"<tbody>{body}</tbody></table>")


def _advices_html(capacity: dict) -> str:
    advices = capacity.get("advices") or []
    if not advices:
        return ""
    body = ""
    for a in advices:
        body += (f"<li><b>{_e(a.get('title'))}</b>（{_e(a.get('scaleType'))} / "
                 f"风险 {_e(a.get('riskLevel'))}）<br/>"
                 f"<span class='muted'>{_e(a.get('description'))}</span></li>")
    return f"<h3>扩容建议</h3><ul class='advices'>{body}</ul>"


def _capacity_html(result: dict) -> str:
    cap = (result or {}).get("capacity")
    if not cap:
        return ""
    jvm = cap.get("jvmAdvice") or {}
    jvm_block = ""
    if jvm:
        params = " ".join(_e(p) for p in (jvm.get("params") or []))
        jvm_block = (f"<p>堆峰值：{_e(jvm.get('peakHeapUsedMb'))}MB / "
                     f"{_e(jvm.get('heapMaxMb'))}MB"
                     f"（{_e(jvm.get('peakHeapRatio'))}%）；GC："
                     f"{_e(jvm.get('gcCollector'))}</p>"
                     f"<p class='muted'>{_e(jvm.get('heapSize'))}<br/>{params}</p>")
    return ("<h2>容量评估</h2>"
            f"<p>目标 P95：{_e(cap.get('targetP95Ms'))}ms ｜ 安全并发："
            f"{_e(cap.get('maxConcurrency'))} ｜ 安全 QPS：{_e(cap.get('maxQps'))}"
            f" ｜ 拟合 R²：{_e(cap.get('r2'))}</p>"
            f"{_advices_html(cap)}{jvm_block}")


_STYLE = (
    "body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;"
    "max-width:860px;margin:32px auto;color:#1f2937;line-height:1.65;padding:0 16px}"
    "h1{border-bottom:2px solid #111;padding-bottom:10px}"
    "h2{margin-top:28px;font-size:18px}"
    "table{border-collapse:collapse;width:100%;margin:10px 0}"
    "th,td{border:1px solid #d1d5db;padding:6px 10px;text-align:left;font-size:14px}"
    "th{background:#f3f4f6;font-weight:600}"
    ".muted{color:#6b7280;font-size:13px}"
    ".advices li{margin:10px 0}"
)


def render_report_html(task: dict, result: dict | None, *,
                       print_mode: bool = False) -> str:
    """渲染独立 HTML 报告（print_mode 附自动打印脚本）。"""
    result = result or {}
    auto_print = ("<script>window.onload=function(){window.print()}</script>"
                  if print_mode else "")
    body = (
        "<h1>压测报告</h1>"
        "<h2>任务信息</h2>"
        "<table><tbody>"
        f"<tr><th>任务</th><td>{_e(task.get('taskName'))}"
        f"（#{_e(task.get('id'))}）</td></tr>"
        f"<tr><th>目标</th><td>{_e(task.get('targetUrl'))}</td></tr>"
        f"<tr><th>应用</th><td>{_e(task.get('appName'))}"
        f"（版本 {_e(task.get('appVersion'))}）</td></tr>"
        f"<tr><th>引擎/方法</th><td>{_e(task.get('engineType'))} / "
        f"{_e(task.get('method'))}</td></tr>"
        f"<tr><th>并发/时长</th><td>{_e(task.get('concurrency'))} 并发 · "
        f"{_e(task.get('durationSec'))}s · QPS 上限 "
        f"{_e(task.get('qpsLimit'))}</td></tr>"
        f"<tr><th>状态/完成时间</th><td>{_e(task.get('status'))} / "
        f"{_e(task.get('finishTime'))}</td></tr>"
        "</tbody></table>"
        "<h2>核心指标</h2>"
        f"<table><tbody>{_metric_rows(result)}</tbody></table>"
        "<h2>分档采样（容量曲线）</h2>"
        f"{_stage_rows(result.get('stagePoints') or [])}"
        f"{_capacity_html(result)}"
    )
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\"/>"
        f"<title>压测报告 #{_e(task.get('id'))}</title>"
        f"<style>{_STYLE}</style></head><body>{body}{auto_print}</body></html>"
    )


def safe_filename(task: dict, ext: str) -> str:
    """报告文件名：中文任务名保留，其余非安全字符折叠为 _。"""
    name = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_",
                  str(task.get("taskName") or "report"))[:60]
    return f"report-{task.get('id')}-{name or 'report'}.{ext}"
