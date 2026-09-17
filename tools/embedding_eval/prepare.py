#!/usr/bin/env python3
"""A4.2a 候选清单 + 下载 + 校验（**选型阶段工具，不进 release**）。

产物落在 gitignored 的 ``runtime/models/_eval/``：模型是大型二进制，
绝不入库；这里只固定「仓库 / revision / 文件名 / size / sha256」这条溯源链。

用法：
    python tools/embedding_eval/prepare.py --list
    python tools/embedding_eval/prepare.py --download
    python tools/embedding_eval/prepare.py --verify      # 只用本地已有文件复验 sha256
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO / "runtime" / "models" / "_eval"
UA = {"User-Agent": "usb-wiki-a42a-selection"}

# ---------------------------------------------------------------------------
# 候选表（shortlist：1 个 reference FP32 + 1 个量化 + 1 个第三方 FP32 对照）
# ---------------------------------------------------------------------------
TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json",
                   "special_tokens_map.json", "vocab.txt")

CANDIDATES = [
    {
        "id": "xenova-fp32",
        "role": "reference_fp32",
        "repo": "Xenova/bge-small-zh-v1.5",
        "revision": "75c43b069aac4d136ba6bc1122f995fedcfd2781",
        "file": "onnx/model.onnx",
        "precision": "fp32",
        "expected_size": 94857997,
        "expected_sha256": None,   # 由 HF tree 的 lfs.oid 填入（见 --list）
        "base_model": "BAAI/bge-small-zh-v1.5",
        "license_declared": None,  # 该转换仓库未声明；base model 为 MIT
    },
    {
        "id": "xenova-int8",
        "role": "quantized_int8",
        "repo": "Xenova/bge-small-zh-v1.5",
        "revision": "75c43b069aac4d136ba6bc1122f995fedcfd2781",
        "file": "onnx/model_int8.onnx",
        "precision": "int8",
        "expected_size": 23906218,
        "expected_sha256": None,
        "base_model": "BAAI/bge-small-zh-v1.5",
        "license_declared": None,
    },
    {
        "id": "qdrant-fp32-opt",
        "role": "third_party_fp32_optimized",
        "repo": "Qdrant/bge-small-zh-v1.5",
        "revision": "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59",
        "file": "model_optimized.onnx",
        "precision": "fp32",
        "expected_size": 94782061,
        "expected_sha256": None,
        "base_model": "BAAI/bge-small-zh-v1.5",
        "license_declared": "mit",
    },
]


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def api_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def resolve_lfs_oids() -> dict:
    """从 HF tree API 取「文件 → lfs sha256 + size」，写回候选表。"""
    out: dict[str, dict] = {}
    for repo in sorted({c["repo"] for c in CANDIDATES}):
        rev = next(c["revision"] for c in CANDIDATES if c["repo"] == repo)
        tree = api_json(f"https://huggingface.co/api/models/{repo}/tree/{rev}?recursive=true")
        for e in tree:
            if e.get("type") != "file":
                continue
            lfs = e.get("lfs") or {}
            out[f"{repo}::{e['path']}"] = {
                "size": lfs.get("size") or e.get("size"),
                "sha256": lfs.get("oid"),
            }
    return out


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as r, tmp.open("wb") as fh:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            fh.write(b)
    tmp.replace(dest)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="A4.2a 候选下载 / 校验")
    ap.add_argument("--list", action="store_true", help="只列候选与上游 sha256/size")
    ap.add_argument("--download", action="store_true", help="下载 shortlist 与 tokenizer")
    ap.add_argument("--verify", action="store_true", help="只复验本地文件 sha256")
    ap.add_argument("--only", default="", help="只处理某个候选 id（逗号分隔）")
    args = ap.parse_args(argv)

    want = {s for s in args.only.split(",") if s}
    cands = [c for c in CANDIDATES if not want or c["id"] in want]

    print(f"评测目录：{EVAL_DIR}")
    meta = resolve_lfs_oids()
    for c in cands:
        m = meta.get(f"{c['repo']}::{c['file']}") or {}
        if m.get("sha256"):
            c["expected_sha256"] = m["sha256"]
        if m.get("size"):
            c["expected_size"] = m["size"]

    if args.list or not (args.download or args.verify):
        for c in cands:
            print(f"\n[{c['id']}] {c['role']}  precision={c['precision']}")
            print(f"  repo      : {c['repo']}")
            print(f"  revision  : {c['revision']}")
            print(f"  file      : {c['file']}")
            print(f"  size      : {c['expected_size']} bytes "
                  f"({c['expected_size']/1048576:.2f} MB)")
            print(f"  sha256    : {c['expected_sha256']}")
            print(f"  license   : {c['license_declared']} (base: MIT)")
        if not (args.download or args.verify):
            return 0

    total = 0
    report = []
    for c in cands:
        dest = EVAL_DIR / c["id"] / Path(c["file"]).name
        url = (f"https://huggingface.co/{c['repo']}/resolve/{c['revision']}/{c['file']}")
        if args.download and not dest.exists():
            print(f"下载 {c['id']} … {c['file']}")
            _download(url, dest)
        if dest.is_file():
            real = sha256_file(dest)
            size = dest.stat().st_size
            total += size
            ok = ((c["expected_sha256"] is None or real == c["expected_sha256"])
                  and (c["expected_size"] is None or size == c["expected_size"]))
            print(f"  {c['id']:16} {size/1048576:8.2f} MB  sha256={real[:16]}…  "
                  f"{'✅ 与上游一致' if ok else '❌ 与上游不一致'}")
            report.append({**c, "local_path": str(dest.relative_to(REPO)),
                           "actual_size": size, "actual_sha256": real, "ok": ok})
        else:
            print(f"  {c['id']:16} 本地缺失")

    # tokenizer：按仓库各取一份（用于验证三家是否同一套分词语义）
    tk_report = {}
    if args.download or args.verify:
        for repo in sorted({c["repo"] for c in cands}):
            rev = next(c["revision"] for c in CANDIDATES if c["repo"] == repo)
            tdir = EVAL_DIR / "tokenizers" / repo.replace("/", "__")
            got = {}
            for name in TOKENIZER_FILES:
                dest = tdir / name
                if args.download and not dest.exists():
                    try:
                        _download(f"https://huggingface.co/{repo}/resolve/{rev}/{name}", dest)
                    except Exception as exc:  # noqa: BLE001 - 缺某个文件不代表失败
                        print(f"  tokenizer {repo}/{name} 不可用：{exc}")
                        continue
                if dest.is_file():
                    got[name] = {"size": dest.stat().st_size, "sha256": sha256_file(dest)}
            tk_report[repo] = got
            print(f"tokenizer {repo}: {', '.join(sorted(got)) or '（无）'}")

    out = {"candidates": report, "tokenizers": tk_report}
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    (EVAL_DIR / "selection_inputs.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"\n本次涉及本地文件合计 {total/1048576:.2f} MB")
    print(f"清单写入 {EVAL_DIR / 'selection_inputs.json'}")
    return 0 if all(r["ok"] for r in report) else 1


if __name__ == "__main__":
    sys.exit(main())
