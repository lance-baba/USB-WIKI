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


class ManifestCorruptedError(RuntimeError):
    """library.json **存在但无法解析**。

    ⚠「已有资料 + manifest 损坏」与「空白新库」完全不是一回事，
    绝不能解析失败 → 当新库 → 自动生成新 manifest（那会丢掉 library_id
    并掩盖真实状态）。恢复必须走**显式**的 :meth:`Library.repair_manifest`。
    """

    code = "MANIFEST_CORRUPTED"

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(
            "library.json 已损坏，为保护资料未做任何修改。"
            "请修复该文件，或显式调用 repair_manifest() 重建"
            + (f"（{detail}）" if detail else "")
        )


# 历史 USB-WIKI 资料库：有 notes/originals/assets 等资料结构，但没有 manifest。
LEGACY_DATA_FORMAT = 0


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
        """读 library.json。

        * 不存在 → 返回 ``{}``（是否是空白新库由 :meth:`ensure` 结合资料判断）
        * **存在但损坏 → 抛** :class:`ManifestCorruptedError`，绝不静默当新库
        """
        p = self.manifest_path
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ManifestCorruptedError(str(exc)[:120]) from exc
        if not isinstance(data, dict):
            raise ManifestCorruptedError("顶层不是 JSON 对象")
        return data

    def write_manifest(self, *, created_by: str, force: bool = False) -> dict:
        """写入 / 更新 library.json。**只放格式识别所需的最小信息。**

        :param force: 显式重建（仅限 :meth:`repair_manifest` 调用）。
            损坏的 manifest 不能被常规写入路径悄悄覆盖。
        """
        cur = {} if force else self.read_manifest()
        data = {
            "format": LIBRARY_FORMAT,
            "data_version": DATA_FORMAT_VERSION,
            "created_by": cur.get("created_by") or created_by,
            "created_at": cur.get("created_at") or datetime.now().isoformat(timespec="seconds"),
            "library_id": cur.get("library_id") or uuid.uuid4().hex[:16],
        }
        # 保留当前程序不认识的未知字段：读取可忽略，但**写回时不得丢弃**，
        # 否则「新版程序读一次 → 未知字段被抹掉」就成了一种静默降级。
        for k, v in cur.items():
            if k not in data:
                data[k] = v
        # 原子写：manifest 也是用户资料的一部分，写坏等于资料库打不开
        from . import atomic_io  # noqa: PLC0415

        atomic_io.atomic_write_text(
            self.manifest_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        )
        return data

    def repair_manifest(self, *, created_by: str) -> dict:
        """**显式**重建损坏的 manifest（供将来「修复资料库」UI 调用）。

        ⚠ 绝不允许由 :meth:`ensure` 自动触发 —— 自动重建会生成新的
        ``library_id`` 并掩盖「资料库曾经损坏」这一事实。
        """
        log.warning("显式重建 library.json（repair_manifest），created_by=%s", created_by)
        return self.write_manifest(created_by=created_by, force=True)

    def _has_permanent_data(self) -> bool:
        """永久目录里是否已有任何资料（用于区分空白新库 / Legacy V0）。"""
        for d in PERMANENT_DIRS:
            p = self.root / d
            if p.is_dir() and any(p.iterdir()):
                return True
        return False

    def ensure(self, *, created_by: str) -> dict:
        """确保目录齐备 + manifest 存在 + **格式版本兼容**。

        三种互斥的入口状态（严格区分，见 Data Contract）：

        * ``library.json`` 损坏 → :class:`ManifestCorruptedError`（**先于任何写操作**）
        * 无 manifest 但已有资料 → Legacy Format 0，安全接管为 V1（幂等）
        * 无 manifest 且空目录 → 全新 V1
        """
        # ① 先读 manifest：损坏必须在**任何 mkdir/写操作之前**失败
        m = self.read_manifest()

        # ② 再建目录
        for d in PERMANENT_DIRS:
            (self.root / d).mkdir(parents=True, exist_ok=True)

        # ③ 无 manifest：区分空白新库 / Legacy V0
        if not m:
            if self._has_permanent_data():
                log.info("检测到无 manifest 的历史资料库（Data Format %d），安全接管为 V1",
                         LEGACY_DATA_FORMAT)
                # 只新增 library.json；notes/originals/assets/snapshots 一律不动
                return self.write_manifest(created_by=created_by)
            return self.write_manifest(created_by=created_by)

        # ④ 有效 manifest：检查兼容性，然后**原样返回，绝不回写**
        ver = m.get("data_version")
        if isinstance(ver, int) and ver > DATA_FORMAT_VERSION:
            raise DataFormatTooNewError(ver, DATA_FORMAT_VERSION)
        fmt = m.get("format")
        if fmt and fmt != LIBRARY_FORMAT:
            # 不是我们的资料库（用户指错了目录）—— 不擅自改动
            raise ValueError(f"该目录不是 USB-WIKI 资料库（format={fmt!r}）")
        # 版本更旧：留给将来的 migration（本轮不动）
        # ⚠ 未知字段 / 未来字段：读取时忽略即可，**不得读一次就把文件重写成
        #   当前程序的简化格式**（否则旧程序+新 manifest 会静默降级）。
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
