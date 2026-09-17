r"""Wiki-USB 路径解析 —— 全部基于安装根目录的相对解析，杜绝绝对盘符绑定。

U 盘盘符在不同机器上会变化（E:\ F:\ G:\ ...），因此任何路径都不得落盘为绝对路径。
所有路径一律由本模块以 ``__file__`` 为锚点动态推导。
"""
from __future__ import annotations

import sys
import os
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

def _detect_data_dir(base: Path) -> Path:
    """资料库根的解析：环境变量优先，默认跟着程序目录。

    ⚠ 这是 **App 与 Library 的边界**：环境变量 ``WIKIUSB_LIBRARY`` 可把资料库
    指到任意位置（另一块盘 / 另一台电脑），程序目录即使整个删除，
    资料库仍可被新程序接管。默认值保持向后兼容（不强行搬目录）。
    """
    env = (os.environ.get("WIKIUSB_LIBRARY") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return (base / "data").resolve()


DATA_DIR: Path = _detect_data_dir(BASE_DIR)
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

# --- 随包本地嵌入资源（属于 App，**不属于 Library**）---
# 可随程序重装 / 覆盖 / rollback / 从 U 盘恢复；Library 不受影响。
#
# ⚠ **不写死任何模型文件名**。资源由 `resources/embedding/artifact.json` 描述，
#   Core 只认识「id / artifact 路径 / tokenizer 路径 / 维度 / 精度 / hash」——
#   换 artifact 只需换资源目录里的字节与清单，不需要改代码
#   （此前 `ONNX_MODEL_FILE = .../bge-small-zh-q4.onnx` 把发布文件名焊死在 Core 里）。
EMBEDDING_ARTIFACT_NAME = "artifact.json"


def _detect_embedding_dir(base: Path) -> Path:
    """嵌入资源目录的解析。

    默认 ``<App>/resources/embedding``（安装态由安装器铺好）。
    ``WIKIUSB_EMBEDDING_DIR`` 可覆盖：开发机指向 ``vendor/cache/embedding``，
    测试指向临时资源目录。**该目录缺失是正常状态**（未随包 / 未取件），
    由 embedder 报告 `local_onnx unavailable` 并按契约降级，不在这里偷偷创建。
    """
    env = (os.environ.get("WIKIUSB_EMBEDDING_DIR") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return (base / "resources" / "embedding").resolve()


EMBEDDING_DIR: Path = _detect_embedding_dir(BASE_DIR)
EMBEDDING_ARTIFACT_JSON: Path = EMBEDDING_DIR / EMBEDDING_ARTIFACT_NAME

CONFIG_FILE: Path = BASE_DIR / "config.ini"
LOG_FILE: Path = DATA_DIR / "wiki-usb.log"

# 相对 data/ 的展示前缀（写入 Markdown frontmatter / API 返回值时统一使用）
NOTES_REL_PREFIX = "notes"


def ensure_dirs() -> None:
    """确保运行期必需的目录存在（含曾被清理过的 data 树）。

    刻意**不**创建 ``EMBEDDING_DIR``：嵌入资源是否存在是事实信息，
    空目录会让「未随包」与「随包但文件缺失」看起来一样。
    """
    for d in (DATA_DIR, NOTES_DIR, SNAPSHOT_DIR, ORIGINALS_DIR, ASSETS_DIR, RUNTIME_DIR):
        d.mkdir(parents=True, exist_ok=True)


def rel_to_data(p: Path) -> str:
    """把绝对路径转换成相对 ``data/`` 的 POSIX 风格路径，用于跨机器持久化。

    ⚠ **绝不返回绝对路径** —— 那会让资料库无法整盘搬移（Data Contract 第 6 条）。
    Library 根被重定向时（如恢复测试 / WIKIUSB_LIBRARY），文件不在 DATA_DIR 下，
    此前 ``except ValueError`` 直接返回 ``p.as_posix()``（绝对路径），
    导致 CI 的换目录测试失败。现按优先级退回相对形式：
    data/ 相对 → notes/ 相对 → 仅文件名。
    """
    p = p.resolve()
    try:
        return p.relative_to(DATA_DIR.resolve()).as_posix()
    except ValueError:
        pass
    try:
        return "notes/" + p.relative_to(NOTES_DIR.resolve()).as_posix()
    except ValueError:
        pass
    return "notes/" + p.name


def abs_from_data(rel: str) -> Path:
    """把 ``notes/xxx.md`` 之类的相对路径还原成本机绝对路径。"""
    rel = rel.replace("\\", "/").lstrip("/")
    if rel.startswith("data/"):
        rel = rel[5:]
    return (DATA_DIR / rel).resolve()
