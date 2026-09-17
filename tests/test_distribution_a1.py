"""A1 —— Release Builder + SSD Installer 骨架验收（临时目录，零真实 LOCALAPPDATA/Documents 写入）。

覆盖：
  构建     build_release → dist/USB-WIKI-vX-win-x64/{installer,payload/{app,python-runtime}}
  安装     install 到临时 app-target / library-target（--app-target / --library-target）
  边界     Library 独立；App/Runtime 可覆盖
  负向     缺 app → 明确失败；缺 python-runtime → 明确失败
  保护     已有 Library 不覆盖；重装 SHA256 一致；安装失败 Library 不变
  启动     (win32 + 嵌入式 runtime 完整) embedded 启动 → healthz=200 → status ready → shutdown rc=0

设计：本模块自包含（自带 check/skip/section 与 PASS/FAIL/SKIP 列表），
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


class _tmp:
    def __enter__(self) -> Path:
        self.d = Path(tempfile.mkdtemp(prefix="wikiusb-a1-"))
        return self.d

    def __exit__(self, *a) -> None:
        shutil.rmtree(self.d, ignore_errors=True)


def _build(tmp: Path) -> Path:
    dist = tmp / "dist"
    out = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "build_release.py"),
         "--output", str(dist)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        print(out.stdout, out.stderr)
    return dist / f"USB-WIKI-v{APP_VERSION}-win-x64"


def _install(release: Path, app: Path, lib: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "install_windows.py"), "install",
         "--release", str(release), "--app-target", str(app),
         "--library-target", str(lib), "--no-verify"],
        capture_output=True, text=True,
    )


def _broken_release(tmp: Path, *, drop_app: bool) -> Path:
    """造一个**介质完整**但 payload 结构有缺陷的发布包。

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


def _t_build() -> None:
    have_rt = _runtime_present()
    with _tmp() as tmp:
        root = _build(tmp)
        ok = (root / "payload" / "app").is_dir()
        ok &= (root / "installer" / "install.py").is_file()
        if have_rt:
            ok &= (root / "payload" / "python-runtime" / "python.exe").is_file()
        label = "build_release 生成 payload/app + installer" + (
            " + python-runtime" if have_rt else "（无 runtime：仅 app+installer）")
        check(label, ok, f"root={root}")


def _t_missing_app() -> None:
    with _tmp() as tmp:
        bad = _broken_release(tmp, drop_app=True)
        r = _install(bad, tmp / "app", tmp / "lib")
        check("缺 app/ → 安装明确失败（rc!=0）", r.returncode != 0, f"rc={r.returncode}")
        check("缺 app/ 属结构校验（非介质损坏）", r.returncode == 2, f"rc={r.returncode}")


def _t_missing_runtime() -> None:
    with _tmp() as tmp:
        bad = _broken_release(tmp, drop_app=False)
        r = _install(bad, tmp / "app", tmp / "lib")
        check("缺 python-runtime/python.exe → 安装明确失败（rc!=0）",
              r.returncode != 0, f"rc={r.returncode}")
        check("缺 runtime 属结构校验（非介质损坏）", r.returncode == 2, f"rc={r.returncode}")


def _t_preserves_existing_library() -> None:
    if not _runtime_present():
        skip("已有 Library 不覆盖（无嵌入式 runtime，跳过）", "runtime 未构建")
        return
    with _tmp() as tmp:
        root = _build(tmp)
        lib = tmp / "lib"
        (lib / "notes").mkdir(parents=True)
        (lib / "notes" / "seed.md").write_text("seed-content", encoding="utf-8")
        r = _install(root, tmp / "app", lib)
        preserved = (lib / "notes" / "seed.md").read_text(encoding="utf-8") == "seed-content"
        check("已有 Library → 安装后原文件不被覆盖",
              r.returncode == 0 and preserved, f"rc={r.returncode} preserved={preserved}")


def _t_reinstall_sha256_stable() -> None:
    if not _runtime_present():
        skip("重装 Library SHA256 一致（无嵌入式 runtime，跳过）", "runtime 未构建")
        return
    with _tmp() as tmp:
        root = _build(tmp)
        lib = tmp / "lib"
        (lib / "notes").mkdir(parents=True)
        (lib / "notes" / "a.md").write_text("aaa", encoding="utf-8")
        app = tmp / "app"
        r1 = _install(root, app, lib)
        h1 = _sha256_tree(lib)
        r2 = _install(root, app, lib)
        h2 = _sha256_tree(lib)
        check("重装前后 Library SHA256 完全一致",
              r1.returncode == 0 and r2.returncode == 0 and h1 == h2,
              f"h1={h1[:10]} h2={h2[:10]}")


def _t_failure_leaves_library() -> None:
    with _tmp() as tmp:
        root = _build(tmp)
        lib = tmp / "lib"
        (lib / "notes").mkdir(parents=True)
        (lib / "notes" / "keep.md").write_text("keep", encoding="utf-8")
        h0 = _sha256_tree(lib)
        # 用缺 python-runtime 的发布包触发安装失败（介质完整、结构有缺陷）
        bad = _broken_release(tmp / "bad", drop_app=False)
        r = _install(bad, tmp / "app", lib)
        h1 = _sha256_tree(lib)
        check("安装失败 → Library SHA256 完全一致",
              r.returncode != 0 and h0 == h1, f"rc={r.returncode}")


def _t_launch_smoke() -> None:
    if sys.platform != "win32":
        skip("embedded runtime 启动 smoke", "非 Windows 跳过")
        return
    if not _runtime_present():
        skip("embedded runtime 启动 smoke", "runtime 未构建")
        return
    with _tmp() as tmp:
        root = _build(tmp)
        app = tmp / "app"
        lib = tmp / "lib"
        r = _install(root, app, lib)
        if r.returncode != 0:
            skip("embedded runtime 启动 smoke", f"install 失败：{r.stderr[-200:]}")
            return
        exe = app / "runtime" / "python.exe"
        if not exe.is_file():
            skip("embedded runtime 启动 smoke", "未找到嵌入式 python.exe")
            return
        # 先确认嵌入式运行时能 import app（不完整则跳过，避免误红）
        # ⚠ 必须显式把 App 根加入 sys.path：嵌入式 `._pth` 接管 sys.path 后 cwd 不在其中，
        # 裸 `-c "import app.launcher"` 必然失败 —— 那会把「运行时完好」误判成「不完整」，
        # 让本用例永远是 skip（而不是 red），等于这条验收不存在。
        probe = subprocess.run(
            [str(exe), "-c", "import sys; sys.path.insert(0, '.'); import app.launcher"],
            cwd=str(app), capture_output=True, text=True, timeout=120)
        if probe.returncode != 0:
            skip("embedded runtime 启动 smoke",
                 "嵌入式运行时不完整（import app.launcher 失败）")
            return
        _smoke_run(exe, app, lib)


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


def run_a1_tests() -> None:
    section("发布构建 + SSD 安装骨架")
    _t_build()
    _t_missing_app()
    _t_missing_runtime()
    _t_preserves_existing_library()
    _t_reinstall_sha256_stable()
    _t_failure_leaves_library()
    _t_launch_smoke()


if __name__ == "__main__":
    run_a1_tests()
    total = len(PASS) + len(SKIP) + len(FAIL)
    print(f"\n  A1 TOTAL={total} PASS={len(PASS)} SKIP={len(SKIP)} FAIL={len(FAIL)}")
    raise SystemExit(1 if FAIL else 0)
