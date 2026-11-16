"""告警通知服务（阶段 5 · 写路径）：逐行对齐 Java NotifyService。

格式自适应：URL 含 feishu 用飞书文本格式、含 dingtalk 用钉钉文本格式，
其余按自定义 POST（title/content 平铺字段）。发送失败仅记日志不重试
（告警尽力而为，不阻断压测/门禁流程）。
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)
_TIMEOUT = 10.0


def _build_body(url: str, title: str, text: str) -> dict:
    lower = url.lower()
    if "feishu" in lower or "open.feishu" in lower:
        return {"msg_type": "text", "content": {"text": f"{title}\n{text}"}}
    if "dingtalk" in lower or "oapi.dingtalk" in lower:
        return {"msgtype": "text", "text": {"content": f"{title}\n{text}"}}
    return {"title": title, "content": text}


def _mask_url(url: str) -> str:
    q = url.find("?")
    return f"{url[:q]}?***" if q > 0 else url


def send(webhook_url: str | None, title: str, text: str) -> None:
    """发送文本告警（webhook 为空时静默跳过；任何失败仅 warn 日志）。"""
    if not webhook_url or not webhook_url.strip():
        return
    try:
        resp = httpx.post(webhook_url.strip(), json=_build_body(webhook_url, title, text),
                          headers={"Content-Type": "application/json;charset=UTF-8"},
                          timeout=_TIMEOUT)
        if resp.status_code // 100 != 2:
            log.warning("notify webhook non-2xx: url=%s, status=%s, body=%s",
                        _mask_url(webhook_url), resp.status_code, resp.text)
    except Exception as exc:  # noqa: BLE001 — 对齐 Java：尽力而为
        log.warning("notify webhook failed: url=%s, error=%s", _mask_url(webhook_url), exc)
