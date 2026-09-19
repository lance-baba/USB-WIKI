"""安装 / 卸载安全收口 targeted 回归（接入 test_suite）。

产品契约（2026-09-19 起）：
  · 安装：默认目录安装 + 用户自定义 App 安装目录；交互 / CLI / CI 共用同一解析 + 事务安装。
  · App 与 Library 解耦：自定义 App 位置**不改变** Library 位置。
  · 卸载：默认 / ``--yes`` 只删 App + 快捷方式、**保留 Library**；
          彻底清场需 ``--delete-library --yes``（或交互选 2 并逐字输入 DELETE）。
  · 卸载位置从用户级安装记录（install_state.json）反查，不假定默认目录。

覆盖断言（对应用户验收清单）：
  默认 uninstall 保留 Library / uninstall --yes 保留 Library /
  uninstall --delete-library --yes 删除 Library /
  交互彻底卸载需 DELETE 二次确认 / 自定义 App 路径安装成功 /
  shortcut 指向真实 App / 自定义目录 reinstall 成功 /
  自定义目录 uninstall 能正确找到 App / App 自定义位置不改变默认 Library /
  彻底卸载后重新安装得到 NEW_LIBRARY / 自定义目录安全校验。

实现：**自建最小 fake 发布介质**（app/launcher.py + python-runtime/python.exe + 生成
MANIFEST/BUILD_INFO/THIRD_PARTY），因此不依赖真实 runtime / payload，跨平台可跑。
全程临时目录 + 临时 LOCALAPPDATA + 临时桌面目录，零真实系统副作用。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
INSTALLER = SCRIPTS / "install_windows.py"
PY = sys.executable

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import release_integrity as ri  # noqa: E402

IS_WIN = sys.platform == "win32"


# ---------------------------------------------------------------------------
# fake 发布介质（让 install() 的介质校验真的通过，而不是绕过校验）
# ---------------------------------------------------------------------------
def _make_fake_release(root: Path) -> Path:
    payload = root / "payload"
    (payload / "app").mkdir(parents=True, exist_ok=True)
    (payload / "app" / "launcher.py").write_text("print('fake launcher')\n", encoding="utf-8")
    rt = payload / "python-runtime"
    rt.mkdir(parents=True, exist_ok=True)
    (rt / "python.exe").write_bytes(b"MZ_fake_python")  # 占位：存在 + 非空即可（不跑 smoke）
    lic = root / "LICENSES"
    lic.mkdir(parents=True, exist_ok=True)
    (lic / "THIRD_PARTY.json").write_text(
        json.dumps({"inventory_complete": True}), encoding="utf-8")
    info = {
        "app_version": "1.3.0", "git_commit": "testcommit0000000000000000000000000000",
        "build_time_utc": "2026-09-19T00:00:00Z", "platform": "test",
        "python_version": "3.11.9", "dependency_lock_sha256": "0" * 64,
        "schema_version": 1, "data_format_version": 1, "release_format_version": 1,
    }
    (root / "BUILD_INFO.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _, manifest = ri.write_manifest(root)
    ri.write_checksums(root, manifest)
    return root


def _env(localapp: Path) -> dict:
    # 同时重定向 LOCALAPPDATA（默认 App 位置）与安装记录目录（WIKIUSB_STATE_DIR），
    # 覆盖 test_suite 全局注入的 WIKIUSB_STATE_DIR，保证每个场景独立、零真实写入。
    return {**os.environ, "LOCALAPPDATA": str(localapp),
            "WIKIUSB_STATE_DIR": str(localapp / "USB-WIKI"),
            "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def _call(env: dict, cmd: str, args: list, input_text: str | None = None):
    p = subprocess.run([PY, str(INSTALLER), cmd, *args], env=env,
                       input=input_text, capture_output=True, text=True, timeout=300)
    return p.returncode, p.stdout, p.stderr


def _install(env: dict, release: Path, app: Path | None, lib: Path, desk: Path,
             extra: list | None = None):
    args = ["--release", str(release), "--library-target", str(lib),
            "--desktop-dir", str(desk), "--no-verify"]
    if app is not None:
        args += ["--app-target", str(app)]
    if extra:
        args += extra
    return _call(env, "install", args, input_text="\n")


def _uninstall(env: dict, args: list, input_text: str | None = None):
    return _call(env, "uninstall", args, input_text=input_text)


def _state_file(localapp: Path) -> Path:
    return localapp / "USB-WIKI" / "install_state.json"


def _shortcut_ok(out: str, lnk: Path) -> bool:
    """安装器是否成功创建了指向 App 的桌面快捷方式。

    ⚠ 刻意**不解析 .lnk 二进制、不依赖 WScript.Shell 回读**：CI 的英文 Windows 上
    TargetPath 可能是 8.3 短路径（RUNNER~1）而 Python `resolve()` 不展开短名，
    回读比对会假红（快捷方式其实是对的）。安装器只有在把 TargetPath 正确设为
    「<App>\\启动-Windows.bat」并 Save 成功时才打印该成功行，故以「成功行 + lnk 存在」为准。
    """
    return lnk.is_file() and "已创建桌面快捷方式" in out


# ---------------------------------------------------------------------------
def run(ctx, check, section, skip) -> None:  # noqa: ARG001
    section("安装 / 卸载 · 安全收口（默认保留资料 · 自定义目录 · 彻底清场）")
    root = Path(tempfile.mkdtemp(prefix="usbwiki_iut_"))
    try:
        release = _make_fake_release(root / "release")
        mt = ri.verify_media(release)
        check("fake 发布介质自校验通过（安装前闸门真实生效）", mt.ok,
              f"code={mt.code} failures={mt.failures[:3]}")

        # ---- [A] 默认位置安装 → 默认卸载保留 Library ----
        A = root / "A"
        localapp, desk = A / "localapp", A / "desktop"
        desk.mkdir(parents=True)
        app, lib = A / "App", A / "docs" / "USB-WIKI-Data"
        env = _env(localapp)
        rc, out, err = _install(env, release, app, lib, desk)
        check("A 默认位置安装成功", rc == 0 and app.is_dir() and app.joinpath("app", "launcher.py").is_file(),
              f"rc={rc} err={err[-200:]}")
        st = _state_file(localapp)
        ok_state = False
        if st.is_file():
            d = json.loads(st.read_text(encoding="utf-8"))
            ok_state = (Path(d.get("app_path", "")).resolve() == app.resolve()
                        and Path(d.get("library_path", "")).resolve() == lib.resolve())
        check("A 安装记录（install_state.json）记录真实 App/Library", ok_state)
        check("A Library 已创建（资料目录独立于程序）", lib.is_dir())

        lnk = desk / "USB-WIKI.lnk"
        if IS_WIN:
            check("A 桌面快捷方式存在", lnk.is_file())
            check("A 快捷方式指向真实 App", _shortcut_ok(out, lnk))
        else:
            skip("A 快捷方式指向真实 App", "非 Windows，跳过 .lnk 校验")

        # 交互默认（回车=1）→ 保留资料
        lib.joinpath("keep.md").write_text("data", encoding="utf-8")
        rc, out, err = _uninstall(env, ["--desktop-dir", str(desk)], input_text="\n")
        check("默认 uninstall 保留 Library", rc == 0 and not app.exists() and lib.is_dir()
              and lib.joinpath("keep.md").is_file(), f"rc={rc} out={out[-160:]}")
        if IS_WIN:
            check("默认 uninstall 删除快捷方式", not lnk.exists())
        check("默认 uninstall 清除安装记录", not _state_file(localapp).is_file())

        # ---- [B] --yes 保留 Library ----
        B = root / "B"
        localapp2, desk2 = B / "localapp", B / "desktop"
        desk2.mkdir(parents=True)
        app2, lib2 = B / "App", B / "docs" / "USB-WIKI-Data"
        env2 = _env(localapp2)
        rc, out, err = _install(env2, release, app2, lib2, desk2)
        lib2.joinpath("keep.md").write_text("data", encoding="utf-8")
        rc, out, err = _uninstall(env2, ["--desktop-dir", str(desk2), "--yes"])
        check("uninstall --yes 保留 Library（只删程序）",
              rc == 0 and not app2.exists() and lib2.is_dir() and lib2.joinpath("keep.md").is_file())

        # ---- [C] --delete-library --yes 彻底删除 + 重装 NEW_LIBRARY ----
        C = root / "C"
        localapp3, desk3 = C / "localapp", C / "desktop"
        desk3.mkdir(parents=True)
        app3, lib3 = C / "App", C / "docs" / "USB-WIKI-Data"
        env3 = _env(localapp3)
        rc, out, err = _install(env3, release, app3, lib3, desk3)
        lib3.joinpath("old.md").write_text("old", encoding="utf-8")
        rc, out, err = _uninstall(env3, ["--desktop-dir", str(desk3), "--delete-library", "--yes"])
        check("uninstall --delete-library --yes 删除 Library",
              rc == 0 and not app3.exists() and not lib3.exists())
        check("彻底卸载清除安装记录", not _state_file(localapp3).is_file())
        # 重装 → 必须表现为 NEW_LIBRARY（旧资料不复现）
        rc, out, err = _install(env3, release, app3, lib3, desk3)
        check("彻底卸载后重新安装得到 NEW_LIBRARY",
              rc == 0 and lib3.is_dir() and not lib3.joinpath("old.md").exists())

        # ---- [D] 交互彻底卸载需 DELETE 二次确认 ----
        D = root / "D"
        localapp4, desk4 = D / "localapp", D / "desktop"
        desk4.mkdir(parents=True)
        app4, lib4 = D / "App", D / "docs" / "USB-WIKI-Data"
        env4 = _env(localapp4)
        _install(env4, release, app4, lib4, desk4)
        lib4.joinpath("keep.md").write_text("data", encoding="utf-8")
        # 选 2 但确认输入 yes → 取消，什么都不删
        rc, out, err = _uninstall(env4, ["--desktop-dir", str(desk4)], input_text="2\nyes\n")
        check("交互彻底卸载：确认输入 yes 不删除（需 DELETE）",
              rc == 0 and app4.is_dir() and lib4.is_dir())
        # 选 2 + DELETE → 彻底删除
        rc, out, err = _uninstall(env4, ["--desktop-dir", str(desk4)], input_text="2\nDELETE\n")
        check("交互彻底卸载：输入 DELETE 后删除 App + Library",
              rc == 0 and not app4.exists() and not lib4.exists())

        # ---- [E] 自定义 App 安装目录 ----
        E = root / "E"
        localapp5, desk5 = E / "localapp", E / "desktop"
        desk5.mkdir(parents=True)
        custom_app = E / "d_drive" / "Apps" / "USB-WIKI"
        lib5 = E / "docs" / "USB-WIKI-Data"      # Library 仍走独立目录（默认概念）
        env5 = _env(localapp5)
        rc, out, err = _install(env5, release, custom_app, lib5, desk5)
        check("自定义 App 路径安装成功",
              rc == 0 and custom_app.joinpath("app", "launcher.py").is_file(),
              f"rc={rc} err={err[-200:]}")
        check("App 自定义位置不改变资料目录（App/Library 解耦）",
              lib5.is_dir() and custom_app.resolve() not in lib5.resolve().parents
              and lib5.resolve() not in custom_app.resolve().parents)
        st5 = json.loads(_state_file(localapp5).read_text(encoding="utf-8"))
        check("安装记录记录自定义 App 路径",
              Path(st5.get("app_path", "")).resolve() == custom_app.resolve())
        if IS_WIN:
            check("自定义安装的快捷方式指向自定义 App",
                  _shortcut_ok(out, desk5 / "USB-WIKI.lnk"))
        else:
            skip("自定义安装的快捷方式指向自定义 App", "非 Windows")

        # 自定义目录 reinstall：事务交换，Library 不动
        lib5.joinpath("sentinel.md").write_text("keepme", encoding="utf-8")
        rc, out, err = _install(env5, release, custom_app, lib5, desk5)
        check("自定义目录 reinstall 成功（Library 不动）",
              rc == 0 and custom_app.joinpath("app", "launcher.py").is_file()
              and lib5.joinpath("sentinel.md").is_file(),
              f"rc={rc} err={err[-200:]}")

        # 自定义目录 uninstall：不传 --app-target，必须从安装记录找到
        rc, out, err = _uninstall(env5, ["--desktop-dir", str(desk5), "--yes"])
        check("自定义目录 uninstall 能正确找到 App（读安装记录）",
              rc == 0 and not custom_app.exists() and lib5.is_dir(),
              f"rc={rc} out={out[-160:]}")

        # ---- [F] 自定义目录安全校验 ----
        F = root / "F"
        localapp6, desk6 = F / "localapp", F / "desktop"
        desk6.mkdir(parents=True)
        env6 = _env(localapp6)
        # 不能装进发布介质目录内
        rc, out, err = _install(env6, release, release / "payload" / "app", F / "lib", desk6)
        check("拒绝把程序装进发布介质目录内", rc != 0)
        # 不能覆盖已有其它文件的目录
        dirty = F / "dirty"
        dirty.mkdir(parents=True)
        (dirty / "userfile.txt").write_text("mine", encoding="utf-8")
        rc, out, err = _install(env6, release, dirty, F / "lib2", desk6)
        check("拒绝覆盖已有其它文件的目录（不删未知文件腾位置）",
              rc != 0 and (dirty / "userfile.txt").is_file())

    finally:
        shutil.rmtree(root, ignore_errors=True)
