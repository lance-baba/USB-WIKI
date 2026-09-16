"""文件摄入边界：统一处理不可信文件名、路径与压缩容器。

## 为什么必须集中一处，而不是各模块自己判 `".." not in path`

本项目有多个入口会接触**外部来源**的文件名与容器：

    用户上传(Office/EPUB/PDF/CSV) → converters → notes / originals
    网页剪藏原件                 → crawler.save_original → originals
    ZIP(OONNX/EPUB) 内部 entry   → converters 读取

只要有一个入口漏判，边界就破了。而且**字符串判断根本不安全**：

* `".." not in name` 挡不住 `..\\evil`（Windows 反斜杠）
* 挡不住绝对路径 `/etc/passwd`、`C:\\Windows\\...`、UNC `\\\\server\\share`
* 挡不住 `....//`、`a/../../b`、混合分隔符、重复分隔符
* 挡不住 NUL 截断与奇怪 Unicode

所以统一用「**解析后判定**」：把候选路径 `resolve()` 出来，确认它**真的**位于
允许根目录之内（:func:`safe_join`）。

## Windows 优先

本产品以 Windows 为主要平台，因此 **盘符 / UNC / 反斜杠** 是一等测试场景，
不是补充说明。判定时把 `\\` 也当作分隔符处理，并识别 `X:` 盘符与 UNC 前缀。

## 压缩容器：两层限制

ZIP 炸弹的防护**不能只看中央目录里的声明值**（`ZipInfo.file_size` 是攻击者可写的）：

1. **预检查**：entry 数、单 entry 声明大小、声明总量、压缩比
2. **实际限流**：读取时累计**真实输出字节**，超限立即停

两层都要有。另外拒绝重复 entry 名与特殊文件类型（symlink / device 等）——
USB-WIKI 没有理由从 Office/EPUB 里恢复符号链接。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# 错误码（前端据此给人话，日志不 dump 文件内容）
# --------------------------------------------------------------------------
PATH_TRAVERSAL = "PATH_TRAVERSAL"
INVALID_FILENAME = "INVALID_FILENAME"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
ARCHIVE_TOO_MANY_ENTRIES = "ARCHIVE_TOO_MANY_ENTRIES"
ARCHIVE_ENTRY_TOO_LARGE = "ARCHIVE_ENTRY_TOO_LARGE"
ARCHIVE_TOTAL_TOO_LARGE = "ARCHIVE_TOTAL_TOO_LARGE"
ARCHIVE_RATIO_TOO_HIGH = "ARCHIVE_RATIO_TOO_HIGH"
ARCHIVE_SPECIAL_FILE = "ARCHIVE_SPECIAL_FILE"
ARCHIVE_DUPLICATE_ENTRY = "ARCHIVE_DUPLICATE_ENTRY"
ARCHIVE_INVALID_ENTRY = "ARCHIVE_INVALID_ENTRY"

REASON_TEXT = {
    PATH_TRAVERSAL: "文件名或压缩包内路径试图跳出允许的目录",
    INVALID_FILENAME: "文件名不合法",
    FILE_TOO_LARGE: "文件超过大小上限",
    ARCHIVE_TOO_MANY_ENTRIES: "压缩包内文件数量过多",
    ARCHIVE_ENTRY_TOO_LARGE: "压缩包内单个文件过大",
    ARCHIVE_TOTAL_TOO_LARGE: "压缩包解压后总量过大",
    ARCHIVE_RATIO_TOO_HIGH: "压缩包压缩比异常（疑似压缩炸弹）",
    ARCHIVE_SPECIAL_FILE: "压缩包含有符号链接等特殊文件，已拒绝",
    ARCHIVE_DUPLICATE_ENTRY: "压缩包内存在重复的同名条目，已拒绝",
    ARCHIVE_INVALID_ENTRY: "压缩包内条目名不合法",
}


class FileGuardError(Exception):
    """文件/路径安全拒绝。``code`` 是稳定的机器可读错误码。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(REASON_TEXT.get(code, "文件不合法") + (f"（{detail}）" if detail else ""))


# --------------------------------------------------------------------------
# 文件名与路径
# --------------------------------------------------------------------------
# Windows 非法字符 + 控制字符 + 分隔符
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
# 保留设备名（Windows 上这些名字无法创建，或会引发异常行为）
_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_MAX_STEM = 120


def safe_filename(name: str, *, default: str = "file", max_len: int = _MAX_STEM) -> str:
    """把**任意不可信文件名**清洗成一个安全的单段文件名。

    只返回文件名部分（不含目录）。做法：
    * 先按 **Windows 与 POSIX 两种分隔符**切段，只取最后一段 —— 这样
      `../../evil`、`..\\evil`、`C:\\Windows\\x`、`/etc/passwd` 都塌缩成末段
    * 去掉非法字符与控制字符（含 NUL）
    * 归一化 Unicode（NFKC，消掉全角/兼容字符伪装）
    * 剥掉 `..` / `.`，处理保留设备名
    * 截断到合法长度

    注意：**清洗 ≠ 放行**。落盘前仍必须用 :func:`safe_join` 做解析级校验。
    """
    raw = unicodedata.normalize("NFKC", str(name or ""))
    raw = raw.replace("\\", "/").replace("\x00", "")
    raw = raw.split("/")[-1]                    # 任何目录成分一律丢弃
    raw = _ILLEGAL_CHARS.sub("_", raw).strip()
    raw = raw.strip(". ")                       # 前导/尾随的 . 与空格
    if not raw or raw in (".", ".."):
        return default
    # 保留设备名（大小写无关，且可能带扩展名，如 con.txt）
    if raw.split(".")[0].lower() in _RESERVED_NAMES:
        raw = f"_{raw}"
    if len(raw) > max_len:
        stem, dot, ext = raw.rpartition(".")
        keep = max_len - (len(ext) + 1 if dot else 0)
        raw = (stem[:keep] + (dot + ext if dot else "")) if keep > 0 else raw[:max_len]
    return raw or default


def looks_like_traversal(candidate: str) -> bool:
    """粗筛：是否含目录成分 / 盘符 / UNC / `..`。仅用于**快速拒绝**与日志，
    真正的判定在 :func:`safe_join`（解析级）。"""
    c = unicodedata.normalize("NFKC", str(candidate or "")).replace("\\", "/")
    if not c:
        return False
    if c.startswith("/") or c.startswith("//"):
        return True                          # 绝对路径 / UNC
    if re.match(r"^[A-Za-z]:", c):
        return True                          # 盘符
    return any(part == ".." for part in c.split("/"))


def safe_join(root: str | Path, untrusted: str) -> Path:
    """把不可信相对路径安全地拼到 ``root`` 下。

    **判定方式是解析级**，不是字符串包含：

        resolved(root / name).is_relative_to(resolved(root))

    因此 `../`、绝对路径、盘符、UNC、混合分隔符、重复分隔符全都挡得住。
    """
    base = Path(root).expanduser().resolve()
    raw = str(untrusted or "").strip()
    if not raw or "\x00" in raw:
        raise FileGuardError(INVALID_FILENAME, "空名或含 NUL")
    if looks_like_traversal(raw):
        raise FileGuardError(PATH_TRAVERSAL, raw[:80])

    candidate = base / safe_filename(raw)
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError) as exc:
        raise FileGuardError(INVALID_FILENAME, str(exc)) from exc
    if not resolved.is_relative_to(base):
        raise FileGuardError(PATH_TRAVERSAL, raw[:80])
    return resolved


# --------------------------------------------------------------------------
# 压缩容器
# --------------------------------------------------------------------------
@dataclass
class ArchiveLimits:
    """容器限制。默认值保守，但足够容纳真实 DOCX/PPTX/XLSX/EPUB。"""

    max_entries: int = 5000
    max_entry_bytes: int = 50 * 1024 * 1024
    max_total_bytes: int = 200 * 1024 * 1024
    max_ratio: int = 100
    # 读取单个 entry 时允许的实际输出上限（防「声明 vs 实际」不一致）
    read_chunk: int = 1 << 20


def _is_special_file(info) -> bool:
    """ZIP 里声明的 Unix 文件类型是否为 symlink / device 等特殊类型。

    取 ``external_attr`` 高 16 位（Unix mode）。没有该信息时按普通文件处理。
    """
    mode = (getattr(info, "external_attr", 0) or 0) >> 16
    if mode == 0:
        return False
    file_type = mode & 0o170000
    if file_type == 0:
        return False
    return file_type != 0o100000          # 只接受普通文件


def validate_archive_entry(name: str) -> str:
    """校验 ZIP entry 名，返回规范化后的安全相对路径。不安全则抛异常。

    即便当前实现只用 ``zipfile.read()`` 读取 XML、**不解压到磁盘**，
    也必须校验 —— 免得以后有人改成解压时，安全边界悄悄消失。
    """
    raw = unicodedata.normalize("NFKC", str(name or ""))
    if not raw or "\x00" in raw:
        raise FileGuardError(ARCHIVE_INVALID_ENTRY, "空条目名")
    n = raw.replace("\\", "/")
    if n.startswith("/") or n.startswith("//"):
        raise FileGuardError(PATH_TRAVERSAL, raw[:80])          # 绝对 / UNC
    if re.match(r"^[A-Za-z]:", n):
        raise FileGuardError(PATH_TRAVERSAL, raw[:80])          # 盘符
    if any(part == ".." for part in n.split("/")):
        raise FileGuardError(PATH_TRAVERSAL, raw[:80])
    return n


def check_archive(z, limits: ArchiveLimits | None = None) -> list:
    """**中央目录预检查**：entry 数 / 特殊文件 / 重复名 / 大小与压缩比。

    返回通过校验的 ``ZipInfo`` 列表（供调用方复用，避免重复解析）。

    ⚠ 声明值（``file_size`` / ``compress_size``）是**攻击者可写**的，
    所以这里只是第一层；读取时必须再由 :func:`bounded_read` 计实际字节。
    """
    lim = limits or ArchiveLimits()
    infos = z.infolist()

    if len(infos) > lim.max_entries:
        raise FileGuardError(ARCHIVE_TOO_MANY_ENTRIES, f"{len(infos)} > {lim.max_entries}")

    seen: set[str] = set()
    total = 0
    for info in infos:
        if info.is_dir():
            continue
        norm = validate_archive_entry(info.filename)
        if _is_special_file(info):
            raise FileGuardError(ARCHIVE_SPECIAL_FILE, info.filename[:80])
        key = norm.lower()                        # Windows 上大小写不敏感
        if key in seen:
            # 重复名会让解析结果依赖 zip 库对「后者」的处理，语义模糊 → 整体拒绝
            raise FileGuardError(ARCHIVE_DUPLICATE_ENTRY, info.filename[:80])
        seen.add(key)

        size = int(info.file_size or 0)
        if size > lim.max_entry_bytes:
            raise FileGuardError(ARCHIVE_ENTRY_TOO_LARGE, f"{info.filename[:60]} {size}")
        total += size
        if total > lim.max_total_bytes:
            raise FileGuardError(ARCHIVE_TOTAL_TOO_LARGE, f"{total} > {lim.max_total_bytes}")

        csize = int(info.compress_size or 0)
        if csize > 0 and size / csize > lim.max_ratio:
            raise FileGuardError(
                ARCHIVE_RATIO_TOO_HIGH,
                f"{info.filename[:60]} {size}/{csize}={size // csize}",
            )
    return infos


def bounded_read(z, name: str, limits: ArchiveLimits | None = None,
                 *, cap: int | None = None) -> bytes | None:
    """**实际限流地**读取一个 entry。

    第二层防护：不信任 ``ZipInfo.file_size``，边读边数**真实输出字节**，
    超过 cap 立即停止并抛 :data:`ARCHIVE_ENTRY_TOO_LARGE`。
    """
    lim = limits or ArchiveLimits()
    limit = cap if cap is not None else lim.max_entry_bytes
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.is_dir() or _is_special_file(info):
        raise FileGuardError(ARCHIVE_SPECIAL_FILE, name[:80])
    validate_archive_entry(name)

    out = bytearray()
    with z.open(name) as fh:
        while True:
            chunk = fh.read(lim.read_chunk)
            if not chunk:
                break
            out.extend(chunk)
            if len(out) > limit:
                # 声明值与实际不符（或声明被伪造）—— 立刻停，不把内存吃满
                raise FileGuardError(ARCHIVE_ENTRY_TOO_LARGE, f"{name[:60]} > {limit}")
    return bytes(out)


@dataclass
class ImportLimits:
    """普通文件（非压缩容器）的大小上限。"""

    max_file_bytes: int = 50 * 1024 * 1024
    archive: ArchiveLimits = field(default_factory=ArchiveLimits)


def check_file_size(size: int, limits: ImportLimits | None = None) -> None:
    """普通上传文件的大小闸门。

    攻击者不需要 ZIP —— 直接扔一个 5GB 文本也能把处理流程打爆，
    所以非容器格式同样要限。超限返回明确的 :data:`FILE_TOO_LARGE`，
    而不是让上层抛 MemoryError 变成 500。
    """
    lim = limits or ImportLimits()
    if int(size or 0) > lim.max_file_bytes:
        raise FileGuardError(FILE_TOO_LARGE, f"{size} > {lim.max_file_bytes}")
