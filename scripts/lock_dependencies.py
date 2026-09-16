#!/usr/bin/env python3
"""Wiki-USB 依赖锁生成器（可复现 release dependency lock）。

职责
----
把 ``requirements.txt``（**直接依赖 + 兼容区间**）解析成完整闭包、用 ``==``
精确锁定，写入 ``requirements-release.lock``。未来重新构建 USB-WIKI 时，只要
安装这份 lock，就能得到**同一套 Python 依赖**——不依赖某个时间点 PyPI 的
``>=`` 解析结果。

设计要点
--------
* 在 **产品真实运行时的 Python（Windows Embedded 3.11.9）** 上做解析，
  这样锁自动落到「3.11 地板」——解析出的版本天然兼容 3.11，也就兼容 3.13。
  绝不在随意一台 3.13 机器上 ``pip freeze``（那只是某次环境快照）。
* 用 ``pip-tools`` 的 ``pip-compile`` 做解析，输出带 environment marker 的
  ``==`` 锁定（如 ``colorama ; sys_platform == "win32"``），跨 Windows/Linux、
  3.11/3.13 都能正确安装。
* 运行时不注入 Ollama / GGUF / ONNX 模型版本——那些属于将来的
  Offline Distribution Manifest，不在 Python 依赖锁范围内。
* 过滤掉构建期工具（pip / setuptools / wheel），它们不是产品运行依赖。

用法
----
    python scripts/lock_dependencies.py                 # 常规生成
    python scripts/lock_dependencies.py --python X.Y   # 指定解析用解释器
    python scripts/lock_dependencies.py --dev           # 仅校验，不写盘

生成的 lock 会被五路 CI（Win Portable / Win 3.11 / Win 3.13 / Ubuntu 3.11 /
Ubuntu 3.13）全部安装验证——任一平台装不上，CI 直接红。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
REQ_FILE = BASE / "requirements.txt"
LOCK_FILE = BASE / "requirements-release.lock"

# pip-tools 本身固定版本：保证「重新生成」的解析器一致，锁才真正可复现。
PIP_TOOLS_VERSION = "7.6.1"
# 国内机器走清华镜像更快；lock 内容（版本号）与镜像无关，不影响可复现性。
DEFAULT_INDEX = os.environ.get(
    "WIKIUSB_PIP_INDEX",
    "https://pypi.tuna.tsinghua.edu.cn/simple",
)
GETPIP_URL = "https://bootstrap.pypa.io/get-pip.py"

# 这些不是产品运行依赖，绝不允许进发布锁。
FORBIDDEN = {"pip", "setuptools", "wheel"}


def log(msg: str) -> None:
    print(f"[lock] {msg}", flush=True)


def _canonical(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def find_base_python() -> Path:
    """优先用产品运行时（嵌入式 3.11），其次当前解释器。"""
    embedded = BASE / "runtime" / "python-3.11-embed" / "python.exe"
    if embedded.exists():
        return embedded
    log("未找到嵌入式运行时，退回当前解释器（锁可能不落在 3.11 地板）")
    return Path(sys.executable)


def _site_packages(base: Path) -> Path:
    out = subprocess.run([str(base), "-c",
                          "import site,sys;print(site.getsitepackages()[0])"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return Path(out)


def _top_levels(sp: Path) -> set[str]:
    return {p.name for p in sp.iterdir() if p.name != "__pycache__"}


def ensure_pip(base: Path, tmp: Path) -> None:
    """确保 base 解释器能调用 pip（嵌入式运行时一般已自带）。"""
    r = subprocess.run([str(base), "-m", "pip", "--version"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        log(f"基础解释器已带 pip：{r.stdout.strip()}")
        return
    log("基础解释器无 pip，下载 get-pip.py 引导…")
    getpip = tmp / "get-pip.py"
    urllib.request.urlretrieve(GETPIP_URL, getpip)
    subprocess.run([str(base), str(getpip), "--no-warn-script-location"],
                   check=True)


def install_pip_tools(base: Path, tmp: Path) -> set[str]:
    """把 pip-tools 装进 base 的 site-packages（_pth 能找到），返回安装前快照。"""
    sp = _site_packages(base)
    snapshot = _top_levels(sp)
    subprocess.run(
        [str(base), "-m", "pip", "install", "--no-warn-script-location",
         "--disable-pip-version-check", f"pip-tools=={PIP_TOOLS_VERSION}",
         "-i", DEFAULT_INDEX],
        check=True,
    )
    return snapshot


def cleanup_pip_tools(base: Path, snapshot: set[str]) -> None:
    """按 site-packages 快照差分卸载 pip-tools 及其依赖，还原干净运行时。"""
    sp = _site_packages(base)
    added = _top_levels(sp) - snapshot
    if added:
        log(f"清理生成期依赖（还原嵌入式运行时）：{', '.join(sorted(added))}")
        import shutil
        for name in added:
            shutil.rmtree(sp / name, ignore_errors=True)
    # console script 在 Scripts/ 下，不在 site-packages 快照内，单独清掉
    for name in ("pip-compile.exe", "pip-sync.exe"):
        cand = base.parent / "Scripts" / name
        if cand.exists():
            cand.unlink(missing_ok=True)


def compile_lock(base: Path, out: Path) -> None:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    # 注意：嵌入式解释器下 `python -m piptools.scripts.compile` **不会真正执行 cli**
    # （无输出、rc=0），console script 又会在 cleanup 时被删。
    # 唯一在所有情况下可靠的方式是直接 import cli 并显式喂 argv（已验证可用）。
    # --strip-extras: 去掉 requirements.txt 里若有的 [extra] 标注
    # 解析闭包 + 精确 == 锁定 + 保留 environment marker
    script = (
        "import sys; from piptools.scripts.compile import cli;"
        f" sys.argv=['pip-compile', {str(REQ_FILE)!r}, '--output-file', "
        f"{str(out)!r}, '--strip-extras', '--quiet']; cli()"
    )
    subprocess.run([str(base), "-c", script], check=True, env=env)


def post_process(in_path: Path) -> tuple[int, list[str]]:
    """过滤构建期工具、把干净闭包写回 LOCK_FILE，返回（保留行数, 被剔除的包）。

    in_path 是本次 pip-compile 的**全新输出**（不被旧 LOCK_FILE 污染），
    因此不会出现「旧自定义头被不断 prepend」的重复头问题。
    """
    raw = in_path.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    dropped: list[str] = []
    for line in raw:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            kept.append(line)
            continue
        # 取包名（忽略 marker / 版本 / 注释）
        name = stripped.split("==")[0].split("[")[0].split(";")[0].strip()
        if _canonical(name) in FORBIDDEN:
            dropped.append(name)
            continue
        kept.append(line)
    LOCK_FILE.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return len(kept), dropped


def write_header(dropped: list[str], base: Path) -> None:
    pyver = subprocess.run([str(base), "--version"], capture_output=True,
                           text=True).stdout.strip()
    header = [
        "# =============================================================================",
        "#  Wiki-USB Release 依赖锁 —— 完整依赖闭包、== 精确锁定、含 environment marker",
        "# =============================================================================",
        "#  本文件由 scripts/lock_dependencies.py 生成，不要手工编辑。",
        "#  重新生成：python scripts/lock_dependencies.py",
        "#",
        f"#  解析解释器 : {pyver}",
        f"#  生成时间   : {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"#  pip-tools  : {PIP_TOOLS_VERSION}",
        f"#  源清单     : requirements.txt（直接依赖 + 兼容区间）",
        "#",
        "#  安装（发布构建 / CI）：  pip install -r requirements-release.lock",
        "#  开发安装（浮动区间）：  pip install -r requirements.txt",
        "#  注意：本锁不含 pip / setuptools / wheel（非产品运行依赖）。",
        "#  SHA256 由运行时（diagnostics）对最终文件计算，不固定在头里以免与正文漂移。",
        "# =============================================================================",
        "",
    ]
    body = LOCK_FILE.read_text(encoding="utf-8")
    LOCK_FILE.write_text("\n".join(header) + body, encoding="utf-8")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def parse_direct_specs() -> dict[str, str]:
    """从 requirements.txt 解析直接依赖 → {canonical: raw_spec}。"""
    specs: dict[str, str] = {}
    if not REQ_FILE.exists():
        return specs
    for raw in REQ_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name = line.split("==")[0].split(">=")[0].split("~=")[0].split("[")[0].strip()
        specs[_canonical(name)] = line
    return specs


def verify_lock_matches_requirements() -> list[str]:
    """校验：requirements.txt 的每个直接依赖都必须在 lock 中 == 锁定。"""
    errors: list[str] = []
    specs = parse_direct_specs()
    lock_text = LOCK_FILE.read_text(encoding="utf-8")
    # 建立 lock 中的包名集合（含 marker 行）
    lock_pkgs: dict[str, str] = {}
    for line in lock_text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        name = _canonical(s.split("==")[0].split("[")[0].split(";")[0].strip())
        lock_pkgs[name] = s
    for canon, raw in specs.items():
        if canon not in lock_pkgs:
            errors.append(f"直接依赖 {raw} 未出现在 lock 中")
            continue
        locked = lock_pkgs[canon]
        # 必须精确 ==（允许之后有 marker / 注释）
        if "==" not in locked.split(";")[0]:
            errors.append(f"直接依赖 {raw} 在 lock 中不是 == 精确锁定：{locked}")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 requirements-release.lock")
    ap.add_argument("--python", help="解析用基础解释器（默认：嵌入式 3.11 或当前）")
    ap.add_argument("--dev", action="store_true", help="仅校验一致性，不重新生成")
    args = ap.parse_args()

    if args.dev:
        log("开发模式：仅校验现有 lock 与 requirements.txt 一致…")
        errs = verify_lock_matches_requirements()
        if errs:
            for e in errs:
                log(f"  ✗ {e}")
            return 1
        log(f"  ✓ 一致（lock SHA256={sha256_of(LOCK_FILE)[:16]}…）")
        return 0

    base = Path(args.python) if args.python else find_base_python()
    log(f"解析解释器：{base}")
    log(f"源清单：{REQ_FILE}")
    log(f"输出锁：{LOCK_FILE}")

    tmp = Path(tempfile.mkdtemp(prefix="wikiusb-lock-"))
    try:
        ensure_pip(base, tmp)
        snapshot = install_pip_tools(base, tmp)
        log(f"pip-tools=={PIP_TOOLS_VERSION} 就绪")
        raw = tmp / "raw.lock"
        compile_lock(base, raw)
        kept, dropped = post_process(raw)
        write_header(dropped, base)
    finally:
        cleanup_pip_tools(base, snapshot)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    sha = sha256_of(LOCK_FILE)
    errs = verify_lock_matches_requirements()
    if errs:
        for e in errs:
            log(f"  ✗ 一致性校验失败：{e}")
        return 1

    # 报告：直接依赖最终锁定版本
    specs = parse_direct_specs()
    lock_lines = {_canonical(l.split("==")[0].split("[")[0].split(";")[0].strip()): l
                  for l in LOCK_FILE.read_text(encoding="utf-8").splitlines()
                  if l.strip() and not l.startswith("#")}
    log("直接依赖最终锁定：")
    for canon in specs:
        log(f"    {lock_lines.get(canon, '??')}")
    log(f"保留行数：{kept}（剔除非运行依赖：{', '.join(dropped) or '无'}）")
    log(f"LOCK SHA256 = {sha}")
    log("✓ 生成完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
