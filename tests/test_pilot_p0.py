"""Pilot P0 targeted tests（2026-09-18，独立运行：python tests/test_pilot_p0.py）。

只覆盖用户 P0 指令要求的 5 项，全程临时目录（不碰真实桌面 / LOCALAPPDATA / Documents）：

  1. 桌面快捷方式 target 指向正式安装目录（读回 .lnk 验证）
  2. 重装不重复创建多个快捷方式（同名覆盖，无 (1)(2) 副本）
  3. 快捷方式创建失败不破坏安装（install 仍 rc=0，提示手动启动路径）
  4. Library 不受影响（marker 文件前后 SHA256 一致）
  5. START_HERE / 用户可见文案不再出现错误的绝对隐私声明

设计：仿 test_distribution_a2.py —— in-process 加载 scripts/install_windows.py，
复用 tests/dist_fixture.make_fake_release 造可校验的假发布包。
不接入 test_suite.py（Pilot 专项，避免改变 A4.2b 已通过的 911 项基线）。
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from tests import dist_fixture as fx                    # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []

FORBIDDEN_CLAIMS = ("永不上传", "绝不上传", "完全不会联网",
                    "不上传任何", "不联网上传", "全部数据只在本机")


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name if cond else f"{name} :: {detail}")
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"  [{detail}]"))
    return cond


def skip(name: str, reason: str = "") -> None:
    SKIP.append(f"{name} :: {reason}" if reason else name)
    print("  ⏭ " + name + (f"  [{reason}]" if reason else ""))


def _psq(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _ps_available() -> bool:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", "$?;"],
                           capture_output=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def read_lnk(lnk: Path) -> dict:
    """用系统 WScript.Shell 读回 .lnk 的 TargetPath / WorkingDirectory。"""
    ps = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
          "$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
          + _psq(lnk) + ");'TARGET='+$s.TargetPath;'WORK='+$s.WorkingDirectory")
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True, timeout=60, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    out = {}
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        for key in ("TARGET", "WORK"):
            if line.startswith(key + "="):
                out[key] = line[len(key) + 1:]
    return out


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run() -> dict:
    installer = fx.load_installer()
    ps_ok = _ps_available()

    tmp = Path(tempfile.mkdtemp(prefix="pilot-p0-"))
    try:
        release = fx.make_fake_release(tmp, app_version="1.0.0", marker="p0")
        app = tmp / "App"
        lib = tmp / "Library"
        desk = tmp / "Desktop"
        lib.mkdir(parents=True)
        (lib / "user_data.md").write_text("# 用户的资料，不能被碰\n", encoding="utf-8")
        lib_marker = _sha256_file(lib / "user_data.md")

        # ---- 首次安装（desktop_dir 注入临时桌面）----
        rc1 = installer.install(release, app, lib, desktop_dir=desk)
        check("T1 首次安装 rc=0", rc1 == 0, f"rc={rc1}")

        lnk = desk / "USB-WIKI.lnk"
        if ps_ok:
            check("T1 快捷方式已生成", lnk.is_file(), str(lnk))
            info = read_lnk(lnk)
            expect = str(app / "启动-Windows.bat")
            check("T1 target 指向正式安装目录",
                  info.get("TARGET", "").lower() == expect.lower(),
                  f"{info.get('TARGET')} != {expect}")
            check("T1 工作目录为正式 App 目录",
                  info.get("WORK", "").lower() == str(app).lower(),
                  info.get("WORK", ""))
        else:
            skip("T1 PowerShell 读回验证", "环境无 PowerShell")

        # ---- 重装（同一 desktop_dir）----
        rc2 = installer.install(release, app, lib, desktop_dir=desk)
        check("T2 重装 rc=0", rc2 == 0, f"rc={rc2}")
        if ps_ok:
            lnks = list(desk.glob("USB-WIKI*.lnk"))
            check("T2 不产生 (1)(2) 副本", len(lnks) == 1,
                  "found: " + ", ".join(p.name for p in lnks))
            if lnks:
                info2 = read_lnk(lnks[0])
                check("T2 重装后 target 仍指向正式 App",
                      info2.get("TARGET", "").lower()
                      == str(app / "启动-Windows.bat").lower(),
                      info2.get("TARGET", ""))
        else:
            skip("T2 PowerShell 读回验证", "环境无 PowerShell")

        # ---- T3：快捷方式创建失败不破坏安装（desktop_dir 是文件 → mkdir 必败）----
        blocker = tmp / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")
        rc3 = installer.install(release, app, lib, desktop_dir=blocker)
        check("T3 快捷方式失败时安装仍 rc=0", rc3 == 0, f"rc={rc3}")
        check("T3 App 完好（launcher 存在）",
              (app / "启动-Windows.bat").is_file())

        # ---- T4：Library 全程不受影响 ----
        check("T4 Library marker SHA256 不变",
              _sha256_file(lib / "user_data.md") == lib_marker)

        # ---- T5：用户可见文案不再有绝对隐私声明 ----
        targets = [REPO / "START_HERE.txt", REPO / "app" / "web" / "index.html",
                   REPO / "app" / "web" / "app.js"]
        bad = []
        for f in targets:
            text = f.read_text(encoding="utf-8", errors="replace")
            for phrase in FORBIDDEN_CLAIMS:
                if phrase in text:
                    bad.append(f"{f.name}:{phrase}")
        check("T5 无绝对隐私声明（不上传/永不联网类）", not bad, "; ".join(bad))
        st = (REPO / "START_HERE.txt").read_text(encoding="utf-8")
        check("T5 START_HERE 含云端 AI 准确表述", "主动配置云端 AI" in st)
        check("T5 START_HERE 桌面为主路径", "双击桌面的「USB-WIKI」" in st
              and "备用方法" in st)
        check("T5 START_HERE 含抓网页联网说明", "抓取网页时需要联网读取该网页" in st)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  P0-TOTAL={len(PASS) + len(FAIL) + len(SKIP)} "
          f"PASS={len(PASS)} SKIP={len(SKIP)} FAIL={len(FAIL)}")
    return {"pass": PASS, "skip": SKIP, "fail": FAIL}


if __name__ == "__main__":
    result = run()
    sys.exit(1 if result["fail"] else 0)
