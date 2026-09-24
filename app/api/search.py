"""检索类 API（/api/search）。

只处理业务，响应统一走 Handler 的 ``_send_json`` 原语；不直接碰底层 HTTP 写入，
也不做安全校验 / 密钥脱敏。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..core import search as search_mod

if TYPE_CHECKING:
    from ..server import Handler


def search(h: "Handler", q: dict) -> None:
    query = (q.get("q") or [""])[0]
    top_k = int((q.get("top_k") or ["5"])[0])
    res = search_mod.hybrid_search(h.ctx.db, h.ctx.embedder, query, top_k_parents=top_k)
    h._send_json(
        {
            "code": 200,
            "data": {
                "route": res.route,
                "counts": res.counts,
                "references": [r.__dict__ for r in res.references],
                "warnings": res.warnings,
            },
        }
    )


def handle_get(h: "Handler", path: str) -> bool:
    q = h._query()
    if path == "/api/search":
        search(h, q)
        return True
    return False
