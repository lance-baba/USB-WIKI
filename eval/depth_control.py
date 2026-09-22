"""USB-WIKI · Eval Lab V1.1 —— Candidate Depth Control

目的：把上一轮 reranker 报告里的 **Recall@3/5 与 Coverage 提升** 中，
「仅仅是候选池加深带来的」与「reranker 重排带来的」彻底分开。

三个对照（**同一套生产 `hybrid_search()`，算法一行未改**）：
  A0 = 生产默认：取 top_k_parents=5
  A2 = 取 top_k_parents=10 → **保持原始排序** → 截 Top5
  A1 = 取 top_k_parents=20 → **保持原始排序** → 截 Top5

严禁：改 score / 改 query analysis / 改 Gold / 加 bonus / 加模型。

结论判定标准：若 A1/A2 与 A0 的指标完全相同，则**候选池深度对头部指标零贡献**，
上一轮的全部提升都来自重排。

用法：runtime/python-3.11-embed/python.exe eval/depth_control.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

import reranker_spike as RS  # noqa: E402  （导入期即激活隔离测试库 + 保险丝）
import run_eval as R  # noqa: E402

REPORTS = HERE / "reports"
#: 名称 → top_k_parents（生产 hybrid_search 的参数，不改算法）
VARIANTS = {"A0_prod_top5": 5, "A2_pool10": 10, "A1_pool20": 20}
KEYS = ["recall@1", "recall@3", "recall@5", "mrr", "coverage_group_recall",
        "coverage_full_rate", "relation_accuracy", "wrong_document_rate",
        "no_answer_fp_rate", "attribution_violation_rate"]


def run_split(split: str) -> dict:
    gold = R.HOLDOUT_GOLD if split == "holdout" else R.GOLD
    fixtures = R.FIXTURES_HOLDOUT if split == "holdout" else R.FIXTURES
    if not gold.exists():
        return {}
    cases = R.load_gold(gold)
    db, emb = R.build_library(fixtures)
    docs, parents = R.doc_map(db), R.parent_index(db)

    out: dict = {"split": split, "cases": len(cases), "variants": {}, "top5_identical": {}}
    pids: dict[str, dict[str, list[str]]] = {}
    for name, k in VARIANTS.items():
        ranking, pool_hit, tot = {}, 0, 0
        for c in cases:
            refs = list(R.S.hybrid_search(db, emb, c["query"], top_k_parents=k).references)
            ranking[c["id"]] = refs[:5]
            g = RS.gold_ids_for(c, parents, docs)
            if g:
                tot += 1
                pool_hit += 1 if any(r.parent_id in g for r in refs[:k]) else 0
        rows = RS.metrics_for(cases, ranking, parents, docs)
        agg = RS.aggregate(rows)
        agg["candidate_recall@pool"] = (pool_hit / tot) if tot else None
        agg["pool"] = k
        out["variants"][name] = agg
        pids[name] = {cid: [r.parent_id for r in v] for cid, v in ranking.items()}

    # 关键证据：加深 pool 后 top-5（顺序也一并比）有没有变
    for a, b in (("A0_prod_top5", "A2_pool10"), ("A0_prod_top5", "A1_pool20")):
        diff = [cid for cid in pids[a] if pids[a][cid] != pids[b][cid]]
        out["top5_identical"][f"{a}__vs__{b}"] = {
            "identical": not diff, "changed_cases": diff, "n_changed": len(diff)}

    # 并入上一轮 reranker（主 pool）做并列比较
    bench = REPORTS / "reranker_benchmark.json"
    if bench.exists() and split == "dev":
        b = json.loads(bench.read_text(encoding="utf-8"))
        main = b["meta"].get("main_pool") or f"N{max(b['meta']['pools'])}"
        out["reranker_main_pool"] = main
        for mn, m in b["models"].items():
            row = dict(m["results"][main])
            row["pool"] = int(main[1:])
            row["disk_mb"] = m["disk_mb"]
            row["ram_warm_mb"] = m["rss_warm_delta_mb"]
            row["latency_ms_p50"] = m["results"][main]["latency_ms_p50"]
            out["variants"][f"rerank:{mn}"] = row
    return out


def render_md(d: dict) -> str:
    names = list(d["variants"])
    L = [f"# USB-WIKI · Candidate Depth Control（split = {d['split']}，{d['cases']} 题）", "",
         "同一套生产 `hybrid_search()`，**未改任何算法**。A1/A2 只加深候选池并**保持原始排序**，",
         "再截 Top5；rerank 两列来自上一轮 Spike（pool=N20）。", ""]
    L += ["| 指标 | " + " | ".join(names) + " |",
          "| --- | " + " | ".join(["---"] * len(names)) + " |"]
    labels = {"recall@1": "Recall@1", "recall@3": "Recall@3", "recall@5": "Recall@5",
              "mrr": "MRR", "coverage_group_recall": "Coverage group",
              "coverage_full_rate": "Coverage full", "relation_accuracy": "Relation",
              "wrong_document_rate": "Wrong-doc", "no_answer_fp_rate": "No-answer FP",
              "attribution_violation_rate": "Attribution err",
              "candidate_recall@pool": "Candidate Recall@pool",
              "latency_ms_p50": "CPU p50", "ram_warm_mb": "RAM(预热)", "disk_mb": "磁盘"}
    def fmt(k, v):
        if v is None:
            return "—"
        if k == "mrr":
            return f"{v:.3f}"
        if k in ("latency_ms_p50",):
            return f"{v:.0f}ms"
        if k in ("ram_warm_mb", "disk_mb"):
            return f"{v:.0f}MB"
        return f"{v*100:.1f}%"
    for k in KEYS + ["candidate_recall@pool", "latency_ms_p50", "ram_warm_mb", "disk_mb"]:
        if not any(k in d["variants"][n] for n in names):
            continue
        L.append(f"| {labels[k]} | " + " | ".join(
            fmt(k, d["variants"][n].get(k)) for n in names) + " |")

    L += ["", "## 关键证据：加深 pool 是否改变了 top-5（含顺序）", ""]
    for k, v in d["top5_identical"].items():
        state = "完全相同" if v["identical"] else f"有 {v['n_changed']} 题不同"
        tail = f" → {v['changed_cases']}" if v["changed_cases"] else ""
        L.append(f"- `{k}`：**{state}**{tail}")
    a0, a1 = d["variants"]["A0_prod_top5"], d["variants"]["A1_pool20"]
    d1 = ((a1["recall@1"] or 0) - (a0["recall@1"] or 0)) * 100
    d3 = ((a1["recall@3"] or 0) - (a0["recall@3"] or 0)) * 100
    dc = ((a1["coverage_full_rate"] or 0) - (a0["coverage_full_rate"] or 0)) * 100
    L += ["", "## 结论：pool depth 的净贡献", "",
          f"- Recall@1：pool 10→20 带来 **{d1:+.1f}pp**",
          f"- Recall@3：pool 10→20 带来 **{d3:+.1f}pp**",
          f"- Coverage full：pool 10→20 带来 **{dc:+.1f}pp**",
          f"- Candidate Recall@pool：A0 {fmt('x', a0.get('candidate_recall@pool'))} / "
          f"A2 {fmt('x', d['variants']['A2_pool10'].get('candidate_recall@pool'))} / "
          f"A1 {fmt('x', a1.get('candidate_recall@pool'))}",
          "",
          "> 若上表显示 pool 加深后 **top-5 与指标完全不变**，则上一轮 reranker 报告里",
          "> Recall@3/5 与 Coverage 的全部提升都来自**重排**，而不是候选池加深。", ""]
    if "rerank:bge-reranker-base" in names:
        r1, r2 = d["variants"]["rerank:bge-reranker-base"], d["variants"]["rerank:bge-reranker-v2-m3"]
        L += ["## reranker 的净贡献（相对 A1，同 pool=20）", "",
              f"- base： Recall@1 {(r1['recall@1']-a1['recall@1'])*100:+.1f}pp · "
              f"Recall@3 {(r1['recall@3']-a1['recall@3'])*100:+.1f}pp · "
              f"Coverage full {(r1['coverage_full_rate']-a1['coverage_full_rate'])*100:+.1f}pp",
              f"- v2-M3：Recall@1 {(r2['recall@1']-a1['recall@1'])*100:+.1f}pp · "
              f"Recall@3 {(r2['recall@3']-a1['recall@3'])*100:+.1f}pp · "
              f"Coverage full {(r2['coverage_full_rate']-a1['coverage_full_rate'])*100:+.1f}pp",
              ""]
    L += ["---", "", "> 只测量，未修改 search.py / query analysis / Gold。", ""]
    return "\n".join(L)


def main() -> int:
    t0 = time.time()
    rep = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "splits": {}}
    for split in ("dev",):
        rep["splits"][split] = run_split(split)
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "depth_control.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORTS / "depth_control.md").write_text(render_md(rep["splits"]["dev"]), encoding="utf-8")

    d = rep["splits"]["dev"]
    print(f"\n=== Candidate Depth Control（DEV {d['cases']} 题，{time.time()-t0:.1f}s）===")
    names = list(d["variants"])
    print(f"{'metric':26s} " + " ".join(f"{n[:18]:>18s}" for n in names))
    for k in KEYS:
        row = " ".join(
            (f"{d['variants'][n][k]*100:17.1f}%" if d['variants'][n].get(k) is not None else f"{'—':>18s}")
            for n in names)
        print(f"{k:26s} {row}")
    for k, v in d["top5_identical"].items():
        print(f"  {k}: {'IDENTICAL' if v['identical'] else str(v['n_changed'])+' changed'}")
    print("\n报告 → eval/reports/depth_control.{json,md}")
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
