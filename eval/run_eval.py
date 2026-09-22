#!/usr/bin/env python3
"""USB-WIKI · RAG Eval Lab V1 —— 可重复运行的检索评测 Harness。

设计红线（与产品约定一致）：
  * **只测不调**：本文件只 *调用* 生产 `search.hybrid_search()`，绝不修改它，
    也绝不修改 chunker / prompt / embedding / top_k 去把分数做漂亮。
  * **绝不碰真实 Library**：启动第一步就 `test_env.activate_test_library()`（复用
    测试安全保险丝），在 `%TEMP%/usb-wiki-eval-*` 下建独立库；fixtures 拷进去再索引。
  * **不读真实 API Key**：检索路径不触碰 AI 配置；只有可选的 `--llm-smoke` 才会用到
    已配置的本地/云端 provider，且不可用时自动跳过。
  * **不进用户安装包**：发布脚本 `scripts/build_release.py` 只 stage `app/` 与 runtime，
    `eval/` 永远不会被复制。

用法：
    python eval/run_eval.py baseline                 # 生成 eval/reports/baseline.{json,md}
    python eval/run_eval.py baseline --llm-smoke 12  # 额外抽 12 题跑真实 LLM answer smoke
    python eval/run_eval.py compare --candidate reranker
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

# ⚠ 必须在任何 `app.core.paths` 导入**之前**激活隔离测试库
from tests import test_env  # noqa: E402

_LIB = test_env.activate_test_library(prefix="usb-wiki-eval-")

from app.core import db as db_mod, indexer, llm, paths, search as S  # noqa: E402
from app.core.embedder import HashEmbedder  # noqa: E402

DIM = 512
TOP_K = 5
_DB = None          # 当前打开的库，退出时先关闭再删临时目录（Windows 上不关就删不掉）
GOLD = HERE / "datasets" / "core_gold.jsonl"          # DEV SET（72 题）
HOLDOUT_GOLD = HERE / "datasets" / "holdout" / "holdout_gold.jsonl"
FIXTURES = HERE / "fixtures"
FIXTURES_HOLDOUT = HERE / "fixtures_holdout"
REPORTS = HERE / "reports"
PRIVATE = HERE / "datasets" / "private"

#: 会做覆盖度评估的类别
COVERAGE_CATS = {"list_coverage", "section_overview", "cross_section"}


# --------------------------------------------------------------------------
# 建库（完全在隔离库内）
# --------------------------------------------------------------------------
def build_library(fixtures_dir: Path | None = None):
    global _DB
    fixtures_dir = fixtures_dir or FIXTURES
    paths.ensure_dirs()
    db = db_mod.get_db(paths.CACHE_DB, embedding_dim=DIM)
    _DB = db
    db.init_schema()
    emb = HashEmbedder(DIM)
    for src in sorted(fixtures_dir.glob("*.md")):
        dst = paths.NOTES_DIR / src.name
        shutil.copyfile(src, dst)
        indexer.index_file(db, dst, emb)
    return db, emb


def load_gold(path: Path | None = None) -> list[dict]:
    rows = []
    with (path or GOLD).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def doc_map(db) -> dict[str, str]:
    return {Path(r["rel_path"]).name: r["doc_id"]
            for r in db.query("SELECT doc_id, rel_path FROM documents")}


def parent_index(db) -> dict[str, list[dict]]:
    """doc_id → 该文档全部父块（含 section_path / 源行范围 / 正文）。"""
    out: dict[str, list[dict]] = defaultdict(list)
    for r in db.query(
        "SELECT parent_id, doc_id, content, COALESCE(section_path,'') AS section_path,"
        " COALESCE(ord,0) AS ord, COALESCE(source_start_line,0) AS s,"
        " COALESCE(source_end_line,0) AS e FROM parent_blocks ORDER BY ord"
    ):
        out[r["doc_id"]].append(dict(r))
    return out


# --------------------------------------------------------------------------
# 单题评测
# --------------------------------------------------------------------------
def _hit_ids(refs, gold_ids, k):
    for r in refs[:k]:
        if r.parent_id in gold_ids:
            return True
    return False


def _first_hit_rank(refs, gold_ids):
    for i, r in enumerate(refs, start=1):
        if r.parent_id in gold_ids:
            return i
    return 0


def _same_line(content: str, a: str, b: str) -> bool:
    """关系必须**同一行**成立（表格行 / 同一句），不能只是"同文共现"。"""
    return any((a in ln and b in ln) for ln in (content or "").splitlines())


def eval_case(case: dict, db, emb, docs: dict[str, str], parents: dict[str, list[dict]]) -> dict:
    exp = case["expected"]
    doc = exp["doc"]
    did = docs.get(doc, "")
    must = exp.get("must_contain") or []
    mustnot = exp.get("must_not_contain") or []
    state = exp.get("answer_state", "answered")

    # gold 证据父块：同文档 + 命中任一 must_contain 词
    gold_ids = {p["parent_id"] for p in parents.get(did, [])
                if any(t in (p["content"] or "") for t in must)} if must else set()
    gold_ranges = [(p["s"], p["e"]) for p in parents.get(did, []) if p["parent_id"] in gold_ids]

    res = S.hybrid_search(db, emb, case["query"], top_k_parents=TOP_K)
    refs = list(res.references)
    top1 = (refs[0].snippet if refs else "") or ""
    top1_parent = next((p for p in parents.get(refs[0].doc_id, []) if p["parent_id"] == refs[0].parent_id),
                       None) if refs else None
    top1_content = (top1_parent or {}).get("content", "") or top1

    out = {
        "id": case["id"], "category": case["category"], "query": case["query"],
        "answer_state": state,
        "n_refs": len(refs), "coverage": bool(res.coverage),
        "qtype": S.analyze_query(case["query"]).question_type,
        "route": res.route,
    }

    # --- 召回 ---
    out["hit@1"] = _hit_ids(refs, gold_ids, 1)
    out["hit@3"] = _hit_ids(refs, gold_ids, 3)
    out["hit@5"] = _hit_ids(refs, gold_ids, 5)
    rr = _first_hit_rank(refs, gold_ids)
    out["rr"] = (1.0 / rr) if rr else 0.0
    out["wrong_doc"] = bool(must) and not any(r.doc_id == did for r in refs)

    # --- 覆盖度 ---
    groups = case.get("evidence_groups") or []
    if groups:
        covered = []
        for g in groups:
            covered.append(any(any(t in (r.snippet or "") or
                                    any(t in (p["content"] or "") for p in parents.get(r.doc_id, [])
                                        if p["parent_id"] == r.parent_id)
                                    for t in g) for r in refs[:5]))
        out["coverage_group_recall"] = sum(covered) / len(groups)
        out["coverage_full"] = all(covered)

    # --- 关系 ---
    rel = exp.get("relation")
    if rel:
        e, v = rel
        ok = False
        for r in refs[:5]:
            for p in parents.get(r.doc_id, []):
                if p["parent_id"] == r.parent_id and _same_line(p["content"], e, v):
                    ok = True
                    break
            if ok:
                break
        out["relation_ok"] = ok

    # --- 负例 / 归因陷阱 ---
    if mustnot:
        out["trap_hit"] = any(t in top1_content for t in mustnot)

    # --- 引用数据 ---
    if refs:
        out["citation_intersect"] = any(
            r.source_start_line > 0 and r.source_end_line >= r.source_start_line
            and any(r.source_start_line <= ge and r.source_end_line >= gs
                    for gs, ge in gold_ranges)
            for r in refs[:5]
        ) if gold_ranges else None

    return out


def citation_structural(db, parents, cases) -> tuple[int, int]:
    """全库引用结构校验：doc_id/parent_id 存在、range 合法、parent 属于该 doc。"""
    ok = tot = 0
    valid_parent_of_doc = {p["parent_id"]: d for d, ps in parents.items() for p in ps}
    for c in cases:
        res = S.hybrid_search(db, None, c["query"], top_k_parents=TOP_K)
        for r in res.references:
            tot += 1
            good = (bool(r.doc_id) and bool(r.parent_id) and r.source_start_line > 0
                    and r.source_end_line >= r.source_start_line
                    and valid_parent_of_doc.get(r.parent_id) == r.doc_id)
            ok += 1 if good else 0
    return ok, tot


# --------------------------------------------------------------------------
# 失败归因
# --------------------------------------------------------------------------
def classify(case, row, db, emb, docs, parents) -> str:
    if row.get("hit@5"):
        groups = case.get("evidence_groups") or []
        if groups and not row.get("coverage_full"):
            return "COVERAGE"
        if case["expected"].get("relation") and row.get("relation_ok") is False:
            return "GROUNDING"
        if row.get("citation_intersect") is False:
            return "CITATION"
        return "OK"
    # top-5 没命中：看宽召回里有没有 → 区分 RECALL / RERANK
    exp = case["expected"]
    did = docs.get(exp["doc"], "")
    must = exp.get("must_contain") or []
    gold_ids = {p["parent_id"] for p in parents.get(did, [])
                if any(t in (p["content"] or "") for t in must)} if must else set()
    wide = S.hybrid_search(db, emb, case["query"], top_k_parents=60)
    if any(r.parent_id in gold_ids for r in wide.references):
        return "RERANK"
    if exp.get("relation"):
        return "GROUNDING"
    return "RECALL"


# --------------------------------------------------------------------------
def run_baseline(args) -> int:
    split = "holdout" if getattr(args, "holdout", False) else "dev"
    gold = HOLDOUT_GOLD if split == "holdout" else GOLD
    fixtures = FIXTURES_HOLDOUT if split == "holdout" else FIXTURES
    report_name = "baseline_holdout" if split == "holdout" else "baseline"
    if split == "holdout":
        print("=" * 70)
        print("HOLDOUT RUN —— 仅用于阶段性 release decision。")
        print("不要根据 Holdout 的逐题失败去调算法（那就是过拟合）。")
        print("=" * 70)
    if not gold.exists():
        print(f"缺少数据集 {gold}，先运行：python eval/build_gold.py")
        return 2
    cases = load_gold(gold)
    t0 = time.time()
    db, emb = build_library(fixtures)
    docs = doc_map(db)
    parents = parent_index(db)

    rows = [eval_case(c, db, emb, docs, parents) for c in cases]
    by_id = {c["id"]: c for c in cases}

    # 指标聚合
    answered = [r for r in rows if r["answer_state"] == "answered"]
    negatives = [r for r in rows if r["category"] == "negative_no_answer"]
    attribution = [r for r in rows if r["category"] == "attribution"]

    def rate(items, key):
        vals = [bool(i.get(key)) for i in items if i.get(key) is not None]
        return (sum(vals) / len(vals)) if vals else None

    cov_rows = [r for r in rows if "coverage_group_recall" in r]
    rel_rows = [r for r in rows if "relation_ok" in r]
    cit_rows = [r for r in rows if r.get("citation_intersect") is not None]
    cit_ok, cit_tot = citation_structural(db, parents, cases)

    metrics = {
        "recall@1": rate(answered, "hit@1"),
        "recall@3": rate(answered, "hit@3"),
        "recall@5": rate(answered, "hit@5"),
        "mrr": (statistics.fmean([r["rr"] for r in answered]) if answered else None),
        "coverage_group_recall": (statistics.fmean([r["coverage_group_recall"] for r in cov_rows])
                                  if cov_rows else None),
        "coverage_full_rate": rate(cov_rows, "coverage_full"),
        "relation_accuracy": rate(rel_rows, "relation_ok"),
        "wrong_document_rate": rate(answered, "wrong_doc"),
        "no_answer_fp_rate": rate(negatives, "trap_hit"),
        "attribution_violation_rate": rate(attribution, "trap_hit"),
        "citation_structural_rate": (cit_ok / cit_tot) if cit_tot else None,
        "citation_intersection_rate": rate(cit_rows, "citation_intersect"),
    }

    # 失败归因
    failures = []
    for r in rows:
        c = by_id[r["id"]]
        bad = (not r["hit@5"]) if r["answer_state"] == "answered" else bool(r.get("trap_hit"))
        if r["answer_state"] == "answered" and (r.get("coverage_full") is False
                                                or r.get("relation_ok") is False):
            bad = True
        if not bad:
            continue
        cls = classify(c, r, db, emb, docs, parents) if not r["hit@5"] else (
            "COVERAGE" if r.get("coverage_full") is False
            else "GROUNDING" if r.get("relation_ok") is False
            else "CITATION")
        failures.append({
            "id": r["id"], "category": r["category"], "query": r["query"],
            "class": cls, "answer_state": r["answer_state"],
            "hit@5": r["hit@5"], "n_refs": r["n_refs"],
            "coverage": r.get("coverage_group_recall"), "trap": r.get("trap_hit"),
        })
    failures.sort(key=lambda x: (x["hit@5"], x["class"]))

    per_cat: dict[str, dict] = {}
    for cat in sorted({c["category"] for c in cases}):
        rs = [r for r in rows if r["category"] == cat]
        ann = [r for r in rs if r["answer_state"] == "answered"]
        per_cat[cat] = {
            "n": len(rs),
            "recall@5": rate(ann, "hit@5"),
            "pass": sum(1 for r in rs if (r["hit@5"] if r["answer_state"] == "answered"
                                          else not r.get("trap_hit"))),
        }

    # 三个点名 Baseline
    def pick(qid):
        r = next((x for x in rows if x["id"] == qid), None)
        return {"query": by_id[qid]["query"], "hit@1": r["hit@1"], "hit@3": r["hit@3"],
                "hit@5": r["hit@5"], "rr": round(r["rr"], 3), "route": r["route"]} if r else None

    highlights = {
        "level_model_query": pick("jkj_m_level_model"),
        "qwen_features": pick("qwen_lc_features"),
        "typhoon_attribution": pick("wea_at_cold_area"),
    }

    llm_smoke = None
    if args.llm_smoke:
        llm_smoke = run_llm_smoke(db, emb, cases, by_id, args.llm_smoke)

    from app.version import APP_VERSION  # noqa: PLC0415

    report = {
        "meta": {
            "app_version": APP_VERSION,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "split": split, "cases": len(cases), "top_k": TOP_K,
            "embedder": f"local_hash({DIM})  # 离线可复现；不依赖 ollama",
            "elapsed_s": round(time.time() - t0, 2),
            "library": str(paths.DATA_DIR),
        },
        "counts_by_category": dict(sorted(Counter(c["category"] for c in cases).items())),
        "metrics": metrics,
        "per_category": per_cat,
        "failures": failures,
        "highlights": highlights,
        "llm_smoke": llm_smoke,
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"{report_name}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORTS / f"{report_name}.md").write_text(render_md(report), encoding="utf-8")
    print_summary(report)
    return 0


# --------------------------------------------------------------------------
def run_llm_smoke(db, emb, cases, by_id, limit: int):
    """抽 N 道代表题跑**真实** provider。不可用则跳过（不引入 LLM-as-Judge）。"""
    picks = [c["id"] for c in cases if c["category"] in
             ("direct_fact", "model_spec", "quantity", "attribution",
              "list_coverage", "negative_no_answer")][:limit]
    try:
        gw = llm.Gateway(db, emb)
        # Gateway 不会自动探活；不先探一次，resolve_provider() 会一律判为 offline。
        gw.ollama_status()
    except Exception as exc:  # noqa: BLE001
        return {"skipped": f"无法构建/探测 Gateway：{exc}"}
    out = []
    for cid in picks:
        c = by_id[cid]
        exp = c["expected"]
        try:
            frames = list(gw.stream_chat(c["query"]))
        except Exception as exc:  # noqa: BLE001
            out.append({"id": cid, "error": str(exc)[:160]})
            continue
        prov = next((f.get("provider") for f in frames if f.get("type") == "meta"), "")
        if prov in ("offline", "error", "", None):
            return {"skipped": f"provider={prov or 'unknown'}（未配置可用聊天模型）",
                    "partial": out}
        text = "".join(f.get("content") or "" for f in frames if f.get("type") == "delta")
        refs = next((f.get("refs") for f in frames if f.get("type") == "references"), []) or []
        must = exp.get("must_contain") or []
        mustnot = exp.get("must_not_contain") or []
        has_cite = any(ch.isdigit() for ch in text if False) or ("[" in text and "]" in text)
        out.append({
            "id": cid, "category": c["category"], "provider": prov,
            "must_contain_hit": sum(1 for t in must if t in text),
            "must_contain_total": len(must),
            "forbidden_leak": [t for t in mustnot if t in text] if c["category"] != "negative_no_answer" else [],
            "has_citation": has_cite, "refs": len(refs),
            "answer_head": text[:120].replace("\n", " "),
        })
    return {"cases": out}


# --------------------------------------------------------------------------
def render_md(rep: dict) -> str:
    m = rep["metrics"]
    def pct(v):
        return "—" if v is None else f"{v*100:.1f}%"

    L = ["# USB-WIKI · RAG Eval Lab V1 — Baseline Report", ""]
    L += [f"- 生成时间：{rep['meta']['generated_at']}",
          f"- 题量：**{rep['meta']['cases']}**（top_k={rep['meta']['top_k']}，"
          f"embedder={rep['meta']['embedder']}）",
          f"- 隔离库：`{rep['meta']['library']}`（TEMP，未触碰真实 Library）",
          f"- 耗时：{rep['meta']['elapsed_s']}s", "",
          "## 总指标", "",
          "| 指标 | 值 |", "| --- | --- |"]
    names = {
        "recall@1": "Evidence Recall@1", "recall@3": "Evidence Recall@3",
        "recall@5": "Evidence Recall@5", "mrr": "MRR",
        "coverage_group_recall": "Coverage Evidence Recall（证据组覆盖）",
        "coverage_full_rate": "Coverage 全覆盖率",
        "relation_accuracy": "Relation Accuracy（同行共现）",
        "wrong_document_rate": "Wrong-document Rate",
        "no_answer_fp_rate": "No-answer False-positive Rate",
        "attribution_violation_rate": "Attribution 误归因率",
        "citation_structural_rate": "Citation 结构合法率",
        "citation_intersection_rate": "Citation 与证据区间相交率",
    }
    for k, label in names.items():
        v = m.get(k)
        L.append(f"| {label} | {pct(v) if 'mrr' != k else ('—' if v is None else f'{v:.3f}')} |")
    L += ["", "## 各类别", "", "| category | n | Recall@5 | 通过 |", "| --- | --- | --- | --- |"]
    for cat, d in rep["per_category"].items():
        L.append(f"| {cat} | {d['n']} | {pct(d['recall@5'])} | {d['pass']}/{d['n']} |")
    L += ["", "## 三个点名 Baseline", ""]
    for name, h in rep["highlights"].items():
        if h:
            L.append(f"- **{name}**：`{h['query']}` → Recall@1={'✅' if h['hit@1'] else '❌'} "
                     f"Recall@3={'✅' if h['hit@3'] else '❌'} Recall@5={'✅' if h['hit@5'] else '❌'} "
                     f"RR={h['rr']} route={h['route']}")
    L += ["", f"## 最差失败 case（共 {len(rep['failures'])}）", "",
          "| id | category | 归因层 | 查询 |", "| --- | --- | --- | --- |"]
    for f in rep["failures"][:10]:
        L.append(f"| {f['id']} | {f['category']} | `{f['class']}` | {f['query']} |")
    if rep.get("llm_smoke"):
        L += ["", "## LLM answer smoke", "", "```json",
              json.dumps(rep["llm_smoke"], ensure_ascii=False, indent=2)[:2000], "```"]
    L += ["", "---", "", "> 本报告仅**测量**，未对 search.py / chunker / prompt / embedding / top_k "
          "做任何改动。Baseline 越真实越有价值。", ""]
    return "\n".join(L)


def print_summary(rep: dict) -> None:
    m = rep["metrics"]
    def pct(v):
        return "  —  " if v is None else f"{v*100:5.1f}%"
    print("\n=== USB-WIKI RAG Eval Lab V1 · Baseline ===")
    print(f"cases={rep['meta']['cases']}  top_k={rep['meta']['top_k']}  "
          f"embedder={rep['meta']['embedder']}")
    for k in ("recall@1", "recall@3", "recall@5"):
        print(f"  {k:24s} {pct(m[k])}")
    print(f"  {'mrr':24s} {m['mrr']:.3f}" if m["mrr"] is not None else "  mrr —")
    for k in ("coverage_group_recall", "coverage_full_rate", "relation_accuracy",
              "wrong_document_rate", "no_answer_fp_rate", "attribution_violation_rate",
              "citation_structural_rate", "citation_intersection_rate"):
        print(f"  {k:24s} {pct(m[k])}")
    name = "baseline_holdout" if rep["meta"].get("split") == "holdout" else "baseline"
    print(f"\n失败 {len(rep['failures'])} 例；报告 → eval/reports/{name}.md")


# --------------------------------------------------------------------------
def cmd_compare(args) -> int:
    base_p = REPORTS / f"{args.baseline}.json"
    cand_p = REPORTS / f"{args.candidate}.json"
    if not base_p.exists():
        print(f"找不到 baseline 报告：{base_p}（先跑 python eval/run_eval.py baseline）")
        return 2
    if not cand_p.exists():
        print(f"[compare] 候选报告尚不存在：{cand_p}")
        print("          V1 只预留接口，**不下载/不集成 reranker**。")
        print("          未来：在 candidate 分支上产出 eval/reports/<name>.json，再执行本命令。")
        return 3
    b = json.loads(base_p.read_text(encoding="utf-8"))["metrics"]
    c = json.loads(cand_p.read_text(encoding="utf-8"))["metrics"]
    print(f"{'metric':28s} {'baseline':>10s} {'candidate':>10s} {'delta':>10s}")
    for k in b:
        vb, vc = b.get(k), c.get(k)
        if vb is None or vc is None:
            continue
        print(f"{k:28s} {vb*100:9.1f}% {vc*100:9.1f}% {(vc-vb)*100:+9.1f}pp")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="eval/run_eval.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("baseline", help="跑全量 gold 并生成 baseline 报告（默认 DEV）")
    b.add_argument("--llm-smoke", type=int, default=0, metavar="N",
                   help="额外抽 N 道题跑真实 provider 的 answer smoke（默认 0=不跑）")
    b.add_argument("--holdout", action="store_true",
                   help="跑 HOLDOUT 集（默认锁定；仅用于阶段性 release decision）")
    c = sub.add_parser("compare", help="对比 baseline 与候选报告")
    c.add_argument("--baseline", default="baseline")
    c.add_argument("--candidate", default="reranker")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "baseline":
            return run_baseline(args)
        return cmd_compare(args)
    finally:
        # 必须先关库再删目录：Windows 上 sqlite 句柄未释放时 rmtree 会失败，
        # 临时库就会在 %TEMP% 里越堆越多。
        if _DB is not None:
            try:
                _DB.checkpoint_and_close()
            except Exception:  # noqa: BLE001
                pass
        # log_util 会把 <DATA_DIR>/wiki-usb.log 一直开着（RotatingFileHandler），
        # 句柄不释放 → rmtree 一定失败（实测残留目录里只剩这一个文件）。
        try:
            import logging  # noqa: PLC0415

            _lg = logging.getLogger("wikiusb")
            for _h in list(_lg.handlers):
                try:
                    _h.close()
                except Exception:  # noqa: BLE001
                    pass
                _lg.removeHandler(_h)
        except Exception:  # noqa: BLE001
            pass
        # Windows 上句柄释放有延迟：删前先显式删 sqlite 文件，再重试几次 rmtree，
        # 否则 %TEMP% 会被 usb-wiki-eval-* 越堆越多。
        for _ in range(4):
            for suffix in ("", "-wal", "-shm"):
                try:
                    (Path(_LIB) / "cache.db").with_name("cache.db" + suffix).unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                test_env.cleanup_test_library(_LIB)
            except Exception:  # noqa: BLE001
                pass
            if not Path(_LIB).exists():
                break
            time.sleep(0.4)
        if Path(_LIB).exists():
            print(f"[warn] 临时库未删除（在 TEMP 内，可手动清理）：{_LIB}")


if __name__ == "__main__":
    raise SystemExit(main())
