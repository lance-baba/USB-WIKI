"""Collections（资料集）与文档组织 API。

端点：
  GET  /api/collections            列出类目 + 成员关系 + 显示标题 + 回收站
  POST /api/collections/create     新建类目        {name}
  POST /api/collections/rename     重命名类目      {id, name}
  POST /api/collections/delete     删除类目        {id}          —— 不删文档
  POST /api/collections/assign     设置文档类目集合 {doc_id|path, collection_ids:[...]}
  POST /api/notes/display_title    改显示标题      {doc_id|path, title}
  POST /api/notes/trash            移入回收站      {doc_id|path}
  POST /api/notes/restore          从回收站恢复    {doc_id|path}
  POST /api/notes/purge            永久删除        {doc_id|path, delete_originals?:bool}
                                                  —— 不可逆；默认只删 Markdown + 索引，
                                                     原件须显式勾选才删

Collections 只做组织 / 浏览，**完全不影响**检索与问答（默认全局）。
只处理业务，响应统一走 Handler 的 response 原语（与 library.py 同约定）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..core import chunker
from ..core import collections as collections_mod
from ..core import crawler, indexer, paths

if TYPE_CHECKING:
    from ..server import Handler


def _doc_id(body: dict) -> str:
    """接受 doc_id，或 path（自动换算 ``chunker.doc_id_for``）。"""
    did = str(body.get("doc_id") or "").strip()
    if did:
        return did
    rel = str(body.get("path") or "").strip()
    if rel:
        return chunker.doc_id_for(rel)
    return ""


def _fail(h: "Handler", exc: Exception) -> None:
    """把 core 层异常映射为 HTTP 语义（不暴露堆栈）。"""
    if isinstance(exc, collections_mod.StoreCorruptedError):
        h._send_json({"code": 500, "message": f"类目数据损坏，未做任何修改（{exc}）"}, 500)
    elif isinstance(exc, (collections_mod.CollectionNotFound, KeyError)):
        h._send_json({"code": 404, "message": "类目不存在"}, 404)
    else:
        h._send_json({"code": 400, "message": str(exc)}, 400)


def list_collections(h: "Handler") -> None:
    try:
        h._send_json({"code": 200, "data": collections_mod.list_all()})
    except Exception as exc:  # noqa: BLE001
        _fail(h, exc)


def create_collection(h: "Handler") -> None:
    body = h._read_json()
    try:
        item = collections_mod.create_collection(str(body.get("name") or ""))
        h._send_json({"code": 200, "message": "已创建类目", "data": item})
    except Exception as exc:  # noqa: BLE001
        _fail(h, exc)


def rename_collection(h: "Handler") -> None:
    body = h._read_json()
    try:
        item = collections_mod.rename_collection(
            str(body.get("id") or ""), str(body.get("name") or ""))
        h._send_json({"code": 200, "message": "已重命名", "data": item})
    except Exception as exc:  # noqa: BLE001
        _fail(h, exc)


def delete_collection(h: "Handler") -> None:
    body = h._read_json()
    try:
        collections_mod.delete_collection(str(body.get("id") or ""))
        h._send_json({"code": 200, "message": "已删除类目（文档不受影响）"})
    except Exception as exc:  # noqa: BLE001
        _fail(h, exc)


def assign_collections(h: "Handler") -> None:
    body = h._read_json()
    try:
        did = _doc_id(body)
        if not did:
            raise ValueError("缺少 doc_id 或 path")
        ids = body.get("collection_ids")
        if not isinstance(ids, list):
            ids = []
        result = collections_mod.assign(did, [str(x) for x in ids])
        h._send_json({"code": 200, "message": "已更新分类", "data": result})
    except Exception as exc:  # noqa: BLE001
        _fail(h, exc)


def set_display_title(h: "Handler") -> None:
    body = h._read_json()
    try:
        did = _doc_id(body)
        if not did:
            raise ValueError("缺少 doc_id 或 path")
        title = collections_mod.set_display_title(did, str(body.get("title") or ""))
        h._send_json({"code": 200, "message": "已更新标题",
                      "data": {"doc_id": did, "title": title}})
    except Exception as exc:  # noqa: BLE001
        _fail(h, exc)


def trash_note(h: "Handler") -> None:
    body = h._read_json()
    try:
        did = _doc_id(body)
        if not did:
            raise ValueError("缺少 doc_id 或 path")
        collections_mod.trash_doc(did)
        h._send_json({"code": 200, "message": "已移入回收站"})
    except Exception as exc:  # noqa: BLE001
        _fail(h, exc)


def purge_note(h: "Handler") -> None:
    """永久删除笔记（**不可逆**）—— 回收站的最后一站。

    分两级 capabilities，避免替用户销毁无法再生的数据：
      * 默认删 **Markdown 真相源 + 派生索引**（索引可全量重建，删除不会丢积木）；
      * 原件（ originals/ ）**只在调用方显式勾 delete_originals 时**才删。

    为什么原件要显式勾选：既有政策是「孤儿原件只报告、不自动删」
    （``crawler.find_orphan_originals`` 只列清单）。永久删除是 UI 上的不可逆动作，
    要不要连原件一起销毁必须让用户当场拍板。
    """
    body = h._read_json()
    try:
        did = _doc_id(body)
        if not did:
            raise ValueError("缺少 doc_id 或 path")
        also_originals = bool(body.get("delete_originals"))

        # rel_path 优先用请求里给的；没有再回查索引（前端往往只带 doc_id）
        rel = str(body.get("path") or "").strip()
        if not rel:
            row = h.ctx.db.query_one(
                "SELECT rel_path FROM documents WHERE doc_id = ?", (did,))
            rel = str(row["rel_path"]) if row else ""
        if not rel:
            raise ValueError("找不到该文档的笔记路径（可能已被清理）")

        # ⚠ rel 是**相对 DATA_DIR** 的路径（形如 notes/xxx.md，见 paths.rel_to_data），
        #   回绝对路径必须用 DATA_DIR 拼 —— 拿 NOTES_DIR 拼会变成 notes/notes/xxx.md，
        #   结果文件根本没删、索引却清了（真机踩过）。
        # 安全底线：解析后必须仍在 notes 目录下（防 ../ 越权删本机任意文件）
        note = (paths.DATA_DIR / rel).resolve()
        try:
            note.relative_to(paths.NOTES_DIR.resolve())
        except ValueError:
            raise ValueError("越权访问被拒绝")

        note_bytes = 0
        if note.is_file():
            note_bytes = note.stat().st_size
            note.unlink()

        purged_chunks = indexer.purge_by_rel_path(h.ctx.db, rel)

        orig_removed = 0
        orig_bytes = 0
        if also_originals:
            orig = crawler.find_original(rel)
            if orig is not None and orig.is_file():
                try:
                    orig.resolve().relative_to(paths.ORIGINALS_DIR.resolve())
                except ValueError:
                    raise ValueError("原件路径越权访问被拒绝")
                orig_bytes = orig.stat().st_size
                orig.unlink()
                orig_removed = 1

        collections_mod.purge_doc(did)      # membership / display_titles / trash 一次抹净
        h._send_json({"code": 200, "message": "已永久删除",
                      "data": {"doc_id": did, "rel_path": rel,
                               "note_bytes": note_bytes,
                               "purged_chunks": purged_chunks,
                               "originals_removed": orig_removed,
                               "original_bytes": orig_bytes}})
    except Exception as exc:  # noqa: BLE001
        _fail(h, exc)


def restore_note(h: "Handler") -> None:
    body = h._read_json()
    try:
        did = _doc_id(body)
        if not did:
            raise ValueError("缺少 doc_id 或 path")
        collections_mod.restore_doc(did)
        h._send_json({"code": 200, "message": "已恢复"})
    except Exception as exc:  # noqa: BLE001
        _fail(h, exc)


def handle_get(h: "Handler", path: str) -> bool:
    if path == "/api/collections":
        list_collections(h)
        return True
    return False


def handle_post(h: "Handler", path: str) -> bool:
    if path == "/api/collections/create":
        create_collection(h)
        return True
    if path == "/api/collections/rename":
        rename_collection(h)
        return True
    if path == "/api/collections/delete":
        delete_collection(h)
        return True
    if path == "/api/collections/assign":
        assign_collections(h)
        return True
    if path == "/api/notes/display_title":
        set_display_title(h)
        return True
    if path == "/api/notes/trash":
        trash_note(h)
        return True
    if path == "/api/notes/restore":
        restore_note(h)
        return True
    if path == "/api/notes/purge":
        purge_note(h)
        return True
    return False
