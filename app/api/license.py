"""离线授权 API。

端点：
  GET  /api/license/status       当前授权状态（含设备码、客户信息、有效期）
  GET  /api/license/device-code  首启生成的设备码（供用户抄给授权方）
  POST /api/license/activate     输入激活码完成激活   {code}
  POST /api/license/clear        清除本机激活

只处理业务，响应统一走 Handler 的 ``_send_json`` / ``_read_json`` 原语；
**不直接** 碰 ``send_response`` / ``wfile.write``，也不做安全校验（那是传输层的事）。

所有端点都返回 HTTP 200 —— 激活失败是「业务结果」而非「协议错误」，
前端据 ``data.ok`` / ``data.status`` 决定提示文案（失败原因已本地化）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..core import license as license_mod

if TYPE_CHECKING:
    from ..server import Handler


def _device_code(h: "Handler") -> None:
    st = license_mod.status()
    h._send_json({"code": 200, "data": {
        "device_code": st["device_code"],
        "device_hash_id": st["device_hash_id"],
        "product": st["product"],
        "key_id": st["key_id"],
        # 「首次运行」：尚无激活记录时即为首启（设备码是确定性派生的，随取随有）
        "first_run": not st["activated"] and st["status"] == license_mod.ST_NO_LICENSE,
    }})


def _activate(h: "Handler") -> None:
    body = h._read_json()
    code = str(body.get("code") or "").strip()
    res = license_mod.activate(code)
    h._send_json({
        "code": 200,
        "data": {"ok": res["ok"], "status": res["status"],
                 "feature": list((res.get("payload") or {}).get("features") or []),
                 "customer_id": (res.get("payload") or {}).get("customer_id"),
                 "expires_at": (res.get("payload") or {}).get("expires_at"),
                 # 成功后再回读一次完整状态，前端免二次请求
                 "state": license_mod.status() if res["ok"] else None},
        "message": res["message"],
    })


def _clear(h: "Handler") -> None:
    h._read_json()
    res = license_mod.clear()
    h._send_json({"code": 200, "data": {"ok": res["ok"], "removed": res["removed"],
                                        "state": license_mod.status()},
                  "message": res["message"]})


def handle_get(h: "Handler", path: str) -> bool:
    if path == "/api/license/status":
        h._send_json({"code": 200, "data": license_mod.status()})
        return True
    if path == "/api/license/device-code":
        _device_code(h)
        return True
    return False


def handle_post(h: "Handler", path: str) -> bool:
    if path == "/api/license/activate":
        _activate(h)
        return True
    if path == "/api/license/clear":
        _clear(h)
        return True
    return False
