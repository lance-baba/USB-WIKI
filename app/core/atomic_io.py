"""原子写：先写同目录临时文件，再 ``os.replace()`` 顶替目标。

## 为什么必须做

本项目里 ``data/notes/*.md`` 是**用户真相源**：``cache.db`` 可以全量重建，
Markdown 不能。而常规写法 ``open(path, "w") -> write()`` 是「先截断再写」——
进程若在写入中途终止（断电、拔 U 盘、被杀、蓝屏），用户拿到的是一份**半截笔记**，
而原内容已经没了。所以这里的首要目标是**异常安全**，不是性能。

## 设计约束（都不是随便定的）

1. **临时文件必须与目标同目录**。U 盘 / 移动硬盘 / exFAT 上，跨卷 ``rename``
   不保证原子。写系统 ``%TEMP%`` 再移过去是**错的**——那是跨盘移动，
   失败时可能留下「两边都不完整」的状态。

2. **换行与编码行为必须与旧代码逐字节一致**。此前这些位置用的是
   ``Path.write_text(text, encoding="utf-8")``，其默认 ``newline=None`` 会把
   ``\\n`` 翻译成 ``os.linesep``（Windows 上 ``\\r\\n``）。
   若这里擅自改成不翻译，已存在的笔记会整体换行符变动、git diff 爆掉。
   故本模块默认按同一规则翻译；要写原始字节序请显式传 ``newline=""``。

3. **失败时绝不碰目标文件**。临时文件写失败 → 清理临时文件 → 原样抛异常；
   目标文件保持旧内容不变。

4. **Windows 上重试替换**。目标可能正被 Obsidian / Typora / 杀毒软件占用，
   ``os.replace`` 会抛 ``PermissionError``（共享冲突）。短暂退避重试比直接失败
   更符合真实使用场景。

## 刻意不做的事

``cache.db`` 由 SQLite 事务保证；``data/assets/`` 可重新抓取；日志可丢弃。
这些**不套用**本模块 —— 可靠性等级不同，统一化只会徒增复杂度。
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

# Windows 共享冲突重试：目标可能正被编辑器 / 杀软占用
_REPLACE_ATTEMPTS = 6
_REPLACE_DELAY_S = 0.05


def _fsync_dir(path: Path) -> None:
    """在 POSIX 上 fsync 父目录，让「重命名」这件事本身也落盘。

    Windows 无法以目录方式打开句柄，故跳过（NTFS 的 MoveFileEx 语义已足够）。
    """
    if os.name == "nt":
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _replace_with_retry(tmp: Path, target: Path) -> None:
    """``os.replace`` + Windows 共享冲突退避重试。"""
    last: OSError | None = None
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, target)
            return
        except PermissionError as exc:      # Windows：目标被占用
            last = exc
            time.sleep(_REPLACE_DELAY_S * (attempt + 1))
    if last is not None:
        raise last


def atomic_write_bytes(path: str | Path, data: bytes, *, fsync: bool = True) -> Path:
    """原子写入字节，返回目标路径。

    流程：同目录临时文件 → 完整写入 → flush → fsync → ``os.replace``。
    任何一步失败都会清理临时文件并抛异常，**目标文件保持原样**。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    # mkstemp 保证 O_CREAT|O_EXCL、二进制模式、随机名不冲突；
    # dir= 指定为**目标同目录**（同文件系统），这是原子性的前提。
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        _replace_with_retry(tmp, target)
        if fsync:
            _fsync_dir(target.parent)
        return target
    except BaseException:
        # 清理临时文件；**不触碰 target**
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_text(
    path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
    errors: str = "strict",
    newline: str | None = None,
    fsync: bool = True,
) -> Path:
    """原子写入文本。

    ``newline`` 默认 ``None``，即**沿用旧行为**：把 ``\\n`` 翻译为 ``os.linesep``
    （Windows 上是 ``\\r\\n``），与原先的 ``Path.write_text(text, encoding="utf-8")``
    一致。传 ``newline=""`` 则原样写出、不做任何翻译。

    注：笔记内容通常是用 ``read_text()``（``newline=None``）读进来的，
    读取时 ``\\r\\n`` 已被归一为 ``\\n``，因此实际写入不会出现 ``\\r\\r\\n``。
    """
    if newline is None:
        text = text.replace("\n", os.linesep)
    return atomic_write_bytes(path, text.encode(encoding, errors), fsync=fsync)
