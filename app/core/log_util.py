"""轻量日志 —— 输出到控制台与 data/wiki-usb.log，全程 UTF-8，容忍控制台编码退化。"""
from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler

from . import paths

_LOGGER_NAME = "wikiusb"
_configured = False


class _RedactingFormatter(logging.Formatter):
    """把**每一条日志**都过一遍脱敏。

    为什么放在 Formatter 而不是逐个 log 调用点：日志路径太分散，
    逐个改必然漏（第三方异常回显、proxy 报错、debug dump…）。
    收口在这里，任何来源的字符串都会被清洗。
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: D102
        try:
            from . import redact  # noqa: PLC0415 - 延迟导入避免环

            # 必须**在副本上**改：旧实现直接改 record.args，把 Mapping 强转成 keys 元组，
            # 于是 log.info("初始化完成: %s", report) 变成 "%s" % ('app_version', ...)
            # → TypeError: not all arguments converted during string formatting。
            # 而且 record 已被写坏，except 里的 super().format(record) 会**再挂一次**。
            safe = logging.makeLogRecord(record.__dict__)
            safe.msg = redact.sanitize_text(str(record.msg))
            args = record.args
            if isinstance(args, Mapping):
                # logging 对「单个 Mapping 参数」有**特殊语义**（%(key)s 插值）——
                # 必须保持 Mapping 类型，只逐 value 脱敏。
                safe.args = {
                    k: (redact.sanitize_text(v) if isinstance(v, str) else v)
                    for k, v in args.items()
                }
            elif isinstance(args, tuple):
                safe.args = tuple(
                    redact.sanitize_text(a) if isinstance(a, str) else a for a in args
                )
            else:
                safe.args = redact.sanitize_text(args) if isinstance(args, str) else args
            return redact.sanitize_text(super().format(safe))
        except Exception:  # noqa: BLE001 - 脱敏失败不能影响日志本身
            # 用**未改动**的原 record 副本兜底（绝不能复用被 mutate 过的 record）
            return logging.Formatter.format(
                self, logging.makeLogRecord(record.__dict__)
            )


class _SafeStreamHandler(logging.StreamHandler):
    """控制台编码不兼容时（老式 CP936 终端）退化为可读转义，绝不抛 UnicodeEncodeError。"""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - 平台相关
        try:
            super().emit(record)
        except UnicodeEncodeError:
            try:
                msg = self.format(record)
                enc = getattr(self.stream, "encoding", None) or "ascii"
                safe = msg.encode(enc, errors="replace").decode(enc, errors="replace")
                self.stream.write(safe + self.terminator)
                self.flush()
            except Exception:
                self.handleError(record)
        except Exception:
            self.handleError(record)


def ensure_utf8_console() -> None:
    """把 stdout / stderr 切到 UTF-8，使中文与符号在任意机器、任意代码页下都能输出。

    为什么必须做：Windows 上当输出被**重定向**（管道 / CI / 写入文件）时，Python 会用
    系统区域代码页编码 stdout —— 英文 Windows 是 cp1252、中文是 cp936。此时打印中文或
    ``✅`` 会抛 ``UnicodeEncodeError`` 并**直接中断程序**（实测：不设 PYTHONUTF8 时
    测试套件以 exit=1 崩掉）。真实控制台走 WindowsConsoleIO，本已是 UTF-8，不受影响。

    因此不依赖调用方"记得设 PYTHONUTF8"，由代码自己保证。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass      # 流不支持重配置（如被替换为自定义对象）时静默跳过


def setup(level: str = "INFO") -> logging.Logger:
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    if _configured:
        return logger

    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    console = _SafeStreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    try:
        paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            paths.LOG_FILE, maxBytes=512 * 1024, backupCount=1, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        # U 盘只读/被占用时不得因为日志而阻断启动
        pass

    logger.propagate = False
    _configured = True
    return logger


def _attach_redacting_formatter(logger: logging.Logger) -> None:
    """给所有 handler 挂上脱敏 Formatter（只挂一次）。"""
    for h in logger.handlers:
        if not isinstance(h.formatter, _RedactingFormatter):
            h.setFormatter(_RedactingFormatter("%(message)s"))


def get_logger() -> logging.Logger:
    logger = setup()
    _attach_redacting_formatter(logger)      # 所有日志过一遍脱敏
    return logger
