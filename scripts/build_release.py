#!/usr/bin/env python3
"""Release Builder —— 把源码仓库组装成发布产物 dist/USB-WIKI-vX.Y.Z-win-x64/。

产物是**构建产物**，不进 Git（dist/ 已在 .gitignore）。

布局：
    dist/USB-WIKI-v1.3.0-win-x64/
    ├─ installer/
    │  ├─ install.py            (SSD 安装骨架，拷贝自 scripts/install_windows.py)
    │  ├─ install.bat           (Windows：用自带嵌入式 Python 跑 install.py)
    │  └─ release_integrity.py  (介质校验唯一实现，随安装器同目录分发)
    ├─ payload/
    │  ├─ app/                  (拷贝自仓库 app/)
    │  └─ python-runtime/       (拷贝自仓库 runtime/python-3.11-embed/)
    ├─ LICENSES/                (第三方分发许可清单，collect_licenses.py 生成)
    ├─ BUILD_INFO.json          (版本可追溯性)
    ├─ RELEASE_MANIFEST.json    (机器可读清单：相对路径 / 固定排序 / 含 sha256)
    └─ SHA256SUMS               (传统校验和，由清单派生)

约定（V1 Freeze / A3 范围）：
- 不联网、不 pip、不下载任何 >100MB 资源。
- 版本号唯一来源：app.version.APP_VERSION（本文件不写版本字面量）。
- SHA256 定位是 **Integrity**（介质损坏检测），不是 Authenticity —— 不签名、不加密。
- BUILD_INFO 绝不含 用户名 / 绝对路径 / HOME / LOCALAPPDATA / IP / 机器标识 / key。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent            # scripts/
REPO = HERE.parent                                 # 仓库根

#: 随发布交付的文档白名单（**显式**列出：内部过程文档 / 已知问题备忘不进客户包）
RELEASE_DOC_FILES = (
    "README.md", "CHANGELOG.md", "LICENSE", "SECURITY.md",
    "CODE_OF_CONDUCT.md", "CONTRIBUTING.md",
)
RELEASE_DOCS_DIR_FILES = (
    "DATA_CONTRACT_V1.md", "DEPENDENCY_LOCK.md", "设计与实现.md",
    "项目架构说明.md", "测试报告.md", "V1.3工程收口基线.md",
)


def _ensure_repo_on_path() -> None:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))


def _copytree_prune(src: Path, dst: Path) -> None:
    """拷贝目录，剔除 __pycache__ / *.pyc（发布物不该带解释器缓存）。"""
    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        dirs_exist_ok=True,
    )


def _fail_build(msg: str) -> "None":
    print(f"[build] 错误：{msg}", file=sys.stderr)
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# 各构建阶段
# ---------------------------------------------------------------------------
def _stage_payload(dist_root: Path) -> bool:
    """app/ + python-runtime/。返回 runtime 是否就位。"""
    payload = dist_root / "payload"
    if not (REPO / "app").is_dir():
        _fail_build(f"仓库 app/ 不存在，无法构建 payload：{REPO / 'app'}")
    _copytree_prune(REPO / "app", payload / "app")

    rt_src = REPO / "runtime" / "python-3.11-embed"
    rt_dst = payload / "python-runtime"
    if (rt_src / "python.exe").is_file():
        _copytree_prune(rt_src, rt_dst)
        return True
    return False


def _stage_installer(dist_root: Path) -> None:
    installer_dir = dist_root / "installer"
    installer_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HERE / "install_windows.py", installer_dir / "install.py")
    # 介质校验唯一实现：安装器同目录副本，避免「构建/校验」两套枚举漂移
    shutil.copy2(HERE / "release_integrity.py", installer_dir / "release_integrity.py")
    (installer_dir / "install.bat").write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "cd /d \"%~dp0\"\r\n"
        "if not exist \"..\\payload\\python-runtime\\python.exe\" (\r\n"
        "  echo MEDIA_CORRUPTED: payload\\python-runtime\\python.exe missing\r\n"
        "  exit /b 5\r\n"
        ")\r\n"
        "..\\payload\\python-runtime\\python.exe install.py %*\r\n",
        encoding="ascii",
    )
    if not (installer_dir / "install.py").is_file():
        _fail_build("installer/install.py 生成失败")
    if not (installer_dir / "release_integrity.py").is_file():
        _fail_build("installer/release_integrity.py 生成失败")


def _stage_docs(dist_root: Path) -> int:
    """随包文档（白名单）—— MANIFEST 需能覆盖到「docs」。"""
    count = 0
    for name in RELEASE_DOC_FILES:
        src = REPO / name
        if src.is_file():
            shutil.copy2(src, dist_root / name)
            count += 1
    docs_dst = dist_root / "docs"
    for name in RELEASE_DOCS_DIR_FILES:
        src = REPO / "docs" / name
        if src.is_file():
            docs_dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, docs_dst / name)
            count += 1
    return count


def _version_sources() -> dict:
    """schema / data format 的**唯一来源**，一律 import 现取，不写字面量。"""
    from app.core.migrations import CURRENT_SCHEMA_VERSION      # noqa: PLC0415
    from app.core.library import DATA_FORMAT_VERSION            # noqa: PLC0415
    return {"schema_version": CURRENT_SCHEMA_VERSION,
            "data_format_version": DATA_FORMAT_VERSION}


def _build_info(root: Path, platform: str, rt_ok: bool) -> dict:
    import release_integrity as ri                              # noqa: PLC0415

    lock = REPO / "requirements-release.lock"
    lock_sha = ri.sha256_file(lock) if lock.is_file() else None
    py_ver = ri.resolve_python_version(root / "payload" / "python-runtime") if rt_ok else None
    versions = _version_sources()
    return ri.build_info(
        platform=platform,
        commit=ri.resolve_git_commit(REPO),
        python_version=py_ver,
        dependency_lock_sha256=lock_sha,
        schema_version=versions["schema_version"],
        data_format_version=versions["data_format_version"],
    )


def build(platform: str, output: Path, strict: bool = False) -> Path:
    _ensure_repo_on_path()
    from app.version import APP_VERSION            # 唯一版本源  # noqa: PLC0415

    name = f"USB-WIKI-v{APP_VERSION}-{platform}"
    dist_root = output / name
    if dist_root.exists():
        shutil.rmtree(dist_root)

    rt_ok = _stage_payload(dist_root)
    if not rt_ok:
        if strict:
            _fail_build(f"严格模式：未找到嵌入式 Python "
                        f"{REPO / 'runtime' / 'python-3.11-embed'}。"
                        " 正式发布前请先 `python setup_runtime_windows.py`。")
        print(f"[build] 警告：未找到嵌入式 Python，跳过 python-runtime 拷贝。"
              " 正式发布前请先 `python setup_runtime_windows.py`。", file=sys.stderr)

    _stage_installer(dist_root)
    doc_count = _stage_docs(dist_root)
    dist_root.mkdir(parents=True, exist_ok=True)

    import collect_licenses as cl                               # noqa: PLC0415
    import release_integrity as ri                              # noqa: PLC0415

    # 1) LICENSES（只审实际随包内容；无运行时则记录 inventory_complete=false）
    licenses = cl.collect(
        dist_root / "payload" / "python-runtime",
        REPO / "requirements-release.lock",
        dist_root,
        python_version=ri.resolve_python_version(dist_root / "payload" / "python-runtime"),
    )

    # 2) BUILD_INFO
    info = _build_info(dist_root, platform, rt_ok)
    ri.write_build_info(dist_root, info)

    # 3) RELEASE_MANIFEST → 4) SHA256SUMS（同一套枚举，单一出口）
    _, manifest = ri.write_manifest(dist_root)
    ri.write_checksums(dist_root, manifest)

    # 5) 自校验：构建产物必须能通过自己的介质校验（否则「看起来成功」最危险）
    check = ri.verify_media(dist_root)

    if strict:
        _strict_gate(dist_root, rt_ok, info, licenses, manifest, check, doc_count)

    print(f"{'BUILD OK' if strict else 'DEV BUILD OK（非正式发布）'} -> {dist_root}")
    print(f"  发布格式版本           {ri.FORMAT_VERSION}")
    print(f"  文件数（清单）         {len(manifest['files'])}")
    print(f"  payload/app/           {(dist_root / 'payload' / 'app').is_dir()}")
    print(f"  payload/python-runtime/ {rt_ok}")
    print(f"  installer/              {(dist_root / 'installer' / 'install.py').is_file()}")
    print(f"  随包文档               {doc_count}")
    print(f"  第三方组件             {licenses['summary']['packages']}"
          f"（待复核 {licenses['summary']['review_required']}）")
    if licenses["summary"]["locked_missing"]:
        print(f"  ⚠ 依赖锁条目未随包     {licenses['summary']['locked_missing']}")
    if licenses["summary"]["version_mismatch"]:
        # 发布构建从 lock 安装，正常应零差异；有差异说明 runtime 非由当前锁构建
        print("  ⚠ 随包版本 ≠ 依赖锁     "
              + "；".join(licenses["summary"]["version_mismatch"]))
    print(f"  介质自校验             {check.code}"
          + (f" 失败 {len(check.failures)} 项" if check.failures else ""))
    return dist_root


def _strict_gate(dist_root: Path, rt_ok: bool, info: dict, licenses: dict,
                 manifest: dict, check, doc_count: int) -> None:
    """正式发布的完整门禁 —— 任何 RELEASE_REQUIRED 文件缺失即非零退出。"""
    problems: list[str] = []

    if not (dist_root / "payload" / "app").is_dir():
        problems.append("缺少 payload/app/")
    if not rt_ok or not (dist_root / "payload" / "python-runtime" / "python.exe").is_file():
        problems.append("缺少 payload/python-runtime/python.exe")
    if not (dist_root / "installer" / "install.py").is_file():
        problems.append("缺少 installer/install.py")
    if not (dist_root / "installer" / "install.bat").is_file():
        problems.append("缺少 installer/install.bat")
    if not info.get("git_commit") or info.get("git_commit") == "unknown":
        problems.append("BUILD_INFO.git_commit 未解析（版本不可追溯）")
    if not info.get("python_version"):
        problems.append("BUILD_INFO.python_version 未解析")
    if not info.get("dependency_lock_sha256"):
        problems.append("BUILD_INFO.dependency_lock_sha256 缺失（依赖锁不在仓库）")
    if not licenses.get("inventory_complete"):
        problems.append("LICENSES 清单不完整（inventory_complete=false）")
    if licenses["summary"]["locked_missing"]:
        problems.append("依赖锁条目未随包："
                        + ", ".join(licenses["summary"]["locked_missing"]))
    if licenses["summary"]["review_required"]:
        problems.append(f"{licenses['summary']['review_required']} 个组件许可待人工复核"
                        "（LICENSE_REVIEW_REQUIRED，V1 发布前必须清零）")
    if not manifest.get("files"):
        problems.append("RELEASE_MANIFEST 为空")
    if not (dist_root / "SHA256SUMS").is_file():
        problems.append("缺少 SHA256SUMS")
    if doc_count == 0:
        problems.append("随包文档为 0（文档白名单与仓库不匹配）")
    if not check.ok:
        problems.append(f"介质自校验未通过（{check.code}）："
                        + "；".join(check.failures[:5]))

    if problems:
        _fail_build("严格模式门禁未通过 —— " + " | ".join(problems))


def main() -> int:
    ap = argparse.ArgumentParser(description="USB-WIKI Release Builder")
    ap.add_argument("--platform", default="win-x64")
    ap.add_argument("--output", default=str(REPO / "dist"), help="dist 根目录")
    ap.add_argument("--strict", action="store_true",
                    help="严格模式（正式发布）：app/runtime/installer/BUILD_INFO/"
                         "LICENSES/MANIFEST/SHA256SUMS/自校验 全通过才退出 0；"
                         "开发模式缺 runtime 仍可构建")
    args = ap.parse_args()
    build(args.platform, Path(args.output).resolve(), strict=args.strict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
