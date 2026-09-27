r"""Collections（资料集）—— 用户对文档的组织层（durable 用户状态）。

设计依据：``docs/COLLECTIONS_ARCHITECTURE.md``（架构冻结）。
一句话：**Collections organize knowledge. Retrieval finds knowledge.**

本模块职责（**只管用户组织，不碰检索**）：

* 类目（Collection）的增 / 改 / 删；
* 文档 ↔ 类目的多对多成员关系（membership）；
* 文档的**显示标题**覆盖（只存此处，**不改** ``notes/*.md`` 文件）；
* 软删除标记（回收站）：删除 ≠ 物理删除，文件保留、可恢复。

存储：``<Library>/metadata/collections.json``（开放 JSON，原子写）。
经 ``atomic_io`` 落盘 → 断电 / 拔盘不会写出半截文件。

## 不变量（与 ADR 一致）

1. 默认检索仍全局；本模块**不参与** search / ask / indexer。
2. 删除类目**绝不删除文档**（只清 membership）。
3. 用户组织状态属 durable 数据，只存这里（可重建的 ``cache.db`` 不放）。
4. ``doc_id`` 由 ``chunker.doc_id_for(rel_path)`` 决定（现有最可用身份）；
   文档改名 / 移动会使成员关系失效 —— 失效项**只保留、不自动清理**。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from . import paths
from .atomic_io import atomic_write_text

SCHEMA_VERSION = 1
STORE_NAME = "collections.json"


class StoreCorruptedError(RuntimeError):
    """``collections.json`` 存在但无法解析。"""


class CollectionNotFound(KeyError):
    """引用了不存在的类目 id。"""


def store_path() -> Path:
    """``<Library>/metadata/collections.json``。"""
    return paths.METADATA_DIR / STORE_NAME


def _empty_store() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "collections": [],     # [{"id", "name", "created_at"}]
        "membership": {},      # {doc_id: [collection_id, ...]}  —— 多对多
        "display_titles": {},  # {doc_id: "自定义显示标题"}
        "trash": [],           # [doc_id, ...]  软删除（回收站）
    }


def _normalize(data: object) -> dict:
    """把读入的 JSON 归一化成合法结构（缺字段补默认，类型错则丢弃该项）。

    只保证顶层字段齐全、类型正确；**不改写**已有条目的语义。
    """
    store = _empty_store()
    if not isinstance(data, dict):
        return store
    cols = data.get("collections")
    if isinstance(cols, list):
        store["collections"] = [
            {
                "id": str(c.get("id")),
                "name": str(c.get("name")),
                "created_at": int(c.get("created_at") or 0),
            }
            for c in cols
            if isinstance(c, dict) and c.get("id") and c.get("name")
        ]
    mem = data.get("membership")
    if isinstance(mem, dict):
        store["membership"] = {
            str(k): [str(x) for x in v]
            for k, v in mem.items()
            if isinstance(v, list)
        }
    titles = data.get("display_titles")
    if isinstance(titles, dict):
        store["display_titles"] = {str(k): str(v) for k, v in titles.items()}
    trash = data.get("trash")
    if isinstance(trash, list):
        store["trash"] = [str(x) for x in trash]
    ver = data.get("schema_version")
    if isinstance(ver, int):
        store["schema_version"] = ver
    return store


def load_store() -> dict:
    """读取 store。

    * 文件不存在 → 返回空结构（正常：新库还没建过类目）。
    * 文件存在但**无法解析** → 抛 :class:`StoreCorruptedError`
      （**不静默当空库**，否则会掩盖「用户类目丢了」这一事实）。
    """
    p = store_path()
    if not p.is_file():
        return _empty_store()
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError) as exc:  # noqa: BLE001
        raise StoreCorruptedError(str(exc)) from exc
    return _normalize(data)


def save_store(data: dict) -> None:
    """原子写回 store（同目录临时文件 → fsync → os.replace）。"""
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(store_path(), text, newline="")


# --------------------------------------------------------------------------- #
# 类目 CRUD
# --------------------------------------------------------------------------- #
def list_all() -> dict:
    """返回完整 store（前端一次性拉取后本地 join / 筛选）。"""
    return load_store()


def create_collection(name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("类目名不能为空")
    data = load_store()
    item = {"id": uuid.uuid4().hex[:8], "name": name, "created_at": int(time.time())}
    data["collections"].append(item)
    save_store(data)
    return item


def rename_collection(collection_id: str, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("类目名不能为空")
    data = load_store()
    for c in data["collections"]:
        if c["id"] == collection_id:
            c["name"] = name
            save_store(data)
            return c
    raise CollectionNotFound(collection_id)


def delete_collection(collection_id: str) -> None:
    """删除类目 —— **只清 membership，绝不删除任何文档**。"""
    data = load_store()
    before = len(data["collections"])
    data["collections"] = [c for c in data["collections"] if c["id"] != collection_id]
    if len(data["collections"]) == before:
        raise CollectionNotFound(collection_id)
    for doc_id, cols in list(data["membership"].items()):
        kept = [x for x in cols if x != collection_id]
        if kept:
            data["membership"][doc_id] = kept
        else:
            data["membership"].pop(doc_id, None)
    save_store(data)


# --------------------------------------------------------------------------- #
# 成员关系（多对多）
# --------------------------------------------------------------------------- #
def assign(doc_id: str, collection_ids: list[str]) -> dict:
    """把文档的类目集合整体设为 ``collection_ids``（去重、过滤不存在的 id）。"""
    if not doc_id:
        raise ValueError("缺少 doc_id")
    data = load_store()
    valid = {c["id"] for c in data["collections"]}
    cols = [x for x in dict.fromkeys(collection_ids or []) if x in valid]
    if cols:
        data["membership"][doc_id] = cols
    else:
        data["membership"].pop(doc_id, None)
    save_store(data)
    return {"doc_id": doc_id, "collection_ids": cols}


def members_of(collection_id: str) -> list[str]:
    data = load_store()
    return [d for d, cols in data["membership"].items() if collection_id in cols]


def collections_of(doc_id: str) -> list[str]:
    return list(load_store()["membership"].get(doc_id, []))


# --------------------------------------------------------------------------- #
# 显示标题（只存此处，不改文件）
# --------------------------------------------------------------------------- #
def set_display_title(doc_id: str, title: str) -> str:
    if not doc_id:
        raise ValueError("缺少 doc_id")
    title = (title or "").strip()
    data = load_store()
    if title:
        data["display_titles"][doc_id] = title
    else:
        data["display_titles"].pop(doc_id, None)
    save_store(data)
    return title


# --------------------------------------------------------------------------- #
# 软删除 / 回收站（删除 ≠ 物理删除）
# --------------------------------------------------------------------------- #
def trash_doc(doc_id: str) -> None:
    if not doc_id:
        raise ValueError("缺少 doc_id")
    data = load_store()
    if doc_id not in data["trash"]:
        data["trash"].append(doc_id)
    save_store(data)


def restore_doc(doc_id: str) -> None:
    data = load_store()
    data["trash"] = [x for x in data["trash"] if x != doc_id]
    save_store(data)


def is_trashed(doc_id: str) -> bool:
    return doc_id in load_store()["trash"]
