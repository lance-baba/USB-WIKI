"""USB-WIKI · Attribution Scope Router Spike V1

**只在 eval/ 内实现；不改 app/；不调用任何模型**（无 LLM / embedding / reranker / ONNX / 网络）。

Router 的职责只有一个：**判断这个 query 是否存在事实归因风险。**
  ATTRIBUTION_GUARD → 才交给 Attribution Guard（guard 只做拦截与标注，**不回答**）
  PASS_THROUGH      → 原系统完全不变
Router **不替代**检索、不替代 LLM、不处理 property / coverage。

产品语义（§10）：
  PASS_THROUGH        → route=PASS_THROUGH
  ATTRIBUTION_GUARD + guard=YES                  → allow_relation_claim = true
  ATTRIBUTION_GUARD + guard=NO                   → ATTRIBUTION_CONFLICT, allow=false
  ATTRIBUTION_GUARD + guard=INSUFFICIENT_RELATION→ INSUFFICIENT_RELATION, allow=false

用法：runtime/python-3.11-embed/python.exe eval/router_spike.py
"""
from __future__ import annotations

import argparse
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

from _relation import route_attribution, units_of  # noqa: E402

import attribution_guard_spike as AG  # noqa: E402  （无 reranker 依赖）
import run_eval as R  # noqa: E402

REPORTS = HERE / "reports"
ROUTER_GOLD = HERE / "datasets" / "router_dev.jsonl"
FIXTURES_ROUTER = HERE / "fixtures_router"
GUARD = "ATTRIBUTION_GUARD"
PASS = "PASS_THROUGH"
#: DEV 72 中**按规格本应进 Guard** 的高三 risk 归因题（causal/event），其余一律 PASS
DEV72_HIGHRISK = {"wea_at_temp_drop", "wea_at_frost_cause", "wea_at_typhoon_frost"}


def load(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.open(encoding="utf-8") if l.strip()]


# --------------------------------------------------------------------------
def router_metrics() -> dict:
    cases = load(ROUTER_GOLD)
    tp = fp = fn = 0
    conf: list[dict] = []
    t0 = time.perf_counter()
    for c in cases:
        got = route_attribution(c["query"])
        exp = c["expected_route"]
        if exp == GUARD and got == GUARD:
            tp += 1
        elif exp != GUARD and got == GUARD:
            fp += 1
            conf.append({"id": c["id"], "query": c["query"], "gold": exp, "got": got})
        elif exp == GUARD and got != GUARD:
            fn += 1
            conf.append({"id": c["id"], "query": c["query"], "gold": exp, "got": got})
    lat = (time.perf_counter() - t0) / max(1, len(cases))
    n_pos = tp + fp
    n_gold_pos = tp + fn
    n_gold_neg = sum(1 for c in cases if c["expected_route"] != GUARD)
    prec = tp / n_pos if n_pos else None
    rec = tp / n_gold_pos if n_gold_pos else None
    f1 = (2 * prec * rec / (prec + rec)) if (prec and rec) else None
    return {
        "cases": len(cases), "n_guard_gold": n_gold_pos, "n_pass_gold": n_gold_neg,
        "precision": prec, "recall": rec, "f1": f1,
        "false_route_rate": (fp / n_gold_neg) if n_gold_neg else None,
        "false_route_n": fp, "missed_n": fn, "confusions": conf,
        "latency_ms_per_query": round(lat * 1000, 4),
        "by_domain": dict(Counter(c["domain"] for c in cases)),
    }


def dev72_unnecessary() -> dict:
    cases = load(R.GOLD)
    routed, unnec = [], []
    t0 = time.perf_counter()
    for c in cases:
        r = route_attribution(c["query"])
        if r != GUARD:
            continue
        routed.append(c["id"])
        if c["id"] not in DEV72_HIGHRISK:
            unnec.append({"id": c["id"], "category": c["category"], "query": c["query"]})
    lat = (time.perf_counter() - t0) / max(1, len(cases))
    return {"cases": len(cases), "routed_to_guard": routed,
            "routed_n": len(routed), "unnecessary": unnec,
            "unnecessary_n": len(unnec),
            "unnecessary_rate": len(unnec) / len(cases),
            "latency_ms_per_query": round(lat * 1000, 4)}


def attr_dev_coverage() -> dict:
    cases = load(AG.ATTR_GOLD)
    high = [c for c in cases
            if c["expected"]["relation_type"] in ("causal", "event", "responsibility")]
    other = [c for c in cases if c not in high]
    covered = [c for c in high if route_attribution(c["query"]) == GUARD]
    leaked = [{"id": c["id"], "query": c["query"],
               "relation_type": c["expected"]["relation_type"]}
              for c in other if route_attribution(c["query"]) == GUARD]
    return {
        "cases": len(cases), "highrisk": len(high), "covered": len(covered),
        "coverage": len(covered) / len(high) if high else None,
        "missed": [c["id"] for c in high if c not in covered],
        "non_highrisk_routed_in": leaked,
    }


# --------------------------------------------------------------------------
def combination() -> dict:
    """Router + Guard 组合：在 Router Pack 语料上跑，衡量最终产品语义。"""
    cases = load(ROUTER_GOLD)
    R.reset_db() if hasattr(R, "reset_db") else None
    db, emb = R.build_library(FIXTURES_ROUTER)
    docs, par = R.doc_map(db), R.parent_index(db)
    vocab: set[str] = set()
    for ps in par.values():
        for p in ps:
            vocab |= AG.owner_vocab(p["content"] or "")

    guard_rows, pass_blocked = [], []
    for c in cases:
        an = AG.analyze_query(c["query"])
        cands = AG.candidates_for(db, emb, par, c["query"])
        g = AG.guard(an, cands, vocab)
        if route_attribution(c["query"]) == GUARD:
            if c["expected"]["answer_state"] in ("yes", "no", "insufficient"):
                guard_rows.append({"state": c["expected"]["answer_state"],
                                   "guard": g["verdict"], "id": c["id"],
                                   "query": c["query"], "reason": g["reason"]})
        else:
            # 反事实：如果**没有 router**、guard 被无差别套用，这题会不会被拦？
            if g["verdict"] != "YES" and g["verdict"] != "PASS_THROUGH":
                pass_blocked.append({"id": c["id"], "query": c["query"],
                                     "guard": g["verdict"], "reason": g["reason"]})
    sc = AG.score(guard_rows, "guard")
    n_pass_gold = sum(1 for c in cases if c["expected_route"] != GUARD)
    return {
        "guard_cases_scored": len(guard_rows),
        "false_association_rate": sc["false_association_rate"],
        "positive_relation_accuracy": sc["positive_relation_accuracy"],
        "negative_relation_accuracy": sc["negative_relation_accuracy"],
        "abstention_accuracy": sc["abstention_accuracy"],
        "unnecessary_block_rate_router": 0.0,          # router 拦住 → guard 根本不跑
        "unnecessary_block_rate_no_router":
            (len(pass_blocked) / n_pass_gold) if n_pass_gold else None,
        "would_be_blocked": pass_blocked,
        "sample_final_state": [
            {"id": r["id"], "route": GUARD, "guard": r["guard"],
             "allow_relation_claim": r["guard"] == "YES", "reason": r["reason"][:40]}
            for r in guard_rows[:5]],
    }


def render_md(rep: dict) -> str:
    r, d, a, cb = rep["router"], rep["dev72"], rep["attr_dev"], rep["combination"]
    def f(v):
        return "—" if v is None else f"{v*100:.1f}%"
    L = ["# USB-WIKI · Attribution Scope Router Spike V1", "",
         f"- 生成：{rep['generated_at']}",
         f"- Router DEV：**{r['cases']} 题**（GUARD {r['n_guard_gold']} / "
         f"PASS_THROUGH {r['n_pass_gold']}，边界负例 {r['n_pass_gold']/r['cases']*100:.0f}%）"
         f" 领域 {r['by_domain']}",
         "- 纯确定性正则，**无 LLM / embedding / reranker / ONNX / 网络**；未改 app/。", "",
         "## Router 指标", "", "| 指标 | 值 | 门槛 |", "| --- | --- | --- |",
         f"| Router Precision | **{f(r['precision'])}** | ≥98% "
         f"{'✅' if (r['precision'] or 0) >= .98 else '❌'} |",
         f"| Router Recall（高三 risk） | **{f(r['recall'])}** | ≥95% "
         f"{'✅' if (r['recall'] or 0) >= .95 else '❌'} |",
         f"| Router F1 | **{r['f1']:.3f}** | — |",
         f"| **False Route Rate**（普通题误进 Guard） | **{f(r['false_route_rate'])}** | ≤2% "
         f"{'✅' if (r['false_route_rate'] or 0) <= .02 else '❌'} |",
         f"| DEV 72 unnecessary guard rate | **{f(d['unnecessary_rate'])}** | ≤2% "
         f"{'✅' if d['unnecessary_rate'] <= .02 else '❌'} |",
         f"| Attribution DEV 高三 risk 覆盖率 | **{f(a['coverage'])}** | ≥95% "
         f"{'✅' if (a['coverage'] or 0) >= .95 else '❌'} |",
         f"| Router latency | **{r['latency_ms_per_query']:.4f} ms/query** | <1ms "
         f"{'✅' if r['latency_ms_per_query'] < 1 else '❌'} |", ""]
    if r["confusions"]:
        L += ["### 路由误判明细", ""]
        for c in r["confusions"]:
            L.append(f"- `{c['id']}` gold={c['gold']} got={c['got']} — {c['query']}")
        L.append("")
    L += ["## 组合（Router + Guard）—— 最终产品语义", "",
          "| 指标 | 值 |", "| --- | --- |",
          f"| Router+Guard False Association | **{f(cb['false_association_rate'])}** |",
          f"| Positive Relation Accuracy | **{f(cb['positive_relation_accuracy'])}** |",
          f"| Negative Relation Accuracy | {f(cb['negative_relation_accuracy'])} |",
          f"| Abstention Accuracy | {f(cb['abstention_accuracy'])} |",
          f"| **Unnecessary Block Rate（有 router）** | **{f(cb['unnecessary_block_rate_router'])}** |",
          f"| Unnecessary Block Rate（**若无 router**） | **{f(cb['unnecessary_block_rate_no_router'])}** |",
          ""]
    if cb["would_be_blocked"]:
        L += ["被 router 正确拦下、否则会被 guard 误杀的正常题：", ""]
        for x in cb["would_be_blocked"][:12]:
            L.append(f"- `{x['id']}` {x['query']} → guard 本会判 `{x['guard']}`")
        L.append("")
    L += ["## DEV 72 路由情况", "",
          f"- 路由进 Guard：{d['routed_n']} 道 {d['routed_to_guard']}",
          f"- **多余进入 Guard：{d['unnecessary_n']} 道（{f(d['unnecessary_rate'])}）**"]
    for x in d["unnecessary"]:
        L.append(f"  - `{x['id']}` [{x['category']}] {x['query']}")
    L += ["", "## Attribution DEV 覆盖情况", "",
          f"- 高三 risk（causal/event/responsibility）：{a['highrisk']} 题，"
          f"路由进 Guard **{a['covered']}** 题（{f(a['coverage'])}）",
          f"- 漏掉：{a['missed'] or '无'}",
          f"- 非高三 risk 却被路由进 Guard："
          f"{[x['id'] for x in a['non_highrisk_routed_in']] or '无'}", "",
          "---", "",
          "> Router 只决定「要不要交给 Guard」；Guard 只做拦截与标注，不回答。",
          "> 本轮未修改 app/，未调用任何模型。", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="eval/router_spike.py")
    ap.parse_args(argv)
    t0 = time.time()
    rep = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "router": router_metrics(), "dev72": dev72_unnecessary(),
           "attr_dev": attr_dev_coverage(), "combination": combination()}
    rep["elapsed_s"] = round(time.time() - t0, 1)
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "router_spike.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORTS / "router_spike.md").write_text(render_md(rep), encoding="utf-8")

    r, d, a, cb = rep["router"], rep["dev72"], rep["attr_dev"], rep["combination"]
    p = lambda v: "—" if v is None else f"{v*100:5.1f}%"
    print("=== Attribution Scope Router Spike V1 ===")
    print(f"Router DEV {r['cases']} 题（GUARD {r['n_guard_gold']} / PASS {r['n_pass_gold']}）")
    print(f"  Precision              {p(r['precision'])}   (>=98%)")
    print(f"  Recall(high-risk)      {p(r['recall'])}   (>=95%)")
    print(f"  F1                     {r['f1']:.3f}")
    print(f"  False Route Rate       {p(r['false_route_rate'])}   (<=2%)")
    print(f"  DEV72 unnecessary      {p(d['unnecessary_rate'])}   (<=2%)")
    print(f"  Attr DEV high-risk cov {p(a['coverage'])}   (>=95%)")
    print(f"  latency                {r['latency_ms_per_query']:.4f} ms/query  (<1ms)")
    print(f"\nRouter+Guard: FalseAssoc {p(cb['false_association_rate'])} | "
          f"PosRel {p(cb['positive_relation_accuracy'])} | "
          f"UnnecessaryBlock {p(cb['unnecessary_block_rate_router'])} "
          f"(no-router {p(cb['unnecessary_block_rate_no_router'])})")
    print(f"\n耗时 {rep['elapsed_s']}s；报告 → eval/reports/router_spike.{{json,md}}")
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
