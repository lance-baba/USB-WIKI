"""Wiki-USB 轻量 HTTP 服务与路由分发（PRD 3.1 / 5.3）。

纯标准库实现（http.server），以便在最小化的嵌入式 Python 运行时中直接跑起来，
不引入任何 Web 框架依赖。
"""
from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import re
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .core import (archiver, paths,
                   redact as redact_mod,
                   security as security_mod)
from .api import (ask as api_ask, capture as api_capture, config as api_config,
                  diagnostics as api_diagnostics, library as api_library,
                  search as api_search, system as api_system)
from .core.context import AppContext, get_ctx
from .core.log_util import get_logger

log = get_logger()

MAX_BODY = 4 * 1024 * 1024  # 4MB 请求体上限


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # 单一版本源：app/version.py（此前这里硬编码 "WikiUSB/1.2"）
    from .version import USER_AGENT_TOKEN  # noqa: PLC0415

    server_version = USER_AGENT_TOKEN
    sys_version = ""

    ctx: AppContext  # 由 Server 注入

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: A003 - 覆盖 stdlib 签名
        log.debug("HTTP %s - %s", self.address_string(), fmt % args)

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 刻意**不发任何 CORS 头**：浏览器因此读不到跨源响应体。
        # 要开放跨域读取只能靠 Access-Control-Allow-Origin，而本服务
        # 装着用户全部私人笔记，默认不对外开放。
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_bytes(self, data: bytes, ctype: str, status: int = 200, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # 内联预览类型（浏览器可直接渲染）；其余走下载
    INLINE_TYPES = {
        ".pdf": "application/pdf",
        ".html": "text/html",
        ".htm": "text/html",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        ".svg": "image/svg+xml",
        ".txt": "text/plain", ".md": "text/plain",
        ".mp4": "video/mp4", ".webm": "video/webm",
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    }

    @staticmethod
    def _content_disposition(disp: str, filename: str) -> str:
        """构造合法的 Content-Disposition。

        ⚠ HTTP 头只能 latin-1 编码 —— 中文文件名直接塞进 header 会抛
        ``UnicodeEncodeError``，且因为响应头已部分发出，客户端会一直等待直到超时。
        按 RFC 6266 用双字段：ASCII 回退名 + RFC 5987 的 UTF-8 百分号编码真名。
        """
        stem = Path(filename).stem
        ext = Path(filename).suffix
        ascii_stem = re.sub(r"[^\w.\-]", "_", stem.encode("ascii", "ignore").decode("ascii"))
        ascii_stem = re.sub(r"_{2,}", "_", ascii_stem).strip("._-")[:80]
        if not ascii_stem:      # 纯中文名被剥空 —— 别退化成 ".pdf" 这种隐藏文件名
            ascii_stem = "download"
        ascii_ext = re.sub(r"[^\w.]", "", ext.encode("ascii", "ignore").decode("ascii"))
        if not ascii_ext.startswith("."):
            ascii_ext = ""
        quoted = urllib.parse.quote(filename, safe="")
        return f'{disp}; filename="{ascii_stem}{ascii_ext}"; filename*=UTF-8\'\'{quoted}'

    def _send_file_range(self, file, ctype: str, inline: bool) -> None:
        """支持 HTTP Range 的文件服务。

        浏览器的内置 PDF 查看器会发 ``Range: bytes=...`` 请求做分段加载；
        不支持 Range 时大 PDF 会加载失败或体验很差。
        """
        try:
            total = file.stat().st_size
        except OSError as exc:
            return self._send_json({"code": 500, "message": f"读取失败：{exc}"}, 500)

        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
        start, end = 0, total - 1
        partial = False
        if m and total:
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), total - 1)
            elif m.group(2):                       # bytes=-N 取末尾 N 字节
                start = max(0, total - int(m.group(2)))
            if start > end or start >= total:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            partial = True

        try:
            with open(file, "rb") as fh:
                fh.seek(start)
                chunk = fh.read(end - start + 1)
        except OSError as exc:
            return self._send_json({"code": 500, "message": f"读取失败：{exc}"}, 500)

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition",
                         self._content_disposition("inline" if inline else "attachment", file.name))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        try:
            self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_asset(self, name: str) -> None:
        """服务网页存档的**本地资源池**（文件名是原始 URL 的哈希）。

        这些资源是剪藏时就地抓下来的，因此「原版预览」在不联网时也能还原版式，
        浏览过程不会向任何外部站点发请求。
        """
        if not archiver.ASSET_NAME_RE.match(name or ""):
            return self._send_json({"code": 404, "message": "资源不存在"}, 404)
        target = paths.ASSETS_DIR / name
        try:
            target.resolve().relative_to(paths.ASSETS_DIR.resolve())
        except (ValueError, OSError):
            return self._send_json({"code": 403, "message": "越权访问被拒绝"}, 403)
        if not target.is_file():
            return self._send_json({"code": 404, "message": "资源不存在"}, 404)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        # 文件名按 URL 哈希寻址，同一 URL 重新抓取会覆盖内容 → 不做长缓存
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(target.stat().st_size))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(target.read_bytes())
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _send_buffer(self, data: bytes, ctype: str, inline: bool, filename: str) -> None:
        """发送内存中的内容（用于需要就地改写的原件，如注入 <base> 的 HTML）。"""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition",
                         self._content_disposition("inline" if inline else "attachment", filename))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _query(self) -> dict[str, list[str]]:
        parsed = urllib.parse.urlparse(self.path)
        return urllib.parse.parse_qs(parsed.query)

    def query_flag(self, name: str, default: str = "") -> str:
        vals = self._query().get(name) or [default]
        return vals[0]

    def _path_only(self) -> str:
        return urllib.parse.urlparse(self.path).path

    # ------------------------------------------------------------------
    # SSE
    # ------------------------------------------------------------------
    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _sse_write(self, text: str) -> bool:
        payload = text.encode("utf-8")
        try:
            self.wfile.write(f"{len(payload):X}\r\n".encode("ascii") + payload + b"\r\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _sse_end(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------
    def do_OPTIONS(self):  # noqa: N802
        """不提供 CORS 预检：本服务不对外开放跨域访问。"""
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------------
    def _guard(self) -> bool:
        """请求安全闸门。放行返回 True；拒绝时已写好响应并返回 False。

        判据见 ``core/security.py``（Host 校验 + Fetch Metadata + 同源判定）。
        刻意**不要求所有请求都带 Origin** —— 本地 CLI / 诊断工具本来就不发它。
        """
        ok, reason = security_mod.check_request(
            self.command,
            self.headers,
            port=self.server.server_address[1] if self.server.server_address else 0,
            allow_lan=bool(getattr(self.server, "allow_lan", False)),
        )
        if ok:
            return True
        log.warning(
            "已拒绝请求 %s %s：%s（Host=%s Origin=%s Sec-Fetch-Site=%s）",
            self.command, self.path, reason,
            self.headers.get("Host"), self.headers.get("Origin"),
            self.headers.get("Sec-Fetch-Site"),
        )
        self._send_json(
            {"ok": False, "error": {"code": reason,
                                    "message": security_mod.REASON_TEXT.get(reason, "已拒绝"),
                                    "details": {}}},
            403,
        )
        return False

    def do_GET(self):  # noqa: N802
        if not self._guard():
            return
        try:
            self._route_get()
        except Exception as exc:  # noqa: BLE001
            self._fail(exc, "GET")

    def do_POST(self):  # noqa: N802
        if not self._guard():
            return
        try:
            self._route_post()
        except ValueError as exc:
            self._fail(exc, "POST", status=400)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc, "POST")

    def send_response(self, code, message=None):  # noqa: D102
        # 标记「已开始输出响应」，供 _fail 判断能否再补发 JSON 错误
        self._responded = True
        super().send_response(code, message)

    def _fail(self, exc: Exception, method: str, status: int = 500) -> None:
        """统一的异常出口。

        关键：若响应头**已经发出**（例如在 send_header 中途抛错），再补发一个 JSON 响应
        会让 HTTP 协议错乱、客户端一直等到超时。此时只能记日志并断开连接。
        """
        # 异常文本可能来自第三方（上游服务会把 Authorization 原样回显进错误信息），
        # 落日志与回前端**都要先脱敏**。
        log.error("%s %s 异常: %s\n%s", method, self.path,
                  redact_mod.sanitize_text(str(exc)),
                  redact_mod.sanitize_text(traceback.format_exc()))
        if getattr(self, "_responded", False):
            log.error("响应头已发出，无法补发错误响应，直接断开连接")
            self.close_connection = True
            return
        try:
            self._send_json(
                {"code": status,
                 "message": redact_mod.sanitize_text(f"服务内部错误：{exc}")},
                status,
            )
        except Exception:  # noqa: BLE001 - 兜底失败只能静默断开
            self.close_connection = True

    # ------------------------------------------------------------------
    def _route_get(self) -> None:
        path = self._path_only()
        q = self._query()

        if path in ("/", "/index.html"):
            return self._serve_static(paths.WEB_DIR / "index.html")
        if path.startswith("/vendor/"):
            return self._serve_static(paths.VENDOR_DIR / posixpath.basename(path))
        if path.startswith("/static/"):
            return self._serve_static(paths.WEB_DIR / posixpath.basename(path))
        # 拆分后的前端静态资源（Commit B）：app.css / app.js 位于 web 根目录，
        # 可选的领域模块放在 /js/ 下（按 basename 提供，杜绝目录穿越）。
        if path in ("/app.css", "/app.js"):
            return self._serve_static(paths.WEB_DIR / path.lstrip("/"))
        if path.startswith("/js/") and "/" not in path[len("/js/"):]:
            return self._serve_static(paths.WEB_DIR / "js" / posixpath.basename(path))
        if path.startswith(archiver.ASSET_URL_PREFIX):
            return self._serve_asset(path[len(archiver.ASSET_URL_PREFIX):])

        if path == "/healthz":
            return self._send_json({"ok": True, "ts": time.time()})

        # ---- 业务 API：薄分发到各域模块（协议零变更）----
        if api_system.handle_get(self, path):
            return
        if api_config.handle_get(self, path):
            return
        if api_diagnostics.handle_get(self, path):
            return
        if api_search.handle_get(self, path):
            return
        if api_ask.handle_get(self, path):
            return
        if api_library.handle_get(self, path):
            return
        if api_capture.handle_get(self, path):
            return

        return self._send_json({"code": 404, "message": "接口不存在"}, 404)

    # ------------------------------------------------------------------
    def _route_post(self) -> None:
        path = self._path_only()

        # ---- 业务 API：薄分发到各域模块（协议零变更）----
        if api_system.handle_post(self, path):
            return
        if api_config.handle_post(self, path):
            return
        if api_ask.handle_post(self, path):
            return
        if api_library.handle_post(self, path):
            return
        if api_capture.handle_post(self, path):
            return

        return self._send_json({"code": 404, "message": "接口不存在"}, 404)

    def _serve_static(self, file: Path) -> None:
        try:
            file = file.resolve()
            file.relative_to(paths.APP_DIR.resolve())
        except ValueError:
            return self._send_json({"code": 403, "message": "越权访问被拒绝"}, 403)
        if not file.is_file():
            return self._send_json({"code": 404, "message": "资源不存在"}, 404)
        ctype = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        cache = "public, max-age=86400" if file.name.endswith(".min.js") else "no-store"
        try:
            return self._send_bytes(file.read_bytes(), ctype, cache=cache)
        except OSError as exc:
            return self._send_json({"code": 500, "message": f"读取失败：{exc}"}, 500)


# --------------------------------------------------------------------------
class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 上 SO_REUSEADDR 允许抢占已占用端口，必须关闭以让 bind 真实失败
    allow_reuse_address = os.name != "nt"

    def __init__(self, addr, ctx: AppContext, on_shutdown=None, allow_lan: bool = False) -> None:
        super().__init__(addr, Handler)
        self.ctx = ctx
        self.on_shutdown = on_shutdown
        # 未显式开启 LAN 时，Host 校验只接受回环地址（防 DNS rebinding）
        self.allow_lan = bool(allow_lan)
        self._shutdown_started = threading.Event()
        Handler.ctx = ctx

    def request_shutdown(self) -> None:
        if self._shutdown_started.is_set():
            return
        self._shutdown_started.set()
        log.info("收到安全退出请求")
        threading.Thread(target=self._graceful, name="graceful-shutdown", daemon=True).start()

    def _graceful(self) -> None:
        time.sleep(0.1)
        try:
            if self.on_shutdown:
                self.on_shutdown()
        finally:
            self.shutdown()
            self.server_close()

    def serve_in_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.3},
                             name="http-server", daemon=True)
        t.start()
        return t
