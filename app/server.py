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

from .core import (archiver, config, crawler, graph as graph_mod, paths,
                   redact as redact_mod, search as search_mod,
                   security as security_mod,
                   topics as topics_mod)
from .core.context import AppContext, get_ctx
from .core.log_util import get_logger

log = get_logger()

MAX_BODY = 4 * 1024 * 1024  # 4MB 请求体上限


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "WikiUSB/1.2"
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
        if path.startswith(archiver.ASSET_URL_PREFIX):
            return self._serve_asset(path[len(archiver.ASSET_URL_PREFIX):])

        if path == "/healthz":
            return self._send_json({"ok": True, "ts": time.time()})

        if path == "/api/status":
            return self._send_json({"code": 200, "data": self.ctx.status()})

        if path == "/api/notes":
            limit = int((q.get("limit") or ["200"])[0])
            return self._send_json(
                {"code": 200, "data": search_mod.rank_documents(self.ctx.db, limit=limit)}
            )

        if path == "/api/notes/content":
            rel = (q.get("path") or [""])[0]
            return self._note_content(rel)

        if path == "/api/notes/original":
            rel = (q.get("path") or [""])[0]
            return self._note_original(rel)

        # 前端在抓取前先问一次「这个网页是不是已经存过」，
        # 以便弹出「打开已有 / 更新已有 / 另存为新版本 / 取消」。
        # 注意：后端在 capture 时**自己也会再查一次** —— 前端判断不可信。
        if path == "/api/capture/duplicate":
            target = (q.get("url") or [""])[0]
            from .core import indexer as _indexer  # noqa: PLC0415

            found = _indexer.find_by_normalized_url(self.ctx.db, target)
            return self._send_json(
                {"ok": True, "data": {"duplicate": bool(found), "existing": found or None}}
            )

        if path == "/api/topics":
            try:
                min_docs = int((q.get("min_docs") or ["2"])[0])
            except ValueError:
                min_docs = 2
            data = topics_mod.build_topics(self.ctx.db, min_docs=max(1, min(20, min_docs)))
            return self._send_json({"code": 200, "data": data})

        if path == "/api/graph":
            thr = float((q.get("threshold") or [str(config.get_float("GRAPH", "semantic_threshold", 0.82))])[0])
            thr = max(0.0, min(1.0, thr))
            term_thr = float(
                (q.get("term_threshold") or [str(config.get_float("GRAPH", "term_threshold", 0.10))])[0]
            )
            term_thr = max(0.0, min(1.0, term_thr))
            # 向量边默认关闭：它依赖嵌入源，且会引入「语义相近但无关」的噪声边。
            # 需要时用 ?use_vectors=1 显式打开。
            use_vec = (q.get("use_vectors") or ["0"])[0] == "1"
            g = graph_mod.build_graph(
                self.ctx.db, threshold=thr, term_threshold=term_thr, use_vectors=use_vec
            )
            return self._send_json({"code": 200, "data": g})

        if path == "/api/search":
            query = (q.get("q") or [""])[0]
            top_k = int((q.get("top_k") or ["5"])[0])
            res = search_mod.hybrid_search(self.ctx.db, self.ctx.embedder, query, top_k_parents=top_k)
            return self._send_json(
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

        if path == "/api/config":
            # ⚠ 此前这里直接返回 config.as_dict() —— 把**明文 api_key**
            # 通过 HTTP 发出去了。设置页读取只能拿到脱敏值（sk-****abcd），
            # 是否已配置用单独的 *_set 字段表达。
            masked = redact_mod.mask_config(config.as_dict())
            for _sec, _items in masked.items():
                if isinstance(_items, dict):
                    for _k in list(_items):
                        if redact_mod.is_secret_key(_k):
                            _items[f"{_k}_set"] = bool(
                                config.get_str(_sec, _k, "")
                            )
            return self._send_json({"code": 200, "data": masked})

        if path == "/api/import/formats":
            from .core import converters  # noqa: PLC0415

            return self._send_json({"code": 200, "data": converters.supported_extensions()})

        if path == "/api/ollama/models":
            host = (q.get("host") or [""])[0] or None
            if self.ctx.gateway is None:
                return self._send_json(
                    {"code": 503, "message": "服务尚未初始化完成", "data": {"available": False, "models": []}}
                )
            info = self.ctx.gateway.list_ollama_models(host)
            return self._send_json({"code": 200, "data": info})

        return self._send_json({"code": 404, "message": "接口不存在"}, 404)

    # ------------------------------------------------------------------
    def _route_post(self) -> None:
        path = self._path_only()

        if path == "/api/chat/completions":
            return self._chat()

        if path == "/api/capture/url":
            body = self._read_json()
            url = str(body.get("url") or "").strip()
            # on_duplicate: abort(默认) / update / new —— 由用户在弹窗里选择后带上。
            # 不给就按 abort 处理：宁可让调用方显式表态，也不默默产生重复笔记。
            on_dup = str(body.get("on_duplicate") or "abort").strip().lower()
            if on_dup not in ("abort", "update", "new"):
                on_dup = "abort"
            result = crawler.capture_url(
                url, db=self.ctx.db, embedder=self.ctx.embedder, on_duplicate=on_dup
            )
            if result.status == "duplicate":
                # 不是服务端错误，而是一个**需要用户决策**的正常状态
                return self._send_json(result.to_dict(), 409)
            return self._send_json(result.to_dict(), 200 if result.ok else 500)

        if path == "/api/notes/save":
            body = self._read_json()
            result = crawler.save_manual_note(
                str(body.get("title") or ""),
                str(body.get("content") or ""),
                db=self.ctx.db,
                embedder=self.ctx.embedder,
            )
            return self._send_json(result.to_dict(), 200 if result.ok else 400)

        if path == "/api/notes/import":
            return self._import_notes()

        if path == "/api/system/rebuild-index":
            self._read_json()
            threading.Thread(
                target=self._rebuild_worker, name="rebuild-index", daemon=True
            ).start()
            return self._send_json(
                {"code": 202, "message": "索引全量重建任务已在后台启动"}
            )

        if path == "/api/system/rebuild-vectors":
            self._read_json()
            threading.Thread(
                target=self._rebuild_worker, kwargs={"recreate_vec": True},
                name="rebuild-vectors", daemon=True,
            ).start()
            return self._send_json({"code": 202, "message": "向量索引重建任务已在后台启动"})

        if path == "/api/system/shutdown":
            self._read_json()
            self._send_json({"code": 200, "message": "正在安全退出，请稍候…"})
            threading.Timer(0.3, self.server.request_shutdown).start()  # type: ignore[attr-defined]
            return

        if path == "/api/config":
            body = self._read_json()
            updated = config.update(body.get("data") or body, persist=True)
            return self._send_json({"code": 200, "message": "配置已保存", "data": updated})

        if path == "/api/ai/test":
            body = self._read_json()
            target = str(body.get("target") or "ollama").lower()
            if self.ctx.gateway is None:
                return self._send_json({"code": 503, "message": "服务尚未初始化完成"}, 503)
            if target == "ollama":
                result = self.ctx.gateway.test_ollama(str(body.get("model") or "") or None)
            elif target == "api":
                result = self.ctx.gateway.test_api()
            else:
                return self._send_json({"code": 400, "message": "target 仅支持 ollama / api"}, 400)
            return self._send_json({"code": 200 if result.get("ok") else 200, "data": result})

        return self._send_json({"code": 404, "message": "接口不存在"}, 404)

    # ------------------------------------------------------------------
    def _chat(self) -> None:
        body = self._read_json()
        query = str(body.get("query") or "").strip()
        history = body.get("history")
        if not isinstance(history, list):
            history = []

        self._sse_start()
        try:
            for frame in self.ctx.gateway.stream_chat(query, history):
                if not self._sse_write(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"):
                    break
        except Exception as exc:  # noqa: BLE001
            log.error("SSE 推流异常: %s", exc)
            self._sse_write(
                f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
            )
        finally:
            self._sse_end()

    def _rebuild_worker(self, recreate_vec: bool = False) -> None:
        try:
            report = self.ctx.rebuild_index(recreate_vec=recreate_vec)
            log.info("重建任务完成: %s", report)
        except Exception as exc:  # noqa: BLE001
            log.error("重建任务异常: %s", exc)

    def _import_notes(self) -> None:
        """批量导入文件（拖拽上传落点）。

        载荷支持两种编码：
        * ``content``          纯文本（.md/.txt 等文本类格式）
        * ``content_base64``   二进制（.docx/.pdf/.xlsx 等），前端按需选用
        """
        import base64

        body = self._read_json()
        items = body.get("files")
        if not isinstance(items, list) or not items:
            items = [{
                "filename": body.get("filename"),
                "content": body.get("content"),
                "content_base64": body.get("content_base64"),
            }]

        if len(items) > 200:
            return self._send_json({"code": 400, "message": "单次最多导入 200 个文件"}, 400)

        results = []
        for it in items:
            if not isinstance(it, dict):
                continue
            fname = str(it.get("filename") or "")
            b64 = it.get("content_base64")
            if isinstance(b64, str) and b64.strip():
                try:
                    data = base64.b64decode(b64, validate=False)
                except Exception as exc:  # noqa: BLE001
                    results.append({"ok": False, "filename": fname,
                                    "message": f"base64 解码失败：{exc}", "file_path": "",
                                    "title": "", "char_count": 0})
                    continue
            else:
                data = str(it.get("content") or "").encode("utf-8")

            r = crawler.import_document(
                fname, data, db=self.ctx.db, embedder=self.ctx.embedder
            )
            results.append({
                "ok": r.ok, "filename": fname, "message": r.message,
                "file_path": r.file_path, "title": r.title, "char_count": r.char_count,
                "extractor": r.used,
            })

        ok_n = sum(1 for r in results if r["ok"])
        return self._send_json({
            "code": 200,
            "message": f"导入完成：成功 {ok_n} / {len(results)}",
            "data": {"results": results, "ok": ok_n, "total": len(results)},
        })

    def _note_content(self, rel: str) -> None:
        if not rel:
            return self._send_json({"code": 400, "message": "缺少 path 参数"}, 400)
        try:
            target = paths.abs_from_data(rel)
        except Exception:  # noqa: BLE001
            return self._send_json({"code": 400, "message": "非法路径"}, 400)
        # 防路径穿越：必须落在 data/ 之下
        try:
            target.resolve().relative_to(paths.DATA_DIR.resolve())
        except ValueError:
            return self._send_json({"code": 403, "message": "越权访问被拒绝"}, 403)
        if not target.is_file():
            return self._send_json({"code": 404, "message": "文件不存在"}, 404)
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return self._send_json({"code": 500, "message": f"读取失败：{exc}"}, 500)

        # 附带原件信息，供前端「原版预览」决定用原生查看器还是沙箱 iframe
        orig = crawler.find_original(rel)
        original = None
        if orig is not None:
            ext = orig.suffix.lower()
            try:
                size = orig.stat().st_size
            except OSError:
                size = 0
            original = {
                "path": paths.rel_to_data(orig),
                "name": orig.name,
                "ext": ext,
                "size": size,
                "inline": ext in self.INLINE_TYPES,
                "kind": {"pdf": "pdf", ".html": "html", ".htm": "html"}.get(ext, ext.lstrip(".")),
            }
        return self._send_json(
            {
                "code": 200,
                "data": {
                    "path": rel,
                    "content": text,
                    "size": target.stat().st_size,
                    "original": original,
                },
            }
        )

    def _note_original(self, rel: str) -> None:
        """返回笔记对应的**原件**（导入的 PDF/Office、剪藏的原始 HTML）。"""
        if not rel:
            return self._send_json({"code": 400, "message": "缺少 path 参数"}, 400)
        try:
            orig = crawler.find_original(rel)
        except Exception:  # noqa: BLE001
            orig = None
        if orig is None:
            return self._send_json({"code": 404, "message": "该笔记没有留存原件"}, 404)
        # 二次防线：解析后必须仍在 data/originals 之下
        try:
            orig.resolve().relative_to(paths.ORIGINALS_DIR.resolve())
        except (ValueError, OSError):
            return self._send_json({"code": 403, "message": "越权访问被拒绝"}, 403)

        ext = orig.suffix.lower()
        ctype = self.INLINE_TYPES.get(ext) or mimetypes.guess_type(str(orig))[0] \
            or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        inline = (self.query_flag("download") != "1") and ext in self.INLINE_TYPES

        # 剪藏的原网页：判定它是「离线存档」还是「旧存档」。
        # - 已本地化（HTML 里引用了本地资源池）：**不注入 base** ——
        #   根相对路径天然指向本服务，浏览时零外部请求，真正离线可用。
        # - 未本地化（旧存档）：保持原有的联网行为，避免存量页面退化成裸 HTML。
        if inline and ext in (".html", ".htm"):
            try:
                text = orig.read_text(encoding="utf-8", errors="replace")
                if archiver.ASSET_URL_PREFIX not in text:
                    text = crawler.inject_base_href(text, crawler.source_url_of(rel))
                return self._send_buffer(text.encode("utf-8"), ctype, True, orig.name)
            except OSError:
                pass   # 读失败则退回按原样流式发送

        return self._send_file_range(orig, ctype, inline)

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
