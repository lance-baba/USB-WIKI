#!/usr/bin/env python3
"""发布介质完整性 —— BUILD_INFO / RELEASE_MANIFEST / SHA256SUMS 的**唯一实现**。

为什么必须唯一
==============
构建侧（`scripts/build_release.py`）要**生成**清单，安装侧（`installer/install.py`）
要**校验**清单。若各写一套文件枚举，两套逻辑迟早漂移 —— 届时「构建产出的清单
通不过安装器校验」或更糟「清单漏了一个目录而校验照样通过」。
因此枚举/哈希/校验只有这一份实现，安装器以同目录副本
（`installer/release_integrity.py`）复用。

定位（V1 Freeze / A3）
======================
    SHA256 == Integrity（介质损坏检测 ≠ 发布者身份）
    不是 Authenticity —— 不做私钥 / 签名 / 激活 / DRM / updater。

边界
====
* 纯标准库；不联网；不 import `app`（安装器运行时尚无 app 可导入）。
* `verify_media()` 只读：不建 Library、不写 App、不产生任何副作用。
* BUILD_INFO 绝不含用户名 / 绝对路径 / HOME / LOCALAPPDATA / IP / 机器标识 / key。

产物布局（相对发布根）::

    RELEASE_MANIFEST.json      ← 本模块生成（自身不进清单，避免递归 hash）
    SHA256SUMS                 ← 本模块生成（由清单派生，永不漂移）
    BUILD_INFO.json            ← 本模块生成（进清单）
    LICENSES/                  ← collect_licenses.py 生成（进清单）
    installer/  payload/
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# 发布格式版本：清单结构 / 布局约定变更时才 +1（与 APP_VERSION / SCHEMA / DATA_FORMAT 无关）
FORMAT_VERSION = 1

MANIFEST_NAME = "RELEASE_MANIFEST.json"
CHECKSUMS_NAME = "SHA256SUMS"
BUILD_INFO_NAME = "BUILD_INFO.json"
LICENSES_DIRNAME = "LICENSES"
THIRD_PARTY_REL = f"{LICENSES_DIRNAME}/THIRD_PARTY.json"

#: 校验时**必须**在清单中出现的关键交付物（缺失即视为不可交付）
REQUIRED_MANIFEST_ENTRIES = (BUILD_INFO_NAME, THIRD_PARTY_REL)

#: 自身与派生文件 —— 不进清单，否则递归 hash / 自引用
_DERIVED_NAMES = frozenset({MANIFEST_NAME, CHECKSUMS_NAME})

#: 解释器缓存与系统垃圾 —— 不随产品交付，不进清单
_JUNK_DIR_NAMES = frozenset({"__pycache__"})
_JUNK_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
_JUNK_SUFFIXES = frozenset({".pyc", ".pyo"})


# ---------------------------------------------------------------------------
# 哈希 / 枚举
# ---------------------------------------------------------------------------
def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _is_junk(rel: Path) -> bool:
    if any(part in _JUNK_DIR_NAMES for part in rel.parts):
        return True
    if rel.name in _JUNK_FILE_NAMES:
        return True
    return rel.suffix.lower() in _JUNK_SUFFIXES


def iter_release_files(root: Path) -> list[Path]:
    """随产品交付的文件（相对 *root*），固定排序 + POSIX 分隔符。

    排除：清单/校验和自身（递归 hash）、解释器缓存、系统垃圾文件。
    排序键固定为 POSIX 相对路径 —— 换机器 / 换 OS 产出同一份清单。
    """
    root = Path(root)
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if rel.name in _DERIVED_NAMES or _is_junk(rel):
            continue
        out.append(rel)
    return sorted(out, key=lambda r: r.as_posix())


# ---------------------------------------------------------------------------
# 清单 / 校验和
# ---------------------------------------------------------------------------
def build_manifest(root: Path) -> dict:
    root = Path(root)
    files = [
        {
            "path": rel.as_posix(),
            "size": (root / rel).stat().st_size,
            "sha256": sha256_file(root / rel),
        }
        for rel in iter_release_files(root)
    ]
    return {"format_version": FORMAT_VERSION, "files": files}


def write_manifest(root: Path) -> tuple[Path, dict]:
    root = Path(root)
    manifest = build_manifest(root)
    dst = root / MANIFEST_NAME
    dst.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    return dst, manifest


def write_checksums(root: Path, manifest: dict) -> Path:
    """传统 `<sha256>  <relative/path>` 格式。

    由清单**派生**而非二次枚举 —— 两者永不漂移（同一套枚举的唯一出口）。
    """
    root = Path(root)
    lines = [f"{e['sha256']}  {e['path']}" for e in manifest["files"]]
    dst = root / CHECKSUMS_NAME
    dst.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return dst


# ---------------------------------------------------------------------------
# BUILD_INFO —— 字段一律取自已有唯一来源，禁止再次硬编码
# ---------------------------------------------------------------------------
def resolve_git_commit(repo: Path) -> str:
    """提交号：git → 环境变量 → "unknown"。不写入任何仓库路径。"""
    try:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    for key in ("WIKIUSB_BUILD_COMMIT", "GITHUB_SHA"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return "unknown"


def resolve_python_version(runtime_dir: Path) -> str | None:
    """随包运行时的**真实** Python 版本（不是构建机的）。

    优先级：实际执行嵌入式 python.exe → 目录内 python3NN.dll 文件名推断 → None。
    构建机版本（如 3.13）与被测运行时（3.11.9）不同，绝不能拿构建机的顶替。
    """
    runtime_dir = Path(runtime_dir)
    exe = runtime_dir / "python.exe"
    if exe.is_file():
        try:
            r = subprocess.run(
                [str(exe), "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"],
                capture_output=True, text=True, timeout=60)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
    for dll in sorted(runtime_dir.glob("python3*.dll")):
        digits = "".join(ch for ch in dll.stem if ch.isdigit())
        if len(digits) >= 2:
            return f"{digits[0]}.{digits[1:]}"
    return None


def build_info(*, platform: str, commit: str, python_version: str | None,
               dependency_lock_sha256: str | None, schema_version: str,
               data_format_version: int, build_time_utc: str | None = None) -> dict:
    """按固定键序构造 BUILD_INFO。**只含版本事实，不含任何环境身份。**"""
    return {
        "app_version": _app_version(),
        "git_commit": commit,
        "build_time_utc": build_time_utc or datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platform": platform,
        "python_version": python_version,
        "dependency_lock_sha256": dependency_lock_sha256,
        "schema_version": schema_version,
        "data_format_version": data_format_version,
        "release_format_version": FORMAT_VERSION,
    }


def _app_version() -> str:
    """APP_VERSION 的唯一来源是 app/version.py —— 不在此处再写一次字面量。"""
    try:
        from app.version import APP_VERSION  # noqa: PLC0415
        return APP_VERSION
    except Exception:
        return "unknown"


def write_build_info(root: Path, info: dict) -> Path:
    dst = Path(root) / BUILD_INFO_NAME
    dst.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    return dst


def read_build_info(root: Path) -> dict | None:
    dst = Path(root) / BUILD_INFO_NAME
    if not dst.is_file():
        return None
    try:
        return json.loads(dst.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 介质校验（安装前闸门 + 独立 verify 命令共用）
# ---------------------------------------------------------------------------
@dataclass
class MediaCheck:
    ok: bool
    code: str                       # "OK" | "MEDIA_CORRUPTED"
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    file_count: int = 0
    build_info: dict | None = None

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "ok": self.ok,
            "file_count": self.file_count,
            "failures": self.failures,
            "warnings": self.warnings,
            "build_info": self.build_info,
        }


def _manifest_entries(manifest: object) -> tuple[list[dict], list[str]]:
    """从已解析的 manifest 中取出条目，顺带做结构校验。"""
    problems: list[str] = []
    if not isinstance(manifest, dict):
        return [], [f"{MANIFEST_NAME}: 顶层不是 JSON 对象"]
    if manifest.get("format_version") != FORMAT_VERSION:
        problems.append(f"{MANIFEST_NAME}: format_version={manifest.get('format_version')!r}"
                        f"（本程序支持 {FORMAT_VERSION}）")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        problems.append(f"{MANIFEST_NAME}: files 缺失或为空")
        return [], problems
    entries: list[dict] = []
    for i, e in enumerate(files):
        if not isinstance(e, dict) or not {"path", "size", "sha256"} <= set(e):
            problems.append(f"{MANIFEST_NAME}: files[{i}] 字段不完整")
            continue
        path = str(e["path"])
        pure = path.replace("\\", "/")
        if pure.startswith("/") or ".." in pure.split("/") or ":" in pure.split("/")[0]:
            problems.append(f"{MANIFEST_NAME}: files[{i}] 非安全相对路径 {path!r}")
            continue
        entries.append({"path": pure, "size": e["size"], "sha256": str(e["sha256"])})
    return entries, problems


def verify_media(root: Path, *, deep: bool = True) -> MediaCheck:
    """校验发布介质：清单可解析 → 文件齐 → size 符 → SHA256 符。

    *deep* 为假时只校验存在性与 size（用于超大介质的快速体检）。
    **只读**：不打日志文件、不建目录、不修改任何输入。
    """
    root = Path(root)
    failures: list[str] = []
    warnings: list[str] = []

    man_path = root / MANIFEST_NAME
    if not man_path.is_file():
        return MediaCheck(False, "MEDIA_CORRUPTED",
                          [f"{MANIFEST_NAME}: 不存在（发布介质不完整）"])

    raw = man_path.read_bytes()
    if not raw.strip():
        return MediaCheck(False, "MEDIA_CORRUPTED",
                          [f"{MANIFEST_NAME}: 空文件 / 被截断"])
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return MediaCheck(False, "MEDIA_CORRUPTED",
                          [f"{MANIFEST_NAME}: 非 UTF-8 文本（介质损坏）"])
    except json.JSONDecodeError as exc:
        return MediaCheck(False, "MEDIA_CORRUPTED",
                          [f"{MANIFEST_NAME}: 非法 JSON（{exc.msg} @行{exc.lineno}）"])

    entries, problems = _manifest_entries(manifest)
    failures.extend(problems)

    paths = {e["path"] for e in entries}
    for required in REQUIRED_MANIFEST_ENTRIES:
        if required not in paths:
            failures.append(f"{MANIFEST_NAME}: 未声明必需交付物 {required}")

    checked = 0
    for e in entries:
        rel, want_size, want_hash = e["path"], e["size"], e["sha256"]
        target = root / rel
        if not target.is_file():
            failures.append(f"{rel}: 文件缺失")
            continue
        checked += 1
        actual_size = target.stat().st_size
        if not isinstance(want_size, int) or actual_size != want_size:
            failures.append(f"{rel}: 大小不符（期望 {want_size}，实际 {actual_size}）")
            continue
        if deep:
            actual_hash = sha256_file(target)
            if actual_hash.lower() != want_hash.lower():
                failures.append(f"{rel}: SHA256 不符（期望 {want_hash[:16]}…，"
                                f"实际 {actual_hash[:16]}…）")
    if not deep:
        warnings.append("已跳过 SHA256 深度校验（--shallow）")

    info = read_build_info(root)
    if info is None:
        failures.append(f"{BUILD_INFO_NAME}: 缺失或无法解析")
    else:
        if not info.get("git_commit") or info.get("git_commit") == "unknown":
            warnings.append(f"{BUILD_INFO_NAME}: git_commit 未解析（版本不可追溯）")
        if not info.get("python_version"):
            warnings.append(f"{BUILD_INFO_NAME}: python_version 缺失")

    tp = root / THIRD_PARTY_REL
    if tp.is_file():
        try:
            inv = json.loads(tp.read_text(encoding="utf-8"))
            if not inv.get("inventory_complete", False):
                warnings.append(f"{THIRD_PARTY_REL}: inventory_complete=false"
                                "（该构建未收集第三方清单，非正式发布）")
        except Exception:
            failures.append(f"{THIRD_PARTY_REL}: 无法解析")

    ok = not failures
    return MediaCheck(ok, "OK" if ok else "MEDIA_CORRUPTED",
                      failures, warnings, checked, info)
