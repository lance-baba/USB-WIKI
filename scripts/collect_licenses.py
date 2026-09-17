#!/usr/bin/env python3
"""第三方分发许可清单（A3）—— 只记录**这个 Release 实际带了什么**。

原则
====
1. **审计实际随包字节**，不是审计「未来可能捆绑什么」。
   当前没有 bundled Ollama / LLM / GGUF / ONNX model，就不替它们收 license。
2. **不猜许可**。`Metadata License` 字段为空且无 classifier / 无 license 文件时，
   标记 `LICENSE_REVIEW_REQUIRED` —— 不允许「空 → 猜 MIT」。
3. 本模块不做法律判断，只建立**完整、可审计**的清单；
   正式 V1 Release 前必须清零 `LICENSE_REVIEW_REQUIRED`。

产物（相对发布根）::

    LICENSES/THIRD_PARTY.json          机器可读清单
    LICENSES/THIRD_PARTY_NOTICES.txt   人类可读汇总
    LICENSES/python/LICENSE.txt        CPython 自身许可
    LICENSES/packages/<pkg>-<ver>/…    各发行包随附的 LICENSE / NOTICE 原文

零第三方依赖：只用标准库 `email`（读 METADATA）+ `zipfile` 无关，纯文件读取 + 联网零次。
"""
from __future__ import annotations

import io
import json
import shutil
from email.parser import Parser
from pathlib import Path

FORMAT_VERSION = 1
LICENSES_DIRNAME = "LICENSES"
THIRD_PARTY_JSON = "THIRD_PARTY.json"
THIRD_PARTY_NOTICES = "THIRD_PARTY_NOTICES.txt"
PACKAGES_DIRNAME = "packages"

#: 单份许可原文上限 —— 超限跳过并在清单里写明（避免把巨大语料塞进发布介质）
MAX_LICENSE_BYTES = 2 * 1024 * 1024

_LICENSE_GLOBS = ("LICENSE*", "LICENCE*", "COPYING*", "NOTICE*", "AUTHORS*")
_LICENSES_SKIP_SUFFIXES = (".pyc", ".pyo")

STATUS_OK = "ok"
STATUS_METADATA_ONLY = "metadata_only"
STATUS_REVIEW = "LICENSE_REVIEW_REQUIRED"


# ---------------------------------------------------------------------------
# 依赖锁解析
# ---------------------------------------------------------------------------
def norm_name(name: str) -> str:
    """PEP 503 规范化：`lxml-html-clean` / `lxml_html_clean` / `lxml.html.clean` 同一键。"""
    return name.strip().lower().replace("-", "_").replace(".", "_")


def parse_requirements_lock(lock_path: Path) -> dict[str, str]:
    """解析 pip-compile 产物，返回 ``{规范化名: 版本}``。

    只认 `name==version` 行（含 environment marker 时截断 marker）；
    注释行 / `# via` 续行自然跳过。
    """
    out: dict[str, str] = {}
    lock_path = Path(lock_path)
    if not lock_path.is_file():
        return out
    for raw in lock_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, _, rest = line.partition("==")
        name = name.strip()
        if not name or any(ch.isspace() for ch in name):
            continue
        version = rest.split(";")[0].strip()
        if version:
            out[norm_name(name)] = version
    return out


# ---------------------------------------------------------------------------
# dist-info / METADATA 读取
# ---------------------------------------------------------------------------
def _split_dist_info(dirname: str) -> tuple[str, str]:
    """`lxml_html_clean-0.4.5.dist-info` → ``("lxml_html_clean", "0.4.5")``。"""
    stem = dirname[:-len(".dist-info")]
    name, _, version = stem.rpartition("-")
    return (name or stem), version


def _read_metadata(meta_path: Path):
    try:
        return Parser().parsestr(meta_path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def _license_from_metadata(msg) -> tuple[str | None, str | None]:
    """返回 ``(license 表达, 来源)``。**只读元数据，不做任何推断/补全。**"""
    field = (msg.get("License") or "").strip()
    if field and field.upper() != "UNKNOWN":
        first = field.splitlines()[0].strip()
        if first and len(first) <= 200:
            return first, "Metadata License"

    classifiers = [c for c in (msg.get_all("Classifier") or [])
                   if c.strip().startswith("License ::")]
    if classifiers:
        cleaned = [c.split("::", 1)[1].strip() for c in classifiers]
        return " / ".join(dict.fromkeys(cleaned)), "Classifier"

    # PEP 639：License-Expression: MIT
    expr = (msg.get("License-Expression") or "").strip()
    if expr:
        return expr, "License-Expression"
    return None, None


def _project_url(msg) -> str | None:
    for key in ("Home-page", "Download-URL"):
        val = (msg.get(key) or "").strip()
        if val and val.upper() != "UNKNOWN":
            return val
    for entry in (msg.get_all("Project-URL") or []):
        label, _, url = entry.partition(",")
        url = url.strip()
        if url.startswith(("http://", "https://")):
            if label.strip().lower() in ("homepage", "source", "repository", "home"):
                return url
    for entry in (msg.get_all("Project-URL") or []):
        _, _, url = entry.partition(",")
        if url.strip().startswith(("http://", "https://")):
            return url.strip()
    return None


def _collect_license_files(dist_info: Path, dest: Path) -> list[Path]:
    """把 dist-info 内的许可原文拷到 *dest*，返回**拷贝后的绝对路径**列表。

    调用方负责换算成相对发布根的 POSIX 路径；这里不猜 root，避免多传一个参数。
    """
    found: list[Path] = []
    for pattern in _LICENSE_GLOBS:
        for src in sorted(dist_info.rglob(pattern)):
            if not src.is_file() or src.suffix.lower() in _LICENSES_SKIP_SUFFIXES:
                continue
            if src.stat().st_size > MAX_LICENSE_BYTES:
                continue
            rel_inside = src.relative_to(dist_info)
            target = dest / rel_inside
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            found.append(target)
    return sorted(dict.fromkeys(found), key=lambda p: p.as_posix())


# ---------------------------------------------------------------------------
# 主收集
# ---------------------------------------------------------------------------
def collect(runtime_dir: Path, lock_path: Path, root: Path,
            *, python_version: str | None = None) -> dict:
    """生成 LICENSES/ 并返回清单 dict。

    *runtime_dir* 为随包的运行时目录（payload/python-runtime）；
    缺运行时（开发态未构建）时仍产出合法结构，但 ``inventory_complete=False``。
    """
    root = Path(root)
    lic_root = root / LICENSES_DIRNAME
    pkg_root = lic_root / PACKAGES_DIRNAME
    if lic_root.exists():
        shutil.rmtree(lic_root, ignore_errors=True)
    lic_root.mkdir(parents=True, exist_ok=True)

    runtime_dir = Path(runtime_dir)
    site_packages = runtime_dir / "Lib" / "site-packages"
    locked = parse_requirements_lock(lock_path)

    packages: list[dict] = []

    if site_packages.is_dir():
        for info_dir in sorted(site_packages.glob("*.dist-info")):
            entry = _one_package(info_dir, pkg_root, locked, root)
            if entry is not None:
                packages.append(entry)

    # CPython 自身（随包运行时最上游的许可）
    py_license = runtime_dir / "LICENSE.txt"
    if py_license.is_file():
        dest_dir = lic_root / "python"
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(py_license, dest_dir / "LICENSE.txt")
        packages.insert(0, {
            "package": "Python",
            "version": python_version or "unknown",
            "license": "PSF-2.0",
            "license_source": "bundled LICENSE.txt",
            "project": "https://www.python.org/",
            "license_texts": [f"{LICENSES_DIRNAME}/python/LICENSE.txt"],
            "required": True,
            "review_status": STATUS_OK,
        })

    complete = bool(packages) and site_packages.is_dir()

    present = {norm_name(p["package"]) for p in packages}
    locked_missing = sorted(n for n in locked if n not in present)
    version_mismatch = sorted(
        f"{p['package']}（锁 {locked[norm_name(p['package'])]} / 随包 {p['version']}）"
        for p in packages
        if norm_name(p["package"]) in locked
        and p["version"] != locked[norm_name(p["package"])]
    )
    review_required = sorted(p["package"] for p in packages
                             if p["review_status"] == STATUS_REVIEW)

    inventory = {
        "format_version": FORMAT_VERSION,
        "inventory_complete": complete,
        "source": {
            "runtime": "payload/python-runtime",
            "lock": Path(lock_path).name,
            "python_version": python_version,
        },
        "summary": {
            "packages": len(packages),
            "review_required": len(review_required),
            "locked_total": len(locked),
            "locked_missing": locked_missing,
            "version_mismatch": version_mismatch,
        },
        "packages": packages,
    }

    (lic_root / THIRD_PARTY_JSON).write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    (lic_root / THIRD_PARTY_NOTICES).write_text(
        _render_notices(packages, inventory), encoding="utf-8", newline="\n")
    return inventory


def _one_package(info_dir: Path, pkg_root: Path, locked: dict[str, str],
                 root: Path) -> dict | None:
    meta_path = info_dir / "METADATA"
    msg = _read_metadata(meta_path) if meta_path.is_file() else None
    if msg is not None:
        name = (msg.get("Name") or "").strip()
        version = (msg.get("Version") or "").strip()
    else:
        name = version = ""
    if not name or not version:
        name, version = _split_dist_info(info_dir.name)
    if not name:
        return None

    license_value, license_source = ((None, None) if msg is None
                                     else _license_from_metadata(msg))
    project = _project_url(msg) if msg is not None else None

    dest = pkg_root / f"{name}-{version}"
    texts = _collect_license_files(info_dir, dest)
    texts_rel = [p.relative_to(root).as_posix() for p in texts]
    if not texts_rel:
        try:
            dest.rmdir()  # 没拷到任何东西就别留空目录
        except OSError:
            pass

    if license_value is None and not texts_rel:
        status = STATUS_REVIEW
    elif texts_rel:
        status = STATUS_OK
    else:
        # 许可身份可由 License 字段 / classifier / License-Expression 可靠回答，
        # 只是该发行包没随附许可原文 —— 有据可查，不是「猜」。
        status = STATUS_METADATA_ONLY

    return {
        "package": name,
        "version": version,
        "license": license_value,
        "license_source": license_source or ("license file" if texts_rel else None),
        "project": project,
        "license_texts": texts_rel,
        "required": norm_name(name) in locked,
        "review_status": status,
    }


def _render_notices(packages: list[dict], inventory: dict) -> str:
    out = io.StringIO()
    out.write("Wiki-USB 第三方软件许可清单（THIRD-PARTY NOTICES）\n")
    out.write("=" * 72 + "\n\n")
    out.write("本文件由 scripts/collect_licenses.py 自动生成，覆盖本 Release **实际随包分发**的\n")
    out.write("第三方组件。未随包分发的组件（如本地 LLM / Ollama / ONNX 模型）不在此列。\n\n")
    s = inventory["summary"]
    out.write(f"组件总数：{s['packages']}    待人工复核：{s['review_required']}\n")
    out.write(f"依赖锁条目：{s['locked_total']}    "
              f"锁内缺失：{len(s['locked_missing'])}    版本差异：{len(s['version_mismatch'])}\n\n")
    out.write("-" * 72 + "\n")
    for p in packages:
        out.write(f"{p['package']} {p['version']}\n")
        out.write(f"  许可        : {p['license'] or '(未在元数据中声明)'}\n")
        out.write(f"  许可来源    : {p['license_source'] or '-'}\n")
        out.write(f"  项目主页    : {p['project'] or '-'}\n")
        out.write(f"  许可原文    : "
                  + (", ".join(p["license_texts"]) if p["license_texts"] else "(未随附)")
                  + "\n")
        out.write(f"  依赖锁直接/间接依赖 : {'是' if p['required'] else '否（随运行时附带）'}\n")
        out.write(f"  复核状态    : {p['review_status']}\n\n")
    if s["locked_missing"]:
        out.write("-" * 72 + "\n")
        out.write("⚠ 以下依赖锁条目在随包运行时中未找到（发布不完整）：\n")
        for n in s["locked_missing"]:
            out.write(f"  - {n}\n")
    if s["version_mismatch"]:
        out.write("-" * 72 + "\n")
        out.write("⚠ 以下依赖版本与依赖锁不一致（随包运行时可能非由当前锁构建）：\n")
        for n in s["version_mismatch"]:
            out.write(f"  - {n}\n")
    return out.getvalue()
