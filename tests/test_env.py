"""测试隔离环境（Test Harness Safety Fuse）—— 破坏性测试的唯一入口。

背景（真实事故，2026-09-21）：`tests/test_suite.py::reset_workspace()` 在**未隔离的
开发 Library**（`F:\\项目\\USB-WIKI\\data`）上运行，删除了用户真实 `notes/*.md`、
`snapshots/*`、`cache.db`。三者在 .gitignore 内，git 无法恢复。

教训：「记得设置 WIKIUSB_LIBRARY」是**人的纪律**，不是安全机制。本模块把它变成
**代码级保险丝**：

1. 测试自己创建临时 Library（`%TEMP%/usb-wiki-test-<random>/`），不依赖操作者；
2. 任何 unlink / rmtree 之前必须过 :func:`assert_test_library_safe`；
3. 三重条件（缺一即拒）：`WIKIUSB_TEST_MODE=1` + sentinel `.usbwiki-test-library`
   + 路径位于系统 TEMP 且**不等于**真实/默认 Library；
4. repo/data 与 Documents/USB-WIKI-Data **硬拒绝**（即使 TEST_MODE=1 也不行）。

本模块**只用标准库**，必须在导入 `app.core.paths` 之前调用
:func:`activate_test_library`（paths 在 import 期就把 DATA_DIR 定下来了）。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 拒绝清单：这些路径**永远**不允许跑破坏性测试（最后一道保险）
REPO_DATA = (ROOT / "data").resolve()
SYSTEM_TEMP = Path(tempfile.gettempdir()).resolve()
DEFAULT_LIBRARIES = (
    REPO_DATA,
    (Path.home() / "Documents" / "USB-WIKI-Data").resolve(),
)

SENTINEL_NAME = ".usbwiki-test-library"
REFUSE_MSG = "REFUSING_DESTRUCTIVE_TEST_ON_NON_TEST_LIBRARY"

_STATE: dict = {"library": None}


# --------------------------------------------------------------------------- 断言
def assert_test_library_safe(library: str | os.PathLike | None = None) -> Path:
    """破坏性操作前的硬闸门。不安全 → raise RuntimeError（绝不返回 False 了事）。"""
    lib = Path(library or os.environ.get("WIKIUSB_LIBRARY") or ".").expanduser()
    try:
        lib = lib.resolve()
    except OSError:
        raise RuntimeError(REFUSE_MSG + f"（路径无法解析：{lib}）") from None

    # ④ 最后一道保险：真实仓库 data / 默认生产 Library 一律拒绝，即使 TEST_MODE=1
    if lib == REPO_DATA or lib in DEFAULT_LIBRARIES:
        raise RuntimeError(
            f"{REFUSE_MSG}（{lib} 是真实/默认 Library，测试永不触碰）")

    checks = {
        "WIKIUSB_TEST_MODE=1": os.environ.get("WIKIUSB_TEST_MODE") == "1",
        f"sentinel {SENTINEL_NAME}": (lib / SENTINEL_NAME).exists(),
        "位于系统 TEMP 下": _is_under(lib, SYSTEM_TEMP) and lib != SYSTEM_TEMP,
    }
    missing = [k for k, ok in checks.items() if not ok]
    if missing:
        raise RuntimeError(f"{REFUSE_MSG}（不满足：{', '.join(missing)}；library={lib}）")
    return lib


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- 生命周期
def create_isolated_library(prefix: str = "usb-wiki-test-") -> Path:
    """创建临时测试 Library（含 sentinel），返回其路径。"""
    lib = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    (lib / SENTINEL_NAME).write_text(
        "Wiki-USB 隔离测试 Library —— 破坏性测试只允许在这里执行。\n",
        encoding="utf-8")
    for sub in ("notes", "snapshots", "originals", "assets"):
        (lib / sub).mkdir(parents=True, exist_ok=True)
    _STATE["library"] = lib
    return lib


def activate_test_library(prefix: str = "usb-wiki-test-") -> Path:
    """**必须在导入 `app.core.paths` 之前**调用：建临时库并写入环境变量。

    返回临时 Library 路径。若发现当前配置指向真实/默认 Library，直接退出进程
    （宁可测试跑不起来，也不能让破坏性测试碰到用户数据）。
    """
    lib = _STATE.get("library") or create_isolated_library(prefix)

    # 先看当前环境是不是已经指着真实 Library（例如操作者手动设了 WIKIUSB_LIBRARY）
    presets = (os.environ.get("WIKIUSB_LIBRARY") or "").strip()
    if presets:
        try:
            p = Path(presets).expanduser().resolve()
        except OSError:
            p = None
        if p in DEFAULT_LIBRARIES:
            print(f"\n  ⛔ 拒绝：WIKIUSB_LIBRARY 指向真实/默认 Library（{p}）。"
                  "\n     测试会覆盖为隔离临时目录；请勿在测试中操作真实资料库。\n", file=sys.stderr)

    os.environ["WIKIUSB_LIBRARY"] = str(lib)
    os.environ["WIKIUSB_TEST_MODE"] = "1"
    assert_test_library_safe(lib)          # 自检：保险丝必须真的生效
    return lib


def print_banner(lib: str | os.PathLike | None = None) -> None:
    """启动横幅：打印隔离目录 + 保险丝状态。"""
    target = Path(lib or os.environ.get("WIKIUSB_LIBRARY") or "?").resolve()
    danger = target in DEFAULT_LIBRARIES
    print("─" * 68)
    print(f"  TEST LIBRARY: {target}")
    print(f"  DESTRUCTIVE TEST GUARD: {'⛔ INACTIVE（真实 Library！）' if danger else 'ACTIVE'}")
    print(f"  WIKIUSB_TEST_MODE: {os.environ.get('WIKIUSB_TEST_MODE', '(unset)')}")
    print("─" * 68)
    if danger:
        raise SystemExit(2)


def cleanup_test_library(lib: str | os.PathLike | None = None) -> None:
    """删除临时测试 Library（同样受保险丝保护）。"""
    target = assert_test_library_safe(lib)
    shutil.rmtree(target, ignore_errors=True)
    if _STATE.get("library") == target:
        _STATE["library"] = None


def is_test_library(library: str | os.PathLike | None = None) -> bool:
    """只读判断（不抛异常），用于打日志/断言细节。"""
    try:
        assert_test_library_safe(library)
        return True
    except RuntimeError:
        return False
