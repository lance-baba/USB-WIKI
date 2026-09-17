#!/usr/bin/env python3
"""A4.2a 检索质量实测：现有 RAG 回归用例 + 人工中文 sanity set。

比较对象（同一套语料、同一套查询、同一套指标）：

    baseline   → embedding=None（纯 FTS 词法，USB-WIKI 的 Level 1 能力）
    candidate  → 各 ONNX artifact（真实 tokenizer + CLS + L2，见 runner.py）

**每个候选独立进程 + 独立临时 Library**：向量签名不同，必须各自全量重建索引，
否则会撞上签名守卫（而且那样测出来的也不是这个候选的真实召回）。

指标：Top1 / Top3 / Top5 命中率、MRR（期望文档的排名倒数）、失败明细。

用法：
    <venv>/python.exe tools/embedding_eval/retrieval_eval.py --all
    <venv>/python.exe tools/embedding_eval/retrieval_eval.py --embedder xenova-int8 --suite sanity
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO / "runtime" / "models" / "_eval"
TOP_K = 5


# ---------------------------------------------------------------------------
# 子进程侧：真正跑一次评测（必须在设置 WIKIUSB_LIBRARY 之后才 import app.*）
# ---------------------------------------------------------------------------
def _run_one(embedder_id: str, suite: str, semantic_only: bool = False) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"a42a-{suite}-{embedder_id}-"))
    os.environ["WIKIUSB_LIBRARY"] = str(tmp / "Library")
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    from app.core import context as ctxmod                     # noqa: E402
    from app.core import indexer, paths                        # noqa: E402
    from app.core import search as search_mod                  # noqa: E402

    try:
        # ---------- 语料 ----------
        if suite == "rag":
            from tests.fixtures import rag_corpus as corpus     # noqa: E402
            docs = dict(corpus.DOCS)
            cases = [dict(c) for c in corpus.CASES]
            for c in cases:
                c["expect"] = Path(c["expect"]).stem if c.get("expect") else None
        else:
            payload = json.loads((Path(__file__).resolve().parent / "sanity_cases.json")
                                 .read_text(encoding="utf-8"))
            docs = dict(payload["docs"])
            cases = [dict(c) for c in payload["cases"]]

        paths.NOTES_DIR.mkdir(parents=True, exist_ok=True)
        for name, body in docs.items():
            (paths.NOTES_DIR / name).write_text(body, encoding="utf-8")

        # ---------- 库 ----------
        # 用 none 启动上下文：不触发任何真实嵌入源解析（我们要自己指定候选）
        os.environ.setdefault("WIKIUSB_TEST_ISOLATED", "1")
        from app.core import config                              # noqa: E402
        config.update({"AI": {"embedding_source": "none", "provider": "offline"}},
                      persist=False)
        if semantic_only:
            # 生产默认 allow_semantic_only=0：**没有词法依据的命中一律不进引用**
            # （防「问台风却引用基坑报告」）。那样 embedding 只能在有词法命中的
            # 候选集合内重排 → 量不出嵌入本身的语义能力。
            # 本开关只用于评测，隔离出「纯语义召回」这一项，不改生产默认。
            config.update({"SEARCH": {"allow_semantic_only": "1"}}, persist=False)
        ctx = ctxmod.AppContext()
        ctx.boot(db_path=tmp / "cache.db", start_syncer=False, probe_ollama=False)
        db = ctx.db

        emb = None
        if embedder_id != "none":
            from runner import OnnxBertEmbedder                  # noqa: E402
            inputs = json.loads((EVAL_DIR / "selection_inputs.json")
                                .read_text(encoding="utf-8"))
            cand = next(c for c in inputs["candidates"] if c["id"] == embedder_id)
            emb = OnnxBertEmbedder(REPO / cand["local_path"],
                                   EVAL_DIR / "tokenizers" / cand["repo"].replace("/", "__"),
                                   model_name=embedder_id)
            emb._lazy()                                          # noqa: SLF001
            if not emb.dim:          # 形状带动态维时用一次真实推理定下来
                emb.embed(["维度探测"])
            db.set_signature(emb.source, emb.model, emb.dim)

        for name in docs:
            indexer.index_file(db, paths.NOTES_DIR / name, emb)

        vec_ready = bool(db.vec_table_ready and not db.signature_mismatch)

        # ---------- 逐用例 ----------
        rows = []
        for c in cases:
            q = c["q"]
            res = search_mod.hybrid_search(db, emb, q, top_k_parents=TOP_K)
            stems = [Path(r.path).stem for r in res.references]
            exp = c.get("expect")
            rank = (stems.index(exp) + 1) if (exp and exp in stems) else None
            mode = c.get("mode", "hybrid")
            # 词法专用 case（fts/like）在正式套件里走独立词法路径；
            # 这里统一走生产路径 hybrid_search 以便横向比较，同时把 mode 记下来。
            rows.append({"q": q, "mode": mode, "expect": exp, "rank": rank,
                         "top": stems, "route": res.route,
                         "negative": exp is None})

        positives = [r for r in rows if not r["negative"]]
        negatives = [r for r in rows if r["negative"]]
        n = len(positives) or 1
        m = {
            "cases_total": len(rows), "positives": len(positives),
            "negatives": len(negatives),
            "top1": round(sum(1 for r in positives if r["rank"] == 1) / n, 4),
            "top3": round(sum(1 for r in positives if r["rank"] and r["rank"] <= 3) / n, 4),
            "top5": round(sum(1 for r in positives if r["rank"] and r["rank"] <= 5) / n, 4),
            "mrr": round(sum((1.0 / r["rank"]) for r in positives if r["rank"]) / n, 4),
            "neg_false_positive": sum(1 for r in negatives if r["top"]),
        }
        return {"embedder": embedder_id, "suite": suite, "vec_ready": vec_ready,
                "semantic_only": semantic_only,
                "docs": len(docs), "metrics": m, "rows": rows}
    finally:
        try:
            ctx.shutdown()                                       # noqa: F841
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
def _print_report(rep: dict) -> None:
    m = rep["metrics"]
    tag = " +semantic_only" if rep.get("semantic_only") else ""
    print(f"  [{rep['embedder']:16}{tag}] docs={rep['docs']:2} cases={m['cases_total']:2} "
          f"(pos {m['positives']} / neg {m['negatives']})  vec_ready={rep['vec_ready']}")
    print(f"      Top1 {m['top1']*100:5.1f}%   Top3 {m['top3']*100:5.1f}%   "
          f"Top5 {m['top5']*100:5.1f}%   MRR {m['mrr']:.3f}   "
          f"负样本误命中 {m['neg_false_positive']}")
    miss = [r for r in rep["rows"] if not r["negative"] and not r["rank"]]
    if miss:
        print(f"      未进 Top{TOP_K}（{len(miss)}）：")
        for r in miss[:6]:
            print(f"        - [{r['mode']}] {r['q']}  期望 {r['expect']} → {r['top'][:3]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="A4.2a 检索质量实测")
    ap.add_argument("--all", action="store_true", help="baseline + 全部候选，各起独立进程")
    ap.add_argument("--embedder", default="none", help="none 或候选 id")
    ap.add_argument("--suite", default="rag", choices=("rag", "sanity", "both"))
    ap.add_argument("--out", default="", help="单次结果 JSON")
    ap.add_argument("--semantic-only", action="store_true",
                    help="评测用：放行「无词法依据」的语义命中（不改生产默认）")
    args = ap.parse_args(argv)

    inputs = json.loads((EVAL_DIR / "selection_inputs.json").read_text(encoding="utf-8"))
    ids = ["none"] + [c["id"] for c in inputs["candidates"]]
    # `--all` 的默认语义是「把两套都跑完」；要单独跑某套显式传 --suite
    if args.all and args.suite == "rag":
        args.suite = "both"
    suites = ("rag", "sanity") if args.suite == "both" else (args.suite,)

    if not args.all:
        # ⚠ 这里必须把 --semantic-only 透传下去。漏传过一次，结果是
        #   子进程永远按生产默认跑，四个候选输出完全一致 ——
        #   看起来像「嵌入没用」，其实是开关没生效（harness 自己的 bug）。
        rep = _run_one(args.embedder, suites[0], semantic_only=args.semantic_only)
        _print_report(rep)
        if args.out:
            Path(args.out).write_text(json.dumps(rep, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8", newline="\n")
        return 0

    allres: dict = {}
    modes = [False, True] if args.semantic_only else [False]
    for suite in suites:
        for so in modes:
            key = suite + ("+semantic_only" if so else "")
            print("=" * 78)
            print(f"套件：{key}")
            for cid in ids:
                out = EVAL_DIR / f"retr_{key}_{cid}.json"
                cmd = [sys.executable, str(Path(__file__).resolve()),
                       "--embedder", cid, "--suite", suite, "--out", str(out)]
                if so:
                    cmd.append("--semantic-only")
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace")
                if proc.returncode != 0:
                    print(f"  ❌ {cid} 评测失败：{(proc.stderr or '')[-500:]}")
                    continue
                rep = json.loads(out.read_text(encoding="utf-8"))
                allres.setdefault(key, {})[cid] = rep
                _print_report(rep)
            print(f"\n  —— {key} 对比 ——")
            print(f"  {'候选':18} {'Top1':>7} {'Top3':>7} {'Top5':>7} {'MRR':>7}")
            for cid in ids:
                r = allres.get(key, {}).get(cid)
                if not r:
                    continue
                m = r["metrics"]
                print(f"  {cid:18} {m['top1']*100:6.1f}% {m['top3']*100:6.1f}% "
                      f"{m['top5']*100:6.1f}% {m['mrr']:7.3f}")

    merged = EVAL_DIR / "retrieval.json"
    merged.write_text(json.dumps(allres, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8", newline="\n")
    print(f"\n结果写入 {merged}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
