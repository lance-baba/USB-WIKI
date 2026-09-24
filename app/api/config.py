"""配置读写 API（/api/config）。

GET 返回脱敏后的配置（明文 api_key 绝不经 HTTP 外发，是否已配置用 ``*_set``
字段表达）；POST 调用 ``config.update`` 持久化。脱敏统一走 ``redact`` 域的
共享工具，本模块不做自己的密钥脱敏逻辑。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..core import config, redact as redact_mod

if TYPE_CHECKING:
    from ..server import Handler


def handle_get(h: "Handler", path: str) -> bool:
    if path != "/api/config":
        return False
    # ⚠ 此前这里直接返回 config.as_dict() —— 把**明文 api_key**
    # 通过 HTTP 发出了。设置页读取只能拿到脱敏值（sk-****abcd），
    # 是否已配置用单独的 *_set 字段表达。
    masked = redact_mod.mask_config(config.as_dict())
    for _sec, _items in masked.items():
        if isinstance(_items, dict):
            for _k in list(_items):
                if redact_mod.is_secret_key(_k):
                    _items[f"{_k}_set"] = bool(config.get_str(_sec, _k, ""))
    # 附带内置默认值，供设置页「恢复默认设置」按钮填回表单（默认值无密钥，无需脱敏）。
    h._send_json({"code": 200, "data": masked, "defaults": config.defaults()})
    return True


def handle_post(h: "Handler", path: str) -> bool:
    if path != "/api/config":
        return False
    body = h._read_json()
    updated = config.update(body.get("data") or body, persist=True)
    h._send_json({"code": 200, "message": "配置已保存", "data": updated})
    return True
