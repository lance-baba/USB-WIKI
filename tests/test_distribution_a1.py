"""A1 —— Release Builder + SSD Installer 骨架验收（临时目录，零真实 LOCALAPPDATA/Documents 写入）。

覆盖：
  构建     build_release → dist/USB-WIKI-vX-win-x64/{installer,payload/{app,python-runtime}}
  安装     install 到临时 app-target / library-target（--app-target / --library-target）
  边界     Library 独立；App/Runtime 可覆盖
  负向     缺 app → 明确失败；缺 python-runtime → 明确失败
  保护     已有 Library 不覆盖；重装 SHA256 一致；安装失败 Library 不变
  启动     (win32 + 嵌入式 runtime 完整) embedded 启动 → healthz=200 → status ready → shutdown rc=0

设计（2026-09-25 性能改造 —— RELEASE_REQUIRED / Test Infrastructure Performance Defect）：
  * **真实 build 只做一次**：整轮 A1 共用同一份真实 release（session fixture）。
    原实现有 **5 处** 各自调用 ``build_release``（每次 ~390s），是 Gate 的主要耗时源。
  * **整轮共用一个 session TEMP root**：各 case 用子目录，**结束时只清理一次**。
    原实现每个 case 各自 ``_tmp().__exit__`` → 一次**同步无限** ``shutil.rmtree``，
    在 Windows 实时防毒下单个清理可达数十分钟并阻断整个 Gate。
  * **cleanup 有界且 best-effort**：子进程 rmtree + 超时；超时只打 ``CLEANUP_WARNING`` 并列残留，
    **绝不改动已完成的产品 PASS/FAIL**，也绝不触碰真实 Library。
  * 负向结构用例（缺 app / 缺 runtime）继续用 **fake release**（``tests/dist_fixture.py``），
    不为「缺一个目录」复制整个 runtime。

本模块自包含（自带 check/skip/section 与 PASS/FAIL/SKIP 列表），
由 tests/test_suite.py 的 main() 调用 run_a1_tests() 并合并结果。
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
    print(f"\n── A1 {t} " + "─" * max(0, 54 - len(t)))


def _runtime_present() -> bool:
    return (REPO / "runtime" / "python-3.11-embed" / "python.exe").is_file()


def _sha256_tree(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode("utf-8"))
            h.update(p.read_bytes())
    return h.hexdigest()


# --------------------------------------------------------------------------
# session TEMP root + 有界 best-effort cleanup
# --------------------------------------------------------------------------
class _Session:
    """整轮 A1 共用的临时根 —— 各 case 用子目录，结束时只清理一次。

    这样做的原因：每个 case 各自 ``rmtree`` 会在 Windows 实时防毒下把 Gate 拖到小时级；
    把「产品断言」与「临时目录清理」解耦后，清理只在最后发生一次，且有超时上限。
    """

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="wikiusb-a1-session-"))
        self.release: Path | None = None
        self.app: Path | None = None
        self.lib: Path | None = None
        self.cleanup_warning: str | None = None

    def sub(self, name: str) -> Path:
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def cleanup(self, timeout: int = 150) -> bool:
        ok = _cleanup_bounded(self.root, timeout)
        if not ok:
            self.cleanup_warning = str(self.root)
        return ok


def _print_cleanup_warning(root: Path, why: str) -> None:
    print(f"\n  ⚠ CLEANUP_WARNING: 临时目录未在限时内清理（{why}）")
    print(f"    leftover : {root}")
    print("    → 不影响本次产品 PASS/FAIL；残留可后续人工/后台清理。")


def _cleanup_bounded(root: Path, timeout: int) -> bool:
    """best-effort 清理：子进程 rmtree + 超时，**绝不阻塞 Gate**。

    * ``ignore_errors=True`` 已容忍个别被占用/只读文件（AV 常短暂锁文件）。
    * 超时 → kill 该子进程、打印 CLEANUP_WARNING、列出残留，**返回 False 但不改 PASS/FAIL**。
    * 只删传入的 session root（在 TEMP 下），**绝不触碰真实 Library**。
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


# --------------------------------------------------------------------------
def _sandbox(tmp: Path) -> tuple[dict, Path]:
    """A1 subprocess 沙箱：LOCALAPPDATA / 安装记录 / 桌面 **全部**重定向到 TEMP。

    为什么必须有它：生产 ``install()`` 成功后会调用 ``create_desktop_shortcut()``
    与 ``_write_state()``，分别写**真实桌面**的 ``USB-WIKI.lnk`` 和
    ``%LOCALAPPDATA%\\USB-WIKI\\install_state.json``。不隔离就违反本文件自我约定的
    「零真实 LOCALAPPDATA/Documents 写入」——A1 会改掉用户的真实安装记录与桌面。
    """
    localapp = tmp / "localapp"
    state = localapp / "USB-WIKI"
    desk = tmp / "Desktop"
    for d in (localapp, state, desk):
        d.mkdir(parents=True, exist_ok=True)
    env = {**os.environ,
           "LOCALAPPDATA": str(localapp),
           "WIKIUSB_STATE_DIR": str(state),
           "PYTHONUTF8": "1",
           "PYTHONIOENCODING": "utf-8"}
    return env, desk


def _run_bounded(cmd, *, phase: str, timeout: int, env: dict,
                 cwd: str | None = None) -> subprocess.CompletedProcess:
    """有界 subprocess —— Release 测试**禁止无限等待**。

    - ``stdin=DEVNULL``：声明本调用已给全 CLI 参数、绝不应进入 ``input()``。
      若生产代码意外进入交互读取，DEVNULL 会让它**立刻 EOF**，而不是永久挂起。
    - ``timeout``：超时即返回 rc=-1 并打印 command / 阶段 / stdout 尾部 / stderr 尾部，
      让对应用例**明确 FAIL**，而不是让整套 suite 卡死在无声处。
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              env=env, cwd=cwd, stdin=subprocess.DEVNULL)
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


def _build(tmp: Path, timeout: int = 900) -> Path:
    """真实构建发布包（整轮 A1 只应调用一次 —— 见 _Session）。"""
    global BUILD_COUNT
    BUILD_COUNT += 1
    env, _ = _sandbox(tmp)
    dist = tmp / "dist"
    out = _run_bounded(
        [sys.executable, str(REPO / "scripts" / "build_release.py"),
         "--output", str(dist)],
        phase="build_release", timeout=timeout, env=env)
    if out.returncode != 0:
        print(out.stdout, out.stderr)
    return dist / f"USB-WIKI-v{APP_VERSION}-win-x64"


def _install(release: Path, app: Path, lib: Path, tmp: Path,
             timeout: int = 1200) -> subprocess.CompletedProcess:
    """安装子进程（沙箱 + 有界）。

    ⚠ timeout 取值依据（实测，非拍脑袋）：本环境重装（had_old=True）会触发
    ``_rmtree`` 删除旧 App（约 6757 个文件 / 240MB），而 Windows 实时防毒对
    **删除** 的吞吐远低于写入 —— 隔离实测重装耗时 354–492s（cProfile 显示 92% 耗在
    ``nt.unlink``/``nt.rmdir``）。安装逻辑本身无缺陷（rc=0 正常返回），只是删除慢。
    初版按用户估算给 180s → 真实重装直接超时；提到 900s 后，在一次**与本任务无关的并发
    磁盘重负载**（后台误跑了一份完整 A1 全量，与本次验证抢同一块 HDD）下仍被拖到 >900s。
    为容纳「真实重装 ~490s + AV/并发 I/O 尖峰余量」，最终定 **1200s（20 分钟）**：
    既给足余量确保 FAIL=0，又仍**严格有界** —— 真正无限挂起会在 1200s 被 _run_bounded 捕获并
    显式 FAIL，而非让整套 suite 静默卡死（这正是 Phase D 要修的原始 bug：旧 _install 无 timeout）。
    注意：1200s 只是「最长等待」，正常重装 ~490s 即返回，不会拖慢通过路径。
    """
    global INSTALL_COUNT
    INSTALL_COUNT += 1
    env, desk = _sandbox(tmp)
    return _run_bounded(
        [sys.executable, str(REPO / "scripts" / "install_windows.py"), "install",
         "--release", str(release), "--app-target", str(app),
         "--library-target", str(lib), "--no-verify",
         "--desktop-dir", str(desk)],
        phase="install", timeout=timeout, env=env)


def _real_side_effects() -> dict:
    """只读快照**真实**（非沙箱）位置：桌面快捷方式 + 安装记录。"""
    desk = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
    state = Path(os.environ.get("LOCALAPPDATA", "")) / "USB-WIKI" / "install_state.json"
    snap = {}
    for key, p in (("desktop_lnk", desk / "USB-WIKI.lnk"),
                   ("install_state", state)):
        try:
            st = p.stat()
            snap[key] = (st.st_size, st.st_mtime_ns)
        except OSError:
            snap[key] = None
    return snap


def _broken_release(tmp: Path, *, drop_app: bool) -> Path:
    """造一个**介质完整**但 payload 结构有缺陷的发布包（fake，极小，不复制大 runtime）。

    A3 起安装前先过介质闸门；因此「payload 缺 app / 缺 runtime」这类结构性缺陷
    必须在**介质校验通过之后**才暴露 —— 删掉文件后重新生成清单即可精确命中
    `validate_payload` 分支（否则测的只是介质闸门，覆盖不到结构校验）。
    """
    root = fx.make_fake_release(tmp, app_version="1.0.0")
    if drop_app:
        shutil.rmtree(root / "payload" / "app")
    else:
        shutil.rmtree(root / "payload" / "python-runtime")
    fx.write_release_artifacts(root, app_version="1.0.0")
    return root


# --------------------------------------------------------------------------
# Cases（对应 spec 的 A–F；真实 release 由 session 共享，不重复 build）
# --------------------------------------------------------------------------
def _case_a_build_structure(sess: _Session) -> None:
    """Case A —— 真实构建一次，并校验结构。"""
    have_rt = _runtime_present()
    root = _build(sess.sub("build"))
    sess.release = root
    ok = (root / "payload" / "app").is_dir()
    ok &= (root / "installer" / "install.py").is_file()
    if have_rt:
        ok &= (root / "payload" / "python-runtime" / "python.exe").is_file()
    label = "build_release 生成 payload/app + installer" + (
        " + python-runtime" if have_rt else "（无 runtime：仅 app+installer）")
    check(label, ok, f"root={root}")


def _case_b_fake_negative(sess: _Session) -> None:
    """Case B —— 结构缺陷（fake release，快）。

    缺 app / 缺 runtime 都必须**在介质校验通过后**由结构校验明确失败（rc=2）。
    """
    d = sess.sub("neg-app")
    bad = _broken_release(d, drop_app=True)
    r = _install(bad, d / "app", d / "lib", d)
    check("缺 app/ → 安装明确失败（rc!=0）", r.returncode != 0, f"rc={r.returncode}")
    check("缺 app/ 属结构校验（非介质损坏）", r.returncode == 2, f"rc={r.returncode}")

    d = sess.sub("neg-runtime")
    bad = _broken_release(d, drop_app=False)
    r = _install(bad, d / "app", d / "lib", d)
    check("缺 python-runtime/python.exe → 安装明确失败（rc!=0）",
          r.returncode != 0, f"rc={r.returncode}")
    check("缺 runtime 属结构校验（非介质损坏）", r.returncode == 2, f"rc={r.returncode}")


def _case_c_fresh_install(sess: _Session) -> None:
    """Case C —— 用共享真实 release 做 fresh install（含已有 Library 保护）。"""
    if sess.release is None or not _runtime_present():
        skip("fresh install（无嵌入式 runtime，跳过）", "runtime 未构建")
        return
    d = sess.sub("install")
    lib = d / "lib"
    (lib / "notes").mkdir(parents=True)
    (lib / "notes" / "seed.md").write_text("seed-content", encoding="utf-8")
    app = d / "app"
    r = _install(sess.release, app, lib, d)
    preserved = (lib / "notes" / "seed.md").read_text(encoding="utf-8") == "seed-content"
    check("已有 Library → 安装后原文件不被覆盖",
          r.returncode == 0 and preserved, f"rc={r.returncode} preserved={preserved}")
    # 沙箱捕获验证：安装记录必须落在 TEMP 沙箱，而不是真实 LOCALAPPDATA
    captured = (d / "localapp" / "USB-WIKI" / "install_state.json").is_file()
    check("安装记录被沙箱捕获（写 TEMP 而非真实 LOCALAPPDATA）",
          captured, f"state={d / 'localapp' / 'USB-WIKI' / 'install_state.json'}")
    if r.returncode == 0:
        sess.app, sess.lib = app, lib


def _case_d_reinstall(sess: _Session) -> None:
    """Case D —— 同一个 App / 同一个 Library 重装，Library SHA256 必须一致。"""
    if sess.app is None or sess.release is None:
        skip("重装 Library SHA256 一致（无可用安装，跳过）", "fresh install 未成功")
        return
    h1 = _sha256_tree(sess.lib)
    r2 = _install(sess.release, sess.app, sess.lib, sess.lib.parent)
    h2 = _sha256_tree(sess.lib)
    check("重装前后 Library SHA256 完全一致",
          r2.returncode == 0 and h1 == h2, f"rc={r2.returncode} h1={h1[:10]} h2={h2[:10]}")


def _case_e_smoke(sess: _Session) -> None:
    """Case E —— 直接复用已安装的 App 做启动 smoke（不再额外安装）。"""
    if sys.platform != "win32":
        skip("embedded runtime 启动 smoke", "非 Windows 跳过")
        return
    if not _runtime_present() or sess.app is None:
        skip("embedded runtime 启动 smoke", "runtime 未构建 / 无可用安装")
        return
    exe = sess.app / "runtime" / "python.exe"
    if not exe.is_file():
        skip("embedded runtime 启动 smoke", "未找到嵌入式 python.exe")
        return
    # 先确认嵌入式运行时能 import app（不完整则跳过，避免误红）
    # ⚠ 必须显式把 App 根加入 sys.path：嵌入式 `._pth` 接管 sys.path 后 cwd 不在其中，
    # 裸 `-c "import app.launcher"` 必然失败 —— 那会把「运行时完好」误判成「不完整」，
    # 让本用例永远是 skip（而不是 red），等于这条验收不存在。
    probe = subprocess.run(
        [str(exe), "-c", "import sys; sys.path.insert(0, '.'); import app.launcher"],
        cwd=str(sess.app), capture_output=True, text=True, timeout=120)
    if probe.returncode != 0:
        skip("embedded runtime 启动 smoke", "嵌入式运行时不完整（import app.launcher 失败）")
        return
    _smoke_run(exe, sess.app, sess.lib)


def _case_f_failure_leaves_library(sess: _Session) -> None:
    """Case F —— 安装失败绝不改动 Library（fake 坏包触发失败）。"""
    d = sess.sub("failure")
    lib = d / "lib"
    (lib / "notes").mkdir(parents=True)
    (lib / "notes" / "keep.md").write_text("keep", encoding="utf-8")
    h0 = _sha256_tree(lib)
    # 用缺 python-runtime 的发布包触发安装失败（介质完整、结构有缺陷）
    bad = _broken_release(d / "bad", drop_app=False)
    r = _install(bad, d / "app", lib, d)
    h1 = _sha256_tree(lib)
    check("安装失败 → Library SHA256 完全一致",
          r.returncode != 0 and h0 == h1, f"rc={r.returncode}")


def _smoke_run(exe: Path, app: Path, lib: Path) -> None:
    import http.client
    port = 28997
    env = {**os.environ, "WIKIUSB_LIBRARY": str(lib),
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    logf = open(app / "a1-smoke.log", "wb")
    proc = subprocess.Popen(
        [str(exe), "app/launcher.py", "--no-browser", "--port", str(port)],
        cwd=str(app), env=env, stdout=logf, stderr=subprocess.STDOUT)

    def call(method: str, path: str):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        c.request(method, path, headers={"Host": f"127.0.0.1:{port}"})
        r = c.getresponse()
        d = r.read()
        c.close()
        return r.status, d

    try:
        ok = False
        for _ in range(60):
            time.sleep(1)
            try:
                st, d = call("GET", "/healthz")
                if st == 200 and json.loads(d).get("ok"):
                    ok = True
                    break
            except Exception:
                pass
        check("embedded runtime 安装后 healthz=200", ok)

        ready = False
        if ok:
            for _ in range(90):
                try:
                    st, d = call("GET", "/api/status")
                    if st == 200 and (json.loads(d).get("data") or {}).get("ready"):
                        ready = True
                        break
                except Exception:
                    pass
                time.sleep(1)
        check("embedded runtime 安装后 status ready=true", ready)

        call("POST", "/api/system/shutdown")
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
        check("embedded runtime 安装后 shutdown rc=0",
              proc.returncode == 0, f"rc={proc.returncode}")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        logf.close()


def _timed(label: str, fn) -> None:
    """逐子测试计时 —— 让「慢 / 挂」能被精确定位到某一步，而不是整套静默卡死。"""
    t0 = time.time()
    try:
        fn()
    finally:
        print(f"  ⏱ {label}: {time.time() - t0:.1f}s")


def run_a1_tests() -> None:
    section("发布构建 + SSD 安装骨架")
    before = _real_side_effects()
    sess = _Session()
    print(f"  A1 session TEMP root: {sess.root}")
    try:
        for label, fn in (
            ("case_a_build_structure", _case_a_build_structure),
            ("case_b_fake_negative", _case_b_fake_negative),
            ("case_c_fresh_install", _case_c_fresh_install),
            ("case_d_reinstall", _case_d_reinstall),
            ("case_e_smoke", _case_e_smoke),
            ("case_f_failure_leaves_library", _case_f_failure_leaves_library),
        ):
            _timed(label, lambda f=fn: f(sess))
    finally:
        # 产品断言与清理解耦：只在这里清理一次，且**有超时上限**。
        _timed("cleanup", lambda: sess.cleanup(timeout=150))
        print(f"  ℹ A1 build 次数={BUILD_COUNT}  install 次数={INSTALL_COUNT}")

    after = _real_side_effects()
    check("A1 全程未写入真实 Desktop / LOCALAPPDATA（副作用只在 TEMP 沙箱）",
          before == after, f"before={before} after={after}")


if __name__ == "__main__":
    run_a1_tests()
    total = len(PASS) + len(SKIP) + len(FAIL)
    print(f"\n  A1 TOTAL={total} PASS={len(PASS)} SKIP={len(SKIP)} FAIL={len(FAIL)}"
          f"  builds={BUILD_COUNT} installs={INSTALL_COUNT}")
    raise SystemExit(1 if FAIL else 0)
