"""外部 Markdown 增量同步（PRD 4.5 + 编码阶段提示 #1 加固）。

exFAT 时间戳精度只有 2 秒，且等长编辑（把"买"改成"卖"）字节数完全相同 ——
单靠「大小变化」会漏更新。因此判定升级为三分支：

  1. ``current_mtime > last_mtime + 2.0s``                → 时间明显后移，必须更新
  2. ``|Δt| <= 2.0s 且 file_size != last_size``           → 容差期内大小变化，必须更新
  3. ``|Δt| <= 2.0s 且 file_size == last_size``           → 比对前 4KB 快速哈希(XXH64/blake2b)
                                                            哈希不同则更新，否则视作未变

分支 1 与分支 3 共同封堵了「等长编辑 + 2 秒窗口边缘」的漏扫盲区。
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path

from . import chunker, config, indexer, paths
from .db import Database
from .log_util import get_logger

log = get_logger()

MTIME_TOLERANCE = 2.0
PREFIX_BYTES = 4096


# --------------------------------------------------------------------------
def prefix_hash(p: Path, size: int = PREFIX_BYTES) -> str:
    """前 4KB 快速哈希；优先 xxhash（可选依赖），缺失时回退 blake2b。"""
    try:
        data = p.open("rb").read(size)
    except OSError:
        return ""
    try:
        import xxhash  # type: ignore

        return xxhash.xxh64(data).hexdigest()
    except ImportError:
        return hashlib.blake2b(data, digest_size=8).hexdigest()


class NoteSyncer:
    """后台轮询扫描 data/notes 的轻量增量同步器。"""

    def __init__(self, db: Database, embedder=None) -> None:
        self.db = db
        self.embedder = embedder
        self.interval = max(3, config.get_int("SYSTEM", "sync_interval", 15))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # rel_path -> {"mtime","size","prefix"}
        self._state: dict[str, dict] = {}
        self.stats = {
            "cycles": 0,
            "indexed": 0,
            "updated": 0,
            "removed": 0,
            "last_run": 0.0,
            "last_report": {},
        }

    # ------------------------------------------------------------------
    def seed_from_db(self) -> None:
        """从 documents 表回灌基线，避免重启后把全库误判为“新增”。"""
        try:
            rows = self.db.query(
                "SELECT rel_path, mtime, file_size, sha1 FROM documents"
            )
        except sqlite3.Error:
            return
        for r in rows:
            self._state[r["rel_path"]] = {
                "mtime": float(r["mtime"] or 0.0),
                "size": int(r["file_size"] or 0),
                "prefix": r["sha1"] or "",
            }
        log.info("同步基线已载入 %d 个文档", len(self._state))

    # ------------------------------------------------------------------
    @staticmethod
    def _decide(p: Path, st, base: dict | None) -> tuple[bool, str, str]:
        """三分支判定：返回 (是否需要重建索引, 判定依据, 当前前缀哈希)。

        分支 1「时间明显后移」与分支 3「等长编辑哈希比对」共同封堵 exFAT
        2 秒时间窗 + 等长编辑造成的漏扫盲区。
        """
        if not base:
            return True, "new", ""

        last_mtime = float(base.get("mtime") or 0.0)
        last_size = int(base.get("size") or 0)
        last_prefix = base.get("prefix") or ""
        delta = st.st_mtime - last_mtime

        # 1) 时间明显后移：即便字节数完全相同也必须更新
        if delta > MTIME_TOLERANCE:
            return True, "mtime-advanced", ""

        # 2) 2 秒容差期内，文件大小发生变化
        if abs(delta) <= MTIME_TOLERANCE and st.st_size != last_size:
            return True, "size-changed", ""

        # 3) 容差期且大小相同 —— 必须比对前 4KB 快速哈希
        if abs(delta) <= MTIME_TOLERANCE:
            cur_prefix = prefix_hash(p)
            if not last_prefix:
                # 基线缺失（首次升级/旧库）：保守重建一次以建立哈希基线
                return True, "baseline-missing", cur_prefix
            if cur_prefix != last_prefix:
                return True, "same-length-edit", cur_prefix
            return False, "unchanged", cur_prefix

        # 时间倒流（外部工具回写 / 时钟漂移）：保守重建
        return True, "mtime-rewound", ""

    # ------------------------------------------------------------------
    def scan_once(self) -> dict:
        """执行一轮扫描，返回本轮报告。"""
        if not paths.NOTES_DIR.exists():
            paths.NOTES_DIR.mkdir(parents=True, exist_ok=True)

        disk: dict[str, Path] = {}
        for p in paths.NOTES_DIR.rglob("*.md"):
            try:
                if p.is_file():
                    disk[paths.rel_to_data(p)] = p
            except OSError:
                continue

        report = {"indexed": 0, "updated": 0, "unchanged": 0, "removed": [], "errors": []}

        for rel, p in sorted(disk.items()):
            try:
                st = p.stat()
            except OSError as exc:
                report["errors"].append(f"{rel}: {exc}")
                continue

            base = self._state.get(rel)
            need, reason, cur_prefix = self._decide(p, st, base)
            if not need:
                report["unchanged"] += 1
                continue

            result = indexer.index_file(self.db, p, self.embedder)
            if result.get("ok"):
                if not cur_prefix:
                    cur_prefix = prefix_hash(p)
                self._state[rel] = {
                    "mtime": float(st.st_mtime),
                    "size": int(st.st_size),
                    "prefix": cur_prefix,
                }
                if reason == "new":
                    report["indexed"] += 1
                else:
                    report["updated"] += 1
                log.info("增量同步 [%s] %s (%d 切片)", reason, rel, result.get("chunks", 0))
            else:
                report["errors"].append(f"{rel}: {result.get('error')}")

        # ---- 重命名 / 删除 -> 孤儿切片回收 ----
        known = set(self._state.keys())
        vanished = known - set(disk.keys())
        for rel in vanished:
            n = indexer.purge_by_rel_path(self.db, rel)
            self._state.pop(rel, None)
            report["removed"].append({"path": rel, "chunks": n})
            log.info("孤儿切片回收: %s (%d 切片)", rel, n)

        # 兜底：DB 中残留但磁盘已无、且不在基线里的历史脏数据
        try:
            stale = indexer.purge_orphans(self.db, set(disk.keys()))
            for s in stale:
                report["removed"].append({"path": s, "chunks": -1})
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"orphan-sweep: {exc}")

        # 孤儿原件回收：笔记没了，导入时留存的原件也就没有存在意义（可达数十 MB）
        try:
            from . import crawler as crawler_mod  # noqa: PLC0415

            for name in crawler_mod.purge_orphan_originals(set(disk.keys())):
                report["removed"].append({"path": f"originals/{name}", "chunks": 0})
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"original-sweep: {exc}")

        self.stats["cycles"] += 1
        self.stats["indexed"] += report["indexed"]
        self.stats["updated"] += report["updated"]
        self.stats["removed"] += len(report["removed"])
        self.stats["last_run"] = time.time()
        self.stats["last_report"] = report
        return report

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        log.info("外部文件增量同步已启动（间隔 %ds）", self.interval)
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as exc:  # noqa: BLE001 - 同步线程绝不因单轮异常而退出
                log.error("同步轮次异常: %s", exc)
            self._stop.wait(self.interval)
        log.info("外部文件增量同步已停止")

    def start(self, run_now: bool = True) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self.seed_from_db()
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="note-syncer", daemon=True)
            self._thread.start()
        if run_now:
            threading.Thread(target=self._safe_initial_scan, daemon=True).start()

    def _safe_initial_scan(self) -> None:
        try:
            self.scan_once()
        except Exception as exc:  # noqa: BLE001
            log.error("首次全量扫描异常: %s", exc)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)

    def status(self) -> dict:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "interval": self.interval,
            "tracked": len(self._state),
            **{k: v for k, v in self.stats.items() if k != "last_report"},
        }
