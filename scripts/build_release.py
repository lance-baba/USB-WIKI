#!/usr/bin/env python3
"""Release Builder —— 把源码仓库组装成发布产物 dist/USB-WIKI-vX.Y.Z-win-x64/。

产物是**构建产物**，不进 Git（dist/ 已在 .gitignore）。

布局：
    dist/USB-WIKI-v1.3.0-win-x64/
    ├─ installer/
    │  ├─ install.py            (SSD 安装骨架，拷贝自 scripts/install_windows.py)
    │  └─ install.bat           (Windows：用自带嵌入式 Python 跑 install.py)
    └─ payload/
       ├─ app/                  (拷贝自仓库 app/)
       └─ python-runtime/       (拷贝自仓库 runtime/python-3.11-embed/；缺则跳过并告警)

约定（V1 Freeze / A1 范围）：
- 不联网、不 pip、不下载任何 >100MB 资源。
- 版本号唯一来源：app.version.APP_VERSION。
- 大型 runtime / 模型 / Ollama 二进制不进入 Git；完整发布前须先跑
  setup_runtime_windows.py 把 runtime/python-3.11-embed 准备好，本脚本再拷贝它。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent            # scripts/
REPO = HERE.parent                                 # 仓库根


def _ensure_repo_on_path() -> None:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))


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


def build(platform: str, output: Path, strict: bool = False) -> Path:
    _ensure_repo_on_path()
    from app.version import APP_VERSION            # 唯一版本源

    name = f"USB-WIKI-v{APP_VERSION}-{platform}"
    dist_root = output / name
    if dist_root.exists():
        shutil.rmtree(dist_root)

    payload = dist_root / "payload"
    app_dst = payload / "app"
    rt_src = REPO / "runtime" / "python-3.11-embed"
    rt_dst = payload / "python-runtime"

    # app 始终必需（缺了就没有可发布的 payload，任何模式都必须失败）
    if not (REPO / "app").is_dir():
        _fail_build(f"仓库 app/ 不存在，无法构建 payload：{REPO / 'app'}")
    _copytree_prune(REPO / "app", app_dst)

    if (rt_src / "python.exe").is_file():
        _copytree_prune(rt_src, rt_dst)
    else:
        # 开发态 checkout 可能没构建 runtime（runtime/ 已 gitignore）。
        # 普通模式只告警、不硬失败；严格模式（正式发布）必须非零退出，
        # 避免出现「看起来成功但不可安装」的发布包。
        if strict:
            _fail_build(f"严格模式：未找到嵌入式 Python {rt_src}。"
                        " 正式发布前请先 `python setup_runtime_windows.py`。")
        print(f"[build] 警告：未找到 {rt_src}，跳过 python-runtime 拷贝。"
              " 正式发布前请先 `python setup_runtime_windows.py`。", file=sys.stderr)

    # installer：拷贝自唯一来源 scripts/install_windows.py
    installer_dir = dist_root / "installer"
    installer_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HERE / "install_windows.py", installer_dir / "install.py")
    (installer_dir / "install.bat").write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "cd /d \"%~dp0\"\r\n"
        "..\\payload\\python-runtime\\python.exe install.py %*\r\n",
        encoding="utf-8",
    )
    if not (installer_dir / "install.py").is_file():
        _fail_build("installer/install.py 生成失败")
    return dist_root


def main() -> int:
    ap = argparse.ArgumentParser(description="USB-WIKI Release Builder")
    ap.add_argument("--platform", default="win-x64")
    ap.add_argument("--output", default=str(REPO / "dist"), help="dist 根目录")
    ap.add_argument("--strict", action="store_true",
                    help="严格模式：缺 app/嵌入式 runtime/installer 时非零退出"
                         "（用于正式发布；开发模式缺 runtime 仍可构建）")
    args = ap.parse_args()
    out = Path(args.output).resolve()
    root = build(args.platform, out, strict=args.strict)
    rt_ok = (root / "payload" / "python-runtime" / "python.exe").is_file()
    print(f"BUILD OK -> {root}" + ("  [strict]" if args.strict else ""))
    print(f"  payload/app/             { (root/'payload'/'app').is_dir() }")
    print(f"  payload/python-runtime/  { rt_ok }")
    print(f"  installer/install.py     { (root/'installer'/'install.py').is_file() }")
    if args.strict and not rt_ok:
        # 双保险：严格模式绝不允许产出缺 runtime 的发布包
        _fail_build("严格模式产出缺少 python-runtime，发布包不可安装")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
