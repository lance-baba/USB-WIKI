r"""Wiki-USB 路径解析 —— 全部基于安装根目录的相对解析，杜绝绝对盘符绑定。

U 盘盘符在不同机器上会变化（E:\ F:\ G:\ ...），因此任何路径都不得落盘为绝对路径。
所有路径一律由本模块以 ``__file__`` 为锚点动态推导。
"""
from __future__ import annotations

import sys
from pathlib import Path


def _detect_base_dir() -> Path:
    """定位 Wiki-USB 安装根目录。

    冻结（PyInstaller/Nuitka）场景取可执行文件所在目录；源码场景取 app/core 的上两级。
    """
    if getattr(sys, "frozen", False):  # pragma: no cover - 冻结发行版专用分支
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


BASE_DIR: Path = _detect_base_dir()

APP_DIR: Path = BASE_DIR / "app"
CORE_DIR: Path = APP_DIR / "core"
WEB_DIR: Path = APP_DIR / "web"
VENDOR_DIR: Path = WEB_DIR / "vendor"

DATA_DIR: Path = BASE_DIR / "data"
NOTES_DIR: Path = DATA_DIR / "notes"
SNAPSHOT_DIR: Path = DATA_DIR / "snapshots"
# 原文件留存：导入的 PDF/Office 原件、剪藏的原始 HTML。
# 让笔记界面能用浏览器原生查看器还原「原版」观感，而 Markdown 只承担可检索的职责。
ORIGINALS_DIR: Path = DATA_DIR / "originals"
# 网页存档的**共享资源池**：剪藏时把 CSS/图片/字体等子资源抓下来存这里，
# 按 URL 哈希命名 → 同一站点的样式与 logo 被多篇文章共用时只存一份。
# 这样「原版预览」才能在不联网的前提下还原版式（Local-First / 零外发）。
ASSETS_DIR: Path = DATA_DIR / "assets"
CACHE_DB: Path = DATA_DIR / "cache.db"
WAL_FILE: Path = DATA_DIR / "cache.db-wal"
SHM_FILE: Path = DATA_DIR / "cache.db-shm"
CHROME_PROFILE_DIR: Path = DATA_DIR / "temp_chrome_profile"

RUNTIME_DIR: Path = BASE_DIR / "runtime"
EMBED_MODELS_DIR: Path = RUNTIME_DIR / "models"
ONNX_MODEL_FILE: Path = EMBED_MODELS_DIR / "bge-small-zh-q4.onnx"

CONFIG_FILE: Path = BASE_DIR / "config.ini"
LOG_FILE: Path = DATA_DIR / "wiki-usb.log"

# 相对 data/ 的展示前缀（写入 Markdown frontmatter / API 返回值时统一使用）
NOTES_REL_PREFIX = "notes"


def ensure_dirs() -> None:
    """确保运行期必需的目录存在（含曾被清理过的 data 树）。"""
    for d in (DATA_DIR, NOTES_DIR, SNAPSHOT_DIR, ORIGINALS_DIR, ASSETS_DIR, RUNTIME_DIR, EMBED_MODELS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def rel_to_data(p: Path) -> str:
    """把绝对路径转换成相对 ``data/`` 的 POSIX 风格路径，用于跨机器持久化。"""
    try:
        return p.resolve().relative_to(DATA_DIR.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def abs_from_data(rel: str) -> Path:
    """把 ``notes/xxx.md`` 之类的相对路径还原成本机绝对路径。"""
    rel = rel.replace("\\", "/").lstrip("/")
    if rel.startswith("data/"):
        rel = rel[5:]
    return (DATA_DIR / rel).resolve()
