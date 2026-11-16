"""全局异常处理：对齐 Java GlobalExceptionHandler（@RestControllerAdvice）。

Java 侧任何异常（含缺参）都返回 HTTP 200 + ApiResponse.error（code=1，data=null），
前端与 CI 脚本按 code 判定。FastAPI 默认对请求校验失败返回 422 detail 结构，
与 Java 契约不符——这里统一转为 Java 形态错误包装：
- RequestValidationError：缺 query 参数时按 Spring 措辞生成 message
  （Required request parameter 'xxx' ... is not present），其余给出参数摘要；
- 其余异常：log 留痕 + "服务器内部错误: " 前缀（对齐 Java handleException）。
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)


def _error(message: str) -> dict:
    return {"code": 1, "message": message, "data": None}


def _validation_message(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = err.get("loc", [])
        if err.get("type") == "missing" and len(loc) >= 2 and loc[0] == "query":
            name = loc[-1]
            parts.append(f"Required request parameter '{name}' for method parameter "
                         "type String is not present")
        else:
            parts.append(f"{'.'.join(str(x) for x in loc)}: {err.get('msg', '')}")
    return "; ".join(parts) if parts else "请求参数校验失败"


async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=200,
                        content=_error("服务器内部错误: " + _validation_message(exc)))


async def handle_exception(request: Request, exc: Exception) -> JSONResponse:
    log.error("unexpected error, path=%s", request.url.path, exc_info=exc)
    return JSONResponse(status_code=200, content=_error(f"服务器内部错误: {exc}"))
