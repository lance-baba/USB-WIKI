"""只读系统诊断 API（/api/diagnostics）。

Core 层 ``diagnostics.collect()`` 的薄包装，不做任何修复动作；只经统一
response 原语回写。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..core import diagnostics

if TYPE_CHECKING:
    from ..server import Handler


def handle_get(h: "Handler", path: str) -> bool:
    if path == "/api/diagnostics":
        h._send_json({"code": 200, "data": diagnostics.collect()})
        return True
    return False
