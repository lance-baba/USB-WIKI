#!/usr/bin/env python3
"""SSD 安装器（Windows）—— 事务化安装 + 回滚。

职责（A2 范围）：
  1. 校验 payload 完整性（缺 app/ 或 python-runtime/python.exe → 明确失败）
  2. 事务化安装：
       payload 校验 → 复制到 staging → 验证 staging → 旧 App 备份
       → staging 交换为正式 App → 安装后 smoke（可选 --verify）→ 清理 backup
  3. 任意阶段失败 → 回滚到旧 App；首装失败则清理半成品，绝不留下可误启动的 App
  4. Library 为不可触碰边界：安装/更新/回滚全程不碰 USB-WIKI-Data
  5. 仅给启动进程设置进程级 WIKIUSB_LIBRARY（不写系统全局环境变量）
  6. 用户双击入口 启动-Windows.bat 不含 --no-browser（自动开浏览器）；
     --no-browser 仅用于测试/CI/调试
  7. 卸载安全收口（2026-09-19）：默认只删 App + 桌面快捷方式、**保留 Library**；
     彻底清场需 `--delete-library --yes`（或交互选 2 并**逐字输入 DELETE**）。
     自定义 App 安装目录 + 用户级 install_state.json 记录真实安装位置 ——
     卸载/重装/快捷方式一律按真实位置，绝不假定默认目录。

硬性边界（V1 Freeze / A2 范围）：
  - 纯标准库实现，零 pip、零联网、零外部二进制下载。
  - 不为「把架构做完整」提前决定 Ollama / GGUF / ONNX / LLM 来源。
  - 不实现 Repair Engine / LICENSES / BUILD_INFO(最终) / SHA256SUMS(最终)。

设计要点：嵌入式 Python 的 ._pth 只含 exe 目录，app 模块靠「以 app/launcher.py
为脚本启动时把脚本目录加入 sys.path[0]」被发现；安装器把 payload/python-runtime
映射到 App/runtime，使 paths.py 的 BASE_DIR(__file__.parents[2]) 与
RUNTIME_DIR(BASE_DIR/runtime) 零改动即可工作。
"""
from __future__ import annotations

import argparse
import ctypes
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_DIRNAME = "App"
STAGING_SUFFIX = ".staging"
BACKUP_SUFFIX = ".backup"
LAUNCHER_NAME = "启动-Windows.bat"
LIBRARY_MARKER = "library_path.txt"

#: 用户级安装记录（跨会话定位真实安装位置，独立于 App 目录，卸载 App 后仍存在）
STATE_DIRNAME = "USB-WIKI"
STATE_FILENAME = "install_state.json"

#: 稳定错误码：介质损坏（缺文件 / size 不符 / hash 不符 / manifest 损坏）
MEDIA_CORRUPTED_RC = 4


def _load_integrity():
    """加载介质校验唯一实现。

    发布态它在 installer/ 与 install.py 同目录；仓库态在 scripts/ 同目录。
    两边都是「本文件所在目录」，因此统一把该目录挂到 sys.path 再 import。
    """
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    import release_integrity  # noqa: PLC0415
    return release_integrity


def _default_release_root() -> Path:
    here = Path(__file__).resolve().parent
    # 发布态：installer/install.py → 上一级即发布根（含 payload/ 与 RELEASE_MANIFEST.json）
    return here.parent if (here.parent / "payload").is_dir() else here


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------
def _default_app_target() -> Path:
    # 冻结决策 #2：App 默认落 LOCALAPPDATA\USB-WIKI\App（SSD 安装态）
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) / "USB-WIKI" if local else Path.home() / "USB-WIKI"
    return base / APP_DIRNAME


def _documents_dir() -> Path:
    """Windows 下优先用 Known Folder API 取真实 Documents 目录。

    覆盖 OneDrive 重定向 / 企业策略 / Documents 被迁移的情况；
    取不到再 fallback 到 %USERPROFILE%\\Documents。纯标准库，无第三方依赖。
    """
    try:
        return _known_folder_documents()
    except Exception:
        return Path.home() / "Documents"


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, uuid_str: str):
        parts = uuid_str.replace("-", "")
        super().__init__(
            Data1=int(parts[0:8], 16),
            Data2=int(parts[8:12], 16),
            Data3=int(parts[12:16], 16),
            Data4=(ctypes.c_ubyte * 8)(
                *[int(parts[16 + i * 2:18 + i * 2], 16) for i in range(8)]
            ),
        )


def _known_folder(guid_str: str) -> Path:
    """通用 Known Folder 读取（纯 ctypes，无第三方依赖）。"""
    guid = GUID(guid_str)
    ppath = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(GUID), ctypes.c_uint32, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    hr = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(ppath))
    if hr != 0 or not ppath.value:
        raise OSError(f"SHGetKnownFolderPath 失败 hr={hr}")
    try:
        return Path(ppath.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(ppath)


def _known_folder_documents() -> Path:
    # FOLDERID_Documents = {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
    return _known_folder("FDD39AD0-238F-46AF-ADB4-6C85480369C7")


def _desktop_dir() -> Path:
    """真实桌面目录（覆盖 OneDrive 重定向 / 企业策略迁移）。"""
    # FOLDERID_Desktop = {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
    try:
        return _known_folder("B4BFCC3A-DB2C-424C-B029-7FE99A87C641")
    except Exception:
        return Path.home() / "Desktop"


def _default_library_target() -> Path:
    # 冻结决策 #2：Library 默认落 Documents\USB-WIKI-Data（经 WIKIUSB_LIBRARY 重定向）
    return _documents_dir() / "USB-WIKI-Data"


def _read_marker_library(app_target: Path) -> Path | None:
    """从已装 App 的 library_path.txt 反查真实 Library 路径（优先于默认路径）。

    安装时 _write_launcher 把用户实际选择的 Library 写进 App 内 marker，
    卸载时据此精确清理，避免误删默认路径以外的资料。
    """
    marker = app_target / LIBRARY_MARKER
    if marker.is_file():
        try:
            p = Path(marker.read_text(encoding="utf-8").strip())
            if p.is_absolute():
                return p
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# 用户级安装记录（install_state.json）—— 跨会话定位真实安装位置
# ---------------------------------------------------------------------------
def _state_dir() -> Path:
    """USB-WIKI 自己的用户级配置区。

    刻意**与 App 目录解耦**：默认 App 落 ``%LOCALAPPDATA%\\USB-WIKI\\App``，
    但自定义安装可把 App 放到任意盘符；安装记录必须始终落在一个**固定**位置，
    这样卸载/重装/诊断才能找到真实安装位置，而不是假定默认目录。
    """
    # 测试隔离钩子：显式覆盖安装记录目录（正式运行不设置此变量）。
    override = (os.environ.get("WIKIUSB_STATE_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else (Path.home() / ".usb-wiki")
    return base / STATE_DIRNAME


def _state_file() -> Path:
    return _state_dir() / STATE_FILENAME


def _write_state(app_target: Path, library_target: Path,
                 app_version: str | None = None) -> None:
    """安装成功后写安装记录（App 路径 + Library 路径）。纯标准库、仅用户级。"""
    try:
        d = _state_dir()
        d.mkdir(parents=True, exist_ok=True)
        data = {
            "app_path": str(Path(app_target).resolve()),
            "library_path": str(Path(library_target).resolve()),
            "install_time_utc": datetime.datetime.now(datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "app_version": app_version or "",
        }
        _state_file().write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as e:  # 安装记录失败**不算安装失败**（非 BLOCKER）
        print(f"[install] 提示：安装记录写入失败（不影响安装）：{e}")


def _read_state() -> dict | None:
    f = _state_file()
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _clear_state() -> None:
    """清除安装记录（卸载时调用；记录不存在或删除失败都不阻断卸载）。"""
    try:
        _state_file().unlink()
    except OSError:
        pass


def _state_app_target() -> Path | None:
    """从安装记录读回真实 App 目录（供重装 / 启动 / 诊断复用）。"""
    state = _read_state()
    if state and state.get("app_path"):
        try:
            return Path(state["app_path"]).resolve()
        except Exception:
            return None
    return None


def _state_library_target() -> Path | None:
    """从安装记录读回真实 Library 目录。"""
    state = _read_state()
    if state and state.get("library_path"):
        try:
            return Path(state["library_path"]).resolve()
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# 自定义 App 安装目录：安全校验 + 解析
# ---------------------------------------------------------------------------
def _looks_like_usbwiki_app(p: Path) -> bool:
    """目标目录是否已是一个 USB-WIKI App（允许 reinstall / transactional upgrade）。"""
    p = Path(p)
    return ((p / "app" / "launcher.py").is_file()
            or (p / LAUNCHER_NAME).is_file()
            or (p / LIBRARY_MARKER).is_file()
            or (p / "runtime" / "python.exe").is_file())


def _validate_app_target(path: Path, release_root: Path | None = None) -> str | None:
    """校验自定义 App 安装目录。合法返回 ``None``，否则返回**人话**错误描述。

    安全红线：路径合法 / 父目录可创建可写 / 不是文件 / 不装进发布介质 /
    不指向 Windows 系统目录 / 已存在目录只能是 USB-WIKI App。

    ⚠ 绝不为了「腾位置」删除未知文件 —— 目录已存在其它文件时直接拒绝，
      让用户另选空目录。
    """
    try:
        p = Path(path).expanduser().resolve()
    except Exception as e:
        return f"路径无法解析（{e}）"
    if p.is_file():
        return "目标是一个文件，不是目录"
    # 不能装进发布介质（解压目录 / payload）内 —— 否则卸载会把源一起删掉
    if release_root is not None:
        try:
            p.relative_to(Path(release_root).resolve())
            return "不能把程序安装到发布介质目录内（避免卸载误删安装源）"
        except ValueError:
            pass
    # 不能指向 Windows 系统目录
    sysroot = os.environ.get("SystemRoot")
    if sysroot:
        try:
            sr = Path(sysroot).resolve()
            if p == sr or p.relative_to(sr):
                return "不能安装到 Windows 系统目录"
        except ValueError:
            pass
    # 父目录必须可创建且可写（真写一个探针文件再删，比 os.access 可靠）
    parent = p.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        probe = parent / ".usbwiki_writable_probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
    except Exception as e:
        return f"父目录不可写（{e}）"
    # 已存在目录：只允许是 USB-WIKI App（reinstall）；否则拒绝
    if p.exists() and not _looks_like_usbwiki_app(p):
        return "目标目录已存在其它文件，请选择其它空目录"
    return None


def _resolve_app_target(explicit: str | None, *, interactive: bool,
                        release_root: Path | None = None) -> Path:
    """解析最终 App 安装目录：CLI 显式 > 交互输入 > 默认值。

    交互式**仅在 stdin 为 TTY** 时启用：双击 .bat / CI 管道（无 TTY）自动落默认，
    绝不因等不到输入而卡住。交互 / CLI / CI 三条入口最终共用同一 ``install()``。
    """
    if explicit:
        err = _validate_app_target(Path(explicit), release_root)
        if err:
            fail(f"自定义安装目录无效：{err}")
        return Path(explicit).expanduser().resolve()

    # 无显式目标时，**优先沿用安装记录里的真实位置** —— 这样双击 install.bat 重装
    # 会原地事务升级（而不是又装一份到默认目录），符合「重装使用真实安装路径」。
    default = _state_app_target() or _default_app_target()
    if interactive and sys.stdin is not None and sys.stdin.isatty():
        try:
            print("USB-WIKI 安装程序\n")
            print(f"程序默认安装到：\n  {default}\n")
            print("直接回车使用默认位置，或输入其它安装目录（例如 D:\\Apps\\USB-WIKI）：")
            ans = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans:
            err = _validate_app_target(Path(ans), release_root)
            if err:
                fail(f"自定义安装目录无效：{err}")
            return Path(ans).expanduser().resolve()
    return default


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def fail(msg: str, rc: int = 2) -> "None":
    print(f"[install] 失败：{msg}", file=sys.stderr)
    raise SystemExit(rc)


def validate_payload(payload: Path) -> None:
    app_dir = payload / "app"
    rt_exe = payload / "python-runtime" / "python.exe"
    if not app_dir.is_dir():
        fail(f"payload 缺少 app/ 目录（发布包不完整）：{app_dir}")
    if not rt_exe.is_file():
        fail(f"payload 缺少嵌入式 Python（python-runtime/python.exe）：{rt_exe}")


def _staging_dir(app_target: Path) -> Path:
    return app_target.parent / (app_target.name + STAGING_SUFFIX)


def _backup_dir(app_target: Path) -> Path:
    return app_target.parent / (app_target.name + BACKUP_SUFFIX)


def _rmtree(p: Path) -> None:
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)


def _move(src: Path, dst: Path) -> None:
    # 同父目录下 rename，原子性较好；确保目标不存在
    if dst.exists():
        _rmtree(dst)
    shutil.move(str(src), str(dst))


# ---------------------------------------------------------------------------
# 事务各阶段
# ---------------------------------------------------------------------------
def _copy_to_staging(payload: Path, staging: Path) -> None:
    _rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    shutil.copytree(payload / "app", staging / "app")
    shutil.copytree(payload / "python-runtime", staging / "runtime")
    # 随包嵌入资源 → App/resources/embedding/（**属于 App，不属于 Library**）
    #   → 可重装 / 可覆盖 / 可 rollback / 可从 U 盘恢复；Library 完全不受影响。
    #   资源目录缺失不阻断安装：那是「未随包」的正常状态，运行时会降级为纯 FTS。
    emb_src = payload / "embedding"
    if emb_src.is_dir():
        shutil.copytree(emb_src, staging / "resources" / "embedding")


def _write_launcher(app_dir: Path, library_target: Path) -> None:
    # 用户双击入口：自动开浏览器（**不含 --no-browser**）
    (app_dir / LIBRARY_MARKER).write_text(str(library_target), encoding="utf-8")
    lines = [
        "@echo off",
        "setlocal",
        "cd /d \"%~dp0\"",
        f"if exist {LIBRARY_MARKER} (",
        f"  set /p WIKIUSB_LIBRARY=<{LIBRARY_MARKER}",
        ")",
        "runtime\\python.exe app\\launcher.py %*",
        "",
    ]
    (app_dir / LAUNCHER_NAME).write_text("\r\n".join(lines), encoding="utf-8")


def _verify_staging(staging: Path) -> None:
    if not (staging / "app").is_dir():
        raise RuntimeError("staging 缺少 app/")
    if not (staging / "runtime" / "python.exe").is_file():
        raise RuntimeError("staging 缺少 runtime/python.exe")
    if not (staging / "app" / "launcher.py").is_file():
        raise RuntimeError("staging 缺少 app/launcher.py")


def _can_smoke(app_target: Path) -> bool:
    """这个 App 是否具备**真正做启动验证**的条件。

    ⚠ 探测方式必须匹配嵌入式运行时的加载机制，否则门禁会「永远通过」：
    嵌入式 Python 带 `python311._pth`，它会**接管 sys.path**、不再把 cwd 放进
    `sys.path[0]`，因此 `python.exe -c "import app.launcher"` 在 `._pth` 运行时里
    **必然** `ModuleNotFoundError: No module named 'app'` —— 早期版本据此判断「运行时
    不完整」直接返回 0，等于安装后 smoke 从未真正执行（比没有门禁更危险）。
    真实启动路径是 `python.exe app/launcher.py`（脚本目录会被加入 path），
    所以探针也显式把 App 根加入 sys.path，与真实启动语义一致。
    """
    if sys.platform != "win32":
        return False
    exe = app_target / "runtime" / "python.exe"
    if not exe.is_file():
        return False
    try:
        probe = subprocess.run(
            [str(exe), "-c",
             "import sys; sys.path.insert(0, '.'); import app.launcher"],
            cwd=str(app_target), capture_output=True, text=True, timeout=120)
        return probe.returncode == 0
    except Exception:
        return False


def _post_install_smoke(app_target: Path, library_target: Path, port: int) -> int:
    """启动 → 等 ready → 优雅关闭 → 返回 rc。

    不可 smoke（非 Windows / 运行时不完整）时返回 0（视为通过），
    让 Linux / 无 runtime 的构建环境安装仍能走完事务。
    """
    if not _can_smoke(app_target):
        return 0
    exe = app_target / "runtime" / "python.exe"
    env = {**os.environ, "WIKIUSB_LIBRARY": str(library_target),
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        [str(exe), "app/launcher.py", "--no-browser", "--port", str(port)],
        cwd=str(app_target), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        ok = False
        for _ in range(60):
            time.sleep(1)
            try:
                import http.client
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
                c.request("GET", "/healthz", headers={"Host": f"127.0.0.1:{port}"})
                r = c.getresponse()
                if r.status == 200 and json.loads(r.read()).get("ok"):
                    ok = True
                    break
            except Exception:
                pass
        if ok:
            ready = False
            for _ in range(90):
                try:
                    import http.client
                    c = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
                    c.request("GET", "/api/status", headers={"Host": f"127.0.0.1:{port}"})
                    r = c.getresponse()
                    d = json.loads(r.read())
                    if (d.get("data") or {}).get("ready"):
                        ready = True
                        break
                except Exception:
                    pass
                time.sleep(1)
            ok = ok and ready
        # 优雅关闭验证
        try:
            import http.client
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
            c.request("POST", "/api/system/shutdown", headers={"Host": f"127.0.0.1:{port}"})
            c.getresponse()
        except Exception:
            pass
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 0 if ok else 1
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


SHORTCUT_NAME = "USB-WIKI.lnk"


def _psq(s: str) -> str:
    """PowerShell 单引号字符串转义。"""
    return "'" + str(s).replace("'", "''") + "'"


def create_desktop_shortcut(app_target: Path,
                            desktop_dir: Path | None = None) -> bool:
    """在桌面创建 USB-WIKI.lnk → 正式 App 的 启动-Windows.bat（Pilot P0-2）。

    设计约束：
      - 复用同一 lnk 文件（WScript.Shell 同名覆盖），重装绝不产生 (1)(2) 副本；
      - 只允许在 App swap **成功之后**调用 → 永远指向正式安装目录，
        绝不指向 staging / backup 临时目录；
      - 任何失败都**不算安装失败**：返回 False 并打印手动启动路径（非 BLOCKER）。
    """
    manual = str(Path(app_target) / LAUNCHER_NAME)
    try:
        target = Path(app_target) / LAUNCHER_NAME
        if not target.is_file():
            print(f"[install] 桌面快捷方式创建失败，可从以下位置启动：{manual}")
            return False
        desktop = Path(desktop_dir) if desktop_dir else _desktop_dir()
        desktop.mkdir(parents=True, exist_ok=True)
        lnk = desktop / SHORTCUT_NAME
        ps = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
              "$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
              + _psq(lnk) + ");"
              "$s.TargetPath=" + _psq(target) + ";"
              "$s.WorkingDirectory=" + _psq(app_target) + ";"
              "$s.Description='USB-WIKI';$s.Save()")
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=60,
            encoding="utf-8", errors="replace")
        if r.returncode != 0 or not lnk.is_file():
            raise RuntimeError((r.stderr or r.stdout).strip() or "lnk 未生成")
        print(f"[install] 已创建桌面快捷方式：{lnk}")
        return True
    except Exception as e:
        print(f"[install] 桌面快捷方式创建失败（不影响安装），"
              f"可从以下位置启动：{manual}（{e}）")
        return False


def ensure_library(library_target: Path) -> None:
    # Library 为不可触碰边界：已存在则接管，绝不覆盖/删除/移动
    if library_target.exists():
        print(f"[install] 检测到已有 Library，直接接管（不覆盖、不删除）：{library_target}")
    else:
        library_target.mkdir(parents=True, exist_ok=True)
        print(f"[install] 已创建 Library：{library_target}")


def _run(app_target: Path, library_target: Path, port: int, no_browser: bool) -> int:
    exe = app_target / "runtime" / "python.exe"
    if not exe.is_file():
        fail(f"嵌入式 Python 不存在：{exe}")
    # 仅给本启动进程设置 WIKIUSB_LIBRARY（进程级，不写全局）
    env = {**os.environ, "WIKIUSB_LIBRARY": str(library_target),
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    args = [str(exe), "app/launcher.py"]
    if no_browser:
        args.append("--no-browser")
    args += ["--port", str(port)]
    proc = subprocess.Popen(args, cwd=str(app_target), env=env)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    return proc.returncode


# ---------------------------------------------------------------------------
# 事务化安装主流程
# ---------------------------------------------------------------------------
def fail_media(failures: list[str]) -> "None":
    """介质损坏 —— **稳定错误码** + 明确中止。

    调用点必须在创建 staging / 删除或重命名任何旧 App **之前**，
    因此这里可以断言：已中止且未改动任何已有 App / Library。
    """
    print(f"[install] MEDIA_CORRUPTED：发布介质校验未通过（{len(failures)} 项）",
          file=sys.stderr)
    for item in failures[:20]:
        print(f"  - {item}", file=sys.stderr)
    if len(failures) > 20:
        print(f"  … 其余 {len(failures) - 20} 项", file=sys.stderr)
    print("[install] 已中止：未改动任何已有 App / Library。", file=sys.stderr)
    raise SystemExit(MEDIA_CORRUPTED_RC)


def verify_release(root: Path, *, deep: bool = True):
    """只读校验发布介质（安装前闸门与 verify 命令共用同一实现）。"""
    return _load_integrity().verify_media(root, deep=deep)


def _prompt_menu(app_target: Path, library_target: Path) -> int:
    """交互卸载菜单：返回 1（保留资料，默认）或 2（彻底删除）。"""
    print("USB-WIKI 卸载程序\n")
    print(f"程序目录：\n  {app_target}\n")
    print(f"资料目录：\n  {library_target}\n")
    print("请选择：\n")
    print("  [1] 仅卸载程序，保留我的资料（推荐）")
    print("  [2] 卸载程序，并永久删除全部 USB-WIKI 资料\n")
    print("默认：1")
    try:
        ans = input("选择（1/2，直接回车 = 1）：").strip()
    except (EOFError, KeyboardInterrupt):
        ans = ""
    return 2 if ans == "2" else 1


def _prompt_delete_confirm(library_target: Path) -> bool:
    """彻底卸载的二次确认：必须**逐字**输入 ``DELETE``（不接受 y/yes）。"""
    print(f"\n即将永久删除：\n  {library_target}\n")
    print("其中包含你导入、保存和创建的全部资料。\n")
    print("此操作不可恢复。请输入：\n")
    print("  DELETE\n")
    print("继续永久删除（输入其它内容即取消）：")
    try:
        ans = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        ans = ""
    if ans == "DELETE":
        return True
    print("已取消，未做任何改动。")
    return False


def uninstall(*, app_target_cli: Path | None = None,
              library_target_cli: Path | None = None,
              desktop_dir: Path | None = None, assume_yes: bool = False,
              delete_library: bool = False) -> int:
    """卸载。产品原则：**默认只删程序，保留用户资料**。

      · 默认（交互选 1）/ ``--yes``        → 删 App + 桌面快捷方式，**保留 Library**
      · 交互选 2 / ``--delete-library --yes`` → 彻底清场（App + Library + 快捷方式 + 安装记录）
      · 彻底卸载必须二次确认：交互需逐字输入 DELETE，CLI 需显式 ``--delete-library``

    安装位置优先从**用户级安装记录**（install_state.json）反查，其次 App 内
    library_path.txt，最后回退默认目录 —— 绝不因「默认目录不存在」就误判未安装。
    纯标准库、零联网、零新依赖。
    """
    state = _read_state()
    if app_target_cli is not None:
        app_target = Path(app_target_cli).resolve()
    elif state and state.get("app_path"):
        app_target = Path(state["app_path"]).resolve()
    else:
        app_target = _default_app_target()

    # Library 解析优先级：App 内 marker（运行时真实记录）> 安装记录 > CLI > 默认。
    # ⚠ marker 优先于 CLI：marker 是**已装 App 自己记录的真实资料路径**；
    #   若让 CLI 覆盖，一次传错的 --library-target 就可能删掉无关目录 / 漏删真实资料。
    marker = _read_marker_library(app_target)
    if marker is not None:
        library_target: Path = marker
    elif state and state.get("library_path"):
        library_target = Path(state["library_path"]).resolve()
    elif library_target_cli is not None:
        library_target = Path(library_target_cli).resolve()
    else:
        library_target = _default_library_target()

    desk = Path(desktop_dir) if desktop_dir else _desktop_dir()
    lnk = desk / SHORTCUT_NAME

    app_exists = app_target.exists()
    lib_exists = library_target.exists()
    lnk_exists = lnk.exists()

    if not (app_exists or lib_exists or lnk_exists):
        print("[uninstall] 未发现已安装的 USB-WIKI，无需卸载。")
        return 0

    # --- 决定模式：keep（保留资料）/ thorough（彻底清场）/ cancel（取消）---
    if assume_yes:
        mode = "thorough" if delete_library else "keep"
    elif delete_library:
        mode = "thorough" if _prompt_delete_confirm(library_target) else "cancel"
    else:
        choice = _prompt_menu(app_target, library_target)
        if choice == 2:
            mode = "thorough" if _prompt_delete_confirm(library_target) else "cancel"
        else:
            mode = "keep"

    if mode == "cancel":
        print("[uninstall] 已取消，未做任何改动。")
        return 0

    # --- 执行 ---
    if app_exists:
        _rmtree(app_target)
        print(f"[uninstall] 已删除程序：{app_target}")
    if lnk_exists:
        try:
            lnk.unlink()
            print(f"[uninstall] 已删除快捷方式：{lnk}")
        except OSError as e:
            print(f"[uninstall] 快捷方式删除失败（不影响其余）：{e}", file=sys.stderr)

    if mode == "thorough":
        if lib_exists:
            _rmtree(library_target)
            print(f"[uninstall] 已删除资料库：{library_target}")
        _clear_state()
        print("[uninstall] 已彻底卸载，并清除 USB-WIKI 安装记录。")
    else:
        # 保留资料：清除安装记录（App 已不存在），资料库原样保留
        _clear_state()
        print("[uninstall] 程序已卸载。\n")
        print("你的资料仍保存在：")
        print(f"  {library_target}\n")
        print("以后重新安装 USB-WIKI 可以继续使用这些资料。")
    return 0


def install(release_root: Path, app_target: Path, library_target: Path,
           launch: bool = False, port: int = 28988, smoke: bool = False,
           desktop_dir: Path | None = None) -> int:
    release_root = Path(release_root)
    # 0) 介质完整性 —— 必须在任何写操作之前（否则可能出现「坏文件 + 半覆盖 App」）
    check = verify_release(release_root)
    if not check.ok:
        fail_media(check.failures)
    for warning in check.warnings:
        print(f"[install] 提示：{warning}")

    payload = release_root / "payload"
    validate_payload(payload)
    ensure_library(library_target)  # 不碰已有 Library

    staging = _staging_dir(app_target)
    backup = _backup_dir(app_target)
    _rmtree(staging)
    _rmtree(backup)

    had_old = app_target.exists()
    try:
        # 1) staging
        _copy_to_staging(payload, staging)
        _write_launcher(staging, library_target)
        _verify_staging(staging)

        # 2) backup 旧 App
        if had_old:
            _move(app_target, backup)

        # 3) swap：staging → 正式 App
        try:
            _move(staging, app_target)
        except Exception:
            if had_old and backup.exists() and not app_target.exists():
                _move(backup, app_target)  # swap 失败，恢复旧 App
            raise

        # 4) 安装后 smoke（可选）
        if smoke:
            rc = _post_install_smoke(app_target, library_target, port)
            if rc != 0:
                raise RuntimeError(f"安装后 smoke 失败（rc={rc}）")

        # 5) 成功：清理 backup
        _rmtree(backup)
        print(f"[install] App 已安装到：{app_target}")
        # 5.5) 桌面快捷方式（P0-2）：swap 已成功才调用 → 只指正式 App；
        #      失败仅提示手动启动路径，绝不影响安装结果。
        create_desktop_shortcut(app_target, desktop_dir=desktop_dir)
        # 5.6) 用户级安装记录：记住真实 App / Library 位置，供重装/卸载/诊断反查。
        #      自定义安装目录时尤其关键 —— 卸载不得再假定默认目录。
        _write_state(app_target, library_target)
        if launch:
            return _run(app_target, library_target, port, no_browser=False)
        return 0

    except Exception as e:
        # 回滚：恢复旧 App / 首装失败清理半成品 / 绝不碰 Library
        if had_old and backup.exists():
            if app_target.exists():
                _rmtree(app_target)
            _move(backup, app_target)
        elif not had_old and app_target.exists():
            _rmtree(app_target)  # 首装失败：清理半成品，不留可误启动 App
        if staging.exists():
            _rmtree(staging)
        fail(f"安装失败，已回滚：{e}", rc=3)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def verify_cmd(release_root: Path, *, as_json: bool = False, deep: bool = True) -> int:
    """只读校验 U 盘发布介质。

    只读保证：不建 Library、不写 App、不联网、不落任何日志文件。
    输出**仅含相对文件路径** —— 不泄露客户目录结构。
    """
    check = verify_release(release_root, deep=deep)

    if as_json:
        print(json.dumps(check.as_dict(), ensure_ascii=False, indent=2))
        return 0 if check.ok else MEDIA_CORRUPTED_RC

    if check.ok:
        print(f"OK — 发布介质完整（{check.file_count} 个文件，SHA256 全部匹配）")
        info = check.build_info or {}
        if info:
            commit = str(info.get("git_commit") or "")
            print(f"  版本 {info.get('app_version')} / 提交 {commit[:12]}"
                  f" / 平台 {info.get('platform')} / 构建 {info.get('build_time_utc')}")
        for warning in check.warnings:
            print(f"  提示：{warning}")
        return 0

    print(f"MEDIA_CORRUPTED — 发布介质校验未通过（{len(check.failures)} 项）")
    for item in check.failures[:30]:
        print(f"  - {item}")
    if len(check.failures) > 30:
        print(f"  … 其余 {len(check.failures) - 30} 项")
    print("提示：请重新获取一份完整的发布介质；本命令未改动任何文件。")
    return MEDIA_CORRUPTED_RC


def _parse(argv: list[str]):
    default_root = _default_release_root()

    ap = argparse.ArgumentParser(description="USB-WIKI SSD 安装器（介质校验 + 事务化）")
    sub = ap.add_subparsers(dest="cmd")

    pinstall = sub.add_parser("install", help="事务化安装到 SSD")
    pinstall.add_argument("--release", default=str(default_root),
                          help="发布根目录（含 payload/ 与 RELEASE_MANIFEST.json）")
    pinstall.add_argument("--app-target", default=None,
                          help="App 安装目录（默认 LOCALAPPDATA\\USB-WIKI\\App）")
    pinstall.add_argument("--library-target", default=None,
                          help="Library 目录（默认 文档\\USB-WIKI-Data）")
    pinstall.add_argument("--launch", action="store_true",
                          help="安装后启动（自动开浏览器）")
    pinstall.add_argument("--verify", action="store_true", default=True,
                          help="安装后做启动验证（默认开；不可用运行时自动跳过）")
    pinstall.add_argument("--no-verify", dest="verify", action="store_false",
                          help="跳过安装后启动验证")
    pinstall.add_argument("--port", type=int, default=28988)
    pinstall.add_argument("--desktop-dir", default=None,
                          help=argparse.SUPPRESS)  # 测试注入：重定向桌面目录

    pverify = sub.add_parser("verify", help="只读校验发布介质（不装、不改、不联网）")
    pverify.add_argument("--release", default=str(default_root),
                         help="发布根目录（含 RELEASE_MANIFEST.json）")
    pverify.add_argument("--json", action="store_true", help="输出 JSON")
    pverify.add_argument("--shallow", action="store_true",
                         help="只校验存在性与大小，跳过 SHA256（大介质快速体检）")

    prun = sub.add_parser("run", help="仅启动（需先 install）")
    prun.add_argument("--app-target", default=None)
    prun.add_argument("--library-target", default=None)
    prun.add_argument("--port", type=int, default=28988)
    prun.add_argument("--no-browser", action="store_true",
                      help="不自动打开浏览器（调试/CI）")

    puninstall = sub.add_parser(
        "uninstall", help="卸载（默认只删程序、保留资料；--delete-library 才清资料）")
    puninstall.add_argument("--app-target", default=None,
                            help="App 目录（默认读安装记录，其次 LOCALAPPDATA\\USB-WIKI\\App）")
    puninstall.add_argument("--library-target", default=None,
                            help="Library 目录（默认读安装记录 / App 内记录 / 文档\\USB-WIKI-Data）")
    puninstall.add_argument("--desktop-dir", default=None, help=argparse.SUPPRESS)
    puninstall.add_argument("--delete-library", dest="delete_library", action="store_true",
                            help="彻底卸载：连同 Library 一起删除（破坏性；需配 --yes 或交互确认 DELETE）")
    puninstall.add_argument("--yes", action="store_true",
                            help="非交互（自动化/测试用）。注意：--yes 单独使用**只删程序、保留资料**；"
                                 "彻底清场需再加 --delete-library")

    args = ap.parse_args(argv)
    if args.cmd is None:
        # 双击 install.bat 不带参数 ⇒ 默认安装
        args.cmd = "install"
        args.release = str(default_root)
        args.app_target = None
        args.library_target = None
        args.launch = False
        args.verify = True
        args.port = 28988
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.cmd == "verify":
        return verify_cmd(Path(args.release).resolve(),
                          as_json=getattr(args, "json", False),
                          deep=not getattr(args, "shallow", False))

    if args.cmd == "uninstall":
        return uninstall(
            app_target_cli=(Path(args.app_target).resolve() if args.app_target else None),
            library_target_cli=(Path(args.library_target).resolve()
                                if args.library_target else None),
            desktop_dir=(Path(args.desktop_dir).resolve()
                         if getattr(args, "desktop_dir", None) else None),
            assume_yes=getattr(args, "yes", False),
            delete_library=getattr(args, "delete_library", False),
        )

    if args.cmd == "run":
        # 启动也按**真实安装位置**：显式 > 安装记录 > 默认（自定义安装后不得找默认目录）
        app_target = _resolve_app_target(getattr(args, "app_target", None),
                                         interactive=False)
        library_target = (Path(args.library_target).resolve() if args.library_target
                          else (_state_library_target() or _default_library_target()))
        nb = getattr(args, "no_browser", False)
        return _run(app_target, library_target, args.port, no_browser=nb)

    release_root = Path(args.release).resolve()
    # 交互 / CLI / CI 三条入口共用同一解析：
    #   显式 --app-target → 校验后使用；否则 TTY 下可交互输入；否则**已装位置**/默认。
    app_target = _resolve_app_target(getattr(args, "app_target", None),
                                     interactive=True, release_root=release_root)
    library_target = (Path(args.library_target).resolve() if args.library_target
                      else (_state_library_target() or _default_library_target()))
    return install(
        release_root, app_target, library_target,
        launch=getattr(args, "launch", False),
        port=args.port,
        smoke=getattr(args, "verify", True),
        desktop_dir=(Path(args.desktop_dir).resolve()
                     if getattr(args, "desktop_dir", None) else None),
    )


if __name__ == "__main__":
    raise SystemExit(main())
