"""只读系统诊断 API（/api/diagnostics）。

Core 层 ``diagnostics.collect()`` 的薄包装，不做任何修复动作；只经统一
response 原语回写。

A4.1：把**运行时** AI 状态注入诊断（嵌入源能否用 / 对话能否用），
诊断报告才不至于只说「配置里写了什么」。注入值是**只读缓存**，不发网络探测。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..core import diagnostics

if TYPE_CHECKING:
    from ..server import Handler


def _runtime_ai(h: "Handler") -> dict:
    """从应用上下文提取运行时 AI 两段状态（embedding / chat）。"""
    ctx = getattr(h, "ctx", None)
    if ctx is None or ctx.gateway is None:
        return {}

    # --- Level 2：向量路是否真的可用 ---
    db = ctx.db
    vec_ok = bool(
        ctx.embedder is not None and db is not None
        and getattr(db, "vec_table_ready", False)
        and not getattr(db, "signature_mismatch", "")
    )
    fallback = ""
    if not vec_ok:
        fallback = (getattr(db, "signature_mismatch", "") or ""
                    or next((w for w in ctx.warnings if "向量" in w or "嵌入" in w), "")
                    or "向量嵌入源不可用，已退化为纯 FTS5 词法检索")

    # --- Level 3：对话就绪状态（缓存，不探测）---
    ready = ctx.ai_readiness()
    ready["provider"] = ctx.gateway.state.last_provider
    return {
        "embedding": {
            "source": ctx.embedder_source,
            "ready": vec_ok,
            "fallback_reason": fallback or None,
        },
        "chat": ready,
    }


def handle_get(h: "Handler", path: str) -> bool:
    if path == "/api/diagnostics":
        h._send_json({"code": 200, "data": diagnostics.collect(
            runtime_ai=lambda: _runtime_ai(h),
        )})
        return True
    return False
