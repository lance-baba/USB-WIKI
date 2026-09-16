"""系统 / 生命周期类 API（状态、重建、安全退出）。

只处理业务，所有响应通过 Handler 的统一原语发出；不直接碰
``send_response`` / ``send_header`` / ``wfile.write``，也不做安全校验与密钥脱敏。
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from ..core.log_util import get_logger

if TYPE_CHECKING:
    from ..server import Handler

log = get_logger()


def _rebuild_worker(h: "Handler", recreate_vec: bool = False) -> None:
    """后台重建索引（从 server.Handler 迁出）。"""
    try:
        report = h.ctx.rebuild_index(recreate_vec=recreate_vec)
        log.info("重建任务完成: %s", report)
    except Exception as exc:  # noqa: BLE001
        log.error("重建任务异常: %s", exc)


def handle_get(h: "Handler", path: str) -> bool:
    if path == "/api/status":
        h._send_json({"code": 200, "data": h.ctx.status()})
        return True
    return False


def handle_post(h: "Handler", path: str) -> bool:
    if path == "/api/system/rebuild-index":
        h._read_json()
        threading.Thread(
            target=_rebuild_worker, args=(h,), name="rebuild-index", daemon=True
        ).start()
        h._send_json({"code": 202, "message": "索引全量重建任务已在后台启动"})
        return True

    if path == "/api/system/rebuild-vectors":
        h._read_json()
        threading.Thread(
            target=_rebuild_worker, args=(h,), kwargs={"recreate_vec": True},
            name="rebuild-vectors", daemon=True,
        ).start()
        h._send_json({"code": 202, "message": "向量索引重建任务已在后台启动"})
        return True

    if path == "/api/system/shutdown":
        h._read_json()
        h._send_json({"code": 200, "message": "正在安全退出，请稍候…"})
        threading.Timer(0.3, h.server.request_shutdown).start()  # type: ignore[attr-defined]
        return True

    return False
