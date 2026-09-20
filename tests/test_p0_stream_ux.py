"""P0 流式状态机 / 超时拆分 / 来源显示 / 高亮摘录 回归（接入 test_suite）。

覆盖用户要求的 6 个真实状态用例（不是只 mock 一个 happy path）：
  Case 1 正常 Ollama        —— delta delta done=true → 成功，绝不 error/offline/fallback
  Case 2 thinking + answer  —— thinking 帧不算断流，正常成功
  Case 3 正文后异常          —— 保留正文 + 「回答可能不完整」，**绝不追加离线全文**
  Case 4 零正文失败          —— 才允许降级（offline 本地搜索）
  Case 5 本地文件来源        —— display_source 用 source_file（不是 docProps 标题）
  Case 6 关键词高亮          —— 命中词高亮，不破坏 Markdown table

外加 P0-2 的**真实三档超时**回归：用本地 stdlib HTTP 服务模拟
「响应头慢（模型加载）」「吐字中途卡住」「连接被拒」三种情形。
"""
from __future__ import annotations

import http.server
import json
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app.core import config, db as db_mod, indexer, llm, net_util, search as search_mod
from app.core.embedder import HashEmbedder
from app.core.llm import Gateway

FAKE_MODEL = "qwen2.5:7b"


@contextmanager
def _patch_stream(fn):
    """把 llm.net_util.http_post_stream 换成构造好的假流（不发起真实网络）。"""
    with patch.object(llm.net_util, "http_post_stream", fn):
        yield

_DONE = '{"message":{"content":""},"done":true}'


def _content(text: str) -> str:
    return json.dumps({"message": {"content": text}, "done": False}, ensure_ascii=False)


def _thinking(text: str) -> str:
    return json.dumps({"message": {"thinking": text}, "done": False}, ensure_ascii=False)


def _off_result(query: str) -> search_mod.SearchResult:
    r = search_mod.SearchResult(query=query, route="like")
    r.lex_terms = ["观测人员"]
    r.parents = [{
        "parent_id": "p1", "doc_id": "d1", "title": "八、观测人员配备",
        "display_source": "八、观测人员配备", "path": "notes/a.md",
        "content": "八、观测人员配备\n\n| 职责 | 姓名 |\n| --- | --- |\n| 现场观测负责人 | 张三 |",
        "score": 1.0, "similarity": None,
    }]
    return r


def _make_gw() -> Gateway:
    gw = Gateway(db=None)
    gw.ollama_status = lambda force=False: True            # type: ignore[assignment]
    gw.state.ollama_checked_at = time.time()
    gw.state.ollama_healthy = True
    gw.state.ollama_models = [FAKE_MODEL]
    gw.retrieve = lambda q: _off_result(q)                 # type: ignore[assignment]
    return gw


def _types(frames) -> list:
    return [f.get("type") for f in frames]


def _text(frames, ftype="delta") -> str:
    return "".join(f.get("content", "") for f in frames if f.get("type") == ftype)


def _with_cfg(**ai):
    """临时改 AI 配置并恢复。"""
    keys = ("provider", "ollama_chat_model", "api_key")
    old = {k: config.get_str("AI", k, "") for k in keys}
    config.update({"AI": ai}, persist=False)
    return old


def _restore(old: dict) -> None:
    config.update({"AI": old}, persist=False)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_POST(self):  # noqa: N802
        p = self.path
        # 像真实服务端（Ollama）一样：POST 无 Content-Length / Transfer-Encoding 直接 400。
        # 低层 putrequest/endheaders **不会**自动补 Content-Length（实测漏了它 → Ollama 全部 400），
        # 这里刻意复现该约束，让回归能真的抓住它。
        if self.headers.get("Content-Length") is None and \
                self.headers.get("Transfer-Encoding") is None:
            self.send_response(400)
            self.end_headers()
            return
        if p == "/slowheaders":
            time.sleep(3.0)                      # 模拟「模型正在加载」：响应头迟迟不来
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":1}\n')
        elif p == "/slowbody":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"a":1}\n')
            self.wfile.flush()
            time.sleep(4.0)                      # 两帧之间长时间空闲
            try:
                self.wfile.write(b'{"b":2}\n')
            except OSError:
                pass
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"x":1}\n')

    def log_message(self, *a):                   # 静音
        pass


def run(ctx, check, section, skip) -> None:  # noqa: ARG001
    section("P0 流式状态机 / 超时 / 来源 / 高亮（真实状态用例）")

    # ================= Case 1：正常 delta delta done =================
    old = _with_cfg(provider="ollama", ollama_chat_model=FAKE_MODEL, api_key="")
    try:
        gw = _make_gw()

        def _ok(url, payload, **kw):
            yield _content("根据知识片段，沉降观测对人员素质的要求如下。")
            yield _content("须持证上岗并熟悉观测方案。")
            yield _DONE

        with _patch_stream(_ok):
            frames = list(gw.stream_chat("人员素质有什么要求", []))
        t = _types(frames)
        txt = _text(frames)
        check("Case1 收到生成答案 + done", "delta" in t and "done" in t, str(t))
        check("Case1 不出现 error / offline / fallback",
              "error" not in t and not any(f.get("type") == "notice" for f in frames), str(t))
        check("Case1 答案不含离线搜索结果（未误判失败）", "本地搜索结果" not in txt, txt[:60])
        check("Case1 meta.provider == ollama",
              any(f.get("provider") == "ollama" for f in frames if f.get("type") == "meta"))

        # —— P0-1 根因直证：done=true 必须被识别为 ollama_done ——
        with _patch_stream(_ok):
            oframes = list(gw._stream_ollama("q", None, _off_result("q")))
        check("Case1 _stream_ollama 在 done=true 时产出 ollama_done",
              {"type": "ollama_done"} in oframes, str(_types(oframes)))

        # ================= Case 2：thinking + answer =================
        def _think(url, payload, **kw):
            yield _thinking("让我先看看人员配备表……")
            yield _content("现场观测负责人为张三。")
            yield _DONE

        with _patch_stream(_think):
            frames2 = list(gw.stream_chat("观测人员有哪些", []))
        t2 = _types(frames2)
        check("Case2 thinking 帧不算断流，正常成功",
              "delta" in t2 and "done" in t2 and "error" not in t2, str(t2))
        check("Case2 答案是生成内容、无离线兜底",
              "张三" in _text(frames2) and "本地搜索结果" not in _text(frames2))

        # ================= Case 3：正文后异常（socket timeout）=================
        def _cut(url, payload, **kw):
            yield _content("根据知识片段，沉降观测对人员素质的具体要求如下……")
            yield _content("（正文继续）")
            raise net_util.StreamError("OLLAMA_STREAM_TIMEOUT", "流式响应中断（长时间无新内容）")

        with _patch_stream(_cut):
            frames3 = list(gw.stream_chat("人员素质有什么要求", []))
        t3 = _types(frames3)
        txt3 = _text(frames3)
        check("Case3 保留已生成的正文", "沉降观测对人员素质" in txt3, txt3[:60])
        check("Case3 提示「回答可能不完整」（留在对话里，不是一闪而过的 toast）",
              "可能不完整" in txt3, txt3[-60:])
        check("Case3 **绝不追加离线全文**", "本地搜索结果" not in txt3, txt3[-80:])
        check("Case3 不以 error 帧收尾（不给用户「失败」假象）", "error" not in t3, str(t3))
        check("Case3 正常 done 收尾", t3 and t3[-1] == "done", str(t3))

        # ================= Case 4：零正文失败 =================
        def _refused(url, payload, **kw):
            raise net_util.StreamError("OLLAMA_CONNECTION_CLOSED", "连接失败：timed out")
            yield  # pragma: no cover

        with _patch_stream(_refused):
            frames4 = list(gw.stream_chat("观测人员有哪些", []))
        t4 = _types(frames4)
        txt4 = _text(frames4)
        check("Case4 零正文 → 出现 error + notice", "error" in t4 and "notice" in t4, str(t4))
        check("Case4 零正文 → 降级为本地搜索（允许 fallback）",
              "本地搜索结果" in txt4, txt4[:60])
    finally:
        _restore(old)

    # ================= Case 5：本地文件来源 =================
    tmp = Path(tempfile.mkdtemp(prefix="p0ux_"))
    try:
        db = db_mod.get_db(db_path=tmp / "cache.db", embedding_dim=512)
        db.init_schema()
        notes = tmp / "notes"
        notes.mkdir(parents=True, exist_ok=True)
        doc_title = "宁波孙氏开发有限公司太阳广场"
        src_file = "年产160万新能源汽车前幅车架及后悬部件项目降观测方案260916(2).docx"
        f = notes / "obs.md"
        f.write_text(
            "---\n"
            f"title: {doc_title}\n"
            f"source_file: {src_file}\n"
            "source_type: docx\n"
            "doc_type: imported\n"
            "status: success\n"
            "---\n\n"
            "八、观测人员配备\n\n"
            "| 职责 | 姓名 |\n| --- | --- |\n| 现场观测负责人 | 张三 |\n\n"
            "仪器设备、人员素质的要求：须持证上岗。\n",
            encoding="utf-8")
        indexer.index_file(db, f, HashEmbedder(512))

        res = search_mod.hybrid_search(db, HashEmbedder(512), "观测人员配备")
        disp = res.references[0].display_source if res.references else ""
        check("Case5 来源显示名 = source_file（不是 docProps 标题）",
              disp == src_file, disp)
        drow = db.query_one("SELECT title FROM documents LIMIT 1")
        check("Case5 内部 title 未被删除（仍用于检索/提示词）",
              drow is not None and drow["title"] == doc_title,
              drow["title"] if drow else "")
        docs = search_mod.rank_documents(db, limit=5)
        check("Case5 资料列表也带 display_source",
              bool(docs) and docs[0].get("display_source") == src_file,
              str(docs[0].get("display_source")) if docs else "")

        # ================= Case 6：关键词高亮 + 表格 =================
        gw5 = Gateway(db=db)
        long_lead = "前言：" + ("与本主题无关的过渡文字。" * 40)
        table = ("| 职责 | 姓名 | 职称 |\n| --- | --- | --- |\n"
                 "| 现场观测负责人 | 张三 | 工程师 |\n| 内业资料整理 | 张三 | 助理工程师 |\n"
                 "| 观测辅助人员 | 李四 | 技术员 |")
        content = long_lead + "\n\n八、观测人员配备\n\n" + table + "\n\n" + ("收尾说明。" * 60)
        result = search_mod.SearchResult(query="人员", route="like")
        result.lex_terms = ["人员"]
        result.parents = [{"parent_id": "p1", "doc_id": "d1", "title": "八、观测人员配备",
                           "display_source": src_file, "path": "notes/obs.md",
                           "content": content, "score": 1.0, "similarity": None}]
        frames6 = list(gw5._offline_answer("人员", result))
        text6 = _text(frames6)
        check("Case6 命中词被高亮标记（哨兵，而非裸 HTML）",
              llm._HL_OPEN in text6 and "<mark>" not in text6, text6[:80])
        check("Case6 Markdown 表格结构未被破坏（分隔行仍在）",
              "| --- | --- | --- |" in text6 and text6.count("|") >= 8)
        check("Case6 来源行用 display_source", src_file in text6, text6[:80])

        # 表格命中：保表头 + 命中行 + 相邻行（不按 300 字硬截断）
        tbl = llm._table_excerpt(content, ["现场观测负责人"])
        check("Case6 表格命中 → 保表头 + 命中行",
              tbl.startswith("| 职责 | 姓名 | 职称 |") and "现场观测负责人" in tbl, tbl[:80])

        # 高亮：英文大小写不敏感 + 长词优先且不嵌套
        hl = llm._highlight("Obsidian and obsidian", ["Obsidian"])
        check("Case6 英文高亮大小写不敏感",
              hl.count(llm._HL_OPEN) == 2 and hl.count(llm._HL_CLOSE) == 2, hl)
        hl2 = llm._highlight("观测人员配备", ["人员", "观测人员"])
        check("Case6 长词优先、不产生嵌套高亮",
              hl2.count(llm._HL_OPEN) == 1 and hl2.count(llm._HL_CLOSE) == 1, hl2)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # ================= P0-2：真实三档超时 =================
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    except OSError as exc:
        skip("P0-2 三档超时（本地 HTTP 服务）", f"无法监听本地端口：{exc}")
    else:
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            base = f"http://127.0.0.1:{port}"

            # 快响应：正常
            got = list(net_util.http_post_stream(
                base + "/fast", {}, connect_timeout=1.0, first_token_timeout=5.0,
                idle_timeout=5.0, with_proxy=False))
            check("P0-2 正常流式：首行可取到（请求须带 Content-Length，否则服务端 400）",
                  any('"x"' in g for g in got), str(got))

            # 响应头慢 3s，connect_timeout 只有 1s —— 旧实现必挂，新实现必须成功
            t0 = time.time()
            got2 = list(net_util.http_post_stream(
                base + "/slowheaders", {}, connect_timeout=1.0, first_token_timeout=10.0,
                idle_timeout=5.0, with_proxy=False))
            check("P0-2 响应头等待不受 connect_timeout 限制（模型加载场景）",
                  any('"ok"' in g for g in got2) and (time.time() - t0) >= 2.5,
                  f"got={got2} elapsed={time.time() - t0:.1f}")

            # 吐字中途长时间空闲 → OLLAMA_STREAM_TIMEOUT（而非 CONNECTION_CLOSED）
            kind = ""
            try:
                list(net_util.http_post_stream(
                    base + "/slowbody", {}, connect_timeout=1.0, first_token_timeout=5.0,
                    idle_timeout=1.0, with_proxy=False))
            except net_util.StreamError as exc:
                kind = exc.kind
            check("P0-2 流式空闲超时 → OLLAMA_STREAM_TIMEOUT", kind == "OLLAMA_STREAM_TIMEOUT", kind)

            # 连接被拒（端口没人听）→ OLLAMA_CONNECTION_CLOSED
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            dead_port = s.getsockname()[1]
            s.close()
            kind2 = ""
            try:
                list(net_util.http_post_stream(
                    f"http://127.0.0.1:{dead_port}/x", {}, connect_timeout=1.0,
                    first_token_timeout=2.0, idle_timeout=2.0, with_proxy=False))
            except net_util.StreamError as exc:
                kind2 = exc.kind
            check("P0-2 连接被拒 → OLLAMA_CONNECTION_CLOSED", kind2 == "OLLAMA_CONNECTION_CLOSED", kind2)
        finally:
            srv.shutdown()
            srv.server_close()
