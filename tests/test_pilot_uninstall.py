"""Pilot 卸载器 targeted 测试（RELEASE_REQUIRED，不接入 test_suite，不动 A4.2b 基线）。

覆盖：
  T1 完整卸载（--yes）：App + marker 指定的 Library + 桌面 lnk 全部删除，
     且未通过 --library-target 传入的默认 Library 不受影响。
  T2 未安装 → rc=0，不创建/不删除任何东西。
  T3 交互确认输入 no → 中止，什么都不删。
  T4 交互确认输入 yes → 等同于 --yes。
  T5 marker 反查：App/library_path.txt 指向非默认路径时按真实路径清理。

全程临时目录，零系统副作用。
"""
import os
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path

PY = sys.executable
INSTALLER = Path(__file__).resolve().parent.parent / "scripts" / "install_windows.py"

passed = failed = 0


def run(args, input_text=None):
    p = subprocess.run(
        [PY, str(INSTALLER), "uninstall", *args],
        input=input_text, capture_output=True, text=True, timeout=120,
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


# ---------------------------------------------------------------------------
print("[T1] 完整卸载 --yes（marker 指定 Library，默认 Library 不受影响）")
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    app = td / "App"
    app.mkdir()
    custom_lib = td / "CustomData"
    custom_lib.mkdir()
    (custom_lib / "note.md").write_text("x", encoding="utf-8")
    (app / "library_path.txt").write_text(str(custom_lib), encoding="utf-8")  # marker
    default_lib = td / "DefaultData"
    default_lib.mkdir()
    (default_lib / "keep.md").write_text("y", encoding="utf-8")
    desk = td / "Desktop"
    desk.mkdir()
    (desk / "USB-WIKI.lnk").write_text("lnk", encoding="utf-8")

    rc, out, err = run([
        "--app-target", str(app),
        "--library-target", str(default_lib),
        "--desktop-dir", str(desk),
        "--yes",
    ])
    check("T1 rc=0", rc == 0)
    check("T1 App 已删", not app.exists())
    check("T1 marker Library 已删", not custom_lib.exists())
    check("T1 默认 Library 不受影响", default_lib.exists())
    check("T1 桌面 lnk 已删", not (desk / "USB-WIKI.lnk").exists())

# ---------------------------------------------------------------------------
print("[T2] 未安装 → rc=0，无副作用")
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    app = td / "App"
    lib = td / "USB-WIKI-Data"
    desk = td / "Desktop"
    desk.mkdir()
    rc, out, err = run([
        "--app-target", str(app),
        "--library-target", str(lib),
        "--desktop-dir", str(desk),
        "--yes",
    ])
    check("T2 rc=0", rc == 0)
    check("T2 未创建 App", not app.exists())
    check("T2 未创建 Library", not lib.exists())
    check("T2 提示无需卸载", "无需卸载" in out)

# ---------------------------------------------------------------------------
print("[T3] 交互确认输入 no → 中止，什么都不删")
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    app = td / "App"
    app.mkdir()
    lib = td / "USB-WIKI-Data"
    lib.mkdir()
    (lib / "keep.md").write_text("y", encoding="utf-8")
    desk = td / "Desktop"
    desk.mkdir()
    (desk / "USB-WIKI.lnk").write_text("lnk", encoding="utf-8")
    rc, out, err = run([
        "--app-target", str(app),
        "--library-target", str(lib),
        "--desktop-dir", str(desk),
    ], input_text="no\n")
    check("T3 rc=0", rc == 0)
    check("T3 App 仍在", app.exists())
    check("T3 Library 仍在", lib.exists())
    check("T3 lnk 仍在", (desk / "USB-WIKI.lnk").exists())
    check("T3 已取消未改动", "未做任何改动" in out)

# ---------------------------------------------------------------------------
print("[T4] 交互确认输入 yes → 等同 --yes")
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    app = td / "App"
    app.mkdir()
    lib = td / "USB-WIKI-Data"
    lib.mkdir()
    (lib / "keep.md").write_text("y", encoding="utf-8")
    desk = td / "Desktop"
    desk.mkdir()
    (desk / "USB-WIKI.lnk").write_text("lnk", encoding="utf-8")
    rc, out, err = run([
        "--app-target", str(app),
        "--library-target", str(lib),
        "--desktop-dir", str(desk),
    ], input_text="yes\n")
    check("T4 rc=0", rc == 0)
    check("T4 App 已删", not app.exists())
    check("T4 Library 已删", not lib.exists())
    check("T4 lnk 已删", not (desk / "USB-WIKI.lnk").exists())

# ---------------------------------------------------------------------------
print("[T5] marker 反查：App marker 指向非默认路径时按其清理")
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    app = td / "App"
    app.mkdir()
    real_lib = td / "RealPath"
    real_lib.mkdir()
    (real_lib / "note.md").write_text("x", encoding="utf-8")
    (app / "library_path.txt").write_text(str(real_lib), encoding="utf-8")
    passed_lib = td / "PassedLib"  # 模拟用户 --library-target 传错/默认不同
    passed_lib.mkdir()
    (passed_lib / "keep.md").write_text("y", encoding="utf-8")
    rc, out, err = run([
        "--app-target", str(app),
        "--library-target", str(passed_lib),
        "--yes",
    ])
    check("T5 rc=0", rc == 0)
    check("T5 真实 Library 已删", not real_lib.exists())
    check("T5 传入 Library 不受影响", passed_lib.exists())

# ---------------------------------------------------------------------------
print(f"\nUNINSTALL-TOTAL={passed + failed} PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
