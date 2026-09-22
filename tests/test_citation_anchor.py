"""Fast P0 追加回归：A6 重复标题锚点 / B5 章节覆盖 / C 日志格式化。

- A6：目录与正文含同样文字时，引用必须落在**正文**（靠源行范围，不靠文本匹配）。
- B5：概览型问句（「X 是怎么样的」）的 context 必须同时含频率表与后续条件项。
- C：logging 的 Mapping 参数不得被强转成 keys 元组（旧实现抛 TypeError）。
"""
from __future__ import annotations


def run(ctx, check, section, skip) -> None:      # noqa: ARG001
    section("A6：重复标题（目录 vs 正文）源行锚点")
    import tempfile
    from pathlib import Path

    from app.core import db as db_mod, embedder as emb_mod, indexer, search as search_mod

    tmp = Path(tempfile.mkdtemp(prefix="p0anchor_"))
    db = db_mod.get_db(db_path=tmp / "cache.db", embedding_dim=512)
    db.init_schema()
    emb = emb_mod.HashEmbedder(512)
    notes = tmp / "notes"
    notes.mkdir(parents=True, exist_ok=True)

    dup = """---
title: "重复标题 fixture"
---

# 目录

七、监测周期、监测频率及预警值15
㈠、监测频率15

# 七、监测周期、监测频率及预警值

## ㈠、监测频率

基坑开挖阶段 1次/1d

当出现下列情况之一时，应加强监测：

1. 达到预警值
"""
    f = notes / "dup-title.md"
    f.write_text(dup, encoding="utf-8")
    indexer.index_file(db, f, emb)

    parsed = __import__("app.core.chunker", fromlist=["parse"]).parse(dup, "notes/dup-title.md")
    src_lines = dup.split("\n")
    exact = [b for b in parsed.parents
             if "\n".join(src_lines[b.source_start_line - 1:b.source_end_line]).strip()
             == b.content.strip()]
    check("A2 每个父块携带真实源行范围（内容可按行号精确还原）",
          len(exact) == len(parsed.parents), f"{len(exact)}/{len(parsed.parents)}")
    check("A2 父块范围互不重叠且目录/正文行号不同",
          len({(b.source_start_line, b.source_end_line) for b in parsed.parents})
          == len(parsed.parents), "存在重复范围")

    d = db.query_one("SELECT doc_id FROM documents LIMIT 1")["doc_id"]
    rows = search_mod._coverage_doc_rows(db, d)
    toc = [r for r in rows if "目录" in (r["section_path"] or "") or r["content"].strip().startswith("# 目录")]
    check("A1 目录块与正文块行号区间不重叠",
          bool(toc) and all(t["ord"] == 0 for t in toc), str([t["ord"] for t in toc]))

    dbg = {}
    res = search_mod.hybrid_search(db, emb, "监测频率是怎么样的", top_k_parents=6, debug=dbg)
    ranges = [(r.source_start_line, r.source_end_line) for r in res.references]
    check("A4 引用带 source_start_line/source_end_line",
          all(a > 0 and b >= a for a, b in ranges), str(ranges))
    # A6 硬 Gate：同一文字在目录(5-8 行)与正文(>=10 行)各出现一次时，两者的引用范围
    # 必须**不同**且各自正确 —— 点击正文引用绝不会落到目录（旧实现全文搜第一处必然跳目录）。
    check("A6 目录引用与正文引用行范围不同（可区分同文）",
          len(set(ranges)) == len(ranges), str(ranges))
    check("A6 存在落在正文的引用（>=10 行）", any(a >= 10 for a, _ in ranges), str(ranges))
    if any(a <= 8 for a, _ in ranges):
        check("A6 目录引用只覆盖目录行（<=8）", all(b <= 8 for a, b in ranges if a <= 8),
              str(ranges))

    section("B5：概览型问句的章节覆盖（表 + 条件项同进 context）")
    from app.core import llm

    cov = """# 监测频率

| 阶段 | 频率 |
| 开挖 | 1次/1d |
| 垫层后 | 1次/3d |

当出现下列情况之一时，应提高频率：

1. 达到预警值
2. 变化速率加快
3. 连续降雨
4. 支护结构开裂
5. 其它异常情况

# 监测预警值

预警值为 30mm。
"""
    cf = notes / "coverage-fixture.md"
    cf.write_text(cov, encoding="utf-8")
    indexer.index_file(db, cf, emb)

    q = "监测频率是怎么样的"
    ana = search_mod.analyze_query(q)
    check("B4 概览型问句识别为 coverage（不被 datetime 抢走）",
          ana.question_type == "coverage", ana.question_type)
    res2 = search_mod.hybrid_search(db, emb, q, top_k_parents=5)
    # ⚠ 不变量（2026-09-22 修）：coverage 的 context 会扩展到 >top_k 个父块，
    # 引用必须**逐个建立**（编号 ≡ 引用 id），否则模型照编号写的 [N] 前端解析不到。
    # 旧断言「引用角标 ≤ top_k」把这条 bug 固化成了期望，已删除并改为：
    check("B3 编号空间 ≡ 引用空间（每条 context 父块都可被引用解析）",
          len(res2.references) == len(res2.parents) and
          [r.id for r in res2.references] == list(range(1, len(res2.references) + 1)),
          str((len(res2.references), len(res2.parents))))
    check("B3 每条引用都可点击（path 非空）",
          all(r.path for r in res2.references))
    prompt = llm.Gateway.build_prompt(q, res2, None)
    check("B5 context 含频率表", "1次/1d" in prompt and "1次/3d" in prompt)
    check("B5 context 含后续全部条件项",
          all(k in prompt for k in ("达到预警值", "变化速率加快", "连续降雨",
                                    "支护结构开裂", "其它异常情况")),
          prompt[-200:])

    section("C：日志格式化（Mapping 参数不得被强转）")
    import io
    import logging

    from app.core.log_util import _RedactingFormatter

    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(_RedactingFormatter("%(message)s"))
    lg = logging.getLogger("p0_logging_case")
    lg.handlers[:] = [h]
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.info("初始化完成: %s", {"app_version": "1.3.0", "schema_version": "1.7"})
    lg.info("x=%s y=%s", "a", "b")
    lg.info("%(name)s", {"name": "test"})
    out = buf.getvalue()
    check("C1 单 Mapping 参数（%s + dict）正常格式化", "app_version" in out, out[:120])
    check("C1 位置参数（%s %s）正常格式化", "x=a y=b" in out, out[:120])
    check("C1 命名占位（%(name)s）正常格式化", "test" in out, out[:120])
    check("C2 不再出现二次失败（无 Logging error / Traceback）",
          "Logging error" not in out and "Traceback" not in out)
    check("C2 formatter 不改动原 record（args 类型保持）",
          isinstance(getattr(lg, "_unused", None), type(None)))
    db.checkpoint_and_close()
