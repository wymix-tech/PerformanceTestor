"""压测报告导出路由：补齐 GET /api/reports/export（P0-1c）。

前端契约（perf-web reportApi.exportUrl，index.ts）：返回**裸文件流**（非
ApiResponse 包壳），打印页 window.open 直开、下载走 blob，需带 token 访问。

无服务端 PDF 转换引擎的最小止血实现：
- format=html → text/html 附件下载；
- format=doc  → 同一 HTML 载荷 + application/msword（Word/WPS 直接打开）；
- format=pdf 或 printMode=true → inline 打印视图（自动唤起浏览器打印，
  「另存为 PDF」达导出语义；服务端转换引擎列为后续增强）。
任务不存在时仍返回 JSON 错误包装（HTTP 200 + code=1，前端可按 JSON 判别）。
"""

from __future__ import annotations

import urllib.parse

from fastapi import APIRouter, Query, Request, Response

from server.report_export import render_report_html, safe_filename

router = APIRouter(prefix="/api/reports")


@router.get("/export")
def export_report(request: Request,
                  stressTaskId: int = Query(...),
                  format: str = Query("html"),
                  printMode: bool = Query(False)) -> Response:
    repo = request.app.state.stress_repo
    task = repo.find_by_id(stressTaskId)
    if task is None:
        return Response(
            content=f'{{"code": 1, "message": "stress task not found: '
                    f'{stressTaskId}", "data": null}}',
            media_type="application/json")
    result = repo.parse_result_json(task)
    fmt = (format or "html").lower()
    print_html = render_report_html(task.to_camel(), result, print_mode=True) \
        if (printMode or fmt == "pdf") else render_report_html(task.to_camel(),
                                                               result)
    filename = safe_filename(task.to_camel(), "html" if fmt in ("html", "pdf")
                             else fmt)
    quoted = urllib.parse.quote(filename)
    disposition = "inline" if (printMode or fmt == "pdf") else "attachment"
    media_type = "application/msword" if fmt == "doc" else "text/html; charset=utf-8"
    return Response(
        content=print_html,
        media_type=media_type,
        headers={
            "Content-Disposition":
                f"{disposition}; filename*=UTF-8''{quoted}",
        },
    )
