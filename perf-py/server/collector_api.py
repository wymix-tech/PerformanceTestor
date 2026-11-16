"""collector 域 router（阶段 5 · 步骤 3）：/api/agent 探针上报通道。

协议字节级兼容要点（对齐 AgentReportController + AgentAuthInterceptor）：
- 鉴权：X-Perf-Agent-Token 请求头；令牌未配置（默认空）或开关关闭时保持开放
  （兼容存量 agent 与内网演示）；校验失败 → HTTP 401 + 固定 JSON 体（逐字节一致）；
  比较用常量时间算法（compare_digest）防时序侧信道。
- 项目准入：targetProjectName 缺省回退 appName（Java String.valueOf 语义，
  null 值映射为 "null"）；项目不存在 → HTTP 200 + code=404 固定 message
  （agent 识别 PROJECT_NOT_FOUND 后自动停报）。
- 上报为容错优先：即使单批数据解析失败也返回成功，避免 agent 重试风暴。
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server.collector_service import handle_report

router = APIRouter(prefix="/api/agent")

TOKEN_HEADER = "X-Perf-Agent-Token"

# 固定响应体（对齐 Java 拦截器 write 的字面量，协议字节级兼容）
AUTH_FAIL_BODY = {"code": 401, "message": "探针令牌无效", "data": None}


def _project_of(body: dict) -> str:
    """对齐 Java：String.valueOf(getOrDefault(targetProjectName, getOrDefault(appName, "")))。

    键存在但值为 JSON null 时，Java String.valueOf(null) → "null"。
    """
    if "targetProjectName" in body:
        value = body.get("targetProjectName")
    else:
        value = body.get("appName", "")
    return "null" if value is None else str(value)


@router.post("/report", response_model=None)
def report(request: Request, body: dict) -> JSONResponse | dict:
    state = request.app.state
    token: str = getattr(state, "agent_token", "") or ""
    auth_enabled: bool = getattr(state, "agent_auth_enabled", True)
    # 未配置令牌或开关关闭：保持开放，兼容存量 agent 与内网演示（对齐拦截器放行分支）
    if auth_enabled and token:
        actual = request.headers.get(TOKEN_HEADER)
        if (actual is None
                or not hmac.compare_digest(token.encode("utf-8"), actual.encode("utf-8"))):
            return JSONResponse(status_code=401, content=AUTH_FAIL_BODY)

    repo = state.collector_repo
    remote_host = request.client.host if request.client else ""
    project = _project_of(body)
    if not repo.project_exists(project):
        return {"code": 404,
                "message": f"PROJECT_NOT_FOUND: 项目「{project}」不存在于平台，数据已全部丢弃",
                "data": None}
    try:
        handle_report(body, remote_host, repo)
    except Exception:  # noqa: BLE001 上报接口容错优先：落库失败仍返回成功
        pass
    return {"code": 0, "message": "success", "data": None}
