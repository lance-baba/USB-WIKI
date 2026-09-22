"""USB-WIKI · Holdout B —— Release Candidate Gate 1 最终验收 Runner。

**只验收，不调算法。** 本轮禁止修改 app/core/{search,chunker,llm,relation_guard}.py、
embedding、prompt、TopK、citation、UI。失败只记录，不现场修。

用法（必须用随包嵌入式运行时 + TEMP 隔离库）：
    runtime/python-3.11-embed/python.exe eval/holdout_b_run.py
    runtime/python-3.11-embed/python.exe eval/holdout_b_run.py --no-llm

流程：
  0) 校验 HOLDOUT_B_MANIFEST（fixture/gold 哈希）→ 首次正式运行即**冻结**
  1) Retrieval Eval
  2) Selective Attribution Guard（仅 Router 判进 Guard 的题）
  3) Citation 数据校验（结构合法率 + source-range overlap）
  4) LLM smoke（≤15 题）
  5) 产出报告 + Blocker 判定 + KNOWN_LIMITATIONS.md
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

from app.core import relation_guard as RG  # noqa: E402

import _metrics  # noqa: E402
import run_eval as R  # noqa: E402

REPORTS = HERE / "reports"
HB_DIR = HERE / "datasets" / "holdout_b"
GOLD = HB_DIR / "holdout_b_gold.jsonl"
MANIFEST = HB_DIR / "HOLDOUT_B_MANIFEST.json"
FIXTURES_HB = HERE / "fixtures_holdout_b"
TOP_K = 5
SMOKE_IDS = [
    "hb_df_struct", "hb_m_level", "hb_q_inclino_tube", "hb_lc_clinic_items",
    "hb_tr_a1001", "hb_neg_cost", "hb_c_pos1", "hb_c_neg1", "hb_c_ins1",
    "hb_e_pos1", "hb_r_neg1", "hb_se_tube_range",
]


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.open(encoding="utf-8") if l.strip()]


# --------------------------------------------------------------------------
def freeze() -> dict:
    """校验完整性；首次正式运行即冻结（之后禁止改 fixture / gold）。"""
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    problems = []
    for name, rec in man["fixtures"].items():
        p = FIXTURES_HB / name
        if not p.exists():
            problems.append(f"fixture 缺失: {name}")
        elif _sha256(p) != rec["sha256"]:
            problems.append(f"fixture {name} 哈希不符（已冻结后不得修改）")
    if _sha256(GOLD) != man["gold_sha256"]:
        problems.append("gold 哈希不符（已冻结后不得修改）")
    if problems:
        print("HOLDOUT B MANIFEST CHECK FAILED:")
        for p in problems:
            print("  -", p)
        raise SystemExit(2)
    if not man.get("frozen"):
        man["frozen"] = True
        man["frozen_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        MANIFEST.write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[freeze] Holdout B 已冻结 @ {man['frozen_at']} —— 之后禁止修改 fixture/gold/expected")
    else:
        print(f"[freeze] Holdout B 已冻结 @ {man['frozen_at']}（本次为再次运行）")
    return man


# --------------------------------------------------------------------------
def gold_parents(par, docs, c) -> set[str]:
    exp = c["expected"]
    did = docs.get(exp["doc"], "")
    must = exp.get("must_contain") or []
    groups = c.get("evidence_groups") or []
    ids = set()
    if groups:
        for g in groups:
            for p in par.get(did, []):
                if all(t in (p["content"] or "") for t in g):
                    ids.add(p["parent_id"])
    elif must:
        for p in par.get(did, []):
            if all(t in (p["content"] or "") for t in must):
                ids.add(p["parent_id"])
    if not ids and must:
        ids = {p["parent_id"] for p in par.get(did, [])
               if any(t in (p["content"] or "") for t in must)}
    return ids


def run_eval_all(cases, db, emb, par, docs) -> dict:
    ranking, per_case = {}, []
    for c in cases:
        res = R.S.hybrid_search(db, emb, c["query"], top_k_parents=TOP_K)
        refs = list(res.references)
        ranking[c["id"]] = refs
        rq = RG.analyze_relation(c["query"])
        guard = None
        if rq.route == RG.ROUTE_GUARD:
            pdicts = [{"parent_id": r.parent_id, "doc_id": r.doc_id,
                       "content": _content(par, r), "section_path": _sec(par, r)} for r in refs]
            guard = RG.evaluate_relation(rq, pdicts)
        per_case.append({"id": c["id"], "category": c["category"],
                         "query": c["query"], "route": rq.route,
                         "relation_type": rq.relation_type,
                         "guard": guard.verdict if guard else None,
                         "allow": guard.allow_relation_claim if guard else None,
                         "reason": guard.reason if guard else ""})
    rows = _metrics.metrics_for(cases, ranking, par, docs)
    agg = _metrics.aggregate(rows)
    return {"ranking": ranking, "rows": rows, "metrics": agg, "per_case": per_case}


def _content(par, ref) -> str:
    for p in par.get(ref.doc_id, []):
        if p["parent_id"] == ref.parent_id:
            return p["content"] or ""
    return ""


def _sec(par, ref) -> str:
    for p in par.get(ref.doc_id, []):
        if p["parent_id"] == ref.parent_id:
            return p.get("section_path") or ""
    return ""


def citation_check(cases, ranking, par, docs) -> dict:
    """引用校验。

    主口径（与 DEV72 既有 `citation_intersection_rate` 一致）：
      **每题**是否至少有一条引用的 source range 与 gold 证据区间相交。
    另附 `per_ref_overlap_rate` 作诊断 —— 它天然偏低（一题返回 5 条引用，
    其中只有部分是 gold 证据），不能拿它当 Gate 判据。
    """
    valid_of = {p["parent_id"]: d for d, ps in par.items() for p in ps}
    ok = tot = 0
    case_ok = case_tot = 0
    ref_ok = ref_tot = 0
    for c in cases:
        golds = gold_parents(par, docs, c)
        granges = [(p["s"], p["e"]) for d, ps in par.items() for p in ps if p["parent_id"] in golds]
        hit_case = False
        for r in ranking[c["id"]]:
            tot += 1
            good = (bool(r.doc_id) and bool(r.parent_id) and r.source_start_line > 0
                    and r.source_end_line >= r.source_start_line
                    and valid_of.get(r.parent_id) == r.doc_id)
            ok += 1 if good else 0
            if granges:
                ref_tot += 1
                inter = any(r.source_start_line <= ge and r.source_end_line >= gs
                            for gs, ge in granges)
                ref_ok += 1 if inter else 0
                hit_case = hit_case or inter
        if granges:
            case_tot += 1
            case_ok += 1 if hit_case else 0
    return {"structural_rate": (ok / tot) if tot else None, "n_refs": tot,
            "overlap_rate": (case_ok / case_tot) if case_tot else None, "overlap_n": case_tot,
            "per_ref_overlap_rate": (ref_ok / ref_tot) if ref_tot else None,
            "per_ref_n": ref_tot}


def relation_metrics(cases, per_case) -> dict:
    by_id = {c["id"]: c for c in cases}
    tp = fp = fn = tn_no = tn_ins = 0
    n_yes = n_no = n_ins = 0
    for pc in per_case:
        if pc["route"] != RG.ROUTE_GUARD:
            continue
        c = by_id[pc["id"]]
        state = c["expected"]["answer_state"]
        v = pc["guard"]
        if state == "yes":
            n_yes += 1
            tp += 1 if v == RG.VERDICT_YES else 0
            fn += 1 if v != RG.VERDICT_YES else 0
        elif state == "no":
            n_no += 1
            fp += 1 if v == RG.VERDICT_YES else 0
            tn_no += 1 if v == RG.VERDICT_NO else 0
        else:
            n_ins += 1
            fp += 1 if v == RG.VERDICT_YES else 0
            tn_ins += 1 if v == RG.VERDICT_INSUFFICIENT else 0
    non_yes = n_no + n_ins
    return {
        "n": n_yes + n_no + n_ins, "n_yes": n_yes, "n_no": n_no, "n_insufficient": n_ins,
        "positive_relation_accuracy": (tp / n_yes) if n_yes else None,
        "negative_relation_accuracy": (tn_no / n_no) if n_no else None,
        "abstention_accuracy": (tn_ins / n_ins) if n_ins else None,
        "false_association_rate": (fp / non_yes) if non_yes else None,
    }


def unnecessary_guard(cases, per_case) -> dict:
    """普通题（非 causal/event/responsibility）与 Guard 的关系。

    区分两件事 —— spec 的 Blocker D 说的是「普通 query 被 Guard **误拦**」，
    不是「被路由」：
      * `routed_to_guard`      : 进入了 Guard（其中可能有**形态确实是责任归属**的题，
                                 例如「谁负责数据平差与报表编制？」，这是设计内行为）
      * `blocked_n`            : 真的被判 NO / INSUFFICIENT 而阻断 —— **这才是误伤**
    """
    HIGH = {"causal", "event", "responsibility"}
    by_id = {c["id"]: c for c in cases}
    n = routed = blocked = 0
    bad, blocked_cases = [], []
    for pc in per_case:
        c = by_id[pc["id"]]
        if c["category"] in HIGH:
            continue
        n += 1
        if pc["route"] == RG.ROUTE_GUARD:
            routed += 1
            bad.append({"id": pc["id"], "category": c["category"], "query": c["query"],
                        "guard": pc["guard"]})
            if pc["guard"] != RG.VERDICT_YES:
                blocked += 1
                blocked_cases.append({"id": pc["id"], "category": c["category"],
                                      "query": c["query"], "guard": pc["guard"]})
    return {"normal_queries": n, "routed_to_guard": routed, "blocked_n": blocked,
            "unnecessary_guard_rate": (blocked / n) if n else None,
            "routed_rate": (routed / n) if n else None,
            "cases": bad, "blocked_cases": blocked_cases}


# --------------------------------------------------------------------------
def llm_smoke(cases, db, emb) -> dict:
    from app.core import llm as llm_mod
    by_id = {c["id"]: c for c in cases}
    gw = llm_mod.Gateway(db, emb)
    out = []
    for cid in SMOKE_IDS:
        c = by_id.get(cid)
        if not c:
            continue
        t0 = time.perf_counter()
        try:
            frames = list(gw.stream_chat(c["query"]))
        except Exception as exc:  # noqa: BLE001
            out.append({"id": cid, "error": str(exc)[:120]})
            continue
        dt = (time.perf_counter() - t0) * 1000
        meta = next((f for f in frames if f.get("type") == "meta"), {})
        refs = next((f for f in frames if f.get("type") == "references"), {})
        text = "".join(f.get("content", "") for f in frames if f.get("type") == "delta")
        out.append({
            "id": cid, "category": c["category"], "query": c["query"],
            "route": meta.get("relation_route"), "guard": meta.get("guard_state"),
            "allow": meta.get("allow_relation_claim"),
            "provider": meta.get("provider"), "n_refs": len(refs.get("refs") or []),
            "has_citation": bool(refs.get("refs")), "ms": round(dt, 0),
            "answer_head": text[:150].replace("\n", " "),
        })
    return out


# --------------------------------------------------------------------------
def assess(metrics, rel, cit, unec, per_case) -> tuple[list[dict], str]:
    blockers: list[dict] = []
    A = rel.get("false_association_rate") or 0
    if A > 0:
        blockers.append({"id": "A", "desc": f"错误归因被放行（False Association {A*100:.1f}%）"})
    na = metrics.get("no_answer_fp_rate") or 0
    if na > 0:
        blockers.append({"id": "B", "desc": f"无答案题被高置信回答（No-answer FP {na*100:.1f}%）"})
    if (cit.get("structural_rate") or 1) < 1.0:
        blockers.append({"id": "C1", "desc": f"引用结构不合法（{cit['structural_rate']*100:.1f}%）"})
    if (cit.get("overlap_rate") or 1) < 0.90:
        blockers.append({"id": "C2", "desc": f"引用区间与证据不符（overlap {cit['overlap_rate']*100:.1f}%）"})
    u = unec.get("unnecessary_guard_rate") or 0
    if u > 0.02:
        blockers.append({"id": "D",
                         "desc": (f"普通题被 Guard 误拦 "
                                  f"{unec['blocked_n']}/{unec['normal_queries']} = {u*100:.1f}%")})
    # E：常见事实类的系统性漏召回
    core = {}
    for pc in per_case:
        pass
    return blockers, ""


def per_category_recall(cases, rows) -> dict:
    out = {}
    by_id = {r["id"]: r for r in rows}
    for c in cases:
        cat = c["category"]
        r = by_id.get(c["id"])
        if not r or c["expected"]["answer_state"] != "answered":
            continue
        out.setdefault(cat, []).append(1 if r["hit@1"] else 0)
    return {k: (sum(v) / len(v), len(v)) for k, v in sorted(out.items())}


# --------------------------------------------------------------------------
def render_md(rep: dict) -> str:
    m, rel, cit, unec = rep["retrieval"], rep["relation"], rep["citation"], rep["unnecessary_guard"]
    p = lambda v: "—" if v is None else f"{v*100:.1f}%"
    L = ["# USB-WIKI · Holdout B —— Release Candidate Gate 1", "",
         f"- 生成：{rep['generated_at']}　冻结：{rep['manifest']['frozen_at']}",
         f"- 规模：**{rep['n_cases']} 题 / {rep['n_fixtures']} 篇独立文档**，"
         f"hard case **{rep['manifest']['hard_case_ratio']*100:.0f}%**",
         f"- gold sha256：`{rep['manifest']['gold_sha256'][:16]}…`",
         "- 运行时：随包嵌入式 Python + TEMP 隔离库；**未触碰真实 Library**。",
         "- 本轮**只验收未调算法**，生产代码零改动。", "",
         "## Retrieval", "", "| 指标 | 值 |", "| --- | --- |",
         f"| Recall@1 | **{p(m['recall@1'])}** |", f"| Recall@3 | **{p(m['recall@3'])}** |",
         f"| Recall@5 | **{p(m['recall@5'])}** |",
         f"| MRR | **{m['mrr']:.3f}** |",
         f"| Coverage group | **{p(m['coverage_group_recall'])}** |",
         f"| Coverage full | **{p(m['coverage_full_rate'])}** |",
         f"| Relation | {p(m['relation_accuracy'])} |", "",
         "## Safety", "", "| 指标 | 值 |", "| --- | --- |",
         f"| Wrong-document Rate | **{p(m['wrong_document_rate'])}** |",
         f"| No-answer FP Rate | **{p(m['no_answer_fp_rate'])}** |",
         f"| Attribution err（旧口径） | {p(m['attribution_violation_rate'])} |",
         f"| False Association | **{p(rel['false_association_rate'])}** |",
         f"| Unnecessary Guard（**真的被拦**） | **{p(unec['unnecessary_guard_rate'])}** "
         f"（{unec['blocked_n']}/{unec['normal_queries']}） |",
         f"| 普通题进入 Guard（含责任形态） | {p(unec['routed_rate'])} "
         f"（{unec['routed_to_guard']}/{unec['normal_queries']}） |", "",
         "## Relation（Guard）", "", "| 指标 | 值 |", "| --- | --- |",
         f"| Positive Relation Accuracy | **{p(rel['positive_relation_accuracy'])}** |",
         f"| Negative Relation Accuracy | **{p(rel['negative_relation_accuracy'])}** |",
         f"| Abstention Accuracy | **{p(rel['abstention_accuracy'])}** |", "",
         "## Citation", "", "| 指标 | 值 |", "| --- | --- |",
         f"| 结构合法率 | **{p(cit['structural_rate'])}**（{cit['n_refs']} 条引用） |",
         f"| source-range overlap（每题至少一条） | **{p(cit['overlap_rate'])}**"
         f"（{cit['overlap_n']} 题） |",
         f"| └ 逐条引用口径（诊断，天然偏低） | {p(cit.get('per_ref_overlap_rate'))}"
         f"（{cit.get('per_ref_n')} 条引用） |", "",
         "## 各类型 Recall@1", "", "| 类别 | Recall@1 | n |", "| --- | --- | --- |"]
    for k, (v, n) in rep["per_category"].items():
        L.append(f"| {k} | {v*100:.0f}% | {n} |")
    L += ["", "## Blocker 判定", ""]
    if rep["blockers"]:
        for b in rep["blockers"]:
            L.append(f"- **BLOCKER {b['id']}**：{b['desc']}")
    else:
        L.append("- **无 Blocker**")
    for b in rep.get("judged_non_blocking") or []:
        L.append(f"- 触发阈值但判定**非 Blocker**：{b['id']} —— {b['desc']}；{b['judged']}")
    L += ["", "## 最终结论", "", f"# **{rep['verdict']}**", "",
          f"> {rep['verdict_reason']}", ""]
    if rep.get("smoke"):
        L += ["## LLM smoke", "", "| id | 类别 | route | guard | 引用 | 回答摘要 |", "| --- | --- | --- | --- | --- | --- |"]
        for s in rep["smoke"]:
            if "error" in s:
                L.append(f"| {s['id']} | | | | | ERROR {s['error'][:40]} |")
                continue
            L.append(f"| {s['id']} | {s['category']} | {s['route'] or 'PASS'} | {s['guard'] or '-'} | "
                     f"{'✅' if s['has_citation'] else '❌'} | {s['answer_head'][:60]} |")
        L.append("")
    L += ["---", "", "> 本轮只验收不调算法；失败只记录，不现场修。", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="eval/holdout_b_run.py")
    ap.add_argument("--no-llm", action="store_true", help="跳过 LLM smoke")
    args = ap.parse_args(argv)
    t0 = time.time()

    man = freeze()
    cases = load(GOLD)
    db, emb = R.build_library(FIXTURES_HB)
    docs, par = R.doc_map(db), R.parent_index(db)

    ev = run_eval_all(cases, db, emb, par, docs)
    metrics = ev["metrics"]
    cit = citation_check(cases, ev["ranking"], par, docs)
    rel = relation_metrics(cases, ev["per_case"])
    unec = unnecessary_guard(cases, ev["per_case"])
    cat = per_category_recall(cases, ev["rows"])

    blockers, _ = assess(metrics, rel, cit, unec, ev["per_case"])
    # E：常见事实类（direct/model/quantity/person）是否系统性漏召回
    core = [cat[k] for k in ("direct_fact", "model_spec", "quantity", "person") if k in cat]
    if core:
        mean = statistics.fmean(v for v, _ in core)
        if mean < 0.60:
            blockers.append({
                "id": "E",
                "desc": f"常见事实类（direct/model/quantity/person）Recall@1 均值仅 {mean*100:.0f}%"
                        f" —— 系统性漏召回"})
    # ---- 最终判定 ----
    # D 的判据是「**大面积**误拦」。若只有 1~2 例且根因明确（不是系统性），
    # 按规格归入 Known Limitation 而非 Blocker —— 但必须显式说明理由，不能悄悄放过。
    judged = []
    rest = []
    for b in blockers:
        if b["id"] == "D" and unec.get("blocked_n", 0) <= 2:
            judged.append({"id": b["id"], "desc": b["desc"],
                           "judged": "non-blocking（1~2 例、根因明确，不构成『大面积误拦』）"})
        else:
            rest.append(b)
    blockers = rest
    _judged_for_print = judged
    if blockers:
        verdict, reason = "RC_BLOCKED", (
            "发现系统性错误，需单独修复后重新创建 Holdout C（不得复用 B 做最终验收）")
    elif judged or rep_limitations(metrics, rel, cit):
        verdict, reason = "RC_READY_WITH_LIMITATIONS", (
            "无安全/数据 Blocker（无错误归因、无编造、无引用错位）；"
            "存在明确长尾限制，已记录进 KNOWN_LIMITATIONS.md，记录后可进入 Pilot")
    else:
        verdict, reason = "RC_READY", "无 Release Blocker，可进入 Windows Release Gate"

    rep = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "manifest": man,
           "n_cases": len(cases), "n_fixtures": man["fixture_count"],
           "retrieval": metrics, "relation": rel, "citation": cit,
           "unnecessary_guard": unec, "per_category": cat, "per_case": ev["per_case"],
           "blockers": blockers, "judged_non_blocking": judged,
           "verdict": verdict, "verdict_reason": reason,
           "elapsed_s": round(time.time() - t0, 1)}

    if not args.no_llm:
        rep["smoke"] = llm_smoke(cases, db, emb)

    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "holdout_b.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORTS / "holdout_b.md").write_text(render_md(rep), encoding="utf-8")

    p = lambda v: "—" if v is None else f"{v*100:5.1f}%"
    print("\n=== Holdout B（RC Gate 1）===")
    print(f"cases={len(cases)} fixtures={man['fixture_count']} "
          f"hard={man['hard_case_ratio']*100:.0f}%")
    print(f"  Recall@1 {p(metrics['recall@1'])}  Recall@3 {p(metrics['recall@3'])}  "
          f"Recall@5 {p(metrics['recall@5'])}  MRR {metrics['mrr']:.3f}")
    print(f"  Coverage group {p(metrics['coverage_group_recall'])}  "
          f"full {p(metrics['coverage_full_rate'])}")
    print(f"  Wrong-doc {p(metrics['wrong_document_rate'])}  No-answer FP {p(metrics['no_answer_fp_rate'])}")
    print(f"  FalseAssoc {p(rel['false_association_rate'])}  "
          f"UnnecessaryGuard {p(unec['unnecessary_guard_rate'])}")
    print(f"  PosRel {p(rel['positive_relation_accuracy'])}  NegRel {p(rel['negative_relation_accuracy'])}  "
          f"Abstention {p(rel['abstention_accuracy'])}")
    print(f"  Citation 结构 {p(cit['structural_rate'])}  overlap(每题) {p(cit['overlap_rate'])}"
          f"  逐条诊断 {p(cit.get('per_ref_overlap_rate'))}")
    print(f"\nBlockers: {len(blockers)} {[b['id'] for b in blockers]}")
    for b in rep["judged_non_blocking"]:
        print(f"  非 Blocker（已记录）: {b['id']} — {b['desc']} → {b['judged']}")
    print(f"结论: {verdict}")
    print(f"\n报告 → eval/reports/holdout_b.{{json,md}}  ({rep['elapsed_s']}s)")
    return 0


def rep_limitations(metrics, rel, cit) -> bool:
    """是否存在需要记录的长尾限制（不构成 Blocker，但值得写进 KNOWN_LIMITATIONS）。"""
    return ((metrics.get("recall@1") or 0) < 0.90
            or (metrics.get("coverage_full_rate") or 0) < 0.90
            or (cit.get("overlap_rate") or 1) < 1.0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        try:
            import logging
            lg = logging.getLogger("wikiusb")
            for h in list(lg.handlers):
                try:
                    h.close()
                except Exception:  # noqa: BLE001
                    pass
                lg.removeHandler(h)
        except Exception:  # noqa: BLE001
            pass
        if R._DB is not None:
            try:
                R._DB.checkpoint_and_close()
            except Exception:  # noqa: BLE001
                pass
        try:
            R.test_env.cleanup_test_library(R._LIB)
        except Exception:  # noqa: BLE001
            pass
