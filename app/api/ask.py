"""问答 / AI 类 API（SSE 对话、模型自测、Ollama 模型列表）。

只处理业务，响应统一走 Handler 的 ``_send_json`` / SSE framing 原语；不直接碰底层
HTTP 写入，也不做安全校验 / 密钥脱敏。``chat/completions`` 的 SSE 推流从
``server.Handler._chat`` 迁出，逻辑与原实现逐字一致。
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..core.log_util import get_logger

if TYPE_CHECKING:
    from ..server import Handler

log = get_logger()


def chat_completions(h: "Handler") -> None:
    body = h._read_json()
    query = str(body.get("query") or "").strip()
    history = body.get("history")
    if not isinstance(history, list):
        history = []

    h._sse_start()
    try:
        for frame in h.ctx.gateway.stream_chat(query, history):
            if not h._sse_write(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"):
                break
    except Exception as exc:  # noqa: BLE001
        log.error("SSE 推流异常: %s", exc)
        h._sse_write(
            f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        )
    finally:
        h._sse_end()


def ai_test(h: "Handler") -> None:
    body = h._read_json()
    target = str(body.get("target") or "ollama").lower()
    if h.ctx.gateway is None:
        h._send_json({"code": 503, "message": "服务尚未初始化完成"}, 503)
        return
    if target == "ollama":
        result = h.ctx.gateway.test_ollama(str(body.get("model") or "") or None)
    elif target == "api":
        result = h.ctx.gateway.test_api()
    else:
        h._send_json({"code": 400, "message": "target 仅支持 ollama / api"}, 400)
        return
    h._send_json({"code": 200 if result.get("ok") else 200, "data": result})


def ollama_models(h: "Handler", q: dict) -> None:
    host = (q.get("host") or [""])[0] or None
    if h.ctx.gateway is None:
        h._send_json(
            {"code": 503, "message": "服务尚未初始化完成", "data": {"available": False, "models": []}}
        )
        return
    info = h.ctx.gateway.list_ollama_models(host)
    h._send_json({"code": 200, "data": info})


def handle_get(h: "Handler", path: str) -> bool:
    q = h._query()
    if path == "/api/ollama/models":
        ollama_models(h, q)
        return True
    return False


def handle_post(h: "Handler", path: str) -> bool:
    if path == "/api/chat/completions":
        chat_completions(h)
        return True
    if path == "/api/ai/test":
        ai_test(h)
        return True
    return False
