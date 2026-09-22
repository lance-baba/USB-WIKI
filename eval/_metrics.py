"""共享评测指标口径（**不依赖任何 reranker / onnxruntime**）。

从 reranker_spike.py 原样抽出，供 reranker A/B 与 attribution guard 共用同一套口径 ——
避免「两套实现悄悄漂移」，也让 attribution guard 完全不需要引入 ONNX 运行时就存在。
"""
from __future__ import annotations

import statistics
from collections import defaultdict

def _content_of(parents, ref):
    for p in parents.get(ref.doc_id, []):
        if p["parent_id"] == ref.parent_id:
            return p["content"] or ""
    return ""


def _same_line(content: str, a: str, b: str) -> bool:
    """关系必须**同一行**成立（表格行 / 同一句），不能只是"同文共现"。"""
    return any((a in ln and b in ln) for ln in (content or "").splitlines())


def gold_ids_for(case, parents, docs):
    exp = case["expected"]
    did = docs.get(exp["doc"], "")
    ps = parents.get(did, [])
    groups = case.get("evidence_groups") or []
    must = exp.get("must_contain") or []
    ids = set()
    if groups:
        for g in groups:
            for p in ps:
                if all(t in (p["content"] or "") for t in g):
                    ids.add(p["parent_id"])
    elif must:
        for p in ps:
            if all(t in (p["content"] or "") for t in must):
                ids.add(p["parent_id"])
    if not ids and must:                       # coverage 类跨章节：退化为"任一期望词"
        ids = {p["parent_id"] for p in ps if any(t in (p["content"] or "") for t in must)}
    return ids


def metrics_for(cases, ranking, parents, docs):
    """ranking: {case_id: [refs...]}（已重排，取前 5 用）。"""
    rows = []
    for c in cases:
        refs = ranking[c["id"]]
        g = gold_ids_for(c, parents, docs)
        exp = c["expected"]
        mustnot = exp.get("must_not_contain") or []
        ranks = [i for i, r in enumerate(refs[:5], start=1) if r.parent_id in g]
        rr = 1.0 / ranks[0] if ranks else 0.0
        row = {
            "id": c["id"], "category": c["category"], "answer_state": exp["answer_state"],
            "hit@1": bool(ranks and ranks[0] == 1), "hit@3": bool(ranks and ranks[0] <= 3),
            "hit@5": bool(ranks), "rr": rr,
            "wrong_doc": bool(g) and not any(r.doc_id == docs.get(exp["doc"], "") for r in refs[:5]),
        }
        groups = c.get("evidence_groups") or []
        if groups:
            cov = [any(any(t in _content_of(parents, r) for t in grp) for r in refs[:5])
                   for grp in groups]
            row["cov_group"] = sum(cov) / len(groups)
            row["cov_full"] = all(cov)
        if mustnot:
            top1 = _content_of(parents, refs[0]) if refs else ""
            row["trap"] = any(t in top1 for t in mustnot)
        rel = exp.get("relation")
        if rel:
            e, v = rel
            row["relation_ok"] = any(
                _same_line(_content_of(parents, r), e, v) for r in refs[:5])
        rows.append(row)
    return rows


def aggregate(rows):
    ans = [r for r in rows if r["answer_state"] == "answered"]
    neg = [r for r in rows if r["category"] == "negative_no_answer"]
    att = [r for r in rows if r["category"] == "attribution"]
    covr = [r for r in rows if "cov_group" in r]
    def rate(xs, k):
        v = [bool(x[k]) for x in xs if x.get(k) is not None]
        return sum(v) / len(v) if v else None
    def cat1(cat):
        sub = [r for r in ans if r["category"] == cat]
        return rate(sub, "hit@1") if sub else None
    return {
        "recall@1": rate(ans, "hit@1"), "recall@3": rate(ans, "hit@3"),
        "recall@5": rate(ans, "hit@5"),
        "mrr": statistics.fmean([r["rr"] for r in ans]) if ans else None,
        "coverage_group_recall": statistics.fmean([r["cov_group"] for r in covr]) if covr else None,
        "coverage_full_rate": rate(covr, "cov_full"),
        "relation_accuracy": rate([r for r in rows if "relation_ok" in r], "relation_ok"),
        "wrong_document_rate": rate(ans, "wrong_doc"),
        "no_answer_fp_rate": rate(neg, "trap"),
        "attribution_violation_rate": rate(att, "trap"),
        "cat_model_spec_r@1": cat1("model_spec"),
        "cat_person_r@1": cat1("person"),
        "cat_similar_entity_r@1": cat1("similar_entity"),
        "cat_table_relation_r@1": cat1("table_relation"),
        "cat_direct_fact_r@1": cat1("direct_fact"),
    }
