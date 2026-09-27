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

Collections 只做组织 / 浏览，**完全不影响**检索与问答（默认全局）。
只处理业务，响应统一走 Handler 的 response 原语（与 library.py 同约定）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..core import chunker
from ..core import collections as collections_mod

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
    return False
