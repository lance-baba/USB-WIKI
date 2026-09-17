#!/usr/bin/env python3
"""第三方分发许可清单（A3 / A3.1）—— 只记录**这个 Release 实际带了什么**。

原则
====
1. **审计实际随包字节**，不是审计「未来可能捆绑什么」。
   当前没有 bundled Ollama / LLM / GGUF / ONNX model，就不替它们收 license。
2. **不猜许可**。`Metadata License` 字段为空且无 classifier / 无 license 文件时，
   标记 `LICENSE_REVIEW_REQUIRED` —— 不允许「空 → 猜 MIT」。
3. **上游不随附时用 vendor 补齐**（A3.1）。某些 wheel 一个许可文件都不带，而义务仍在。
   许可原文入库到 `vendor/licenses/<pkg>/<ver>/`（含 `PROVENANCE.json` 记录来源与 sha256），
   构建时**只做本地拷贝，永不联网** —— 否则「正式发布构建」既依赖网络又不可复现。
4. 本模块不做法律判断，只建立**完整、可审计**的清单。

产物（相对发布根）::

    LICENSES/THIRD_PARTY.json          机器可读清单
    LICENSES/THIRD_PARTY_NOTICES.txt   人类可读汇总
    LICENSES/python/LICENSE.txt        CPython 自身许可
    LICENSES/packages/<pkg>-<ver>/…    各发行包随附（或 vendor 补齐）的许可原文

零第三方依赖：只用标准库 `email`（读 METADATA），纯文件读取 + **零联网**。
"""
from __future__ import annotations

import io
import json
import shutil
from email.parser import Parser
from pathlib import Path

from release_integrity import sha256_file   # 哈希唯一实现（scripts/ 同目录）

FORMAT_VERSION = 1
LICENSES_DIRNAME = "LICENSES"
THIRD_PARTY_JSON = "THIRD_PARTY.json"
THIRD_PARTY_NOTICES = "THIRD_PARTY_NOTICES.txt"
PACKAGES_DIRNAME = "packages"
#: 随包**模型**的许可目录（与 Python 包的 packages/ 分开：许可关系不同 ——
#  模型的许可来自 base model，转换仓库常不声明，必须分开如实记录）
MODELS_DIRNAME = "models"

#: 源码侧许可原文库（KB 级纯文本，可进 Git）；构建时只读本地
VENDOR_SUBDIR = ("vendor", "licenses")
PROVENANCE_NAME = "PROVENANCE.json"

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


#: 上游 METADATA 里已知的占位 URL（真实案例：sqlite-vec 的 Home-page 就是 `https://TODO.com`）。
#: 仅当**已有经 sha256 校验的 vendor 溯源**时才用它覆盖 —— 不是猜，是用更可靠的证据。
_PLACEHOLDER_URL_MARKERS = ("todo", "example.com", "example.invalid", "localhost")


def _is_placeholder_url(url: str | None) -> bool:
    if not url:
        return True
    low = url.lower()
    return any(m in low for m in _PLACEHOLDER_URL_MARKERS)


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
            target = dest / src.relative_to(dist_info)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            found.append(target)
    return sorted(dict.fromkeys(found), key=lambda p: p.as_posix())


# ---------------------------------------------------------------------------
# vendor 许可原文库（A3.1）
# ---------------------------------------------------------------------------
def vendor_licenses_dir() -> Path:
    """源码侧许可原文库根目录（相对本文件：``<repo>/vendor/licenses``）。

    发布态安装器不需要本模块，故这里用 `__file__` 推导即可；假仓库测试把
    `collect_licenses.py` 拷进自己的 `scripts/`，自然指向自己的 vendor。
    """
    return Path(__file__).resolve().parents[1].joinpath(*VENDOR_SUBDIR)


def find_vendor(vendor_dir: Path | None, pkg_name: str, version: str) -> Path | None:
    """按「规范化包名 + 精确版本」定位 vendor 目录；版本不同必须另开目录，不许复用。"""
    root = Path(vendor_dir) if vendor_dir else vendor_licenses_dir()
    if not root.is_dir():
        return None
    want = norm_name(pkg_name)
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or norm_name(entry.name) != want:
            continue
        candidate = entry / version
        if candidate.is_dir():
            return candidate
    return None


def apply_vendor(vendor_pkg_dir: Path, dest: Path,
                 problems: list[str]) -> tuple[list[Path], dict | None]:
    """把 vendor 声明的许可原文拷进发布包；任何不一致都记为 problem（不静默放过）。

    校验三件事：`PROVENANCE.json` 可解析且 `files` 非空、每个声明的文件存在、
    size 与 sha256 与声明一致 —— 后者就是「许可文本没被手写/改写」的执行器。
    """
    label = f"{vendor_pkg_dir.parent.name}@{vendor_pkg_dir.name}"
    prov_path = vendor_pkg_dir / PROVENANCE_NAME
    if not prov_path.is_file():
        problems.append(f"{label}: vendor 缺 {PROVENANCE_NAME}（来源不可审计）")
        return [], None
    try:
        prov = json.loads(prov_path.read_text(encoding="utf-8"))
    except Exception as exc:
        problems.append(f"{label}: {PROVENANCE_NAME} 无法解析（{exc}）")
        return [], None
    if not isinstance(prov, dict):
        problems.append(f"{label}: {PROVENANCE_NAME} 顶层不是对象")
        return [], None
    if prov.get("format_version") != FORMAT_VERSION:
        problems.append(f"{label}: {PROVENANCE_NAME}.format_version="
                        f"{prov.get('format_version')!r}（本程序支持 {FORMAT_VERSION}）")

    declared = prov.get("files")
    if not isinstance(declared, list) or not declared:
        problems.append(f"{label}: {PROVENANCE_NAME}.files 缺失或为空")
        return [], None

    copied: list[Path] = []
    for item in declared:
        if not isinstance(item, dict):
            problems.append(f"{label}: files 条目不是对象")
            continue
        name = str(item.get("name") or "").strip()
        if not name or name.startswith(".") or "/" in name or "\\" in name:
            problems.append(f"{label}: 非法许可文件名 {name!r}")
            continue
        src = vendor_pkg_dir / name
        if not src.is_file():
            problems.append(f"{label}: 随包许可原文缺失 {name}（vendor 不完整）")
            continue
        want_hash = str(item.get("sha256") or "").lower()
        if not want_hash:
            problems.append(f"{label}: {name} 未声明 sha256（无法证明未改写）")
            continue
        actual = sha256_file(src)
        if actual != want_hash:
            problems.append(f"{label}: {name} sha256 与 PROVENANCE 不一致"
                            f"（期望 {want_hash[:16]}…，实际 {actual[:16]}…）")
            continue
        size = item.get("size")
        if isinstance(size, int) and src.stat().st_size != size:
            problems.append(f"{label}: {name} 大小与 PROVENANCE 不一致"
                            f"（期望 {size}，实际 {src.stat().st_size}）")
            continue
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / name
        shutil.copy2(src, target)
        copied.append(target)
    return copied, prov


def _vendor_provenance_record(prov: dict, vendor_pkg_dir: Path) -> dict:
    """写进 THIRD_PARTY.json 的溯源块 —— 让许可来源可被机器审计。"""
    return {
        "upstream_project": prov.get("upstream_project"),
        "upstream_ref": prov.get("upstream_ref"),
        "upstream_commit": prov.get("upstream_commit"),
        "retrieved_at_utc": prov.get("retrieved_at_utc"),
        "declared_in_metadata": prov.get("declared_in_metadata"),
        "vendor_source": "/".join([*VENDOR_SUBDIR, vendor_pkg_dir.parent.name,
                                   vendor_pkg_dir.name]),
        "files": [
            {"name": f.get("name"),
             "source_url": f.get("source_url"),
             "sha256": f.get("sha256")}
            for f in (prov.get("files") or []) if isinstance(f, dict)
        ],
    }


# ---------------------------------------------------------------------------
# 主收集
# ---------------------------------------------------------------------------
def collect(runtime_dir: Path, lock_path: Path, root: Path,
            *, python_version: str | None = None,
            vendor_dir: Path | None = None) -> dict:
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
    vendor_root = Path(vendor_dir) if vendor_dir else vendor_licenses_dir()

    packages: list[dict] = []
    vendor_problems: list[str] = []
    model_problems: list[str] = []

    if site_packages.is_dir():
        for info_dir in sorted(site_packages.glob("*.dist-info")):
            entry = _one_package(info_dir, pkg_root, locked, root,
                                 vendor_root, vendor_problems)
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

    # 随包模型的许可（与 Python 包分开记录；同样只做本地拷贝）
    models = _collect_model_licenses(lic_root, root, model_problems)

    complete = (bool(packages) and site_packages.is_dir()
                and not vendor_problems and not model_problems)

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
    metadata_only = sorted(p["package"] for p in packages
                           if p["review_status"] == STATUS_METADATA_ONLY)
    # A3.1：**随包运行依赖**（依赖锁内）不得停留在 metadata_only ——
    # 元数据能回答「是什么许可」，但发布物仍缺许可原文，分发义务未闭。
    required_metadata_only = sorted(
        p["package"] for p in packages
        if p["review_status"] == STATUS_METADATA_ONLY and p["required"]
    )

    inventory = {
        "format_version": FORMAT_VERSION,
        "inventory_complete": complete,
        "source": {
            "runtime": "payload/python-runtime",
            "lock": Path(lock_path).name,
            "vendor_licenses": "/".join(VENDOR_SUBDIR),
            "python_version": python_version,
        },
        "summary": {
            "packages": len(packages),
            "review_required": len(review_required),
            "metadata_only": len(metadata_only),
            "required_metadata_only": required_metadata_only,
            "locked_total": len(locked),
            "locked_missing": locked_missing,
            "version_mismatch": version_mismatch,
            "vendor_problems": vendor_problems,
            "models": len(models),
            "model_problems": model_problems,
        },
        "packages": packages,
        "models": models,
    }

    (lic_root / THIRD_PARTY_JSON).write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    (lic_root / THIRD_PARTY_NOTICES).write_text(
        _render_notices(packages, inventory), encoding="utf-8", newline="\n")
    return inventory


def _one_package(info_dir: Path, pkg_root: Path, locked: dict[str, str], root: Path,
                 vendor_root: Path, vendor_problems: list[str]) -> dict | None:
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
    bundled = _collect_license_files(info_dir, dest)

    vendored, prov = [], None
    vdir = find_vendor(vendor_root, name, version)
    if vdir is not None:
        vendored, prov = apply_vendor(vdir, dest, vendor_problems)

    texts = [*bundled, *vendored]
    texts_rel = sorted({p.relative_to(root).as_posix() for p in texts})
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
        # ⚠ 但若它属于随包运行依赖（required），strict 门禁仍会以
        #   LICENSE_TEXT_MISSING 拒绝发布 —— 见 collect() 的 required_metadata_only。
        status = STATUS_METADATA_ONLY

    # 许可「是什么」的证据来源 与 许可「原文从哪来」是两件事，分开记：
    #   license_source       —— 身份依据（Metadata License / Classifier / License-Expression / license file）
    #   license_text_source  —— 原文出处（随包 dist-info / vendor 许可原文库）
    if vendored and bundled:
        text_source = "随包 dist-info + vendor 许可原文库（均校验）"
    elif vendored:
        text_source = "vendor 许可原文库（已校验 sha256）"
    elif bundled:
        text_source = "随包 dist-info"
    else:
        text_source = None

    entry = {
        "package": name,
        "version": version,
        "license": license_value,
        "license_source": license_source or ("license file" if texts_rel else None),
        "license_text_source": text_source,
        "project": project,
        "license_texts": texts_rel,
        "required": norm_name(name) in locked,
        "review_status": status,
    }
    if prov is not None:
        entry["provenance"] = _vendor_provenance_record(prov, vdir)
        upstream = prov.get("upstream_project")
        if upstream and _is_placeholder_url(entry["project"]):
            # 元数据里的 Home-page 是上游占位符（如 https://TODO.com）时，
            # 用已 pin commit 的 vendor 溯源 URL —— 只在此情形覆盖，不做揣测。
            entry["project"] = upstream
    return entry


def _collect_model_licenses(lic_root: Path, root: Path,
                            problems: list[str]) -> list[dict]:
    """拷贝随包模型的许可原文 → ``LICENSES/models/<model>/``。

    与 Python 包分开处理的原因：模型许可的**来源关系**更复杂 ——
    许可来自 base model（BAAI，MIT），而社区转换仓库常常不声明 license。
    因此这里逐字段如实记录（base / conversion 分列），**不把转换仓库硬标成 MIT**。

    ⚠ 同样只用本地 vendor/ 里的字节，构建时不联网。
    """
    out: list[dict] = []
    src_root = Path(__file__).resolve().parents[1] / "vendor" / "licenses"
    for entry in sorted(src_root.iterdir()) if src_root.is_dir() else []:
        prov_path = entry / "PROVENANCE.json"
        if not entry.is_dir() or not prov_path.is_file():
            continue
        try:
            prov = json.loads(prov_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"模型许可 {entry.name}: PROVENANCE.json 无法解析（{exc}）")
            continue
        if prov.get("kind") != "bundled_model":
            continue          # 其它 vendor 条目是 Python 包，走 packages/ 那条路

        dest = lic_root / MODELS_DIRNAME / entry.name
        dest.mkdir(parents=True, exist_ok=True)
        copied = []
        for item in prov.get("files") or []:
            name = str(item.get("name") or "")
            src = entry / name
            if not src.is_file():
                problems.append(f"模型许可 {entry.name}: 缺 {name}")
                continue
            want = str(item.get("sha256") or "").lower()
            actual = sha256_file(src)
            if want and actual != want:
                problems.append(
                    f"模型许可 {entry.name}: {name} sha256 不符"
                    f"（期望 {want[:16]}…，实际 {actual[:16]}…）")
                continue
            shutil.copy2(src, dest / name)
            copied.append(dest / name)
        shutil.copy2(prov_path, dest / "PROVENANCE.json")

        base = prov.get("base_model") or {}
        conv = prov.get("conversion_artifact") or {}
        out.append({
            "package": prov.get("package") or entry.name,
            "version": conv.get("revision") or "",
            "kind": "bundled_model",
            # ⚠ 分开记录，不伪造
            "license_basis": conv.get("license_basis"),
            "base_model": base.get("id"),
            "base_model_license": base.get("license"),
            "conversion_source": conv.get("repository"),
            "conversion_revision": conv.get("revision"),
            "conversion_repository_license": conv.get("repository_license"),
            "precision": conv.get("file"),
            "license_texts": sorted({p.relative_to(root).as_posix() for p in copied}
                                    | {f"{LICENSES_DIRNAME}/{MODELS_DIRNAME}/{entry.name}/PROVENANCE.json"}),
            "review_status": STATUS_OK if copied else STATUS_REVIEW,
        })
    return out


def _render_notices(packages: list[dict], inventory: dict) -> str:
    out = io.StringIO()
    out.write("Wiki-USB 第三方软件许可清单（THIRD-PARTY NOTICES）\n")
    out.write("=" * 72 + "\n\n")
    out.write("本文件由 scripts/collect_licenses.py 自动生成，覆盖本 Release **实际随包分发**的\n")
    out.write("第三方组件。未随包分发的组件（如本地 LLM / Ollama / ONNX 模型）不在此列。\n\n")
    s = inventory["summary"]
    out.write(f"组件总数：{s['packages']}    待人工复核：{s['review_required']}"
              f"    仅元数据（无许可原文）：{s['metadata_only']}\n")
    out.write(f"依赖锁条目：{s['locked_total']}    "
              f"锁内缺失：{len(s['locked_missing'])}    版本差异：{len(s['version_mismatch'])}\n\n")
    out.write("-" * 72 + "\n")
    for p in packages:
        out.write(f"{p['package']} {p['version']}\n")
        out.write(f"  许可        : {p['license'] or '(未在元数据中声明)'}\n")
        out.write(f"  许可依据    : {p['license_source'] or '-'}\n")
        out.write(f"  原文来源    : {p.get('license_text_source') or '(未随附)'}\n")
        out.write(f"  项目主页    : {p['project'] or '-'}\n")
        out.write(f"  许可原文    : "
                  + (", ".join(p["license_texts"]) if p["license_texts"] else "(未随附)")
                  + "\n")
        out.write(f"  依赖锁直接/间接依赖 : {'是' if p['required'] else '否（随运行时附带）'}\n")
        out.write(f"  复核状态    : {p['review_status']}\n")
        prov = p.get("provenance")
        if prov:
            out.write(f"  上游溯源    : {prov.get('upstream_project') or '-'}"
                      f" @ {prov.get('upstream_ref') or '-'}"
                      f" ({prov.get('upstream_commit') or '-'})"
                      f"  取样于 {prov.get('retrieved_at_utc') or '-'}\n")
            out.write(f"  原文库      : {prov.get('vendor_source') or '-'}\n")
            for f in prov.get("files") or []:
                out.write(f"    - {f.get('name')}  <- {f.get('source_url')}\n")
        out.write("\n")
    if s["required_metadata_only"]:
        out.write("-" * 72 + "\n")
        out.write("⚠ 以下**随包运行依赖**只有元数据、缺许可原文（分发义务未闭，"
                  "请在 vendor/licenses/ 补齐）：\n")
        for n in s["required_metadata_only"]:
            out.write(f"  - {n}\n")
    if s["vendor_problems"]:
        out.write("-" * 72 + "\n")
        out.write("⚠ vendor 许可原文库存在不一致（strict 构建会拒绝）：\n")
        for n in s["vendor_problems"]:
            out.write(f"  - {n}\n")
    if inventory.get("models"):
        out.write("-" * 72 + "\n")
        out.write("随包模型（许可来源分列记录，不伪造转换仓库许可）：\n")
        for m in inventory["models"]:
            out.write(f"{m['package']}\n")
            out.write(f"  base model   : {m['base_model']}（{m['base_model_license']}）\n")
            out.write(f"  转换来源     : {m['conversion_source']} @ {m['conversion_revision']}"
                      f"（仓库许可：{m['conversion_repository_license']}）\n")
            out.write(f"  许可依据     : {m['license_basis']}\n")
            out.write(f"  许可原文     : {', '.join(m['license_texts'])}\n\n")
    if s["model_problems"]:
        out.write("-" * 72 + "\n")
        out.write("⚠ 模型许可不完整（strict 构建会拒绝）：\n")
        for n in s["model_problems"]:
            out.write(f"  - {n}\n")
    if s["locked_missing"]:
        out.write("-" * 72 + "\n")
        out.write("⚠ 以下依赖锁条目在随包运行时中未找到（发布不完整）：\n")
        for n in s["locked_missing"]:
            out.write(f"  - {n}\n")
    if s["version_mismatch"]:
        out.write("-" * 72 + "\n")
        out.write("⚠ 以下依赖版本与依赖锁不一致（随包运行时非由当前锁构建，"
                  "strict 构建会以 RUNTIME_LOCK_MISMATCH 拒绝）：\n")
        for n in s["version_mismatch"]:
            out.write(f"  - {n}\n")
    return out.getvalue()
