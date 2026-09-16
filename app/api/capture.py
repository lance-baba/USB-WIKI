"""网页抓取 / 判重类 API（/api/capture/url、/api/capture/duplicate）。

只处理业务，响应统一走 Handler 的 ``_send_json`` 原语；不直接碰底层 HTTP 写入，
也不做安全校验 / 密钥脱敏。``capture/url`` 返回 409 表示「需要用户决策的重复」状态，
200/500 表示成功/失败，与原实现一致。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..core import crawler, indexer

if TYPE_CHECKING:
    from ..server import Handler


def capture_url(h: "Handler") -> None:
    body = h._read_json()
    url = str(body.get("url") or "").strip()
    # on_duplicate: abort(默认) / update / new —— 由用户在弹窗里选择后带上。
    # 不给就按 abort 处理：宁可让调用方显式表态，也不默默产生重复笔记。
    on_dup = str(body.get("on_duplicate") or "abort").strip().lower()
    if on_dup not in ("abort", "update", "new"):
        on_dup = "abort"
    result = crawler.capture_url(
        url, db=h.ctx.db, embedder=h.ctx.embedder, on_duplicate=on_dup
    )
    if result.status == "duplicate":
        # 不是服务端错误，而是一个**需要用户决策**的正常状态
        h._send_json(result.to_dict(), 409)
        return
    h._send_json(result.to_dict(), 200 if result.ok else 500)


def capture_duplicate(h: "Handler", q: dict) -> None:
    # 前端在抓取前先问一次「这个网页是不是已经存过」，
    # 以便弹出「打开已有 / 更新已有 / 另存为新版本 / 取消」。
    # 注意：后端在 capture 时**自己也会再查一次** —— 前端判断不可信。
    target = (q.get("url") or [""])[0]
    found = indexer.find_by_normalized_url(h.ctx.db, target)
    h._send_json({"ok": True, "data": {"duplicate": bool(found), "existing": found or None}})


def handle_get(h: "Handler", path: str) -> bool:
    q = h._query()
    if path == "/api/capture/duplicate":
        capture_duplicate(h, q)
        return True
    return False


def handle_post(h: "Handler", path: str) -> bool:
    if path == "/api/capture/url":
        capture_url(h)
        return True
    return False
