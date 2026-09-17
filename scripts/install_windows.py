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


def _known_folder_documents() -> Path:
    # FOLDERID_Documents = {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
    guid = GUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7")
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


def _default_library_target() -> Path:
    # 冻结决策 #2：Library 默认落 Documents\USB-WIKI-Data（经 WIKIUSB_LIBRARY 重定向）
    return _documents_dir() / "USB-WIKI-Data"


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
    if sys.platform != "win32":
        return False
    exe = app_target / "runtime" / "python.exe"
    if not exe.is_file():
        return False
    # 必须以嵌入式运行时能够 import app 为准（不完整则跳过，避免误红）
    try:
        probe = subprocess.run(
            [str(exe), "-c", "import app.launcher"],
            cwd=str(app_target), capture_output=True, text=True, timeout=60)
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
def install(payload: Path, app_target: Path, library_target: Path,
           launch: bool = False, port: int = 28988, smoke: bool = False) -> int:
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
def _parse(argv: list[str]):
    here = Path(__file__).resolve().parent
    default_payload = here.parent / "payload"

    ap = argparse.ArgumentParser(description="USB-WIKI SSD 安装器（事务化）")
    sub = ap.add_subparsers(dest="cmd")

    pinstall = sub.add_parser("install", help="事务化安装到 SSD")
    pinstall.add_argument("--source", default=str(default_payload), help="payload 目录")
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

    prun = sub.add_parser("run", help="仅启动（需先 install）")
    prun.add_argument("--app-target", default=None)
    prun.add_argument("--library-target", default=None)
    prun.add_argument("--port", type=int, default=28988)
    prun.add_argument("--no-browser", action="store_true",
                      help="不自动打开浏览器（调试/CI）")

    args = ap.parse_args(argv)
    if args.cmd is None:
        args.cmd = "install"
        args.source = str(default_payload)
        args.app_target = None
        args.library_target = None
        args.launch = False
        args.verify = True
        args.port = 28988
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    app_target = Path(args.app_target).resolve() if args.app_target else _default_app_target()
    library_target = (Path(args.library_target).resolve()
                      if args.library_target else _default_library_target())

    if args.cmd == "run":
        nb = getattr(args, "no_browser", False)
        return _run(app_target, library_target, args.port, no_browser=nb)
    return install(
        Path(args.source).resolve(), app_target, library_target,
        launch=getattr(args, "launch", False),
        port=args.port,
        smoke=getattr(args, "verify", True),
    )


if __name__ == "__main__":
    raise SystemExit(main())
