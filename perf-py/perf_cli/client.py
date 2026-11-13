"""平台 HTTP 客户端：封装 /api/stress/* 调用。

认证方式对齐平台 JwtAuthInterceptor：静态 API Token 走 X-Perf-Api-Token 头。
平台统一响应包装为 {code, message, data}（code=0 成功）；
注意 POST /api/stress/gate 在压测失败时 HTTP 仍为 200，须解析 data.passed。
"""

from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    """平台调用异常（网络/HTTP 状态/业务 code != 0），对应退出码 2。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class PerfClient:
    def __init__(
        self,
        server_url: str,
        api_token: str,
        timeout: float = 1800.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = server_url.rstrip("/")
        self._token = api_token
        headers = {"X-Perf-Api-Token": api_token} if api_token else {}
        self._client = httpx.Client(base_url=self._base, headers=headers, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PerfClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- internals
    def _unwrap(self, resp: httpx.Response, action: str) -> dict[str, Any]:
        if resp.status_code == 401:
            raise ApiError(f"{action}: 认证失败（HTTP 401），请检查 PERF_API_TOKEN", status_code=401)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ApiError(f"{action}: HTTP {resp.status_code}: {resp.text[:500]}", status_code=resp.status_code) from exc
        try:
            body = resp.json()
        except ValueError as exc:
            raise ApiError(f"{action}: 响应非 JSON: {resp.text[:500]}", status_code=resp.status_code) from exc
        code = body.get("code")
        if code not in (0, "0"):
            raise ApiError(f"{action}: 平台返回 code={code}, message={body.get('message')}")
        data = body.get("data")
        if not isinstance(data, dict):
            raise ApiError(f"{action}: 响应缺少 data 对象")
        return data

    # ------------------------------------------------------------- api
    def run_gate(
        self,
        payload: dict[str, Any],
        *,
        baseline_task_id: int | None = None,
        timeout_sec: int = 0,
        fail_on_no_baseline: bool = False,
        webhook: str = "",
    ) -> dict[str, Any]:
        """同步触发性能门禁压测并等待完成（POST /api/stress/gate）。

        返回 GateVerdict（dict）：passed/status/taskId/rows/summary 等。
        """
        params: dict[str, Any] = {"failOnNoBaseline": str(fail_on_no_baseline).lower()}
        if baseline_task_id:
            params["baselineTaskId"] = baseline_task_id
        if timeout_sec and timeout_sec > 0:
            params["timeoutSec"] = timeout_sec
        if webhook:
            params["webhook"] = webhook
        resp = self._client.post("/api/stress/gate", json=payload, params=params)
        return self._unwrap(resp, "调用门禁接口")

    def get_task(self, task_id: int) -> dict[str, Any]:
        resp = self._client.get(f"/api/stress/tasks/{task_id}")
        return self._unwrap(resp, f"查询任务 #{task_id}")

    def get_task_report(self, task_id: int) -> dict[str, Any]:
        resp = self._client.get(f"/api/stress/tasks/{task_id}/report")
        return self._unwrap(resp, f"获取任务 #{task_id} 报告")
