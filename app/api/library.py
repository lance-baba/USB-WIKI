"""笔记库 / 文件 / 原件 / 资产类 API。

涵盖 /api/notes（列表）、/api/notes/content（Markdown 正文 + 原件信息）、
/api/notes/original（导入的 PDF/Office、剪藏 HTML 原件，含 HTTP Range 与沙箱回退）、
/api/notes/evidence（引用精确跳转：按 path+parent_id 返回证据块）、
/api/notes/save（手动笔记）、/api/notes/import（批量导入）、/api/import/formats
（支持的导入扩展名）。

只处理业务，响应统一走 Handler 的 response 原语；不直接碰底层 HTTP 写入，也不做
安全校验 / 密钥脱敏。源文件路径校验（防穿越）属于资源级业务校验，保留在模块内。
"""
from __future__ import annotations

import base64
import mimetypes
from typing import TYPE_CHECKING

from ..core import archiver, converters, crawler, paths, search as search_mod

if TYPE_CHECKING:
    from ..server import Handler


def list_notes(h: "Handler", q: dict) -> None:
    limit = int((q.get("limit") or ["200"])[0])
    h._send_json({"code": 200, "data": search_mod.rank_documents(h.ctx.db, limit=limit)})


def note_content(h: "Handler", rel: str) -> None:
    if not rel:
        h._send_json({"code": 400, "message": "缺少 path 参数"}, 400)
        return
    try:
        target = paths.abs_from_data(rel)
    except Exception:  # noqa: BLE001
        h._send_json({"code": 400, "message": "非法路径"}, 400)
        return
    # 防路径穿越：必须落在 data/ 之下
    try:
        target.resolve().relative_to(paths.DATA_DIR.resolve())
    except ValueError:
        h._send_json({"code": 403, "message": "越权访问被拒绝"}, 403)
        return
    if not target.is_file():
        h._send_json({"code": 404, "message": "文件不存在"}, 404)
        return
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        h._send_json({"code": 500, "message": f"读取失败：{exc}"}, 500)
        return

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
            "inline": ext in h.INLINE_TYPES,
            "kind": {"pdf": "pdf", ".html": "html", ".htm": "html"}.get(ext, ext.lstrip(".")),
        }
    h._send_json(
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


def note_original(h: "Handler", rel: str) -> None:
    """返回笔记对应的**原件**（导入的 PDF/Office、剪藏的原始 HTML）。"""
    if not rel:
        h._send_json({"code": 400, "message": "缺少 path 参数"}, 400)
        return
    try:
        orig = crawler.find_original(rel)
    except Exception:  # noqa: BLE001
        orig = None
    if orig is None:
        h._send_json({"code": 404, "message": "该笔记没有留存原件"}, 404)
        return
    # 二次防线：解析后必须仍在 data/originals 之下
    try:
        orig.resolve().relative_to(paths.ORIGINALS_DIR.resolve())
    except (ValueError, OSError):
        h._send_json({"code": 403, "message": "越权访问被拒绝"}, 403)
        return

    ext = orig.suffix.lower()
    ctype = h.INLINE_TYPES.get(ext) or mimetypes.guess_type(str(orig))[0] \
        or "application/octet-stream"
    if ctype.startswith("text/"):
        ctype += "; charset=utf-8"
    inline = (h.query_flag("download") != "1") and ext in h.INLINE_TYPES

    # 剪藏的原网页：判定它是「离线存档」还是「旧存档」。
    # - 已本地化（HTML 里引用了本地资源池）：把资源**内联成 data: URI** 再下发。
    #   因为快照在 <iframe sandbox=""> 里渲染 → iframe 是不透明源 → 请求
    #   /api/assets/... 会带 Sec-Fetch-Site: cross-site，被跨站闸门拒（图片/样式全 404）。
    #   内联后页面自包含、零子请求，既不触发闸门也真正离线（B 方案）。
    # - 未本地化（旧存档）：保持原有的联网行为，注入 <base> 避免退化成裸 HTML。
    if inline and ext in (".html", ".htm"):
        try:
            text = orig.read_text(encoding="utf-8", errors="replace")
            if archiver.ASSET_URL_PREFIX in text:
                text = archiver.inline_assets(text)
            else:
                text = crawler.inject_base_href(text, crawler.source_url_of(rel))
            h._send_buffer(text.encode("utf-8"), ctype, True, orig.name)
            return
        except OSError:
            pass   # 读失败则退回按原样流式发送

    h._send_file_range(orig, ctype, inline)


def note_evidence(h: "Handler", rel: str, parent_id: str) -> None:
    """引用精确跳转（B）：按 path + parent_id 返回证据块的定位信息。

    返回 ``section_path``（章节路径，供展示）与 ``content``（父块原文，供前端在
    渲染视图里按文本定位）。前端优先用 parent_id 建立稳定定位（B3），本接口就是
    它的数据源；snippet 只作为兜底，不做「全文搜第一个相同句子」式的脆弱定位。
    """
    if not rel or not parent_id:
        h._send_json({"code": 400, "message": "缺少 path 或 parent_id 参数"}, 400)
        return
    db = h.ctx.db
    from ..core import search as _search  # noqa: PLC0415 - 复用列存在性探测

    has_src = _search._has_source_lines(db)
    src_cols = (", COALESCE(pb.source_start_line,0) AS s_line, "
                "COALESCE(pb.source_end_line,0) AS e_line") if has_src else                (", 0 AS s_line, 0 AS e_line")
    row = db.query_one(
        """SELECT pb.content, pb.section_path, d.rel_path""" + src_cols + """
           FROM parent_blocks pb JOIN documents d ON d.doc_id = pb.doc_id
           WHERE pb.parent_id = ?""",
        (parent_id,),
    )
    if row is None:
        h._send_json({"code": 404, "message": "证据块不存在（索引可能已重建）"}, 404)
        return
    if row["rel_path"] != rel:
        # 证据块不属于该笔记 —— 防止跨文档错位引用
        h._send_json({"code": 404, "message": "证据块与该笔记不匹配"}, 404)
        return
    h._send_json(
        {
            "code": 200,
            "data": {
                "parent_id": parent_id,
                "section_path": row["section_path"] or "",
                "content": row["content"] or "",
                # A: stable anchor —— 源行范围（0 表示老索引没有）
                "source_start_line": row["s_line"] or 0,
                "source_end_line": row["e_line"] or 0,
            },
        }
    )


def save_note(h: "Handler") -> None:
    body = h._read_json()
    result = crawler.save_manual_note(
        str(body.get("title") or ""),
        str(body.get("content") or ""),
        db=h.ctx.db,
        embedder=h.ctx.embedder,
    )
    h._send_json(result.to_dict(), 200 if result.ok else 400)


def import_notes(h: "Handler") -> None:
    """批量导入文件（拖拽上传落点）。

    载荷支持两种编码：
    * ``content``          纯文本（.md/.txt 等文本类格式）
    * ``content_base64``   二进制（.docx/.pdf/.xlsx 等），前端按需选用
    """
    body = h._read_json()
    items = body.get("files")
    if not isinstance(items, list) or not items:
        items = [{
            "filename": body.get("filename"),
            "content": body.get("content"),
            "content_base64": body.get("content_base64"),
        }]

    if len(items) > 200:
        h._send_json({"code": 400, "message": "单次最多导入 200 个文件"}, 400)
        return

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
            fname, data, db=h.ctx.db, embedder=h.ctx.embedder
        )
        results.append({
            "ok": r.ok, "filename": fname, "message": r.message,
            "file_path": r.file_path, "title": r.title, "char_count": r.char_count,
            "extractor": r.used,
        })

    ok_n = sum(1 for r in results if r["ok"])
    h._send_json({
        "code": 200,
        "message": f"导入完成：成功 {ok_n} / {len(results)}",
        "data": {"results": results, "ok": ok_n, "total": len(results)},
    })


def import_formats(h: "Handler") -> None:
    h._send_json({"code": 200, "data": converters.supported_extensions()})


def handle_get(h: "Handler", path: str) -> bool:
    q = h._query()
    if path == "/api/notes":
        list_notes(h, q)
        return True
    if path == "/api/notes/content":
        note_content(h, (q.get("path") or [""])[0])
        return True
    if path == "/api/notes/original":
        note_original(h, (q.get("path") or [""])[0])
        return True
    if path == "/api/notes/evidence":
        note_evidence(h, (q.get("path") or [""])[0], (q.get("parent_id") or [""])[0])
        return True
    if path == "/api/import/formats":
        import_formats(h)
        return True
    return False


def handle_post(h: "Handler", path: str) -> bool:
    if path == "/api/notes/save":
        save_note(h)
        return True
    if path == "/api/notes/import":
        import_notes(h)
        return True
    return False
