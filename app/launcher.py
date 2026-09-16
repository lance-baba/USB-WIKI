"""Wiki-USB 自愈启动器（PRD 4.1）。

职责：
1. 端口探测与避让（28765 → 28775，连续重试 10 次）
2. 启动前 WAL 残留自愈
3. 先起服务、再唤出浏览器，后台完成重初始化，保证「冷启动 → 页面渲染」体感最快
4. 多层优雅安全退出：前端按钮 / Win32 控制台钩子 / POSIX 信号
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):  # 允许 `python app/launcher.py` 直接运行
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config, db as db_mod, paths  # noqa: E402

from app.core.log_util import ensure_utf8_console  # noqa: E402

# 输出被重定向（管道/CI）时 Windows 会用系统代码页编码 stdout，打印中文会直接崩。
# 在输出任何内容之前切到 UTF-8，不依赖调用方是否设置了 PYTHONUTF8。
ensure_utf8_console()
from app.core.context import get_ctx  # noqa: E402
from app.core.log_util import get_logger, setup  # noqa: E402
from app.server import Server  # noqa: E402

log = get_logger()

PORT_ATTEMPTS = 10
SHUTDOWN_EVENT = threading.Event()
_CALLBACK_REF = []  # 保持 ctypes 回调引用，防止被 GC 回收


# --------------------------------------------------------------------------
def probe_port(host: str, start_port: int, attempts: int = PORT_ATTEMPTS) -> tuple[int, int]:
    """返回 (可用端口, 实际尝试次数)。全部失败抛 OSError。

    注意：**绝不设置 SO_REUSEADDR**。在 Windows 上 SO_REUSEADDR 的语义近似
    SO_REUSEPORT，会让 bind 在端口已被其它进程占用时依然"成功"，既会让避让
    逻辑失效，也可能抢占他人端口。
    """
    last_exc: OSError | None = None
    for i in range(attempts):
        port = start_port + i
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port, i + 1
            except OSError as exc:
                last_exc = exc
                log.warning("端口 %d 被占用（%s），尝试下一个…", port, exc.errno or exc)
    raise OSError(
        f"端口 {start_port}~{start_port + attempts - 1} 连续 {attempts} 次被占用，"
        f"无法启动服务：{last_exc}"
    )


def _install_exit_hooks(on_exit) -> None:
    """注册 POSIX 信号 + Win32 控制台关闭事件，为 I/O 截断争取底层响应窗口。"""
    import signal

    def _posix(signum, _frame):  # pragma: no cover - 需要真实信号
        log.warning("收到信号 %s，开始安全退出…", signum)
        on_exit()
        SHUTDOWN_EVENT.set()

    for sig in ("SIGINT", "SIGTERM", "SIGHUP"):
        s = getattr(signal, sig, None)
        if s is None:
            continue
        try:
            signal.signal(s, _posix)
        except (ValueError, OSError):
            pass

    if os.name == "nt":  # pragma: no cover - Windows 专用
        try:
            import ctypes

            handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

            def _win(ctrl_type: int) -> bool:
                # 0=CTRL_C 1=CTRL_BREAK 2=CTRL_CLOSE 5=LOGOFF 6=SHUTDOWN
                log.warning("捕获控制台退出事件 (type=%d)，执行 WAL 检查点…", ctrl_type)
                on_exit()
                SHUTDOWN_EVENT.set()
                return True

            cb = handler_type(_win)
            _CALLBACK_REF.append(cb)
            ctypes.windll.kernel32.SetConsoleCtrlHandler(cb, True)  # type: ignore[attr-defined]
            log.debug("已注册 Win32 控制台退出回调")
        except Exception as exc:  # noqa: BLE001
            log.warning("Win32 控制台钩子注册失败（不影响前端安全退出）: %s", exc)


def _banner(host: str, port: int, ctx_report: dict | None) -> str:
    # 版本来自单一源 app/version.py —— 这里不再硬编码
    from app.version import APP_NAME, APP_NAME_CN, APP_VERSION  # noqa: PLC0415

    lines = [
        "",
        "  ╔══════════════════════════════════════════════════════╗",
        f"  ║   {APP_NAME}  ·  {APP_NAME_CN} v{APP_VERSION}",
        "  ╚══════════════════════════════════════════════════════╝",
        f"   控制台地址： http://{host}:{port}",
        f"   数据目录　： {paths.DATA_DIR}",
        "   ────────────────────────────────────────────────────",
        "   关闭本窗口 或 点击界面右上角「安全退出」可触发 WAL 检查点并释放锁",
        "",
    ]
    if ctx_report:
        lines.insert(
            6,
            f"   嵌入引擎　： {ctx_report.get('embedding_source')}"
            f"（向量召回{'已启用' if ctx_report.get('vec_ready') else '未启用'}）",
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="Wiki-USB", add_help=True)
    parser.add_argument("--host", default=None, help="监听地址（默认读 config.ini）")
    parser.add_argument("--port", type=int, default=None, help="起始端口（默认读 config.ini）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--no-sync", action="store_true", help="不启动外部文件增量同步")
    parser.add_argument(
        "--allow-lan", action="store_true",
        help="显式允许把本服务暴露到局域网（默认只监听回环，见 README 安全说明）",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    # 让 ctrl-C 生效（即使被批处理脚本套了一层）
    try:
        import signal

        signal.signal(signal.SIGINT, signal.default_int_handler)
    except (ValueError, OSError):
        pass

    setup(config.get_str("SYSTEM", "log_level", "INFO"))
    paths.ensure_dirs()

    host = args.host or config.get_str("SYSTEM", "host", "127.0.0.1")

    # 非回环地址必须**显式**开启 —— 否则用户可能因为改了 config.ini 里的 host，
    # 在不知情的情况下把装着全部私人笔记的服务暴露给整个局域网。
    from app.core import security as _security  # noqa: PLC0415

    if not _security.is_loopback(host) and not args.allow_lan:
        print(
            f"\n  拒绝监听 {host}：该地址会向本机之外暴露整个知识库。"
            "\n  本服务没有登录鉴权，默认只允许本机访问。"
            "\n  确实需要时请显式加上 --allow-lan，并自行确保网络环境可信。\n",
            file=sys.stderr,
        )
        sys.exit(2)
    if not _security.is_loopback(host):
        print(
            f"\n  ⚠️  已按 --allow-lan 监听 {host}：**同一局域网内的任何人都能访问**"
            "\n     你的全部笔记与 API Key 配置。请确认当前网络可信。\n",
            file=sys.stderr,
        )
    start_port = args.port or config.get_int("SYSTEM", "port", 28765)

    # 1) 端口探测与避让
    try:
        port, tries = probe_port(host, start_port)
    except OSError as exc:
        log.error(str(exc))
        print(f"\n  [启动失败] {exc}\n", file=sys.stderr)
        return 2
    if tries > 1:
        log.info("端口避让成功：%d -> %d（尝试 %d 次）", start_port, port, tries)

    # 2) 启动前 WAL 残留自愈（必须早于任何业务连接）
    healed = db_mod.wal_self_heal()
    if healed:
        log.info("启动前完成 WAL 残留自愈")

    ctx = get_ctx()
    shutdown_done = threading.Event()
    lock = threading.Lock()

    def on_exit() -> None:
        with lock:
            if shutdown_done.is_set():
                return
            shutdown_done.set()
        t0 = time.time()
        try:
            ctx.shutdown()
        except Exception as exc:  # noqa: BLE001
            log.error("安全退出异常: %s", exc)
        log.info("安全退出完成，用时 %.0fms", (time.time() - t0) * 1000)
        # ⚠ 必须唤醒主循环。
        # 此前这里只关了库和 HTTP server，**从不设置 SHUTDOWN_EVENT**，
        # 于是主线程永远停在 `while not SHUTDOWN_EVENT.is_set()` 上 ——
        # 表现为「点了安全退出，端口释放了，但进程变成残留」。
        # 信号路径（Ctrl+C / 关窗口）会设置它，所以那个入口一直正常，
        # 掩盖了 API 退出路径的问题。
        SHUTDOWN_EVENT.set()

    # 2.5) 索引结构兼容预检 —— 放在**绑定端口之前**：
    # 「索引由更新版本创建」时必须拒绝启动，而不是带病跑起来再报错。
    from app.core import migrations as _migrations  # noqa: PLC0415
    from app.core import paths as _paths            # noqa: PLC0415
    try:
        _state = _migrations.needs_migration(_paths.CACHE_DB)
    except Exception:                                # noqa: BLE001
        _state = _migrations.FRESH
    if _state == _migrations.DOWNGRADE:
        _probe = _migrations.probe(_paths.CACHE_DB)
        print(
            "\n  无法打开该知识库：索引结构为 " + _probe.version
            + "，而当前程序只支持 " + _migrations.CURRENT_SCHEMA_VERSION + "。"
            + "\n  该索引由更新版本的 USB-WIKI 创建，请升级 USB-WIKI 后再打开。"
            + "\n  （不会自动重建或降级，以免破坏新版本产生的数据状态）\n",
            file=sys.stderr,
        )
        sys.exit(2)
    # 3) 先起服务框架（此时 /api/status 返回 ready:false，前端显示初始化中）
    try:
        server = Server((host, port), ctx, on_shutdown=on_exit,
                        allow_lan=bool(args.allow_lan or not _security.is_loopback(host)))
    except OSError as exc:
        log.error("服务绑定失败: %s", exc)
        print(f"\n  [启动失败] 无法绑定 {host}:{port} —— {exc}\n", file=sys.stderr)
        return 2
    ctx.port = port

    _install_exit_hooks(on_exit)
    server.serve_in_thread()
    url = f"http://{host}:{port}"

    # 4) 唤出浏览器（先让用户看到界面）
    printed = {"done": False}

    def _print_banner(report):
        if not printed["done"]:
            printed["done"] = True
            print(_banner(host, port, report), flush=True)

    _print_banner(None)
    if not args.no_browser:
        threading.Thread(target=_open_browser, args=(url,), daemon=True).start()

    # 5) 后台完成重量级初始化（数据库/嵌入源/同步器）
    boot_error: list[str] = []

    def _boot():
        try:
            report = ctx.boot(start_syncer=not args.no_sync)
            log.info("初始化完成: %s", report)
        except Exception as exc:  # noqa: BLE001
            boot_error.append(str(exc))
            log.error("初始化失败: %s", exc)

    boot_thread = threading.Thread(target=_boot, name="boot", daemon=True)
    boot_thread.start()
    boot_thread.join(timeout=60)
    _print_banner(ctx.boot_report)

    if boot_error:
        print(f"\n  [初始化异常] {boot_error[0]}\n", file=sys.stderr)

    # 6) 主线程等待退出信号
    try:
        while not SHUTDOWN_EVENT.is_set():
            SHUTDOWN_EVENT.wait(0.5)
    except KeyboardInterrupt:  # pragma: no cover
        pass
    on_exit()
    try:
        server.shutdown()
        server.server_close()
    except Exception:  # noqa: BLE001
        pass
    return 0


def _open_browser(url: str) -> None:
    for _ in range(20):
        try:
            with socket.create_connection(
                (urllib_host(url), urllib_port(url)), timeout=0.4
            ):
                break
        except OSError:
            time.sleep(0.1)
    try:
        import webbrowser

        webbrowser.open_new_tab(url)
    except Exception as exc:  # noqa: BLE001
        log.warning("浏览器唤出失败，请手动访问 %s (%s)", url, exc)


def urllib_host(url: str) -> str:
    return url.split("//", 1)[-1].split(":")[0]


def urllib_port(url: str) -> int:
    try:
        return int(url.rsplit(":", 1)[-1].split("/")[0])
    except ValueError:
        return 80


if __name__ == "__main__":
    raise SystemExit(main())
