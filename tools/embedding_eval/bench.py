#!/usr/bin/env python3
"""A4.2a 候选基准：基本正确性 + tokenizer 行为 + Windows CPU 性能。

只做**实测**：输出维度 / 有限值 / L2 范数 / 确定性 / cold load / batch 延迟 / 内存。
不引入 profiling 框架（§11.3）；内存用 ctypes 读 Windows PROCESS_MEMORY_COUNTERS。

⚠ **每个候选在独立进程里测**：同进程顺序测会让「进程峰值」被前一个候选污染，
冷加载时间也不再是冷的。`--all` 会自动为每个候选起子进程。

用法：
    <venv>/python.exe tools/embedding_eval/bench.py --all
    <venv>/python.exe tools/embedding_eval/bench.py --only xenova-int8 --out x.json
"""
from __future__ import annotations

import argparse
import ctypes
import json
import math
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner import OnnxBertEmbedder          # noqa: E402

EVAL_DIR = REPO / "runtime" / "models" / "_eval"

#: §11.2 tokenizer 压力用例
TOKENIZER_CASES = {
    "纯中文": "这是一段纯中文的测试文本，用来验证分词是否正确。",
    "中英混合": "USB-WIKI 使用 SQLite FTS5 做混合检索 hybrid search。",
    "数字": "2026 年第 3 季度营收 1234567.89 元，同比增长 12.5%。",
    "URL": "参考 https://huggingface.co/BAAI/bge-small-zh-v1.5 与 http://127.0.0.1:11434/api/tags",
    "标点": "（一）、二；三：四！五？六……「七」——八。",
    "长文本": "知识库检索" * 120,
    "空白": "   ",
    "特殊字符": "emoji🙂 与 <script>alert(1)</script> 以及 a\\b\tc 和 #$%^&*()",
}


class _PMC(ctypes.Structure):
    """Windows PROCESS_MEMORY_COUNTERS。"""
    _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def _mem_mb() -> tuple[float, float]:
    """(当前工作集, 峰值工作集) MB —— 非 Windows 返回 (-1, -1)。

    ⚠ 必须传 `c_void_p(-1)` 而不是 `kernel32.GetCurrentProcess()`：
    后者返回 Python int，不声明 argtypes 时会被当 32 位截断，调用静默返回 0
    —— 量出来永远是 0（本阶段实测踩过这个坑）。
    """
    if sys.platform != "win32":
        return -1.0, -1.0
    try:
        fn = ctypes.windll.psapi.GetProcessMemoryInfo
        fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC), ctypes.c_uint32]
        fn.restype = ctypes.c_int
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        if not fn(ctypes.c_void_p(-1), ctypes.byref(pmc), pmc.cb):
            return -1.0, -1.0
        return pmc.WorkingSetSize / 1048576, pmc.PeakWorkingSetSize / 1048576
    except Exception:  # noqa: BLE001 - 量不到就不报数，绝不编数
        return -1.0, -1.0


def _time_it(fn, *, warmup: int = 2, runs: int = 5) -> dict:
    for _ in range(warmup):
        fn()
    ts = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t0)
    return {"median_ms": round(statistics.median(ts) * 1000, 1),
            "min_ms": round(min(ts) * 1000, 1),
            "max_ms": round(max(ts) * 1000, 1),
            "runs": runs}


def run_one(cand: dict) -> dict:
    cid = cand["id"]
    model_path = REPO / cand["local_path"]
    tdir = EVAL_DIR / "tokenizers" / cand["repo"].replace("/", "__")

    print("=" * 78)
    print(f"[{cid}] {cand['precision']}  {cand['file']}  ({cand['actual_size']/1048576:.2f} MB)")

    # 先把三方库导入完，再开始量内存 —— 否则会把「库导入开销」算进 artifact 成本
    import numpy as np          # noqa: F401,PLC0415
    import onnxruntime as ort   # noqa: F401,PLC0415
    import tokenizers           # noqa: F401,PLC0415

    base_ws, _ = _mem_mb()
    emb = OnnxBertEmbedder(model_path, tdir, model_name=cid)
    t0 = time.perf_counter()
    emb._lazy()                                   # noqa: SLF001 - 测的就是冷加载
    cold_ms = (time.perf_counter() - t0) * 1000
    loaded_ws, _ = _mem_mb()

    print(f"  IO names   : in={emb.io_names['inputs']} out={emb.io_names['outputs']}")
    print(f"  output shp : {emb.io_names['shapes']}")
    print(f"  provider   : {emb.io_names['providers']}")

    # ---------- 1. 基本正确性 ----------
    probe = ["这是一句用于正确性检查的中文测试文本。", "short en text"]
    v1 = emb.embed(probe)
    v2 = emb.embed(probe)
    dims = sorted({len(v) for v in v1})
    finite = all(math.isfinite(x) for v in v1 for x in v)
    norms = [round(math.sqrt(sum(x * x for x in v)), 6) for v in v1]
    deterministic = v1 == v2
    print(f"  dim        : {dims}  finite={finite}  L2={norms}  deterministic={deterministic}")

    # ---------- 2. tokenizer ----------
    tk = {}
    for label, text in TOKENIZER_CASES.items():
        enc = emb._tok.encode(text)                       # noqa: SLF001
        unk_id = emb._tok.token_to_id("[UNK]")            # noqa: SLF001
        tk[label] = {"tokens": len(enc.ids),
                     "unk": sum(1 for i in enc.ids if i == unk_id)}
    print("  tokenizer  : " + "; ".join(
        f"{k}={v['tokens']}" + (f"(UNK {v['unk']})" if v["unk"] else "")
        for k, v in tk.items()))
    short_probe = emb._tok.encode("USB-WIKI").tokens       # noqa: SLF001
    print(f"  例：'USB-WIKI' → {short_probe}")

    # ---------- 3. CPU 性能 ----------
    s1 = "知识库检索质量评估：这是一个用于延迟测试的中文句子，长度接近真实查询。"
    b8 = [f"第{i}条测试文本：本地向量检索在低配 CPU 上的表现如何。" for i in range(8)]
    one = _time_it(lambda: emb.embed([s1]))
    eight = _time_it(lambda: emb.embed(b8), runs=3)
    end_ws, peak_ws = _mem_mb()
    print(f"  cold load  : {cold_ms:.0f} ms")
    print(f"  batch=1    : median {one['median_ms']} ms  (min {one['min_ms']} / max {one['max_ms']})")
    print(f"  batch=8    : median {eight['median_ms']} ms  "
          f"({round(eight['median_ms']/8, 1)} ms/条)")
    print(f"  内存        : session 后 +{loaded_ws-base_ws:.0f} MB，跑完 +{end_ws-base_ws:.0f} MB，"
          f"进程峰值 {peak_ws:.0f} MB")

    return {
        "id": cid, "role": cand["role"], "precision": cand["precision"],
        "file": cand["file"], "size": cand["actual_size"], "sha256": cand["actual_sha256"],
        "repo": cand["repo"], "revision": cand["revision"],
        "cold_load_ms": round(cold_ms, 1), "batch1": one, "batch8": eight,
        "ws_base_mb": round(base_ws, 1), "ws_loaded_mb": round(loaded_ws, 1),
        "ws_end_mb": round(end_ws, 1), "process_peak_ws_mb": round(peak_ws, 1),
        "session_ram_mb": round(loaded_ws - base_ws, 1),
        "total_ram_mb": round(end_ws - base_ws, 1),
        "dim": dims, "finite": finite, "l2_norms": norms, "deterministic": deterministic,
        "tokenizer_cases": tk, "io": emb.io_names,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="A4.2a 候选基准")
    ap.add_argument("--all", action="store_true", help="每个候选起独立进程")
    ap.add_argument("--only", default="", help="只测某个候选 id")
    ap.add_argument("--out", default="", help="单候选结果 JSON 路径")
    args = ap.parse_args(argv)

    inputs = json.loads((EVAL_DIR / "selection_inputs.json").read_text(encoding="utf-8"))
    cands = inputs["candidates"]

    if args.all:
        results = {}
        for c in cands:
            out = EVAL_DIR / f"bench_{c['id']}.json"
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()),
                 "--only", c["id"], "--out", str(out)],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            print(proc.stdout, end="")
            if proc.returncode != 0:
                print(f"  ❌ {c['id']} 基准失败：{proc.stderr[-400:]}")
                continue
            results[c["id"]] = json.loads(out.read_text(encoding="utf-8"))
        merged = EVAL_DIR / "bench.json"
        merged.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8", newline="\n")
        print(f"\n汇总写入 {merged}")
        return 0

    want = args.only
    sel = [c for c in cands if not want or c["id"] == want]
    if not sel:
        print(f"未知候选：{want}", file=sys.stderr)
        return 2
    res = run_one(sel[0])
    if args.out:
        Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
