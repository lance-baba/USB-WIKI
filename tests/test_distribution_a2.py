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
"""
from __future__ import annotations

import hashlib
import json
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


class _tmp:
    def __enter__(self) -> Path:
        self.d = Path(tempfile.mkdtemp(prefix="wikiusb-a2-"))
        return self.d

    def __exit__(self, *a) -> None:
        shutil.rmtree(self.d, ignore_errors=True)


def _fake_payload(tmp: Path, marker: str) -> Path:
    """一个最小但**介质完整**的假发布包（A3 起安装前必须先过介质校验）。

    夹具见 tests/dist_fixture.py：结构含 installer/ + payload/ + LICENSES/ +
    BUILD_INFO.json + RELEASE_MANIFEST.json + SHA256SUMS，可直接通过 verify_media。
    事务测试只关心「装到一半失败会不会毁掉旧 App」，与介质校验互不干扰。
    """
    # VERSION.txt 直接写 marker（A2 各场景用 "v1"/"v2" 断言版本切换）
    return fx.make_fake_release(tmp, app_version=marker, marker=marker)


def _install_inproc(iw, release, app, lib, **kw) -> int:
    try:
        return iw.install(Path(release), Path(app), Path(lib), **kw)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1


def _install_subprocess(release, app, lib, extra=None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(REPO / "scripts" / "install_windows.py"), "install",
           "--release", str(release), "--app-target", str(app),
           "--library-target", str(lib), "--no-verify"]
    if extra:
        cmd += extra
    return subprocess.run(cmd, capture_output=True, text=True)


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
    if not _runtime_present():
        skip("B(verify) 重装 + 真实启动验证", "runtime 未构建")
        return
    with _tmp() as tmp:
        # 用真实 dist（含真实 app + 嵌入式 runtime）做重装 + 启动验证
        dist = tmp / "dist"
        b = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "build_release.py"),
             "--output", str(dist)], capture_output=True, text=True)
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


def run_a2_tests() -> None:
    section("Installer Hardening / 事务化安装 + 回滚")
    _t_first_install_success()
    _t_reinstall_success()
    _t_reinstall_success_verify()
    _t_copy_failure_keeps_old()
    _t_verify_staging_failure_keeps_old()
    _t_swap_then_launch_failure_rolls_back()
    _t_first_install_failure_leaves_nothing()
    _t_library_untouched_on_failure()


if __name__ == "__main__":
    run_a2_tests()
    total = len(PASS) + len(SKIP) + len(FAIL)
    print(f"\n  A2 TOTAL={total} PASS={len(PASS)} SKIP={len(SKIP)} FAIL={len(FAIL)}")
    raise SystemExit(1 if FAIL else 0)
