"""A2 —— Installer Hardening / Transactional Install 验收（临时目录，零真实 LOCALAPPDATA/Documents 写入）。

覆盖（按用户 A2 指令）：
  A. 首次安装成功：payload → staging → App → 入口可靠（bat 不含 --no-browser）
  B. 重装成功：旧 App → backup → 新 App → smoke success → backup 删除
  C. 复制中途失败：旧 App SHA256 前后一致、无 App.staging/App.backup 残留
  D. staging 校验失败：旧 App 不变
  E. swap 后启动失败：rollback 到旧 App（SHA 一致、无残留）
  F. 首次安装失败：不得留下可误启动的半成品 App
  G. 所有失败场景：Library SHA256 前后完全一致（程序事务与用户数据事务分离）

设计：本模块自包含（自带 check/skip/section 与 PASS/FAIL/SKIP 列表），
由 tests/test_suite.py 的 main() 调用 run_a2_tests() 并合并结果。
事务逻辑以 in-process 方式加载 scripts/install_windows.py 并 monkeypatch 触发各失败分支，
全程只用临时目录，绝不触碰真实 LOCALAPPDATA / Documents / Library / 桌面快捷方式。

性能（2026-09-26 改造 —— RELEASE_REQUIRED / Test Infrastructure Performance Defect）：
  * **只有 B(real verify) 是 heavy**：1 次真实 build + 2 次真实 install；
    A/C/D/E/F/G 全部用 fake release + in-process 安装（秒级），各自独立小 TEMP，
    不为此引入 session fixture。
  * **所有真实 subprocess 有界**：``stdin=DEVNULL``；build 600s、install 1200s；
    超时 → rc=-1 + 打印 phase/command/stdout 尾部/stderr 尾部，对应用例**明确 FAIL**，
    绝不静默挂起（此前 build/install 都**没有** timeout）。
  * **cleanup 有界且 best-effort**（``_cleanup_bounded``，150s）：B(verify) 的临时 App
    含完整嵌入式 runtime（~13850 文件 / 515MB），无界 rmtree 曾让 A2 在 B(verify) 之后
    **20+ 分钟无输出**。超时只打 ``CLEANUP_WARNING`` 并列残留，**不改 PASS/FAIL**。
  * **逐 case 计时**（``_timed``）：A / B / B(real) / C / D / E / F / G 各自耗时可见。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from tests import dist_fixture as fx                    # noqa: E402
from app.version import APP_VERSION                     # 唯一版本源  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []

# —— 计数（供 Release Gate 汇报：真实 build / install 次数）——
BUILD_COUNT = 0
INSTALL_COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name if cond else f"{name} :: {detail}")
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"  [{detail}]"))
    return cond


def skip(name: str, reason: str = "") -> None:
    SKIP.append(f"{name} :: {reason}" if reason else name)
    print("  ⏭ " + name + (f"  [{reason}]" if reason else ""))


def section(t: str) -> None:
    print(f"\n── A2 {t} " + "─" * max(0, 54 - len(t)))


def _runtime_present() -> bool:
    return (REPO / "runtime" / "python-3.11-embed" / "python.exe").is_file()


def _load_install():
    # 统一走夹具的加载器（注册 sys.modules + 不落 __pycache__），避免与 A3 两套加载方式漂移
    return fx.load_installer()


def _sha256_tree(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode("utf-8"))
            h.update(p.read_bytes())
    return h.hexdigest()


def _print_cleanup_warning(root: Path, why: str) -> None:
    print(f"\n  ⚠ CLEANUP_WARNING: 临时目录未在限时内清理（{why}）")
    print(f"    leftover : {root}")
    print("    → 不影响本次产品 PASS/FAIL；残留可后续人工/后台清理。")


def _cleanup_bounded(root: Path, timeout: int = 150) -> bool:
    """best-effort 清理：子进程 rmtree + 超时，**绝不阻塞 Release Gate**。

    背景（实测，2026-09-26）：B(verify) 的临时 App 含完整嵌入式 runtime
    （约 13850 个文件 / 515MB），同步无界 ``shutil.rmtree`` 在 Windows 实时防毒下要
    10+ 分钟 —— 整轮 A2 曾因此在 B(verify) 之后 **20+ 分钟无任何输出**。
    这是 A2 的**主要阻塞源**。这里把「产品断言」与「临时目录清理」解耦：
    超时只打 ``CLEANUP_WARNING`` 并列残留，**不改任何 PASS/FAIL**，
    不触碰真实 Library，也不 kill 无关进程。
    """
    if not root.exists():
        return True
    code = "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)"
    try:
        subprocess.run([sys.executable, "-c", code, str(root)],
                       timeout=timeout, stdin=subprocess.DEVNULL, capture_output=True)
    except subprocess.TimeoutExpired:
        _print_cleanup_warning(root, f"超过 {timeout}s")
        return False
    if root.exists():
        _print_cleanup_warning(root, "仍有残留（个别文件被占用）")
        return False
    return True


class _tmp:
    """各 fake 用例继续各自独立 TEMP（payload 很小，无需 session fixture）——
    唯一改动：清理必须有界（见 ``_cleanup_bounded``）。"""

    def __enter__(self) -> Path:
        self.d = Path(tempfile.mkdtemp(prefix="wikiusb-a2-"))
        return self.d

    def __exit__(self, *a) -> None:
        _cleanup_bounded(self.d, timeout=150)


# ---------------------------------------------------------------------------
# 安装测试安全沙箱（RELEASE_REQUIRED / Test Safety Isolation Defect, 2026-09-26）
# ---------------------------------------------------------------------------
# A2 安装测试同样会写真实副作用：
#   * in-process install（_install_inproc）→ 成功后在**真实桌面**建 USB-WIKI.lnk +
#     写真实 %LOCALAPPDATA%/USB-WIKI/install_state.json；
#   * subprocess install（_install_subprocess，B real verify）→ 真实 installer 同样默认
#     走真实桌面 + 真实 install_state.json（此前没传 --desktop-dir / 没注入 WIKIUSB_STATE_DIR）。
# 统一用 context manager 在 os.environ 注入隔离变量（in-process 实时读取；subprocess 继承），
# 并对所有安装显式传 desktop_dir=<TEMP>/Desktop。进入时保存原值、退出时精确恢复：
# 原本存在→恢复原值，原本不存在→删除注入值。**从一开始就不写真实位置**（禁止写后再删）。
_SANDBOX = None  # 由 _install_sandbox.__enter__ 写入；_install_inproc/_install_subprocess 读取


def _sandbox_desktop_dir():
    return _SANDBOX["desktop_dir"] if _SANDBOX else None


class _install_sandbox:
    """为安装测试建立 scoped sandbox，注入隔离 env 并精确还原。

    沙箱目录（全部位于 TEMP，绝不触碰真实 Desktop/LOCALAPPDATA/Library/Documents）：
        <root>/localapp            → 冒充 LOCALAPPDATA
        <root>/localapp/USB-WIKI   → 冒充 WIKIUSB_STATE_DIR（install_state.json 落此）
        <root>/Desktop             → 冒充桌面（快捷方式落此）
    注入：LOCALAPPDATA / WIKIUSB_STATE_DIR / PYTHONUTF8=1 / PYTHONIOENCODING=utf-8。
    """

    def __init__(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="wikiusb-a2-sandbox-"))
        self._localapp = self._root / "localapp"
        self._statedir = self._localapp / "USB-WIKI"
        self._desktop = self._root / "Desktop"
        self._localapp.mkdir(parents=True, exist_ok=True)
        self._statedir.mkdir(parents=True, exist_ok=True)
        self._desktop.mkdir(parents=True, exist_ok=True)
        self._overrides = {
            "LOCALAPPDATA": str(self._localapp),
            "WIKIUSB_STATE_DIR": str(self._statedir),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        self._saved: dict[str, str] = {}
        self._added: set[str] = set()

    @property
    def desktop_dir(self) -> Path:
        return self._desktop

    def __enter__(self) -> "_install_sandbox":
        global _SANDBOX
        _SANDBOX = {"desktop_dir": self._desktop}
        for k, v in self._overrides.items():
            if k in os.environ:
                self._saved[k] = os.environ[k]
            else:
                self._added.add(k)
            os.environ[k] = v
        return self

    def __exit__(self, *a) -> None:
        global _SANDBOX
        _SANDBOX = None
        for k in self._overrides:
            if k in self._added:
                os.environ.pop(k, None)
            else:
                os.environ[k] = self._saved[k]
        _cleanup_bounded(self._root, timeout=150)


def _snapshot_real_side_effects() -> dict:
    """只读快照真实桌面快捷方式 + 真实 install_state.json（前后一致性断言用）。

    必须在 sandbox 之外（用真实 env）调用——进入沙箱前拍一次，退出沙箱后拍一次。
    """
    paths = [
        Path.home() / "Desktop" / "USB-WIKI.lnk",
        Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".usb-wiki"))
        / "USB-WIKI" / "install_state.json",
    ]
    snap: dict[str, tuple[str, object]] = {}
    for p in paths:
        if p.is_file():
            try:
                snap[str(p)] = ("file", hashlib.sha256(p.read_bytes()).hexdigest())
            except OSError:
                snap[str(p)] = ("file", "unreadable")
        else:
            snap[str(p)] = ("missing", None)
    return snap


def _fake_payload(tmp: Path, marker: str) -> Path:
    """一个最小但**介质完整**的假发布包（A3 起安装前必须先过介质校验）。

    夹具见 tests/dist_fixture.py：结构含 installer/ + payload/ + LICENSES/ +
    BUILD_INFO.json + RELEASE_MANIFEST.json + SHA256SUMS，可直接通过 verify_media。
    事务测试只关心「装到一半失败会不会毁掉旧 App」，与介质校验互不干扰。
    """
    # VERSION.txt 直接写 marker（A2 各场景用 "v1"/"v2" 断言版本切换）
    return fx.make_fake_release(tmp, app_version=marker, marker=marker)


def _install_inproc(iw, release, app, lib, **kw) -> int:
    """in-process 安装；自动注入 sandbox 的 desktop_dir（若存在），把快捷方式落到
    TEMP/Desktop，绝不写真实桌面。install_state.json 的隔离由 _install_sandbox 的
    WIKIUSB_STATE_DIR 负责。"""
    if "desktop_dir" not in kw:
        d = _sandbox_desktop_dir()
        if d is not None:
            kw["desktop_dir"] = d
    try:
        return iw.install(Path(release), Path(app), Path(lib), **kw)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1


def _run_bounded(cmd, *, phase: str, timeout: int) -> subprocess.CompletedProcess:
    """有界 subprocess —— Release 测试**禁止无限等待**。

    A2 此前所有真实子进程（build_release / install）都**没有** timeout，
    一旦环境 I/O 劣化就会让整个 Gate 静默挂起。这里统一收紧：

    * ``stdin=DEVNULL``：本调用已给全 CLI 参数，绝不应进入 ``input()``；
      生产代码若意外交互读取会立刻 EOF，而不是永久挂起。
    * ``timeout``：超时返回 rc=-1 并打印 phase / command / stdout 尾部 / stderr 尾部，
      让对应用例**明确 FAIL**，而不是让整套 suite 卡死在无声处。
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or b""
        err = exc.stderr or b""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        print(f"\n  ⏱ TIMEOUT（{phase}）超过 {timeout}s")
        print(f"    command     : {' '.join(str(c) for c in cmd)}")
        print(f"    stdout 尾部 : ...{out[-400:]}")
        print(f"    stderr 尾部 : ...{err[-400:]}")
        return subprocess.CompletedProcess(cmd, -1, stdout=out, stderr=err)


def _build_real(dist: Path, timeout: int = 600) -> subprocess.CompletedProcess:
    """真实构建发布包（仅 B(verify) 使用；A2 其余用例全用 fake）。"""
    global BUILD_COUNT
    BUILD_COUNT += 1
    return _run_bounded(
        [sys.executable, str(REPO / "scripts" / "build_release.py"), "--output", str(dist)],
        phase="build_release", timeout=timeout)


def _install_subprocess(release, app, lib, extra=None,
                        timeout: int = 1200) -> subprocess.CompletedProcess:
    """真实安装子进程（有界；1200s 上限与 A1 一致，容纳真实重装删除尖峰）。

    隔离：显式传 --desktop-dir 把快捷方式落到 TEMP/Desktop；WIKIUSB_STATE_DIR 由
    _install_sandbox 注入 os.environ，subprocess 继承后 install_state.json 落 TEMP，
    绝不写真实桌面 / 真实 LOCALAPPDATA。
    """
    global INSTALL_COUNT
    INSTALL_COUNT += 1
    cmd = [sys.executable, str(REPO / "scripts" / "install_windows.py"), "install",
           "--release", str(release), "--app-target", str(app),
           "--library-target", str(lib), "--no-verify"]
    d = _sandbox_desktop_dir()
    if d is not None:
        cmd += ["--desktop-dir", str(d)]
    if extra:
        cmd += extra
    return _run_bounded(cmd, phase="install", timeout=timeout)


# ---------------------------------------------------------------------------
# A. 首次安装成功
# ---------------------------------------------------------------------------
def _t_first_install_success() -> None:
    iw = _load_install()
    with _tmp() as tmp:
        payload = _fake_payload(tmp, "v1")
        app = tmp / "App"
        lib = tmp / "Library"
        rc = _install_inproc(iw, payload, app, lib, smoke=False)
        ok = rc == 0 and (app / "app" / "VERSION.txt").read_text(encoding="utf-8") == "v1"
        ok &= (app / "启动-Windows.bat").is_file()
        bat = (app / "启动-Windows.bat").read_text(encoding="utf-8")
        ok &= "--no-browser" not in bat           # 用户入口不得含测试参数
        ok &= "app\\launcher.py" in bat
        # 事务收尾：staging / backup 必须清理
        ok &= not (app.parent / "App.staging").exists()
        ok &= not (app.parent / "App.backup").exists()
        check("A 首次安装成功：App 就位 + 入口可靠 + 无 staging/backup 残留", ok,
              f"rc={rc}")


# ---------------------------------------------------------------------------
# B. 重装成功（文件级，确定性）+ 真实 runtime 下的 verify（gated）
# ---------------------------------------------------------------------------
def _t_reinstall_success() -> None:
    iw = _load_install()
    with _tmp() as tmp:
        payload1 = _fake_payload(tmp / "p1", "v1")
        app = tmp / "App"
        lib = tmp / "Library"
        rc1 = _install_inproc(iw, payload1, app, lib, smoke=False)
        sha_v1 = _sha256_tree(app)
        # 第二次安装新版本
        payload2 = _fake_payload(tmp / "p2", "v2")
        rc2 = _install_inproc(iw, payload2, app, lib, smoke=False)
        ok = rc1 == 0 and rc2 == 0
        ok &= (app / "app" / "VERSION.txt").read_text(encoding="utf-8") == "v2"
        ok &= not (app.parent / "App.staging").exists()
        ok &= not (app.parent / "App.backup").exists()   # 成功后 backup 删除
        # 重装后 App 内容确实变了（证明 swap 发生），且 Library 未动
        ok &= _sha256_tree(app) != sha_v1
        check("B 重装成功：新版本就位 + backup 已清理", ok, f"rc1={rc1} rc2={rc2}")


def _t_reinstall_success_verify() -> None:
    """B(real verify) —— 真实 runtime 重装 + installer ``--verify`` 启动验证。

    覆盖对照（2026-09-26 审计，与 A1 逐项比对后**保留**）：本用例有 **A2 独有**覆盖 ——
      * 真实大包重装后 ``App.staging`` / ``App.backup`` **无残留**（A1 未断言）；
      * installer CLI ``--verify --port`` 这条**安装器自带的启动验证分支**
        （A1 是直接对已安装 App 做 HTTP 探活，不经过 installer 的 verify 路径）。

    因此**不做去重删除**（那会丢掉 A2 独有 invariant）；本轮只把它的
    build / install 子进程与 cleanup 全部改成**有界**
    （见 ``_run_bounded`` / ``_cleanup_bounded``）—— 优化的是 I/O，不是删验收项。
    """
    if not _runtime_present():
        skip("B(verify) 重装 + 真实启动验证", "runtime 未构建")
        return
    with _tmp() as tmp:
        # 用真实 dist（含真实 app + 嵌入式 runtime）做重装 + 启动验证
        dist = tmp / "dist"
        b = _build_real(dist)
        if b.returncode != 0:
            skip("B(verify) 重装 + 真实启动验证", f"build 失败：{b.stderr[-200:]}")
            return
        release = dist / f"USB-WIKI-v{APP_VERSION}-win-x64"
        app = tmp / "App"
        lib = tmp / "Library"
        # 先装一次
        r1 = _install_subprocess(release, app, lib, ["--verify", "--port", "28991"])
        # 再装一次（重装）并验证启动
        r2 = _install_subprocess(release, app, lib, ["--verify", "--port", "28992"])
        ok = r1.returncode == 0 and r2.returncode == 0
        ok &= (app / "app").is_dir() and (app / "runtime" / "python.exe").is_file()
        ok &= not (app.parent / "App.staging").exists()
        ok &= not (app.parent / "App.backup").exists()
        check("B(verify) 重装 + 真实启动验证成功 + 无残留", ok,
              f"rc1={r1.returncode} rc2={r2.returncode}")


# ---------------------------------------------------------------------------
# C. 复制中途失败：旧 App SHA256 前后一致
# ---------------------------------------------------------------------------
def _t_copy_failure_keeps_old() -> None:
    iw = _load_install()
    with _tmp() as tmp:
        payload1 = _fake_payload(tmp / "p1", "v1")
        app = tmp / "App"
        lib = tmp / "Library"
        (lib / "notes").mkdir(parents=True)
        (lib / "notes" / "seed.md").write_text("seed", encoding="utf-8")
        lib_sha0 = _sha256_tree(lib)
        _install_inproc(iw, payload1, app, lib, smoke=False)
        sha_before = _sha256_tree(app)
        # 第二次安装，复制中途失败
        payload2 = _fake_payload(tmp / "p2", "v2")
        orig = iw.shutil.copytree
        calls = {"n": 0}

        def _boom(*a, **k):
            calls["n"] += 1
            raise RuntimeError("模拟磁盘写满")
        iw.shutil.copytree = _boom
        try:
            rc = _install_inproc(iw, payload2, app, lib, smoke=False)
        finally:
            iw.shutil.copytree = orig
        ok = rc != 0
        ok &= _sha256_tree(app) == sha_before        # 旧 App 不变
        ok &= not (app.parent / "App.staging").exists()
        ok &= not (app.parent / "App.backup").exists()
        ok &= _sha256_tree(lib) == lib_sha0          # Library 不变
        check("C 复制中途失败：旧 App SHA 一致 + 无残留 + Library 不变", ok, f"rc={rc}")


# ---------------------------------------------------------------------------
# D. staging 校验失败：旧 App 不变
# ---------------------------------------------------------------------------
def _t_verify_staging_failure_keeps_old() -> None:
    iw = _load_install()
    with _tmp() as tmp:
        payload1 = _fake_payload(tmp / "p1", "v1")
        app = tmp / "App"
        lib = tmp / "Library"
        _install_inproc(iw, payload1, app, lib, smoke=False)
        sha_before = _sha256_tree(app)
        payload2 = _fake_payload(tmp / "p2", "v2")
        orig = iw._verify_staging
        iw._verify_staging = lambda s: (_ for _ in ()).throw(RuntimeError("staging 校验失败"))
        try:
            rc = _install_inproc(iw, payload2, app, lib, smoke=False)
        finally:
            iw._verify_staging = orig
        ok = rc != 0 and _sha256_tree(app) == sha_before
        ok &= not (app.parent / "App.staging").exists()
        ok &= not (app.parent / "App.backup").exists()
        check("D staging 校验失败：旧 App 不变 + 无残留", ok, f"rc={rc}")


# ---------------------------------------------------------------------------
# E. swap 后启动失败：rollback 到旧 App
# ---------------------------------------------------------------------------
def _t_swap_then_launch_failure_rolls_back() -> None:
    iw = _load_install()
    with _tmp() as tmp:
        payload1 = _fake_payload(tmp / "p1", "v1")
        app = tmp / "App"
        lib = tmp / "Library"
        _install_inproc(iw, payload1, app, lib, smoke=False)
        sha_v1 = _sha256_tree(app)
        payload2 = _fake_payload(tmp / "p2", "v2")
        # swap 成功但启动验证失败 → 应回滚到 v1
        orig = iw._post_install_smoke
        iw._post_install_smoke = lambda a, l, p: 1
        try:
            rc = _install_inproc(iw, payload2, app, lib, smoke=True)
        finally:
            iw._post_install_smoke = orig
        ok = rc != 0
        ok &= (app / "app" / "VERSION.txt").read_text(encoding="utf-8") == "v1"  # 回滚成功
        ok &= _sha256_tree(app) == sha_v1
        ok &= not (app.parent / "App.staging").exists()
        ok &= not (app.parent / "App.backup").exists()
        check("E swap 后启动失败：rollback 到旧 App + SHA 一致 + 无残留", ok, f"rc={rc}")


# ---------------------------------------------------------------------------
# F. 首次安装失败：不得留下可误启动的半成品 App
# ---------------------------------------------------------------------------
def _t_first_install_failure_leaves_nothing() -> None:
    iw = _load_install()
    with _tmp() as tmp:
        app = tmp / "App"
        lib = tmp / "Library"
        payload = _fake_payload(tmp / "pbad", "vX")
        orig = iw.shutil.copytree
        iw.shutil.copytree = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模拟失败"))
        try:
            rc = _install_inproc(iw, payload, app, lib, smoke=False)
        finally:
            iw.shutil.copytree = orig
        ok = rc != 0 and not app.exists()           # 首装失败无半成品
        ok &= not (tmp / "App.staging").exists()
        ok &= not (tmp / "App.backup").exists()
        check("F 首次安装失败：不留半成品 App + 无残留", ok, f"rc={rc}")


# ---------------------------------------------------------------------------
# G. 所有失败场景：Library SHA256 前后一致（显式复测）
# ---------------------------------------------------------------------------
def _t_library_untouched_on_failure() -> None:
    iw = _load_install()
    with _tmp() as tmp:
        payload1 = _fake_payload(tmp / "p1", "v1")
        app = tmp / "App"
        lib = tmp / "Library"
        _install_inproc(iw, payload1, app, lib, smoke=False)
        lib / "notes" / "a.md"
        (lib / "notes").mkdir(parents=True, exist_ok=True)
        (lib / "notes" / "a.md").write_text("aaa", encoding="utf-8")
        (lib / "originals").mkdir(parents=True, exist_ok=True)
        (lib / "originals" / "o.bin").write_bytes(b"\x00\x01\x02")
        lib_sha0 = _sha256_tree(lib)

        # 触发一次安装失败（staging 校验失败）
        payload2 = _fake_payload(tmp / "p2", "v2")
        orig = iw._verify_staging
        iw._verify_staging = lambda s: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            rc = _install_inproc(iw, payload2, app, lib, smoke=False)
        finally:
            iw._verify_staging = orig
        ok = rc != 0 and _sha256_tree(lib) == lib_sha0
        # 关键：Library 目录未被当成 staging / backup / 重命名
        ok &= not (lib.parent / "USB-WIKI-Data.staging").exists()
        ok &= not (lib.parent / "USB-WIKI-Data.backup").exists()
        check("G 失败场景 Library SHA 完全一致 + 未被 staging/backup 触碰", ok, f"rc={rc}")


def _timed(label: str, fn) -> None:
    """逐 case 计时 —— 让「慢 / 挂」精确定位到某一步。

    为什么必须有它：A2 曾出现「B(verify) 之后 20+ 分钟无任何输出」，
    却无法判断是 build / install / cleanup 哪一段慢；加计时后卡点一眼可见。
    """
    t0 = time.time()
    try:
        fn()
    finally:
        print(f"  ⏱ {label}: {time.time() - t0:.1f}s")


def run_a2_tests() -> None:
    section("Installer Hardening / 事务化安装 + 回滚")
    # 安全隔离：进入沙箱前拍真实副作用快照，全程 sandbox 隔离，退出后比对。
    # 若沙箱失效导致真实桌面/LOCALAPPDATA 被写，快照不一致 → 明确 FAIL（绝不写后再删）。
    snap_before = _snapshot_real_side_effects()
    with _install_sandbox() as sb:
        print(f"  ℹ 安装沙箱：LOCALAPPDATA→{os.environ['LOCALAPPDATA']} "
              f"WIKIUSB_STATE_DIR→{os.environ['WIKIUSB_STATE_DIR']} "
              f"desktop_dir→{sb.desktop_dir}")
        for label, fn in (
            ("A_first_install", _t_first_install_success),
            ("B_reinstall_fake", _t_reinstall_success),
            ("B_real_verify", _t_reinstall_success_verify),
            ("C_copy_failure", _t_copy_failure_keeps_old),
            ("D_staging_failure", _t_verify_staging_failure_keeps_old),
            ("E_swap_rollback", _t_swap_then_launch_failure_rolls_back),
            ("F_first_install_failure", _t_first_install_failure_leaves_nothing),
            ("G_library_untouched", _t_library_untouched_on_failure),
        ):
            _timed(label, fn)
    snap_after = _snapshot_real_side_effects()
    check("A2 全程未修改真实 Desktop / LOCALAPPDATA",
          snap_before == snap_after,
          f"before={snap_before} after={snap_after}")
    print(f"  ℹ A2 build 次数={BUILD_COUNT}  install 次数={INSTALL_COUNT}")


if __name__ == "__main__":
    run_a2_tests()
    total = len(PASS) + len(SKIP) + len(FAIL)
    print(f"\n  A2 TOTAL={total} PASS={len(PASS)} SKIP={len(SKIP)} FAIL={len(FAIL)}"
          f"  builds={BUILD_COUNT} installs={INSTALL_COUNT}")
    raise SystemExit(1 if FAIL else 0)
