"""USB-WIKI · Reranker Offline A/B Spike V1

**只在 eval/ 内实现；不改任何生产代码**（search.py / chunker.py / llm.py / config / UI / 安装包）。

第一阶段严格保持生产不变：`search.hybrid_search()`（FTS + Dense + RRF）取 **Top N** 候选父块；
第二阶段只用本地 cross-encoder 对这 N 个候选重排，然后取 Top1/3/5 计算指标。

跑法：
    runtime/python-3.11-embed/python.exe eval/reranker_spike.py fetch          # 下载模型（不入库）
    runtime/python-3.11-embed/python.exe eval/reranker_spike.py run --pools 10,20,30
    runtime/python-3.11-embed/python.exe eval/reranker_spike.py run --models bge-reranker-base

为什么用 **ONNX int8** 而不是原版 fp32：USB-WIKI 不要求独显，产品要回答的是「纯 CPU 机器是否
负担得起」。int8 导出是**可随包分发**的现实形态（体积也小 4 倍）。报告里会同时列出 fp32 体积
作参考，且明确标注本次实测的产物 —— 不隐瞒、不用小模型假装大模型。
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

import run_eval as R  # noqa: E402  （它已在导入期激活隔离测试库 + 复用测试保险丝）

import numpy as np  # noqa: E402
import onnxruntime as ort  # noqa: E402
from tokenizers import Tokenizer  # noqa: E402

MODELS_DIR = HERE / "models"
from _metrics import (_content_of, _same_line, aggregate,  # noqa: E402
                     gold_ids_for, metrics_for)

REPORTS = HERE / "reports"
MIRROR = "https://hf-mirror.com"

#: name -> 元数据（repo / 实测产物 / 上游原始 repo / license）
MODELS = {
    "bge-reranker-base": {
        "repo": "Xenova/bge-reranker-base",
        "onnx": "onnx/model_quantized.onnx",       # int8（实测产物）
        "tokenizer": "tokenizer.json",
        "upstream": "BAAI/bge-reranker-base",      # MIT
        "license": "MIT",
        "fp32_mb": 1060.9,                          # 参考：ONNX fp32 导出体积
    },
    "bge-reranker-v2-m3": {
        "repo": "onnx-community/bge-reranker-v2-m3-ONNX",
        "onnx": "onnx/model_quantized.onnx",       # int8（实测产物）
        "tokenizer": "tokenizer.json",
        "upstream": "BAAI/bge-reranker-v2-m3",     # Apache-2.0
        "license": "Apache-2.0",
        "fp32_mb": 2166.5,                          # 参考：fp32 = 0.6 + 2165.9(external data)
    },
}


# --------------------------------------------------------------------------
# 下载（可续传；记录 revision/license/体积）
# --------------------------------------------------------------------------
#: 除权重/分词器外一并留存「必要配置」——保证模型在本机可长期复用、不必重下。
#: 缺失的文件跳过（不是每个导出都带全部文件）。
OPTIONAL_FILES = [
    "config.json", "tokenizer_config.json", "special_tokens_map.json",
    "sentencepiece.bpe.model", "spm.model", "vocab.txt", "added_tokens.json",
]


def _head(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=20) as r:
        return int(r.headers.get("Content-Length") or 0)


def _exists(url: str) -> bool:
    try:
        _head(url)
        return True
    except Exception:  # noqa: BLE001 - 404/网络问题一律视为不存在
        return False


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    expect = _head(url)
    if dst.exists() and dst.stat().st_size == expect:
        print(f"    skip (已存在，大小一致) {dst.name}")
        return
    done = tmp.stat().st_size if tmp.exists() else 0
    headers = {"Range": f"bytes={done}-"} if done else {}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r, tmp.open("ab") as fh:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
    if tmp.stat().st_size != expect:
        raise RuntimeError(f"{dst.name} 体积不符：{tmp.stat().st_size} != {expect}")
    tmp.replace(dst)
    print(f"    ok {dst.name} ({expect/1048576:.1f} MB)")


def _write_info(name: str, revision: str) -> dict:
    """写 MODEL_INFO.json：绝对路径 + 每个文件的 体积/SHA256（供长期复用与完整性校验）。"""
    meta = MODELS[name]
    out = MODELS_DIR / name
    files = {}
    for p in sorted(out.iterdir()):
        if p.is_file() and p.name != "MODEL_INFO.json":
            files[p.name] = {"bytes": p.stat().st_size, "sha256": _sha256(p)}
    info = {
        "name": name, "hf_repo": meta["repo"], "upstream": meta["upstream"],
        "revision": revision, "license": meta["license"],
        "measured_artifact": meta["onnx"],
        "local_dir": str(out.resolve()),
        "files": files,
        "disk_bytes": sum(v["bytes"] for v in files.values()),
        "fp32_reference_mb": meta["fp32_mb"],
    }
    (out / "MODEL_INFO.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return info


def cmd_fetch(args) -> int:
    want = args.models or list(MODELS)
    for name in want:
        meta = MODELS[name]
        out = MODELS_DIR / name
        print(f"== {name}  ← {meta['repo']}   （持久目录：{out.resolve()}）")
        with urllib.request.urlopen(f"{MIRROR}/api/models/{meta['repo']}", timeout=20) as r:
            revision = json.loads(r.read()).get("sha") or ""
        rels = [meta["onnx"], meta["tokenizer"]]
        for rel in OPTIONAL_FILES:                      # 必要配置（缺则跳过）
            if _exists(f"{MIRROR}/{meta['repo']}/resolve/main/{rel}"):
                rels.append(rel)
        for rel in rels:
            _download(f"{MIRROR}/{meta['repo']}/resolve/main/{rel}", out / Path(rel).name)
        info = _write_info(name, revision)
        print(f"    revision={revision[:12]} license={meta['license']} "
              f"disk={info['disk_bytes']/1048576:.1f}MB files={len(info['files'])}")
    print("\n模型已持久保存（gitignored，不进任何安装包）。重复执行本命令不会重新下载"
          "（体积一致即跳过）。校验完整性：... reranker_spike.py verify")
    return 0


def cmd_verify(args) -> int:
    """校验本地模型完整性（对 SHA256），避免重复下载。"""
    bad = 0
    for name in (args.models or list(MODELS)):
        d = MODELS_DIR / name
        if not (d / "MODEL_INFO.json").exists():
            print(f"== {name}: 尚未下载"); bad += 1; continue
        info = json.loads((d / "MODEL_INFO.json").read_text(encoding="utf-8"))
        print(f"== {name}  {info['local_dir']}")
        print(f"   repo={info['hf_repo']} rev={info['revision'][:12]} "
              f"license={info['license']} disk={info['disk_bytes']/1048576:.1f}MB")
        for fn, rec in info["files"].items():
            p = d / fn
            if not p.exists():
                print(f"   ✗ MISSING  {fn}"); bad += 1
            elif p.stat().st_size != rec["bytes"]:
                print(f"   ✗ SIZE     {fn}"); bad += 1
            elif _sha256(p) != rec["sha256"]:
                print(f"   ✗ SHA256   {fn}"); bad += 1
            else:
                print(f"   ✓ {fn:34s} {rec['bytes']/1048576:7.1f}MB  {rec['sha256'][:16]}…")
    print("\nverify:", "OK（无需重新下载）" if bad == 0 else f"{bad} 个问题")
    return 0 if bad == 0 else 1


# --------------------------------------------------------------------------
# 重排器
# --------------------------------------------------------------------------
class OnnxReranker:
    def __init__(self, name: str, max_len: int = 512, threads: int | None = None):
        d = MODELS_DIR / name
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if threads:
            so.intra_op_num_threads = threads
        t0 = time.perf_counter()
        self.sess = ort.InferenceSession(str(d / Path(MODELS[name]["onnx"]).name), so,
                                         providers=["CPUExecutionProvider"])
        self.load_s = time.perf_counter() - t0
        self.tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        self.tok.enable_truncation(max_length=max_len)
        self.inputs = {i.name for i in self.sess.get_inputs()}

    def score(self, query: str, passages: list[str]) -> list[float]:
        encs = [self.tok.encode(query, p) for p in passages]
        n = max(len(e.ids) for e in encs)
        ids = np.zeros((len(encs), n), dtype=np.int64)
        mask = np.zeros((len(encs), n), dtype=np.int64)
        for i, e in enumerate(encs):
            k = len(e.ids)
            ids[i, :k] = e.ids
            mask[i, :k] = e.attention_mask
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            tt = np.zeros((len(encs), n), dtype=np.int64)
            for i, e in enumerate(encs):
                tt[i, :len(e.ids)] = e.type_ids
            feed["token_type_ids"] = tt
        out = self.sess.run(None, feed)[0]
        return [float(v) for v in np.asarray(out).reshape(len(encs), -1)[:, 0]]


# --------------------------------------------------------------------------
# 内存
# --------------------------------------------------------------------------
class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def rss_mb() -> float:
    """进程工作集（MB）。

    必须用 **kernel32.K32GetProcessMemoryInfo** 并显式声明 argtypes/restype：
    实测 `ctypes.windll.psapi.GetProcessMemoryInfo` + 默认签名在本机返回 0（静默失败），
    会导致 RAM 增量永远报 0MB —— 一个"看起来正常"的错误数字。
    """
    try:
        from ctypes import wintypes  # noqa: PLC0415

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = k32.K32GetProcessMemoryInfo
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
        fn.restype = wintypes.BOOL
        h = k32.GetCurrentProcess
        h.restype = wintypes.HANDLE
        c = _PMC(); c.cb = ctypes.sizeof(_PMC)
        if not fn(h(), ctypes.byref(c), c.cb):
            return float("nan")
        return c.WorkingSetSize / 1048576
    except Exception:  # noqa: BLE001 - 非 Windows 或 API 不可用
        return float("nan")


# --------------------------------------------------------------------------
# 指标
# --------------------------------------------------------------------------






# --------------------------------------------------------------------------
def cmd_run(args) -> int:
    pools = [int(x) for x in str(args.pools).split(",") if x.strip()]
    names = args.models or ["bge-reranker-base", "bge-reranker-v2-m3"]
    for n in names:
        if not (MODELS_DIR / n / "MODEL_INFO.json").exists():
            print(f"缺少模型 {n}，先运行：... reranker_spike.py fetch")
            return 2

    cases = R.load_gold()
    db, emb = R.build_library()
    docs = R.doc_map(db)
    parents = R.parent_index(db)
    base_rss = rss_mb()

    # --- 第一阶段（生产不变）：按最大 pool 取一次候选，再按 N 截断 ---
    max_n = max(pools)
    cand: dict[str, list] = {}
    for c in cases:
        res = R.S.hybrid_search(db, emb, c["query"], top_k_parents=max_n)
        cand[c["id"]] = list(res.references)

    report = {"meta": {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cases": len(cases), "pools": pools,
        "embedder": f"local_hash({R.DIM})",
        "artifact": "ONNX int8 (quantized) — CPU-only 可分发形态；fp32 体积见 models",
        "cpus": [], "note": "第一阶段完全使用生产 hybrid_search()，未改动其算法",
    }, "models": {}, "baseline": None, "pools": {}}

    # --- Baseline（同一 pool 下，未重排；第一阶段排名本身不变）---
    bl, bl_rows = {}, {}
    for N in pools:
        ranking = {c["id"]: cand[c["id"]][:N] for c in cases}
        rows = metrics_for(cases, ranking, parents, docs)
        bl_rows[f"N{N}"] = rows
        bl[f"N{N}"] = aggregate(rows)
    report["baseline"] = bl
    report["baseline_original"] = json.loads(
        (REPORTS / "baseline.json").read_text(encoding="utf-8"))["metrics"]

    for name in names:
        info = json.loads((MODELS_DIR / name / "MODEL_INFO.json").read_text(encoding="utf-8"))
        rr = OnnxReranker(name)
        art = Path(info["measured_artifact"]).name
        m = {"repo": info["hf_repo"], "upstream": info["upstream"],
             "revision": info["revision"], "license": info["license"],
             "measured_artifact": info["measured_artifact"],
             "local_dir": info["local_dir"],
             "weights_sha256": (info["files"].get(art) or {}).get("sha256", ""),
             "disk_mb": round(info["disk_bytes"] / 1048576, 1),
             "fp32_reference_mb": info["fp32_reference_mb"],
             "load_s": round(rr.load_s, 3),
             "rss_after_load_mb": round(rss_mb(), 1),
             "rss_delta_mb": round(rss_mb() - base_rss, 1),
             "results": {}}
        print(f"== {name}  load={rr.load_s:.2f}s rss+{m['rss_delta_mb']:.0f}MB")
        for N in pools:
            lat, rows = [], []
            ranking = {}
            first = None
            for c in cases:
                refs = cand[c["id"]][:N]
                if not refs:
                    ranking[c["id"]] = []
                    continue
                texts = [_content_of(parents, r) for r in refs]
                t0 = time.perf_counter()
                sc = rr.score(c["query"], texts)
                dt = time.perf_counter() - t0
                lat.append(dt)
                if first is None:
                    first = dt
                order = sorted(range(len(refs)), key=lambda i: -sc[i])
                ranking[c["id"]] = [refs[i] for i in order]
            _rows = metrics_for(cases, ranking, parents, docs)
            agg = aggregate(_rows)
            m.setdefault("_rows", {})[f"N{N}"] = _rows
            agg["latency_ms_first"] = round((first or 0) * 1000, 1)
            agg["latency_ms_p50"] = round(statistics.median(lat) * 1000, 1)
            agg["latency_ms_p95"] = round(
                sorted(lat)[min(len(lat) - 1, int(len(lat) * 0.95))] * 1000, 1)
            agg["latency_ms_mean"] = round(statistics.fmean(lat) * 1000, 1)
            m["results"][f"N{N}"] = agg
            if N == max_n:
                m["_ranking"] = {k: [r.parent_id for r in v[:5]] for k, v in ranking.items()}
            print(f"   N={N:2d}  R@1={agg['recall@1']*100:5.1f}%  R@3={agg['recall@3']*100:5.1f}%  "
                  f"MRR={agg['mrr']:.3f}  p50={agg['latency_ms_p50']:7.1f}ms  "
                  f"p95={agg['latency_ms_p95']:7.1f}ms")
        # 预热后稳态内存（ORT 常在首次推理时才真正分配）
        warm = rss_mb()
        m["rss_warm_mb"] = round(warm, 1)
        m["rss_warm_delta_mb"] = round(warm - base_rss, 1)
        print(f"   RAM: after_load +{m['rss_delta_mb']:.0f}MB → warm +{m['rss_warm_delta_mb']:.0f}MB")
        report["models"][name] = m

    # --- 主表 pool（已饱和的最小 N）+ 与 baseline 的"是否恶化"标记 ---
    main = pick_main(report)
    report["meta"]["main_pool"] = main
    report["meta"]["main_pool_reason"] = (
        f"{main} 已饱和：更深的 pool 指标完全相同，无需继续加深"
        if main != f"N{max(pools)}" else "取最大 pool")
    for name in names:
        for N in pools:
            k = f"N{N}"
            cur, b = report["models"][name]["results"][k], bl[k]
            report["models"][name]["results"][k]["wrong_doc_worse"] = (
                (cur.get("wrong_document_rate") or 0) > (b.get("wrong_document_rate") or 0) + 1e-9)
            report["models"][name]["results"][k]["no_answer_worse"] = (
                (cur.get("no_answer_fp_rate") or 0) > (b.get("no_answer_fp_rate") or 0) + 1e-9)
            report["models"][name]["results"][k]["attribution_worse"] = (
                (cur.get("attribution_violation_rate") or 0)
                > (b.get("attribution_violation_rate") or 0) + 1e-9)
    # 逐题 diff（主 pool）：具体哪些题被修好、哪些题被改坏 —— 这才是决策依据
    for name in names:
        ml = report["models"][name]["_rows"][main]
        bm = {x["id"]: x for x in bl_rows[main]}
        report["models"][name]["main_delta"] = {
            "fixes@1": [x["id"] for x in ml if x["hit@1"] and not bm[x["id"]]["hit@1"]],
            "regressions@1": [x["id"] for x in ml if (not x["hit@1"]) and bm[x["id"]]["hit@1"]],
            "new_trap_hits": [x["id"] for x in ml if x.get("trap") and not bm[x["id"]].get("trap")],
        }
        report["models"][name].pop("_rows", None)

    # --- 重点 case before/after ---
    focus = ["jkj_m_level_model", "jkj_df_purpose", "jkj_p_list", "jkj_so_person",
             "qwen_lc_features", "jkj_so_freq"]
    by_id = {c["id"]: c for c in cases}
    rep = []
    for cid in focus:
        c = by_id[cid]
        g = gold_ids_for(c, parents, docs)
        def pos(refs):
            for i, r in enumerate(refs[:5], start=1):
                if r.parent_id in g:
                    return i
            return None
        item = {"id": cid, "query": c["query"], "category": c["category"],
                "baseline_rank": pos(cand[cid][:max_n])}
        item["baseline_top1_hit"] = item["baseline_rank"] == 1
        for name in names:
            rk = report["models"][name]["_ranking"].get(cid) or []
            item[f"{name}_rank"] = None
            if rk:
                for i, pid in enumerate(rk[:5], start=1):
                    if pid in g:
                        item[f"{name}_rank"] = i
                        break
        # coverage 单独报告（不因排名提升就宣称覆盖已修好）
        groups = c.get("evidence_groups") or []
        if groups:
            item["evidence_groups"] = len(groups)
            _did = docs.get(c["expected"]["doc"], "")
            by_pid = {p["parent_id"]: p["content"] or "" for p in parents.get(_did, [])}
            # 基线覆盖率（未重排）
            base_got = sum(
                1 for grp in groups
                if any(t in by_pid.get(r.parent_id, "") for r in cand[cid][:max_n] for t in grp)
            )
            item["baseline_groups_covered"] = f"{base_got}/{len(groups)}"
            for name in names:
                rk = report["models"][name]["_ranking"].get(cid) or []
                got = sum(1 for grp in groups
                          if any(any(t in by_pid.get(pid, "") for t in grp) for pid in rk[:5]))
                item[f"{name}_groups_covered"] = f"{got}/{len(groups)}"
        rep.append(item)
    report["focus_cases"] = rep
    for name in names:
        report["models"][name].pop("_ranking", None)

    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "reranker_benchmark.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORTS / "reranker_benchmark.md").write_text(render_md(report), encoding="utf-8")
    print("\n报告 → eval/reports/reranker_benchmark.{json,md}")
    return 0


def pick_main(rep: dict) -> str:
    """选「已饱和」的最小 pool：若 N20 与 N30 指标完全相同，就没必要用 N30。"""
    pools = rep["meta"]["pools"]
    ref = f"N{max(pools)}"
    for N in pools:
        k = f"N{N}"
        same = True
        for m in rep["models"]:
            a, b = rep["models"][m]["results"][k], rep["models"][m]["results"][ref]
            if any(abs((a.get(x) or 0) - (b.get(x) or 0)) > 1e-9
                   for x in ("recall@1", "recall@3", "recall@5")):
                same = False
                break
        if same:
            return k
    return ref


def _tag(m):
    """产品信号标签（实验标签，不等于产品决定）。"""
    if m is None:
        return "—"
    d = (m["recall@1"] - m["baseline_ref"]) * 100
    p50 = m["latency_ms_p50"]
    worse = (m.get("wrong_doc_worse") or m.get("no_answer_worse")
             or m.get("attribution_worse"))
    if p50 > 2500:
        return "TOO_HEAVY"
    if d >= 8 and not worse and p50 <= 1200:
        return "STRONG_CANDIDATE"
    if 3 <= d < 8:
        return "MARGINAL"
    return "WEAK"


def render_md(rep: dict) -> str:
    pools = rep["meta"]["pools"]
    main = rep["meta"].get("main_pool") or f"N{max(pools)}"
    bl = rep["baseline"][main]
    names = list(rep["models"])
    L = ["# USB-WIKI · Reranker Offline A/B Spike V1", "",
         f"- 生成：{rep['meta']['generated_at']}",
         f"- 题量：**{rep['meta']['cases']}**；embedder=`{rep['meta']['embedder']}`",
         f"- 实测产物：**{rep['meta']['artifact']}**",
         f"- 第一阶段：生产 `hybrid_search()`（FTS+Dense+RRF）取 Top N，**算法未改**",
         "- LLM smoke 本轮**未跑**（隔离变量）。", "",
         "## 主表（pool = %s）" % main, "",
         "| 指标 | Baseline | " + " | ".join(names) + " |",
         "| --- | --- | " + " | ".join(["---"] * len(names)) + " |"]
    keys = ["recall@1", "recall@3", "recall@5", "mrr", "coverage_group_recall",
            "coverage_full_rate", "relation_accuracy", "cat_model_spec_r@1", "cat_person_r@1",
            "cat_similar_entity_r@1", "cat_table_relation_r@1", "cat_direct_fact_r@1",
            "wrong_document_rate", "no_answer_fp_rate", "attribution_violation_rate"]
    labels = {"recall@1": "Recall@1", "recall@3": "Recall@3", "recall@5": "Recall@5",
              "mrr": "MRR", "coverage_group_recall": "Coverage group",
              "coverage_full_rate": "Coverage full", "relation_accuracy": "Relation",
              "cat_model_spec_r@1": "model_spec R@1",
              "cat_person_r@1": "person R@1", "cat_similar_entity_r@1": "similar_entity R@1",
              "cat_table_relation_r@1": "table_relation R@1", "cat_direct_fact_r@1": "direct_fact R@1",
              "wrong_document_rate": "Wrong-doc", "no_answer_fp_rate": "No-answer FP",
              "attribution_violation_rate": "Attribution err"}
    def f(v, k):
        if v is None:
            return "—"
        return f"{v:.3f}" if k == "mrr" else f"{v*100:.1f}%"
    for k in keys:
        L.append(f"| {labels[k]} | {f(bl.get(k), k)} | " +
                 " | ".join(f(rep['models'][n]['results'][main].get(k), k) for n in names) + " |")
    L.append(f"| CPU p50 | — | " + " | ".join(
        f"{rep['models'][n]['results'][main]['latency_ms_p50']:.0f}ms" for n in names) + " |")
    L.append(f"| CPU p95 | — | " + " | ".join(
        f"{rep['models'][n]['results'][main]['latency_ms_p95']:.0f}ms" for n in names) + " |")
    L.append(f"| 模型加载 | — | " + " | ".join(
        f"{rep['models'][n]['load_s']:.1f}s" for n in names) + " |")
    L.append(f"| RAM 增量(加载) | — | " + " | ".join(
        f"{rep['models'][n]['rss_delta_mb']:.0f}MB" for n in names) + " |")
    L.append(f"| RAM 增量(预热后) | — | " + " | ".join(
        f"{rep['models'][n]['rss_warm_delta_mb']:.0f}MB" for n in names) + " |")
    L.append(f"| 磁盘(int8) | — | " + " | ".join(
        f"{rep['models'][n]['disk_mb']:.0f}MB" for n in names) + " |")

    L += ["", "## 模型信息 / 标签", "", "| 模型 | 上游 | license | revision | int8 磁盘 | fp32 参考 | 标签 |",
          "| --- | --- | --- | --- | --- | --- | --- |"]
    for n in names:
        m = rep["models"][n]
        mm = dict(m["results"][main]); mm["baseline_ref"] = bl["recall@1"]
        L.append(f"| {n} | {m['upstream']} | {m['license']} | `{m['revision'][:12]}` | "
                 f"{m['disk_mb']:.0f}MB | {m['fp32_reference_mb']:.0f}MB | **{_tag(mm)}** |")

    L += ["", "### 本地持久路径 / 校验（可直接复用，无需重新下载）", ""]
    for n in names:
        m = rep["models"][n]
        L += [f"- **{n}** → `{m['local_dir']}`",
              f"  - 权重 `{Path(m['measured_artifact']).name}` · sha256 `{m['weights_sha256']}`",
              f"  - 上游 `{m['upstream']}` · revision `{m['revision']}` · license **{m['license']}** · "
              f"磁盘 {m['disk_mb']:.0f}MB（fp32 导出参考 {m['fp32_reference_mb']:.0f}MB）"]
    L += ["", "> 校验：`runtime/python-3.11-embed/python.exe eval/reranker_spike.py verify`"]
    L += ["", f"主表 pool = **{main}**（{rep['meta'].get('main_pool_reason','')}）。",
          "", "### ⚠ 内存与分发影响（便携场景的硬约束）", "",
          "| 模型 | int8 磁盘 | 加载后 RAM | 预热后 RAM |", "| --- | --- | --- | --- |"]
    for n in names:
        m = rep["models"][n]
        L.append(f"| {n} | {m['disk_mb']:.0f}MB | +{m['rss_delta_mb']:.0f}MB "
                 f"| +{m['rss_warm_delta_mb']:.0f}MB |")
    L += ["", "> USB-WIKI 是**便携、不要求独显**的产品：准确率之外，RAM 增量与分发体积是同等"
          "硬约束。把一个 288~561MB 的模型塞进 122MB 的精简包、并常驻数百 MB 内存，"
          "需要单独决策（默认关闭 / 按需加载 / 仅高配启用），本轮不替产品做这个决定。"]

    _hdr = [f"{n} R@1/3/5" for n in names] + [f"{n} p50" for n in names]
    L += ["", "## Candidate pool 深度", "",
          "| N | " + " | ".join(_hdr) + " |",
          "| --- | " + " | ".join(["---"] * len(_hdr)) + " |"]
    for N in pools:
        k = f"N{N}"
        row = [f"{N}"]
        for n in names:
            a = rep["models"][n]["results"][k]
            row.append(f"{a['recall@1']*100:.1f}/{a['recall@3']*100:.1f}/{a['recall@5']*100:.1f}%")
        for n in names:
            row.append(f"{rep['models'][n]['results'][k]['latency_ms_p50']:.0f}ms")
        L.append("| " + " | ".join(row) + " |")

    L += ["", "## 逐题 diff（主 pool）—— 谁被修好、谁被改坏", ""]
    for n in names:
        d = rep["models"][n].get("main_delta") or {}
        L += [f"- **{n}**：Recall@1 修好 {len(d.get('fixes@1', []))} 题 "
              f"{d.get('fixes@1', [])}；**改坏 {len(d.get('regressions@1', []))} 题** "
              f"{d.get('regressions@1', [])}；新增命中陷阱词 {d.get('new_trap_hits', [])}"]
    L += ["", "## 重点 Case（before → after）", "",
          "| case | 查询 | Baseline 名次 | " + " | ".join(names) + " |",
          "| --- | --- | --- | " + " | ".join(["---"] * len(names)) + " |"]
    for it in rep["focus_cases"]:
        def fmt(v):
            return "❌未进前5" if v is None else ("✅1" if v == 1 else f"{v}")
        L.append(f"| {it['id']} | {it['query']} | {fmt(it['baseline_rank'])} | " +
                 " | ".join(fmt(it.get(f"{n}_rank")) for n in names) + " |")

    L += ["", "## Coverage 特别说明", "",
          "> **不要把 Recall@1 提升误认为 Coverage 已修好。** 覆盖度单列在重点 case 里"
          "（`groups_covered`），本轮**不修** coverage。", "",
          "---", "", "> 本轮只测量，未修改任何生产代码。", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="eval/reranker_spike.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--models", nargs="*", default=None)
    r = sub.add_parser("run")
    r.add_argument("--pools", default="10,20,30")
    r.add_argument("--models", nargs="*", default=None)
    v = sub.add_parser("verify", help="校验本地模型 SHA256（确认无需重新下载）")
    v.add_argument("--models", nargs="*", default=None)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "fetch":
            return cmd_fetch(args)
        if args.cmd == "verify":
            return cmd_verify(args)
        return cmd_run(args)
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


if __name__ == "__main__":
    raise SystemExit(main())
