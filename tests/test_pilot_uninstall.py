"""Pilot 卸载器 targeted 测试（RELEASE_REQUIRED，不接入 test_suite，不动 A4.2b 基线）。

产品契约（2026-09-19 起）：
  · 默认 / ``--yes``            → 删 App + 桌面快捷方式，**保留 Library（用户资料）**
  · 交互选 2 / ``--delete-library --yes`` → 彻底清场（App + Library + 快捷方式 + 安装记录）
  · 彻底卸载必须二次确认：交互需逐字输入 DELETE，CLI 需显式 ``--delete-library``

覆盖：
  T1 ``--yes`` 只删程序，保留 Library（marker 指定的 Library 与默认 Library 都不动）。
  T2 未安装 → rc=0，不创建/不删除任何东西。
  T3 交互直接回车（默认 1）→ 保留资料。
  T4 交互选 1 → 保留资料。
  T5 彻底卸载需逐字 DELETE：输入 "2" 后输入 yes → 取消，什么都不删。
  T6 交互 "2" + "DELETE" → 彻底删除（App + Library + lnk）。
  T7 ``--delete-library --yes`` → 彻底删除，且清除安装记录。
  T8 非默认 App 位置：从安装记录（install_state.json）反查真实 App 并删除。
  T9 marker 反查：彻底卸载时按 App/library_path.txt 真实路径清理 Library。

全程临时目录 + 临时 LOCALAPPDATA + 临时桌面目录，零真实系统副作用。
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PY = sys.executable
INSTALLER = Path(__file__).resolve().parent.parent / "scripts" / "install_windows.py"

passed = failed = 0


def run(args, td, input_text=None, env_extra=None):
    """调用卸载器。**始终**把 LOCALAPPDATA 指向该用例的临时目录，
    避免 _clear_state() 误删真实用户的安装记录。"""
    localapp = Path(td) / "_localapp"
    localapp.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "LOCALAPPDATA": str(localapp),
           "WIKIUSB_STATE_DIR": str(localapp / "USB-WIKI"), **(env_extra or {})}
    p = subprocess.run(
        [PY, str(INSTALLER), "uninstall", *args],
        input=input_text, capture_output=True, text=True, timeout=120, env=env,
    )
    return p.returncode, p.stdout, p.stderr


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}")


def _seed_app(td, marker_lib=None):
    app = td / "App"
    app.mkdir()
    if marker_lib is not None:
        (app / "library_path.txt").write_text(str(marker_lib), encoding="utf-8")
    return app


# ---------------------------------------------------------------------------
print("[T1] --yes 只删程序，保留 Library")
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    marker_lib = td / "CustomData"
    marker_lib.mkdir()
    (marker_lib / "note.md").write_text("x", encoding="utf-8")
    app = _seed_app(td, marker_lib)
    default_lib = td / "DefaultData"
    default_lib.mkdir()
    (default_lib / "keep.md").write_text("y", encoding="utf-8")
    desk = td / "Desktop"
    desk.mkdir()
    (desk / "USB-WIKI.lnk").write_text("lnk", encoding="utf-8")

    rc, out, err = run(["--app-target", str(app), "--library-target", str(default_lib),
                        "--desktop-dir", str(desk), "--yes"], td)
    check("T1 rc=0", rc == 0)
    check("T1 App 已删", not app.exists())
    check("T1 桌面 lnk 已删", not (desk / "USB-WIKI.lnk").exists())
    check("T1 marker Library 被保留", marker_lib.exists())
    check("T1 默认 Library 被保留", default_lib.exists())

# ---------------------------------------------------------------------------
print("[T2] 未安装 → rc=0，无副作用")
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    desk = td / "Desktop"
    desk.mkdir()
    rc, out, err = run(["--app-target", str(td / "App"), "--library-target", str(td / "USB-WIKI-Data"),
                        "--desktop-dir", str(desk), "--yes"], td)
    check("T2 rc=0", rc == 0)
    check("T2 未创建 App", not (td / "App").exists())
    check("T2 未创建 Library", not (td / "USB-WIKI-Data").exists())
    check("T2 提示无需卸载", "无需卸载" in out)

# ---------------------------------------------------------------------------
print("[T3] 交互直接回车（默认 1）→ 保留资料")
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    app = _seed_app(td)
    lib = td / "USB-WIKI-Data"
    lib.mkdir()
    (lib / "keep.md").write_text("y", encoding="utf-8")
    desk = td / "Desktop"
    desk.mkdir()
    (desk / "USB-WIKI.lnk").write_text("lnk", encoding="utf-8")
    rc, out, err = run(["--app-target", str(app), "--library-target", str(lib),
                        "--desktop-dir", str(desk)], td, input_text="\n")
    check("T3 rc=0", rc == 0)
    check("T3 App 已删", not app.exists())
    check("T3 lnk 已删", not (desk / "USB-WIKI.lnk").exists())
    check("T3 Library 保留", lib.exists())

# ---------------------------------------------------------------------------
print("[T4] 交互选 1 → 保留资料")
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    app = _seed_app(td)
    lib = td / "USB-WIKI-Data"
    lib.mkdir()
    (lib / "keep.md").write_text("y", encoding="utf-8")
    desk = td / "Desktop"
    desk.mkdir()
    rc, out, err = run(["--app-target", str(app), "--library-target", str(lib),
                        "--desktop-dir", str(desk)], td, input_text="1\n")
    check("T4 rc=0", rc == 0)
    check("T4 App 已删", not app.exists())
    check("T4 Library 保留", lib.exists())

# ---------------------------------------------------------------------------
print("[T5] 彻底卸载二次确认：'2' 后输入 yes → 取消，什么都不删")
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    app = _seed_app(td)
    lib = td / "USB-WIKI-Data"
    lib.mkdir()
    (lib / "keep.md").write_text("y", encoding="utf-8")
    desk = td / "Desktop"
    desk.mkdir()
    (desk / "USB-WIKI.lnk").write_text("lnk", encoding="utf-8")
    rc, out, err = run(["--app-target", str(app), "--library-target", str(lib),
                        "--desktop-dir", str(desk)], td, input_text="2\nyes\n")
    check("T5 rc=0", rc == 0)
    check("T5 App 仍在", app.exists())
    check("T5 Library 仍在", lib.exists())
    check("T5 lnk 仍在", (desk / "USB-WIKI.lnk").exists())
    check("T5 提示已取消", "已取消" in out)

# ---------------------------------------------------------------------------
print("[T6] 交互 '2' + 'DELETE' → 彻底删除")
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    app = _seed_app(td)
    lib = td / "USB-WIKI-Data"
    lib.mkdir()
    (lib / "keep.md").write_text("y", encoding="utf-8")
    desk = td / "Desktop"
    desk.mkdir()
    (desk / "USB-WIKI.lnk").write_text("lnk", encoding="utf-8")
    rc, out, err = run(["--app-target", str(app), "--library-target", str(lib),
                        "--desktop-dir", str(desk)], td, input_text="2\nDELETE\n")
    check("T6 rc=0", rc == 0)
    check("T6 App 已删", not app.exists())
    check("T6 Library 已删", not lib.exists())
    check("T6 lnk 已删", not (desk / "USB-WIKI.lnk").exists())

# ---------------------------------------------------------------------------
print("[T7] --delete-library --yes → 彻底删除并清除安装记录")
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    app = _seed_app(td)
    lib = td / "USB-WIKI-Data"
    lib.mkdir()
    (lib / "keep.md").write_text("y", encoding="utf-8")
    desk = td / "Desktop"
    desk.mkdir()
    (desk / "USB-WIKI.lnk").write_text("lnk", encoding="utf-8")
    state_dir = td / "_localapp" / "USB-WIKI"
    state_dir.mkdir(parents=True)
    (state_dir / "install_state.json").write_text(
        json.dumps({"app_path": str(app), "library_path": str(lib)}), encoding="utf-8")
    rc, out, err = run(["--app-target", str(app), "--library-target", str(lib),
                        "--desktop-dir", str(desk), "--delete-library", "--yes"], td)
    check("T7 rc=0", rc == 0)
    check("T7 App 已删", not app.exists())
    check("T7 Library 已删", not lib.exists())
    check("T7 lnk 已删", not (desk / "USB-WIKI.lnk").exists())
    check("T7 安装记录已清除", not (state_dir / "install_state.json").exists())

# ---------------------------------------------------------------------------
print("[T8] 非默认 App 位置：从安装记录反查真实 App 并删除（保留 Library）")
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    custom_app = td / "d_drive" / "Apps" / "USB-WIKI"
    custom_app.mkdir(parents=True)
    (custom_app / "启动-Windows.bat").write_text("@echo off", encoding="ascii")  # App 特征
    lib = td / "USB-WIKI-Data"
    lib.mkdir()
    (lib / "keep.md").write_text("y", encoding="utf-8")
    desk = td / "Desktop"
    desk.mkdir()
    (desk / "USB-WIKI.lnk").write_text("lnk", encoding="utf-8")
    state_dir = td / "_localapp" / "USB-WIKI"
    state_dir.mkdir(parents=True)
    (state_dir / "install_state.json").write_text(
        json.dumps({"app_path": str(custom_app), "library_path": str(lib)}), encoding="utf-8")

    # 不传 --app-target：必须从安装记录反查到自定义 App
    rc, out, err = run(["--desktop-dir", str(desk), "--yes"], td)
    check("T8 rc=0", rc == 0)
    check("T8 自定义 App 已删", not custom_app.exists())
    check("T8 lnk 已删", not (desk / "USB-WIKI.lnk").exists())
    check("T8 Library 保留", lib.exists())
    check("T8 安装记录已清除", not (state_dir / "install_state.json").exists())

# ---------------------------------------------------------------------------
print("[T9] marker 反查：彻底卸载时按 App/library_path.txt 真实路径清理 Library")
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    real_lib = td / "RealPath"
    real_lib.mkdir()
    (real_lib / "note.md").write_text("x", encoding="utf-8")
    app = _seed_app(td, real_lib)
    passed_lib = td / "PassedLib"  # 模拟用户 --library-target 传错
    passed_lib.mkdir()
    (passed_lib / "keep.md").write_text("y", encoding="utf-8")
    desk = td / "Desktop"
    desk.mkdir()
    rc, out, err = run(["--app-target", str(app), "--library-target", str(passed_lib),
                        "--desktop-dir", str(desk), "--delete-library", "--yes"], td)
    check("T9 rc=0", rc == 0)
    check("T9 (marker 优先) 真实 Library 已删", not real_lib.exists())
    check("T9 传入 Library 不受影响", passed_lib.exists())

# ---------------------------------------------------------------------------
print(f"\nUNINSTALL-TOTAL={passed + failed} PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
