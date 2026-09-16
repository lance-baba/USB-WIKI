"""USB-WIKI Library：用户**永久资料**的边界与协议。

## 两个必须分清的东西

    USB-WIKI App       程序目录 / Python 运行时 / 依赖 / 模型 —— **可以整个删除**
    USB-WIKI Library   用户在 data/ 下的资料                  —— **不可以丢**

本项目此前把两者绑在一起：`DATA_DIR = BASE_DIR / "data"`。
删掉程序目录，用户的笔记与原件也跟着没了。本模块建立边界：

    LIBRARY_ROOT（资料库根）
      ├─ library.json     格式与兼容性的最小协议
      ├─ notes/           永久：开放 Markdown
      ├─ originals/       永久：用户上传的原件
      ├─ assets/          永久：网页归档资产（网页可能已消失）
      ├─ snapshots/       永久：降级抓取的原始快照
      ├─ cache.db         可重建：纯派生索引
      └─ wiki-usb.log     临时：可删

## 终局目标（Data Contract V1）

> 只要 Library 文件夹还在，即使程序、运行时、Ollama、模型、SQLite、FTS、
> 向量索引、配置**全部丢失**，最新版程序都能从它恢复出一个可工作的知识库。

## 什么绝不能进 library.json

API key / token / 绝对路径 / cache 状态 / 模型路径 / Ollama 状态 / 端口 / 机器名。
它只是**识别资料库格式与兼容性**的最小协议，不是数据库、也不是配置。
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .log_util import get_logger

log = get_logger()

# 资料库格式版本。
# ⚠ 三个版本**严格独立**，禁止互相派生：
#     APP_VERSION          程序版本，可以频繁升（1.3.0 → 1.3.1）
#     SCHEMA_VERSION       cache.db 索引结构版本，可以频繁升（坏了删了重建）
#     DATA_FORMAT_VERSION  永久用户资料格式，**整个项目里最谨慎升级**
# 三者之间任何 `X = Y` 的赋值都是设计错误。
LIBRARY_FORMAT = "usb-wiki-library"
DATA_FORMAT_VERSION = 1

MANIFEST_NAME = "library.json"

# Library 内的永久子目录（相对路径，永不落绝对路径）
PERMANENT_DIRS = ("notes", "originals", "assets", "snapshots")

_ENV_ROOT = "WIKIUSB_LIBRARY"


class DataFormatTooNewError(RuntimeError):
    """资料库由更新版本的程序创建 —— 拒绝写入。

    与「索引比程序新」同理：旧程序不该擅自改写新程序的资料格式。
    提示由调用方展示，这里是保护性失败。
    """

    def __init__(self, lib_version: int, app_version: int) -> None:
        self.lib_version = lib_version
        self.app_version = app_version
        super().__init__(
            f"此资料库由更新版本的 USB-WIKI 创建（资料格式 {lib_version}，"
            f"当前程序支持 {app_version}）。请升级程序后使用。"
        )


@dataclass
class Library:
    """资料库句柄。所有路径都是**相对 Library 根**推导出来的。"""

    root: Path

    # ---- 永久资料 ----
    @property
    def notes_dir(self) -> Path:
        return self.root / "notes"

    @property
    def originals_dir(self) -> Path:
        return self.root / "originals"

    @property
    def assets_dir(self) -> Path:
        return self.root / "assets"

    @property
    def snapshots_dir(self) -> Path:
        return self.root / "snapshots"

    # ---- 可重建 / 临时 ----
    @property
    def cache_db(self) -> Path:
        return self.root / "cache.db"

    @property
    def log_file(self) -> Path:
        return self.root / "wiki-usb.log"

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    # ---- manifest ----
    def read_manifest(self) -> dict:
        """读 library.json；不存在或损坏时返回空 dict（由 ensure 决定怎么建）。"""
        p = self.manifest_path
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError) as exc:
            log.warning("library.json 无法解析（将重新生成）: %s", exc)
            return {}

    def write_manifest(self, *, created_by: str) -> dict:
        """写入 / 更新 library.json。**只放格式识别所需的最小信息。**"""
        cur = self.read_manifest()
        data = {
            "format": LIBRARY_FORMAT,
            "data_version": DATA_FORMAT_VERSION,
            "created_by": cur.get("created_by") or created_by,
            "created_at": cur.get("created_at") or datetime.now().isoformat(timespec="seconds"),
            "library_id": cur.get("library_id") or uuid.uuid4().hex[:16],
        }
        # 原子写：manifest 也是用户资料的一部分，写坏等于资料库打不开
        from . import atomic_io  # noqa: PLC0415

        atomic_io.atomic_write_text(
            self.manifest_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        )
        return data

    def ensure(self, *, created_by: str) -> dict:
        """确保目录齐备 + manifest 存在 + **格式版本兼容**。

        遇到比自己新的资料格式直接拒绝（:class:`DataFormatTooNewError`）。
        """
        for d in PERMANENT_DIRS:
            (self.root / d).mkdir(parents=True, exist_ok=True)
        m = self.read_manifest()

        ver = m.get("data_version")
        if isinstance(ver, int) and ver > DATA_FORMAT_VERSION:
            raise DataFormatTooNewError(ver, DATA_FORMAT_VERSION)
        fmt = m.get("format")
        if fmt and fmt != LIBRARY_FORMAT:
            # 不是我们的资料库（用户指错了目录）—— 不擅自改动
            raise ValueError(f"该目录不是 USB-WIKI 资料库（format={fmt!r}）")

        if not m:
            return self.write_manifest(created_by=created_by)
        # 版本更旧：留给将来的 migration（本轮不动）
        return m

    # ---- 相对路径（永久 metadata 只允许相对路径）----
    def rel(self, path: Path | str) -> str:
        """把 Library 内的路径转成**相对路径**字符串。

        永久 metadata 里禁止出现 `C:\\Users\\...` —— 资料库可能被整个挪盘，
        绝对路径会让所有引用失效。
        """
        p = Path(path)
        try:
            return p.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            # 不在 Library 内：不返回绝对路径，避免它被写进永久 metadata
            return p.name

    def abs(self, rel_path: str) -> Path:
        """相对路径 → 绝对路径（只在运行时用，不落盘）。"""
        return (self.root / str(rel_path).lstrip("/")).resolve()


def detect_library_root(base_dir: Path) -> Path:
    """资料库根的解析顺序：

    1. 环境变量 ``WIKIUSB_LIBRARY`` —— 便携/多资料库/测试隔离用
    2. 默认 ``<程序目录>/data``（保持向后兼容，不强行搬目录）

    ⚠ 刻意**不默认放到用户主目录**：本产品是「随身」形态，资料跟着程序走是合理的；
    但通过环境变量可以把它指到任意位置（含另一块盘），这就是 App 与 Library 的边界。
    """
    env = (os.environ.get(_ENV_ROOT) or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return (base_dir / "data").resolve()
