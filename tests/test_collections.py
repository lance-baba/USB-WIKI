"""Collections（资料集）—— 用户组织层验收（durable metadata / 多对多 / 软删）。

覆盖：
  数据    <Library>/metadata/collections.json 落点；schema_version；原子写
  类目    新建 / 重命名 / 删除（**删类目不删文档**）
  组织    一篇多分类（many-to-many）；assign 去重 + 过滤不存在的 id
  标题    显示标题只存 metadata（不改 notes 文件）
  删除    软删进回收站 + 恢复；不物理删
  隔离    损坏的 json → StoreCorruptedError（**不静默当空库**）
  不变量  **不参与检索**：collections.py 不 import search / indexer / llm
  API     handle_get/handle_post 路由 + FakeHandler 端到端（create/list/assign/trash/restore/title）

设计：本模块自带 check/section 与 PASS/FAIL/SKIP 列表，
由 tests/test_suite.py 的 main() 调用 run_collections_tests() 并合并结果。
独立运行（``python tests/test_collections.py``）时自带临时 Library 隔离。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.core import collections as C          # noqa: E402
from app.api import collections as api_coll     # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name if cond else f"{name} :: {detail}")
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"  [{detail}]"))
    return cond


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


class _FakeHandler:
    """最小 Handler 替身：只实现 api 层用到的原语。"""

    def __init__(self, body: dict | None = None, query: dict | None = None):
        self._body = body or {}
        self._query = query or {}
        self.resp: tuple | None = None

    def _send_json(self, payload, status: int = 200) -> None:
        self.resp = (payload, status)

    def _read_json(self) -> dict:
        return self._body

    def _query(self) -> dict:
        return self._query

    def _path_only(self) -> str:  # 兼容（未用）
        return ""


def _fresh() -> None:
    """删掉 store，让每个 case 从干净状态开始。"""
    p = C.store_path()
    if p.exists():
        p.unlink()


def run_collections_tests() -> None:
    section("Collections 资料集 + 笔记管理（durable metadata，不影响检索）")
    _fresh()

    # ---------- 1. 文件落点 + schema ----------
    c1 = C.create_collection("工程监测")
    check("类目落盘到 <Library>/metadata/collections.json",
          C.store_path().is_file() and C.store_path().parent.name == "metadata",
          str(C.store_path()))
    check("schema_version = 1 且为新结构",
          C.load_store()["schema_version"] == C.SCHEMA_VERSION)
    check("持久化为合法 JSON（可被外部读取）",
          isinstance(json.loads(C.store_path().read_text(encoding="utf-8")), dict))

    # ---------- 2. many-to-many ----------
    c2 = C.create_collection("投资研究")
    C.assign("docA", [c1["id"], c2["id"]])
    check("★ 一篇文档可属于多个类目（many-to-many）",
          C.collections_of("docA") == [c1["id"], c2["id"]], str(C.collections_of("docA")))
    check("类目反查成员",
          set(C.members_of(c1["id"])) == {"docA"} and set(C.members_of(c2["id"])) == {"docA"})

    # ---------- 3. assign 去重 + 过滤不存在的 id ----------
    C.assign("docA", [c1["id"], c1["id"], "不存在的id"])
    check("★ assign 去重且过滤不存在的类目 id",
          C.collections_of("docA") == [c1["id"]], str(C.collections_of("docA")))
    C.assign("docA", [])
    check("assign([]) 清空成员关系",
          C.collections_of("docA") == [] and "docA" not in C.load_store()["membership"])

    # ---------- 4. 显示标题（不改文件） ----------
    C.set_display_title("docA", "自定义显示标题")
    check("显示标题只存 metadata", C.load_store()["display_titles"].get("docA") == "自定义显示标题")
    C.set_display_title("docA", "")
    check("空标题 → 清除覆盖", "docA" not in C.load_store()["display_titles"])

    # ---------- 5. 软删 / 回收站 ----------
    C.trash_doc("docB")
    check("★ 删除 = 软删进回收站（非物理删）", C.is_trashed("docB"))
    C.restore_doc("docB")
    check("可从回收站恢复", not C.is_trashed("docB"))
    C.trash_doc("docB")
    C.trash_doc("docB")
    check("重复软删不产生重复项", list(C.load_store()["trash"]).count("docB") == 1)

    # ---------- 6. 删除类目绝不删除文档 ----------
    C.assign("docC", [c1["id"], c2["id"]])
    C.delete_collection(c1["id"])
    check("★ 删类目后：该类目从所有成员里移除，文档关系其余保留",
          C.collections_of("docC") == [c2["id"]], str(C.collections_of("docC")))
    check("类目已从 collections 列表移除",
          all(x["id"] != c1["id"] for x in C.load_store()["collections"]))

    # ---------- 7. 重命名 ----------
    c3 = C.create_collection("待改名")
    C.rename_collection(c3["id"], "新名字")
    check("重命名生效",
          any(x["id"] == c3["id"] and x["name"] == "新名字"
              for x in C.load_store()["collections"]))

    # ---------- 8. 损坏不静默当空库 ----------
    good = C.store_path().read_text(encoding="utf-8")
    C.store_path().write_text("{ 这不是合法 json", encoding="utf-8")
    corrupted = False
    try:
        C.load_store()
    except C.StoreCorruptedError:
        corrupted = True
    check("★ 损坏的 collections.json → StoreCorruptedError（不静默当空库）", corrupted)
    C.store_path().write_text(good, encoding="utf-8")   # 复原

    # ---------- 9. 不变量：不参与检索 ----------
    src = (REPO / "app" / "core" / "collections.py").read_text(encoding="utf-8")
    check("★ collections.py 不 import search / indexer / llm（不参与检索）",
          all(f"import {m}" not in src and f" {m} as" not in src
              for m in ("search", "indexer", "llm")))

    # ---------- 10. API 路由匹配 ----------
    check("GET /api/collections 命中", api_coll.handle_get(_FakeHandler(), "/api/collections"))
    check("POST 路由命中（7 个端点）",
          all(api_coll.handle_post(_FakeHandler({"name": "x"}), p) for p in (
              "/api/collections/create", "/api/collections/rename",
              "/api/collections/delete", "/api/collections/assign",
              "/api/notes/display_title", "/api/notes/trash", "/api/notes/restore")))
    check("未知路径不误判", not api_coll.handle_post(_FakeHandler(), "/api/nope")
          and not api_coll.handle_get(_FakeHandler(), "/api/nope"))

    # ---------- 11. API 端到端（FakeHandler） ----------
    _fresh()
    h = _FakeHandler({"name": "通过 API 建类目"})
    api_coll.handle_post(h, "/api/collections/create")
    ok_create = h.resp and h.resp[0]["code"] == 200 and h.resp[0]["data"]["id"]
    check("API create 返回 200 + 类目 id", bool(ok_create))

    cid = h.resp[0]["data"]["id"]
    h2 = _FakeHandler({"path": "notes/demo.md", "collection_ids": [cid]})
    api_coll.handle_post(h2, "/api/collections/assign")
    did = __import__("app.core.chunker", fromlist=["doc_id_for"]).doc_id_for("notes/demo.md")
    check("API assign 支持传 path（自动换算 doc_id）",
          collections_of_ok := (h2.resp[0]["code"] == 200 and C.collections_of(did) == [cid]),
          str(h2.resp))

    h3 = _FakeHandler({"path": "notes/demo.md", "title": "改过的标题"})
    api_coll.handle_post(h3, "/api/notes/display_title")
    check("API display_title 生效",
          h3.resp[0]["code"] == 200 and C.load_store()["display_titles"].get(did) == "改过的标题")

    h4 = _FakeHandler({"path": "notes/demo.md"})
    api_coll.handle_post(h4, "/api/notes/trash")
    check("API trash 生效", h4.resp[0]["code"] == 200 and C.is_trashed(did))
    api_coll.handle_post(_FakeHandler({"path": "notes/demo.md"}), "/api/notes/restore")
    check("API restore 生效", not C.is_trashed(did))

    h5 = _FakeHandler()
    api_coll.handle_get(h5, "/api/collections")
    check("API list 返回完整 store",
          h5.resp[0]["code"] == 200
          and {"collections", "membership", "display_titles", "trash"} <= set(h5.resp[0]["data"]))

    h6 = _FakeHandler({"name": ""})
    api_coll.handle_post(h6, "/api/collections/create")
    check("API 空类目名 → 400", h6.resp[1] == 400)

    h7 = _FakeHandler({"id": "不存在"})
    api_coll.handle_post(h7, "/api/collections/delete")
    check("API 删不存在的类目 → 404", h7.resp[1] == 404)

    _fresh()


if __name__ == "__main__":
    # 独立运行：自建临时 Library 隔离（绝不碰真实 data/）
    os.environ["WIKIUSB_LIBRARY"] = tempfile.mkdtemp(prefix="usbwiki-collections-")
    import importlib

    from app.core import paths as _paths
    importlib.reload(_paths)
    importlib.reload(C)
    run_collections_tests()
    total = len(PASS) + len(SKIP) + len(FAIL)
    print(f"\n  Collections TOTAL={total} PASS={len(PASS)} SKIP={len(SKIP)} FAIL={len(FAIL)}")
