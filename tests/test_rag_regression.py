"""RAG 检索回归：防止 chunker / FTS / LIKE / embedding / RRF / parent / citation
被悄悄改坏。

## 分层测，不只测「最终结果」

`fts` / `like` / `hybrid` / `noemb` 四种模式**分别**跑 —— 这样某层退化时能立刻
定位到是哪一层，而不是只看到「总结果不对」。

## deterministic

核心 CI 用项目自带的 `HashEmbedder`（纯 Python、固定输出），
**不依赖在线 API / Ollama / 真实模型**，Windows 与 Linux 结果完全一致。
真实 Ollama 嵌入可作为将来的 optional integration test。
"""

from __future__ import annotations

from pathlib import Path

from app.core import indexer, paths
from app.core import search as search_mod
from app.core.embedder import HashEmbedder
from tests.fixtures import rag_corpus as corpus

TOP_K = 5


def _stem_of(rel_path: str) -> str:
    """`notes/cn_rag_long.md` → `cn_rag_long`。"""
    return Path(rel_path or "").stem


def _install_corpus() -> set[str]:
    """把语料写进隔离的笔记目录，返回文件名集合（供清理）。"""
    paths.NOTES_DIR.mkdir(parents=True, exist_ok=True)
    for name, body in corpus.DOCS.items():
        (paths.NOTES_DIR / name).write_text(body, encoding="utf-8")
    return set(corpus.DOCS)


def _chunk_walk(db, chunk_ids: list[str]) -> list[str]:
    """按 chunk_ids 顺序返回对应文档 stem（去重前）。"""
    if not chunk_ids:
        return []
    ph = ",".join("?" * len(chunk_ids))
    rows = db.query(
        f"""SELECT cm.chunk_id AS cid, d.rel_path AS rp FROM chunk_metadata cm
              JOIN documents d ON d.doc_id = cm.doc_id
             WHERE cm.chunk_id IN ({ph})""",
        tuple(chunk_ids),
    )
    m = {r["cid"]: _stem_of(r["rp"]) for r in rows}
    return [m[c] for c in chunk_ids if c in m]


def run(ctx, check, section, skip) -> None:
    # 安全保险丝：本模块会往 Library 的 notes/ 写文件 —— 先确认处于隔离测试库
    from tests import test_env as _te

    _te.assert_test_library_safe()

    """由 tests/test_suite.py 调用（复用其 section/check/skip）。"""
    section("RAG 检索回归（分层：fts / like / hybrid / noemb）")
    db = ctx.db
    emb = HashEmbedder(512)          # 确定性嵌入，不依赖任何外部服务

    installed = _install_corpus()
    try:
        for name in corpus.DOCS:
            indexer.index_file(db, paths.NOTES_DIR / name, emb)

        docs_n = len(corpus.DOCS)
        cases = corpus.CASES

        def topk_stems_for(case) -> tuple[list[str], dict]:
            """按模式执行，返回 (文档 stem 序列, 附加信息)。"""
            mode = case.get("mode", "hybrid")
            q = case["q"]
            if mode in ("fts", "like"):
                # 分层：直接打词法层，绕过 hybrid
                term_sets = search_mod.content_term_sets(q)
                resolved = search_mod.resolve_lexical_terms(db, term_sets)
                if not resolved:
                    return [], {"route": "empty"}
                if mode == "like" or search_mod.should_use_like_terms(resolved):
                    ids, route = search_mod._like_search(db, resolved, 50)
                else:
                    ids, route = search_mod._fts_search(db, " ".join(resolved), 50)
                # 父块粒度去重（同一父块只算一次）
                seen_pid, ordered = set(), []
                ph_rows = db.query(
                    f"""SELECT chunk_id, parent_id FROM chunk_metadata
                         WHERE chunk_id IN ({",".join("?" * len(ids))})""",
                    tuple(ids),
                ) if ids else []
                pmap = {r["chunk_id"]: r["parent_id"] for r in ph_rows}
                for cid in ids:
                    pid = pmap.get(cid, cid)
                    if pid in seen_pid:
                        continue
                    seen_pid.add(pid)
                    ordered.append(cid)
                stems = _chunk_walk(db, ordered[:TOP_K])
                return stems, {"route": route, "raw": len(ids)}

            # hybrid / noemb
            use_emb = emb if mode == "hybrid" else None
            res = search_mod.hybrid_search(db, use_emb, q, top_k_parents=TOP_K)
            return ([_stem_of(r.path) for r in res.references],
                    {"route": res.route, "refs": res.references, "res": res})

        failures: list[str] = []
        passed = 0
        checked_citations = 0

        for i, case in enumerate(cases):
            q, mode = case["q"], case.get("mode", "hybrid")
            stems, info = topk_stems_for(case)
            expect = case.get("expect")

            ok = True
            why = ""
            expect_any = case.get("expect_any")
            if expect is None and not expect_any:
                # 负样本：不该命中任何语料文档
                if stems:
                    ok, why = False, f"应搜不到，却命中 {stems[:3]}"
            elif expect_any:
                # 纯语义改写：只要求命中相关文档之一，不锁死具体哪篇（避免脆弱排名断言）
                if not any(e in stems for e in expect_any):
                    ok, why = False, f"{expect_any} 均未进 Top-{TOP_K} → {stems[:5]}"
            elif expect not in stems:
                ok, why = False, f"{expect!r} 未进 Top-{TOP_K} → {stems[:5]}"

            if ok and case.get("forbid"):
                bad = [f for f in case["forbid"] if f in stems]
                if bad:
                    ok, why = False, f"命中了禁止文档 {bad}"

            if ok and case.get("top1"):
                if not stems or stems[0] != case["top1"]:
                    ok, why = False, f"Top-1 应为 {case['top1']!r}，实为 {stems[:1]}"

            if ok and case.get("max_per_doc"):
                from collections import Counter

                top = Counter(stems).most_common(1)
                if top and top[0][1] > case["max_per_doc"]:
                    ok, why = False, f"同一文档霸榜 {top[0]}"

            # citation 校验
            if ok and case.get("cites") and mode in ("hybrid", "noemb"):
                refs = info.get("refs") or []
                checked_citations += 1
                ids = [r.id for r in refs]
                if len(ids) != len(set(ids)):
                    ok, why = False, f"引用 id 重复：{ids}"
                elif not refs:
                    ok, why = False, "有 cites 断言却没有引用"
                else:
                    for r in refs:
                        if not (r.title and r.path and r.parent_id and r.doc_id):
                            ok, why = False, f"引用字段不全：{r.title!r}"
                            break
                        if not (paths.DATA_DIR / r.path).exists():
                            ok, why = False, f"引用指向不存在的文件：{r.path}"
                            break
                        # 注意比的是**文件名**（语料键是 xxx.md），不是 stem
                        if Path(r.path).name not in corpus.DOCS:
                            ok, why = False, f"引用指向语料之外的文档：{r.path}"
                            break

            if ok:
                passed += 1
            else:
                failures.append(f"[{mode}] {q} :: {why}")

        # ---------- 分层可用性：四种模式都真的跑通 ----------
        report = {"cases": len(cases), "pass": passed, "fail": len(failures)}
        check(f"RAG 用例全部通过（{report['pass']}/{report['cases']}）",
              not failures, "; ".join(failures[:4]))
        check(f"语料规模（{docs_n} 篇）与用例数（{len(cases)}）在预期区间",
              20 <= docs_n <= 40 and 30 <= len(cases) <= 60,
              f"docs={docs_n} cases={len(cases)}")

        # fts 层独立可用
        # 走**真实管线**（词项解析 → FTS）：裸串能命中不代表管线能命中，反之亦然。
        # 用 4 字词（三元组索引要求 ≥3 字符），否则会被路由到 LIKE 兜底。
        fts_terms = search_mod.resolve_lexical_terms(
            db, search_mod.content_term_sets("召回融合"))
        ids, route = search_mod._fts_search(db, " ".join(fts_terms or ["召回融合"]), 10)
        check("分层：纯 FTS 可独立运行（真实管线）", len(ids) > 0, f"{route} {len(ids)}")

        # like 层独立可用（两字中文走兜底）
        ids2, route2 = search_mod._like_search(db, ["检索"], 10)
        check("分层：短查询 LIKE 兜底可独立运行", len(ids2) > 0 and route2 == "like",
              f"{route2} {len(ids2)}")

        # hybrid 层
        res_h = search_mod.hybrid_search(db, emb, "分块", top_k_parents=3)
        check("分层：hybrid（RRF）可独立运行", len(res_h.references) > 0, str(res_h.route))

        # 无 embedding 时的降级
        res_n = search_mod.hybrid_search(db, None, "分块", top_k_parents=3)
        check("分层：无嵌入源时仍能检索（降级到纯词法）",
              len(res_n.references) > 0, str(res_n.route))
        check("无嵌入源时如实说明「向量路未启用」",
              any("向量" in w for w in res_n.warnings), str(res_n.warnings[:1]))

        # 词法确定的 case 在无嵌入下也应同样命中
        noemb_miss = []
        for case in cases:
            if case.get("mode") not in ("fts", "like") or case.get("expect") is None:
                continue
            r = search_mod.hybrid_search(db, None, case["q"], top_k_parents=TOP_K)
            got = [_stem_of(x.path) for x in r.references]
            if case["expect"] not in got:
                noemb_miss.append(case["q"])
        check("无嵌入源时，词法类 case 仍全部命中",
              not noemb_miss, str(noemb_miss[:4]))

        check("citation 校验覆盖了足够用例",
              checked_citations >= 3, f"checked={checked_citations}")

        print(f"      RAG_CASES={report['cases']}  PASS={report['pass']}  "
              f"FAIL={report['fail']}  CITATION_CHECKED={checked_citations}")
    finally:
        # 清理：其它测试断言 NOTES_DIR 的精确篇数，留下垃圾会让它们无故失败
        for f in list(paths.NOTES_DIR.glob("*.md")):
            if f.name in installed:
                try:
                    f.unlink()
                except OSError:
                    pass
        try:
            indexer.rebuild_all(db, emb)
        except Exception:  # noqa: BLE001
            pass
