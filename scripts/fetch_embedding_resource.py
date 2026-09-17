#!/usr/bin/env python3
"""按资源契约取回 bundled embedding 字节（**Release Maintainer 侧动作**）。

与构建严格分离
--------------
资源获取和构建 Release 是**两个动作**：

    maintainer: fetch → verify            （可联网，脚本自己校验）
    release   : build_release.py --strict （**绝不联网**，缺件即失败）

客户安装阶段 0 联网；运行时只用 ``Tokenizer.from_file(本地 tokenizer.json)``。

职责
----
    读 resources/embedding/default.json
      ↓ 按 **pin 死的 revision** 下载固定文件（不下 main / latest）
      ↓ 逐文件校验 size
      ↓ 逐文件校验 SHA256
      ↓ 落到 gitignored 的 vendor/cache/embedding/
      ↓ 把契约复制为 artifact.json（使该目录本身即为一个**可直接使用的资源目录**）

用法
----
    python scripts/fetch_embedding_resource.py            # 取件 + 校验
    python scripts/fetch_embedding_resource.py --check     # 只校验现有 cache
    python scripts/fetch_embedding_resource.py --print-urls
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CONTRACT = BASE / "resources" / "embedding" / "default.json"
CACHE_DIR = BASE / "vendor" / "cache" / "embedding"
ARTIFACT_JSON = "artifact.json"
UA = {"User-Agent": "usb-wiki-embedding-resource"}

#: 单个文件的安全上限：契约里若出现超限条目，说明契约本身有问题，拒绝下载
MAX_FILE_BYTES = 200 * 1024 * 1024


def log(msg: str) -> None:
    print(f"[resource] {msg}", flush=True)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def load_contract() -> dict:
    if not CONTRACT.is_file():
        raise SystemExit(f"缺少资源契约：{CONTRACT}")
    data = json.loads(CONTRACT.read_text(encoding="utf-8"))
    if data.get("format_version") != 1:
        raise SystemExit(f"契约 format_version 不受支持：{data.get('format_version')!r}")
    return data


def entries(contract: dict) -> list[dict]:
    """(目标文件名, 上游仓库路径, revision, sha256, size) 列表。"""
    out = [
        {
            "name": Path(contract["local_files"]["model"]).name,
            "repo_path": contract["artifact_file"],
            "source": contract["artifact_source"],
            "revision": contract["artifact_revision"],
            "sha256": contract["artifact_sha256"],
            "size": contract["artifact_size"],
            "role": "artifact",
        }
    ]
    for name, rec in (contract.get("tokenizer_files") or {}).items():
        out.append({
            "name": name,
            "repo_path": rec["repo_path"],
            "source": rec["source"],
            "revision": rec["revision"],
            "sha256": rec["sha256"],
            "size": rec["size"],
            "role": "tokenizer",
        })
    return out


def url_of(e: dict) -> str:
    return f"https://huggingface.co/{e['source']}/resolve/{e['revision']}/{e['repo_path']}"


def download(e: dict, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url_of(e), headers=UA)
    with urllib.request.urlopen(req, timeout=600) as r, tmp.open("wb") as fh:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            fh.write(b)
    tmp.replace(dest)


def verify_one(e: dict, path: Path) -> str | None:
    if not path.is_file():
        return f"{e['name']}：缺失"
    size = path.stat().st_size
    if isinstance(e["size"], int) and size != e["size"]:
        return f"{e['name']}：大小不符（期望 {e['size']}，实际 {size}）"
    actual = sha256_file(path)
    if actual != str(e["sha256"]).lower():
        return (f"{e['name']}：SHA256 不符（期望 {str(e['sha256'])[:16]}…，"
                f"实际 {actual[:16]}…）")
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="取回 bundled embedding 资源（maintainer 侧）")
    ap.add_argument("--check", action="store_true", help="只校验本地 cache，不下载")
    ap.add_argument("--print-urls", action="store_true", help="只打印将要下载的 URL")
    ap.add_argument("--dest", default=str(CACHE_DIR), help="目标目录（默认 vendor/cache/embedding）")
    args = ap.parse_args(argv)

    contract = load_contract()
    dest_dir = Path(args.dest)
    items = entries(contract)

    if args.print_urls:
        for e in items:
            print(f"{e['name']:26} {url_of(e)}")
        return 0

    log(f"资源 id      : {contract['id']}")
    log(f"artifact     : {contract['artifact_source']} @ {contract['artifact_revision']}")
    log(f"base model   : {contract['base_model']}（license: "
        f"{contract['license']['base_model_license']}；转换仓库: "
        f"{contract['license']['conversion_repository_license']}）")
    log(f"目标目录     : {dest_dir}")

    problems: list[str] = []
    for e in items:
        if isinstance(e["size"], int) and e["size"] > MAX_FILE_BYTES:
            problems.append(f"{e['name']}：超过单文件上限 {MAX_FILE_BYTES} 字节，拒绝下载")
            continue
        target = dest_dir / e["name"]
        if args.check:
            err = verify_one(e, target)
        else:
            err = verify_one(e, target)
            if err and target.exists():
                log(f"  {e['name']} 已存在但校验不过，重新下载")
                target.unlink(missing_ok=True)
                err = verify_one(e, target)
            if err:
                log(f"  下载 {e['name']} ← {url_of(e)}")
                download(e, target)
                err = verify_one(e, target)
        if err:
            problems.append(err)
        else:
            log(f"  ✅ {e['name']:26} {target.stat().st_size:>10} B  "
                f"sha256={e['sha256'][:16]}…")

    if problems:
        for p in problems:
            log(f"  ❌ {p}")
        log("取件失败：资源目录不可用于构建")
        return 1

    # 契约复制成 artifact.json —— 让 cache 目录本身就是一个「可直接使用的资源目录」
    # （运行时按 artifact.json 解析；开发机也可用 WIKIUSB_EMBEDDING_DIR 指向它）
    shutil.copyfile(CONTRACT, dest_dir / ARTIFACT_JSON)
    log(f"契约已落位：{dest_dir / ARTIFACT_JSON}")
    log("✓ 资源就绪（构建阶段将只做本地拷贝，绝不联网）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
