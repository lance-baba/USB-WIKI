"""A/B 轮契约回归：回答归因（Grounding）+ 引用精确跳转。

A（Answer Grounding / Attribution）：
  真实失败样本——文章里同时出现「东北冷空气 / 华西秋雨 / 台风杜鹃」，模型把
  四川/重庆/湖北 都归因为「台风影响地区」。这是**同文共现 → 错误因果**。
  契约：系统提示必须显式禁止（规则在 SYSTEM_PROMPT），片段标号必须用 [1] 风格
  （前端与提示词两端一致），且不得再出现「根据知识片段」类 RAG 术语模板。

B（Citation 精确跳转）：
  正文角标 [1] = 证据（parent_id 定位）；底部来源 = 文档。新增
  /api/notes/evidence 按 path+parent_id 返回证据块（section_path + content），
  供前端做稳定 DOM 定位（snippet 只作兜底）。

天气归因的**行为**验证（真 LLM 回答不含 东北/四川/重庆/湖北）需要 Ollama，
属于开发态 Gate 脚本；本文件做离线可断言的契约部分。
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from app.core import crawler, paths


def run(ctx, check, section, skip) -> None:
    section("A：回答归因契约（SYSTEM_PROMPT / build_prompt）")
    from app.core import llm, search

    sp = llm.SYSTEM_PROMPT

    # ---- A1/A2：禁止同文共现 → 因果；资料不足必须明说 ----
    check("A1 系统提示要求事实必须由证据直接支持", "直接支持" in sp)
    check("A1 禁止同文共现自动建立因果/归属/影响关系",
          "同一" in sp and ("因果" in sp or "影响关系" in sp))
    check("A2 资料不足时必须说「没有明确说明」", "没有明确说明" in sp)
    check("A2 禁止用同文其它内容补齐答案", "补齐" in sp)

    # ---- A3：RAG 术语模板必须被明令禁止 ----
    check("A3 明令禁止「根据知识片段」类模板",
          "根据知识片段" in sp and "从知识片段来看" in sp)

    # ---- A4：引用格式 [1]，不教 [^1] ----
    check("A4 引用格式规则使用 [1] 风格", "[1]" in sp)
    check("A4 不再教模型写 [^1] 角标", "[^1]、[^2]" not in sp)

    # ---- build_prompt：片段标号 [i]，头部为【资料片段】 ----
    res = search.SearchResult(query="台风甲会影响哪些地区？", route="fts")
    res.parents = [{
        "parent_id": "p1", "doc_id": "d", "title": "天气简报",
        "path": "notes/weather.md",
        "content": "东北受到冷空气影响，将明显降温。\n\n"
                   "四川、重庆、湖北受到华西秋雨影响，未来几天有持续强降水。\n\n"
                   "台风甲预计先向西移动，之后转向北上，登陆概率下降，甚至可能不登陆。",
        "score": 1.0,
    }]
    prompt = llm.Gateway.build_prompt("台风甲会影响哪些地区？", res, None)
    check("A4 片段标号为 [1] 来源（非 [^1]）", "[1] 来源：天气简报" in prompt and "[^1]" not in prompt)
    check("A2/A3 提示词头部为【资料片段】", "【资料片段】" in prompt)
    # 脱敏天气 fixture 必须真的进了提示词（这是行为 Gate 的语料前提）
    check("回归 fixture：三类独立天气事件都在片段里",
          all(k in prompt for k in ("冷空气", "华西秋雨", "台风甲")))

    section("B：证据跳转（/api/notes/evidence）")

    # ---- 造一篇可检索的文档，拿一个真实 parent_id ----
    # 优先 PDF（真机语料形态）；转换器缺失时退 Markdown（导入逻辑同链路）
    rel_path, parent_id = "", ""
    try:
        from tests.test_converters import make_pdf as _make_pdf
        r = crawler.import_document("证据跳转验证.pdf", base64.b64decode(_make_pdf()),
                                    db=ctx.db, embedder=ctx.embedder)
        if r.ok:
            row = ctx.db.query_one(
                "SELECT pb.parent_id, d.rel_path FROM parent_blocks pb "
                "JOIN documents d ON d.doc_id = pb.doc_id ORDER BY pb.ord LIMIT 1")
            if row:
                parent_id, rel_path = row["parent_id"], row["rel_path"]
    except ImportError:
        pass
    if not parent_id:
        r = crawler.import_markdown("证据跳转验证.md",
                                    "# 观测内容\n\n沉降观测点8个，点号CJ1-CJ8。\n",
                                    db=ctx.db, embedder=ctx.embedder)
        if not r.ok:
            skip("证据跳转（文档导入失败）")
            return
        row = ctx.db.query_one(
            "SELECT pb.parent_id, d.rel_path FROM parent_blocks pb "
            "JOIN documents d ON d.doc_id = pb.doc_id ORDER BY pb.ord LIMIT 1")
        if row is None:
            skip("证据跳转（无 parent 块）")
            return
        parent_id, rel_path = row["parent_id"], row["rel_path"]

    # ---- Handler 桩：捕获 _send_json 输出 ----
    class _StubHandler:                                   # noqa: D401
        def __init__(self, db):
            self.ctx = type("C", (), {"db": db})()
            self.captured = None

        def _send_json(self, payload, status=200):
            self.captured = (payload, status)

    from app.api import library as lib

    # 1) 正常：path+parent_id 匹配 → 200 + section_path + content
    h = _StubHandler(ctx.db)
    lib.note_evidence(h, rel_path, parent_id)
    payload, status = h.captured
    check("B 证据接口返回 200", status == 200 and payload["code"] == 200, str(status))
    check("B 证据接口带 section_path 与 content",
          payload["data"]["section_path"] is not None and payload["data"]["content"],
          json.dumps({k: bool(v) for k, v in payload["data"].items()}, ensure_ascii=False))

    # 2) 跨文档错位引用：parent_id 属于别篇 → 拒绝
    h2 = _StubHandler(ctx.db)
    lib.note_evidence(h2, "notes/随手记.md", parent_id)
    check("B 证据块与笔记不匹配时拒绝（防跨文档错位）",
          h2.captured[1] == 404, str(h2.captured[1]))

    # 3) 不存在的 parent_id → 404
    h3 = _StubHandler(ctx.db)
    lib.note_evidence(h3, rel_path, "no-such-parent")
    check("B 证据块不存在时返回 404", h3.captured[1] == 404)

    # 4) 缺参 → 400
    h4 = _StubHandler(ctx.db)
    lib.note_evidence(h4, rel_path, "")
    check("B 缺 parent_id 返回 400", h4.captured[1] == 400)

    # ---- 清理：本测试造的文档必须全部撤掉，否则会污染共享 workspace 里
    #      后续测试的计数断言（test_index_and_search / test_graph 都按 docs 数断言）----
    from app.core import indexer
    for row in ctx.db.query(
        "SELECT doc_id, rel_path FROM documents WHERE rel_path LIKE '%证据跳转验证%'"
    ):
        indexer.purge_document(ctx.db, row["doc_id"])
        try:
            (paths.NOTES_DIR / Path(row["rel_path"]).name).unlink(missing_ok=True)
        except OSError:
            pass
    left = ctx.db.query_one(
        "SELECT COUNT(*) AS n FROM documents WHERE rel_path LIKE '%证据跳转验证%'")
    check("测试文档已清理（不污染共享 workspace）", left["n"] == 0, str(left["n"]))
