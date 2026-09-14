"""轻量日志 —— 输出到控制台与 data/wiki-usb.log，全程 UTF-8，容忍控制台编码退化。"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from . import paths

_LOGGER_NAME = "wikiusb"
_configured = False


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


def get_logger() -> logging.Logger:
    return setup()
