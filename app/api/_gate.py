"""功能门禁：未激活时限制**部分**能力（不是整体锁死）。

## 边界（与产品约定一致）

未激活（`license.status().activated == False`）时：
  * **允许**：笔记浏览、本地检索（含智能/向量检索）、拖入文件入库、粘贴文字入库、设置。
  * **禁止**：网页抓取入库（`/api/capture/url`）、AI 问答（`/api/chat/completions`）。

原则：**绝不用"锁死用户自己的数据"来施压** —— 用户已经录入/将要录入的笔记永远可读、可写、可搜。
只拦"依赖外部抓取或模型推理"的产品能力。

## 为什么读 ctx 快照而不是每次现算

`AppContext.boot()` 已算出 `ctx.license`；激活/清除时由 `app/api/license.py` 即时刷新，
因此门禁读快照即可，避免每个请求都读盘 + 做一次 Ed25519 验签。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..server import Handler

#: 未激活时的统一提示（前端也会在受限入口旁提示同一件事）
UNAUTH_MESSAGE = "此功能需激活后使用。请到「设置 → 产品授权」完成激活。"


def is_activated(h: "Handler") -> bool:
    """ctx 快照里是否已激活（缺字段一律视为未激活）。"""
    lic = getattr(h.ctx, "license", None)
    return bool(lic.get("activated")) if isinstance(lic, dict) else False


def require_activation(h: "Handler") -> bool:
    """放行返回 True；未激活时写出 403 并返回 False（调用方直接 return）。"""
    if is_activated(h):
        return True
    h._send_json({"code": 403, "message": UNAUTH_MESSAGE}, 403)
    return False
