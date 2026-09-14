"""Wiki-USB Windows 嵌入式运行时安装器（PRD 4.1「Windows 嵌入式环境配置补齐」）。

做四件事：
  1. 下载官方 **python-3.11 embeddable** amd64 包并解压到 ``runtime/python-3.11-embed``
  2. 修补 ``python311._pth`` —— 解除 ``import site`` 注释并追加 ``./Lib/site-packages``
  3. 用内置解释器引导 pip，并把**核心依赖**装进 ``Lib/site-packages``（约 65MB）
  4. 可选：``--with-onnx`` 额外安装 onnxruntime（+140MB）并下载本地 ONNX 嵌入模型

用法：
    python setup_runtime_windows.py                # 精简安装（约 65MB，需联网一次）
    python setup_runtime_windows.py --with-onnx    # 同时启用本地 ONNX 嵌入引擎
    python setup_runtime_windows.py --check        # 仅体检现有运行时

⚠ 本脚本可被宿主 Python 运行，但**只向 U 盘目录内写入**，不触碰注册表与系统环境。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
RUNTIME = BASE / "runtime"
EMBED_DIR = RUNTIME / "python-3.11-embed"
EMBED_PY = EMBED_DIR / "python.exe"
PTH_FILE = EMBED_DIR / "python311._pth"
SITE_PACKAGES = EMBED_DIR / "Lib" / "site-packages"

PY_VERSION = "3.11.9"
EMBED_URL = (
    f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
)
GETPIP_URL = "https://bootstrap.pypa.io/get-pip.py"
MIRROR_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
ONNX_MODEL_URL = (
    "https://huggingface.co/BAAI/bge-small-zh-v1.5/resolve/main/onnx/model_quantized.onnx"
)

# 依赖清单的**唯一来源** —— 与 Linux/macOS 启动脚本、CI 共用同一份，
# 避免多份列表互相漂移（历史教训：曾出现 requirements.txt 有、本脚本没有的依赖）。
REQ_FILE: Path = BASE / "requirements.txt"

# 可选重型依赖：仅在 --with-onnx 时安装。
# onnxruntime + numpy 约 +140MB，且缺少模型文件时**不提供任何能力**，
# 因此默认不随发布包分发，以守住「精简优先」的体积目标。
# requirements.txt 中对应行是注释状态，这里是它的可执行形态。
ONNX_REQUIREMENTS = ["onnxruntime>=1.17"]


def load_requirements(path: Path | None = None) -> list[str]:
    """从 requirements.txt 解析核心依赖。

    跳过空行、整行注释与行尾注释；因此文件里**被注释掉的 `# onnxruntime>=1.17`
    不会被装上**——这正是「可选依赖」的表达方式。
    """
    target = path or REQ_FILE
    if not target.exists():
        raise SystemExit(f"[setup] 缺少依赖清单：{target}")
    deps: list[str] = []
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            deps.append(line)
    if not deps:
        raise SystemExit(f"[setup] {target} 中未解析到任何依赖，文件可能已损坏")
    return deps

PTH_CONTENT = """python311.zip
.
Lib/site-packages
import site
"""


def log(msg: str) -> None:
    print(f"[setup] {msg}", flush=True)


def _download(url: str, dest: Path, label: str = "") -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)

    def hook(block: int, size: int, total: int) -> None:
        if total > 0:
            pct = min(100.0, block * size * 100.0 / total)
            sys.stdout.write(f"\r[setup] 下载 {label or dest.name} … {pct:5.1f}%")
            sys.stdout.flush()

    log(f"GET {url}")
    try:
        urllib.request.urlretrieve(url, dest, reporthook=hook)
    except Exception as exc:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        raise SystemExit(
            f"\n[setup] 下载失败：{exc}\n"
            f"        请检查网络/代理，或手动下载后放到 {dest}"
        ) from exc
    sys.stdout.write("\n")
    return dest


def step1_download_embed() -> None:
    if EMBED_PY.exists():
        log(f"已存在嵌入式运行时，跳过下载：{EMBED_PY}")
        return
    zip_path = RUNTIME / "python-embed.zip"
    _download(EMBED_URL, zip_path, f"python-{PY_VERSION}-embed-amd64.zip")
    log("解压…")
    EMBED_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(EMBED_DIR)
    zip_path.unlink(missing_ok=True)
    if not EMBED_PY.exists():
        raise SystemExit(f"[setup] 解压后未找到 {EMBED_PY}")
    log(f"运行时就位：{EMBED_DIR}")


def step2_patch_pth() -> None:
    """解除 import site 屏蔽并追加相对依赖目录（PRD 明确要求）。"""
    if not EMBED_DIR.exists():
        raise SystemExit("[setup] 运行时目录不存在，请先执行下载步骤")
    existing = PTH_FILE.read_text(encoding="utf-8") if PTH_FILE.exists() else ""
    if "Lib/site-packages" in existing and "\nimport site" in "\n" + existing:
        log("python311._pth 已修补，跳过")
        return
    PTH_FILE.write_text(PTH_CONTENT, encoding="utf-8")
    log(f"已修补 {PTH_FILE.name}（启用 import site + 追加相对依赖目录）")
    log("  内容如下：")
    for line in PTH_CONTENT.strip().splitlines():
        log(f"    {line}")


def step3_install_deps(mirror: bool) -> None:
    SITE_PACKAGES.mkdir(parents=True, exist_ok=True)
    getpip = RUNTIME / "get-pip.py"
    if not (SITE_PACKAGES / "pip").exists():
        _download(GETPIP_URL, getpip, "get-pip.py")
        log("引导 pip…")
        subprocess.run(
            [str(EMBED_PY), str(getpip), "--no-warn-script-location"],
            check=True, cwd=str(EMBED_DIR),
        )
        getpip.unlink(missing_ok=True)

    deps = load_requirements()          # 单一来源：requirements.txt
    cmd = [str(EMBED_PY), "-m", "pip", "install", "--no-warn-script-location",
           "--disable-pip-version-check", *deps]
    if mirror:
        cmd += ["-i", MIRROR_INDEX]
    log(f"从 {REQ_FILE.name} 安装 {len(deps)} 项核心依赖：" + " ".join(deps))
    result = subprocess.run(cmd, cwd=str(EMBED_DIR))
    if result.returncode != 0:
        raise SystemExit("[setup] 依赖安装失败，请检查网络后重试")
    log("依赖安装完成")


def step4_onnx(mirror: bool) -> None:
    """可选：安装 onnxruntime 并下载本地 ONNX 嵌入模型。"""
    cmd = [str(EMBED_PY), "-m", "pip", "install", "--no-warn-script-location",
           "--disable-pip-version-check", *ONNX_REQUIREMENTS]
    if mirror:
        cmd += ["-i", MIRROR_INDEX]
    log("安装可选依赖（onnxruntime，约 +140MB）：" + " ".join(ONNX_REQUIREMENTS))
    if subprocess.run(cmd, cwd=str(EMBED_DIR)).returncode != 0:
        log("⚠ onnxruntime 安装失败，将保持降级嵌入源（不影响启动）")
        return

    models = RUNTIME / "models"
    models.mkdir(parents=True, exist_ok=True)
    target = models / "bge-small-zh-q4.onnx"
    if target.exists():
        log(f"ONNX 模型已存在：{target.name}（{target.stat().st_size/1e6:.1f} MB）")
        return
    try:
        _download(ONNX_MODEL_URL, target, "bge-small-zh ONNX 模型")
    except SystemExit as exc:
        log(f"模型下载失败（不影响启动，可稍后重试）：{exc}")


def check() -> int:
    ok = True
    print("=" * 66)
    print("  Wiki-USB 运行时体检")
    print("=" * 66)
    print(f"  安装根目录 : {BASE}")
    print(f"  嵌入式运行时: {'✅ 存在' if EMBED_PY.exists() else '❌ 缺失'}  {EMBED_PY}")
    if PTH_FILE.exists():
        txt = PTH_FILE.read_text(encoding="utf-8")
        site_ok = "import site" in txt and not txt.splitlines().count("#import site")
        sp_ok = "Lib/site-packages" in txt
        print(f"  _pth 补丁  : {'✅' if site_ok else '❌'} import site 已启用，"
              f"{'✅' if sp_ok else '❌'} Lib/site-packages 已追加")
        ok = ok and site_ok and sp_ok
    else:
        print("  _pth 补丁  : ❌ python311._pth 不存在")
        ok = False

    if EMBED_PY.exists():
        probe = (
            "import sqlite3, sys;"
            "print('  python     :', sys.version.split()[0]);"
            "print('  sqlite     :', sqlite3.sqlite_version);"
            "print('  fts5       :', any('FTS5' in r[0] for r in sqlite3.connect(':memory:').execute('pragma compile_options')));"
        )
        print(subprocess.run([str(EMBED_PY), "-c", probe], capture_output=True,
                             text=True).stdout, end="")
        for mod in ("sqlite_vec", "trafilatura", "lxml", "yaml"):
            r = subprocess.run([str(EMBED_PY), "-c", f"import {mod}"],
                               capture_output=True, text=True)
            print(f"  {mod:<11}: {'✅' if r.returncode == 0 else '❌ 未安装'}")
            ok = ok and r.returncode == 0
        onnx = subprocess.run([str(EMBED_PY), "-c", "import onnxruntime"],
                              capture_output=True, text=True)
        print(f"  onnxruntime: {'✅ 已安装' if onnx.returncode == 0 else '⚠ 未安装（可选，加 --with-onnx）'}")

    model = RUNTIME / "models" / "bge-small-zh-q4.onnx"
    print(f"  ONNX 模型  : {'✅' if model.exists() else '⚠ 未下载（将降级嵌入源）'}")
    web = BASE / "app" / "web"
    print(f"  离线前端   : {'✅' if (web / 'index.html').exists() else '❌'} index.html"
          f" ，{'✅' if (web / 'vendor' / 'd3.v7.min.js').exists() else '❌'} vendor/d3.v7.min.js")
    print("=" * 66)
    print("  结论：" + ("✅ 运行时可用于发布" if ok else "❌ 存在缺失项，请执行完整安装"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Wiki-USB Windows 嵌入式运行时安装器")
    ap.add_argument("--check", action="store_true", help="仅体检，不做任何修改")
    ap.add_argument("--with-onnx", action="store_true", help="额外下载本地 ONNX 嵌入模型")
    ap.add_argument("--no-mirror", action="store_true", help="不使用清华 PyPI 镜像")
    ap.add_argument("--force", action="store_true", help="重装依赖")
    args = ap.parse_args()

    if args.check:
        return check()

    if os.name != "nt":
        print("[setup] 提示：本脚本用于生成 Windows 发布版。")
        print("        macOS / Linux 无需嵌入式包 —— 直接运行 启动-macOS.command / 启动-Linux.sh，")
        print("        启动脚本会自动检测宿主 Python 3.10+ 并提示安装缺失依赖。")

    if sys.version_info[:2] < (3, 9):
        raise SystemExit("[setup] 宿主 Python 版本过低，请使用 3.9+ 运行本脚本")

    log(f"安装根目录：{BASE}")
    step1_download_embed()
    step2_patch_pth()
    if args.force and SITE_PACKAGES.exists():
        shutil.rmtree(SITE_PACKAGES, ignore_errors=True)
        log("已清空旧的 site-packages（--force）")
    step3_install_deps(mirror=not args.no_mirror)
    if args.with_onnx:
        step4_onnx(mirror=not args.no_mirror)
    else:
        log("已跳过 onnxruntime 与 ONNX 模型（需要时加 --with-onnx）")

    print()
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
