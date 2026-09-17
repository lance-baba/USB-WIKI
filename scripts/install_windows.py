#!/usr/bin/env python3
"""SSD 安装骨架（Windows）。

职责（A1 范围，仅做骨架）：
  1. 校验 payload 完整性
       - 缺 app/                  → 明确失败（非零退出）
       - 缺 python-runtime/python.exe → 明确失败（非零退出）
  2. 拷贝 payload → App 目标目录
       - App / Runtime **可以覆盖**
  3. 创建 / 接管 Library（默认 %USERPROFILE%\\Documents\\USB-WIKI-Data）
       - Library **绝不覆盖、绝不删除**；已存在则接管
  4. 仅给**启动进程**设置进程级 WIKIUSB_LIBRARY
       - **不写系统全局环境变量**
  5. 用自带嵌入式 Python 启动 app/launcher.py

硬性边界（V1 Freeze / A1 范围）：
  - 纯标准库实现，零 pip、零联网、零外部二进制下载。
  - 不为「把架构做完整」提前决定 Ollama / GGUF / ONNX / LLM 来源（留作开放决策）。
  - 不实现 Repair Engine / LICENSES 汇总 / BUILD_INFO(最终) / SHA256SUMS(最终)。

设计要点：嵌入式 Python 的 ._pth 只含 exe 目录（.）、Lib/site-packages，因此
app 模块靠「以 app/launcher.py 为脚本启动时把脚本目录加入 sys.path[0]」被发现；
安装器把 payload/python-runtime 映射到 App/runtime，使 paths.py 的
BASE_DIR(__file__.parents[2]) 与 RUNTIME_DIR(BASE_DIR/runtime) 零改动即可工作。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _default_app_target() -> Path:
    # 冻结决策 #2：App 默认落 LOCALAPPDATA\USB-WIKI\App（SSD 安装态）
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "USB-WIKI" / "App"
    return Path.home() / "USB-WIKI" / "App"


def _default_library_target() -> Path:
    # 冻结决策 #2：Library 默认落 文档\USB-WIKI-Data，经 WIKIUSB_LIBRARY 重定向
    return Path.home() / "Documents" / "USB-WIKI-Data"


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


def _write_launcher(app_target: Path, library_target: Path) -> None:
    # 仅给本启动脚本设置进程级 WIKIUSB_LIBRARY（不写全局环境变量）。
    # 把 Library 路径记到安装目录内的 library_path.txt，bat 启动时读取，
    # 这样「重装 / 手动再启动」都能找到同一份 Library，而无需改动系统环境。
    (app_target / "library_path.txt").write_text(
        str(library_target), encoding="utf-8")
    (app_target / "启动-Windows.bat").write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "cd /d \"%~dp0\"\r\n"
        "if exist library_path.txt (\r\n"
        "  set /p WIKIUSB_LIBRARY=<library_path.txt\r\n"
        ")\r\n"
        "runtime\\python.exe app\\launcher.py --no-browser %*\r\n",
        encoding="utf-8",
    )


def install(payload: Path, app_target: Path, library_target: Path,
           launch: bool, port: int) -> int:
    validate_payload(payload)

    # ---- App / Runtime：可覆盖 ----
    app_target.mkdir(parents=True, exist_ok=True)
    app_dst = app_target / "app"
    rt_dst = app_target / "runtime"          # 映射 python-runtime -> runtime
    if app_dst.exists():
        shutil.rmtree(app_dst)
    if rt_dst.exists():
        shutil.rmtree(rt_dst)
    shutil.copytree(payload / "app", app_dst)
    shutil.copytree(payload / "python-runtime", rt_dst)

    # ---- Library：绝不覆盖 / 绝不删除 ----
    if library_target.exists():
        print(f"[install] 检测到已有 Library，直接接管（不覆盖、不删除）：{library_target}")
    else:
        library_target.mkdir(parents=True, exist_ok=True)
        print(f"[install] 已创建 Library：{library_target}")

    _write_launcher(app_target, library_target)

    print(f"[install] App 已安装到：{app_target}")
    if launch:
        return _run(app_target, library_target, port)
    return 0


def _run(app_target: Path, library_target: Path, port: int) -> int:
    exe = app_target / "runtime" / "python.exe"
    if not exe.is_file():
        fail(f"嵌入式 Python 不存在：{exe}")
    # 仅给本启动进程设置 WIKIUSB_LIBRARY（进程级，不写全局）
    env = {
        **os.environ,
        "WIKIUSB_LIBRARY": str(library_target),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    proc = subprocess.Popen(
        [str(exe), "app/launcher.py", "--no-browser", "--port", str(port)],
        cwd=str(app_target), env=env,
    )
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    return proc.returncode


def _parse(argv: list[str]):
    here = Path(__file__).resolve().parent
    default_payload = here.parent / "payload"      # installer/../payload

    ap = argparse.ArgumentParser(description="USB-WIKI SSD 安装骨架")
    sub = ap.add_subparsers(dest="cmd")

    pinstall = sub.add_parser("install", help="安装到 SSD")
    pinstall.add_argument("--source", default=str(default_payload), help="payload 目录")
    pinstall.add_argument("--app-target", default=None,
                          help="App 安装目录（默认 LOCALAPPDATA\\USB-WIKI\\App）")
    pinstall.add_argument("--library-target", default=None,
                          help="Library 目录（默认 文档\\USB-WIKI-Data）")
    pinstall.add_argument("--launch", action="store_true", help="安装后启动服务")
    pinstall.add_argument("--port", type=int, default=28988)

    prun = sub.add_parser("run", help="仅启动（需先 install）")
    prun.add_argument("--app-target", default=None)
    prun.add_argument("--library-target", default=None)
    prun.add_argument("--port", type=int, default=28988)

    args = ap.parse_args(argv)
    if args.cmd is None:
        # 默认动作 = install（不带 launch）
        args.cmd = "install"
        args.source = str(default_payload)
        args.app_target = None
        args.library_target = None
        args.launch = False
        args.port = 28988
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    app_target = Path(args.app_target).resolve() if args.app_target else _default_app_target()
    library_target = (Path(args.library_target).resolve()
                      if args.library_target else _default_library_target())

    if args.cmd == "run":
        return _run(app_target, library_target, args.port)
    return install(Path(args.source).resolve(), app_target, library_target,
                   getattr(args, "launch", False), args.port)


if __name__ == "__main__":
    raise SystemExit(main())
