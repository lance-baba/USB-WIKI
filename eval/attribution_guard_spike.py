"""USB-WIKI · Attribution Guard Spike V1

**只在 eval/ 内实现；不修改任何生产代码**（search.py / llm.py / chunker / prompt / UI）。
**不加载任何 reranker**（不 import onnxruntime、不碰 eval/models/）。

核心命题：
  「相关」 != 「存在事实关系」。
  同一 parent 共现、同一文档、相邻章节、名称相近的实体 —— 都**不构成**关系证据。

Pipeline（生产检索不变的加法层）：
  hybrid_search() → candidate parents → Evidence Unit 切分 → Attribution Guard → 判定/弃答

判定只有三种：YES（有 STRONG/MEDIUM 关系单元） / NO（存在归因冲突） / INSUFFICIENT_RELATION。

用法：
  runtime/python-3.11-embed/python.exe eval/attribution_guard_spike.py
  ... --holdout        # 仅在**算法定稿后**跑一次，看 Holdout A 是否自然修复
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

import _metrics
from app.core import relation_guard as RG  # noqa: E402  （生产单一语义源）

import run_eval as R  # noqa: E402

REPORTS = HERE / "reports"
ATTR_GOLD = HERE / "datasets" / "attribution_dev.jsonl"
FIXTURES_ATTR = HERE / "fixtures_attribution"
TOP_K = 5                       # 生产默认


def naive_verdict(rq, cands: list[dict]) -> str:
    """基线：**只看共现**（现在的系统就是这种语义：词都出现 → 当成有关系）。

    与 guard 用同一份 `rq`，保证 baseline / candidate 只差"看不看 unit"。
    """
    a, t = rq.anchor, rq.target
    blob = " ".join((p.get("content") or "") for p in cands)
    if a and t:
        return "YES" if (a in blob and t in blob) else "INSUFFICIENT_RELATION"
    if a:
        return "YES" if a in blob else "INSUFFICIENT_RELATION"
    if t:
        return "YES" if t in blob else "INSUFFICIENT_RELATION"
    return "PASS_THROUGH"


def load_cases(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
    return out


def reset_db() -> None:
    """清空隔离库的 cache.db，保证每个 split 从零索引（防跨 split 污染）。"""
    if R._DB is not None:
        try:
            R._DB.checkpoint_and_close()
        except Exception:  # noqa: BLE001
            pass
        R._DB = None
    base = R.paths.CACHE_DB
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(base) + suffix).unlink(missing_ok=True)
        except OSError:
            pass


def build(split: str):
    """每个 split **必须用全新库**。

    否则后一个 split 的 fixture 会被索引叠加到前一个之上 —— DEV 查询会多出一批
    attribution 文档作为干扰件，"回归没问题"就可能是假的。这是必须显式清库的地方。
    """
    fixtures = {"attribution": FIXTURES_ATTR, "dev": R.FIXTURES,
                "holdout": R.FIXTURES_HOLDOUT}[split]
    reset_db()
    db, emb = R.build_library(fixtures)
    docs = R.doc_map(db)
    par = R.parent_index(db)
    return db, emb, docs, par


def candidates_for(db, emb, par, q: str, k: int = TOP_K) -> list[dict]:
    res = R.S.hybrid_search(db, emb, q, top_k_parents=k)
    out = []
    for r in res.references:
        content, section = "", ""
        for p in par.get(r.doc_id, []):
            if p["parent_id"] == r.parent_id:
                content = p["content"] or ""
                section = p.get("section_path") or ""
                break
        out.append({"parent_id": r.parent_id, "doc_id": r.doc_id,
                    "content": content, "section_path": section})
    return out


def score(rows: list[dict], key: str) -> dict:
    """rows: [{'state','verdict'}]；verdict 与 state 同为 yes/no/insufficient。"""
    norm = {"YES": "yes", "NO": "no", "INSUFFICIENT_RELATION": "insufficient",
            "PASS_THROUGH": None}
    tp = fp = fn = tn_no = tn_ins = 0
    n_yes = n_no = n_ins = 0
    for r in rows:
        v = norm.get(r[key])
        s = r["state"]
        if s == "yes":
            n_yes += 1
            tp += 1 if v == "yes" else 0
            fn += 1 if v != "yes" else 0
        elif s == "no":
            n_no += 1
            fp += 1 if v == "yes" else 0
            tn_no += 1 if v == "no" else 0
        else:
            n_ins += 1
            fp += 1 if v == "yes" else 0
            tn_ins += 1 if v == "insufficient" else 0
    non_yes = n_no + n_ins
    return {
        "n": len(rows), "n_yes": n_yes, "n_no": n_no, "n_insufficient": n_ins,
        "attribution_precision": (tp / (tp + fp)) if (tp + fp) else None,
        "attribution_recall": (tp / n_yes) if n_yes else None,
        "positive_relation_accuracy": (tp / n_yes) if n_yes else None,
        "negative_relation_accuracy": (tn_no / n_no) if n_no else None,
        "abstention_accuracy": (tn_ins / n_ins) if n_ins else None,
        "false_association_rate": (fp / non_yes) if non_yes else None,
    }


def run_attribution(db, emb, par) -> dict:
    cases = load_cases(ATTR_GOLD)
    rows, rows_in_scope, per_case, t0 = [], [], [], time.perf_counter()
    for c in cases:
        rq = RG.analyze_relation(c["query"])
        cands = candidates_for(db, emb, par, c["query"])
        gr = RG.evaluate_relation(rq, cands)
        rec = {"state": c["expected"]["answer_state"],
               "guard": gr.verdict, "naive": naive_verdict(rq, cands)}
        rows.append(rec)
        if rq.route == RG.ROUTE_GUARD:
            rows_in_scope.append(rec)
        per_case.append({"id": c["id"], "category": c["category"], "query": c["query"],
                         "state": c["expected"]["answer_state"], "guard": gr.verdict,
                         "naive": naive_verdict(rq, cands), "reason": gr.reason,
                         "unit": gr.unit[:80]})
    dt = (time.perf_counter() - t0) / max(1, len(cases))
    # 主口径：只统计 **Router 实际交给 Guard** 的题（causal/event/responsibility）。
    # property / confusion / impact 现在按设计走 PASS_THROUGH（普通检索），
    # 把它们也算进 Guard 指标会把「正确不进 Guard」误判成 guard 失败。
    in_scope = [r for r in rows_in_scope]
    return {"baseline": score(in_scope, "naive"), "candidate": score(in_scope, "guard"),
            "all_cases_baseline": score(rows, "naive"),
            "all_cases_candidate": score(rows, "guard"),
            "in_scope_n": len(in_scope), "out_of_scope_n": len(rows) - len(in_scope),
            "per_case": per_case, "guard_ms_per_query": round(dt * 1000, 2),
            "cases": len(cases),
            "by_category": dict(Counter(c["category"] for c in cases))}


def dev_regression() -> dict:
    """原 DEV 72：guard 只是**加法标注层**，不重排 → 检索指标应完全不变。"""
    cases = R.load_gold()
    reset_db()
    db, emb = R.build_library(R.FIXTURES)
    docs, par = R.doc_map(db), R.parent_index(db)
    rows, rel_q, blocked, blocked_ids = [], 0, 0, []
    t0 = time.perf_counter()
    for c in cases:
        refs = list(R.S.hybrid_search(db, emb, c["query"], top_k_parents=TOP_K).references)
        rows.append({"id": c["id"], "category": c["category"], "refs": refs})
        rq = RG.analyze_relation(c["query"])
        if not rq.is_relation:
            continue
        rel_q += 1
        # ★ 关键风险面：guard 会不会把**普通可回答事实题**也拦下来？
        cands = candidates_for(db, emb, par, c["query"])
        v = RG.evaluate_relation(rq, cands).verdict
        if v != "YES" and c["expected"]["answer_state"] == "answered":
            blocked += 1
            blocked_ids.append({"id": c["id"], "category": c["category"],
                                "query": c["query"], "guard": v})
    ms = (time.perf_counter() - t0) / max(1, len(cases)) * 1000
    agg = R_metrics(cases, rows, par, docs)
    return {"metrics": agg, "relation_type_queries": rel_q, "cases": len(cases),
            "pipeline_ms_per_query": round(ms, 2),
            "guard_blocked_normal_queries": blocked,
            "guard_blocked_ids": blocked_ids}


def R_metrics(cases, rows, par, docs) -> dict:
    """复用 reranker_spike 的指标口径（与 DEV baseline 同源）。"""
    import _metrics as RS  # noqa: PLC0415  （无 reranker 依赖）
    ranking = {r["id"]: r["refs"] for r in rows}
    m = RS.aggregate(RS.metrics_for(cases, ranking, par, docs))
    keep = ("recall@1", "recall@3", "recall@5", "mrr", "coverage_group_recall",
            "coverage_full_rate", "relation_accuracy", "wrong_document_rate",
            "no_answer_fp_rate", "attribution_violation_rate")
    return {k: m.get(k) for k in keep}


def holdout_validation() -> dict:
    cases = R.load_gold(R.HOLDOUT_GOLD)
    db, emb = build("holdout")[0:2]
    docs, par = R.doc_map(db), R.parent_index(db)
    out = {}
    for c in cases:
        rq = RG.analyze_relation(c["query"])
        if not rq.is_relation and c["category"] != "attribution":
            continue
        cands = candidates_for(db, emb, par, c["query"])
        gr = RG.evaluate_relation(rq, cands)
        out[c["id"]] = {"query": c["query"], "state": c["expected"]["answer_state"],
                        "guard": gr.verdict, "naive": naive_verdict(rq, cands),
                        "reason": gr.reason,
                        "forbidden": c["expected"].get("must_not_contain") or []}
    return out


# --------------------------------------------------------------------------
def render_md(rep: dict) -> str:
    a, bl, ca = rep["attribution"], rep["attribution"]["baseline"], rep["attribution"]["candidate"]
    L = ["# USB-WIKI · Attribution Guard Spike V1", "",
         f"- 生成：{rep['generated_at']}",
         f"- Attribution DEV Pack：**{a['cases']} 题**（{a['by_category']}）",
         "- 生产代码未改动；**未加载任何 reranker**；guard 为纯确定性逻辑。", "",
         "## Attribution 指标（baseline = 只看共现 / candidate = guard）", "",
         "| 指标 | Baseline（共现） | Candidate（guard） |", "| --- | --- | --- |"]
    names = [("positive_relation_accuracy", "Positive Relation Accuracy"),
             ("negative_relation_accuracy", "Negative Relation Accuracy"),
             ("abstention_accuracy", "Abstention Accuracy"),
             ("attribution_precision", "Attribution Precision"),
             ("attribution_recall", "Attribution Recall"),
             ("false_association_rate", "False Association Rate")]
    for k, lab in names:
        def f(v):
            return "—" if v is None else f"{v*100:.1f}%"
        L.append(f"| {lab} | {f(bl.get(k))} | **{f(ca.get(k))}** |")
    L += ["", f"（正例 {ca['n_yes']} / 负例 {ca['n_no']} / 无答案 {ca['n_insufficient']}）",
          "",
          f"> 主口径只统计 **Router 交给 Guard 的 {a['in_scope_n']} 题**"
          f"（causal/event/responsibility）；另有 {a['out_of_scope_n']} 题按设计走 "
          f"PASS_THROUGH（property/confusion/impact），不进入 Guard 指标。", ""
          "## 逐题判定（guard）", "", "| id | category | 期望 | guard | 说明 |",
          "| --- | --- | --- | --- | --- |"]
    for c in a["per_case"]:
        L.append(f"| {c['id']} | {c['category']} | {c['state']} | `{c['guard']}` | {c['reason'][:44]} |")
    d = rep["dev_regression"]
    L += ["", "## 原 DEV 72 回归（guard 是加法标注层，不重排）", "",
          f"基线取 **{rep.get(chr(34)+chr(34)) or rep.get('dev_baseline_source')}**（与本次同口径）。",
          "| 指标 | 值 | delta |", "| --- | --- | --- |"]
    base = rep["dev_baseline"]
    for k, v in d["metrics"].items():
        b = base.get(k)
        if v is None or b is None:
            L.append(f"| {k} | — | — |")
            continue
        dv = (v - b) * 100
        L.append(f"| {k} | {v*100:.1f}% | {dv:+.1f}pp |")
    L += ["", f"- 关系型查询数：{d['relation_type_queries']} / {d['cases']}",
          f"- ⚠ **guard 在 DEV 72 上误拦了 {d['guard_blocked_normal_queries']} 道"
          f"本可回答的普通题** → 说明 guard 不能无差别地套在所有关系型查询上",
          f"- 端到端耗时（检索+guard）：{d['pipeline_ms_per_query']} ms/query",
          f"- **guard 自身耗时：{a['guard_ms_per_query']} ms/query**（目标 < 20ms）", ""]
    if rep.get("holdout"):
        L += ["## Holdout A 验证（算法定稿后仅跑一次）", "",
              "| id | 期望 | guard | baseline | 说明 |", "| --- | --- | --- | --- | --- |"]
        for k, v in rep["holdout"].items():
            L.append(f"| {k} | {v['state']} | `{v['guard']}` | {v['naive']} | {v['reason'][:40]} |")
        L += ["", f"- `ho_at_sand_fog` 是否自然修复：**{rep['ho_at_sand_fog_fixed']}**", ""]
    L += ["---", "", "> 只测量，未修改生产代码；未使用 Holdout A 做调参反馈。", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="eval/attribution_guard_spike.py")
    ap.add_argument("--holdout", action="store_true",
                    help="算法定稿后跑一次 Holdout A 验证（不用于调参）")
    args = ap.parse_args(argv)
    t_start = time.time()

    db, emb, docs, par = build("attribution")
    attr = run_attribution(db, emb, par)
    R._DB.checkpoint_and_close() if R._DB else None

    dev = dev_regression()
    # 回归比较必须**同口径**：dev_regression 用的是 reranker_spike 的聚合器
    # （coverage 题按 evidence_groups 判 gold），而 run_eval 的 baseline.json 用的是
    # 严格 ALL-must_contain 规则。二者本来就差 1~2pp —— 拿错基线会把"口径差"误报成
    # "guard 导致退化"。因此优先取 depth_control 的 A0（同一聚合器、未加 guard）。
    dc = REPORTS / "depth_control.json"
    if dc.exists():
        a0 = json.loads(dc.read_text(encoding="utf-8"))["splits"]["dev"]["variants"]["A0_prod_top5"]
        dev_base = {k: a0.get(k) for k in dev["metrics"]}
        dev_base_src = "depth_control.A0（同口径）"
    else:
        dev_base = json.loads((REPORTS / "baseline.json").read_text(encoding="utf-8"))["metrics"]
        dev_base_src = "baseline.json（口径略异，仅参考）"

    rep = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "attribution": attr, "dev_regression": dev, "dev_baseline": dev_base,
           "holdout": None, "ho_at_sand_fog_fixed": None,
           "dev_baseline_source": dev_base_src}

    if args.holdout:
        h = holdout_validation()
        rep["holdout"] = h
        f = h.get("ho_at_sand_fog")
        rep["ho_at_sand_fog_fixed"] = bool(f and f["guard"] in ("NO", "INSUFFICIENT_RELATION"))

    rep["elapsed_s"] = round(time.time() - t_start, 1)
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "attribution_guard.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORTS / "attribution_guard.md").write_text(render_md(rep), encoding="utf-8")

    bl, ca = attr["baseline"], attr["candidate"]
    print("=== Attribution Guard Spike V1 ===")
    print(f"cases={attr['cases']}  yes={ca['n_yes']} no={ca['n_no']} insuff={ca['n_insufficient']}")
    for k in ("positive_relation_accuracy", "negative_relation_accuracy",
              "abstention_accuracy", "attribution_precision", "false_association_rate"):
        b, c = bl.get(k), ca.get(k)
        fb = "—" if b is None else f"{b*100:5.1f}%"
        fc = "—" if c is None else f"{c*100:5.1f}%"
        print(f"  {k:30s} baseline {fb}  →  candidate {fc}")
    print(f"  guard 自身耗时: {attr['guard_ms_per_query']} ms/query")
    print("\nDEV 72 回归:")
    for k, v in dev["metrics"].items():
        b = dev_base.get(k)
        if v is None:
            continue
        print(f"  {k:28s} {v*100:5.1f}%  delta {(v-b)*100:+.1f}pp"
              if b is not None else "")
    if rep["holdout"]:
        print(f"\nHoldout A: ho_at_sand_fog 自然修复 = {rep['ho_at_sand_fog_fixed']}")
        for k, v in rep["holdout"].items():
            print(f"  {k}: {v['state']} -> {v['guard']}")
    print(f"\n耗时 {rep['elapsed_s']}s；报告 → eval/reports/attribution_guard.{{json,md}}")
    return 0


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
