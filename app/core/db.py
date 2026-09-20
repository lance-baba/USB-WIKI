"""SQLite 存储引擎 —— Schema 定义、WAL 自愈、优雅退出与向量空间签名守卫。

设计要点（对应 PRD 4.1 / 4.3 / 5.1）：
* 真相源是 ``data/notes/*.md``，本库（``data/cache.db``）纯属**可全量重建**的衍生索引。
* 所有连接启用 WAL + synchronous=NORMAL，规避 U 盘写放大。
* 启动前检测 ``-wal`` 残留并执行 ``wal_checkpoint(RESTART)`` 完成自愈。
* ``sys_meta.embedding_signature`` 守卫向量空间：模型/维度变更时**强制阻断**向量召回。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from . import paths
from .log_util import get_logger
from .migrations import CURRENT_SCHEMA_VERSION

log = get_logger()

META_EMBED_SIGNATURE = "embedding_signature"
META_VEC_DIM = "vec_dim"
META_LAST_REBUILD = "last_rebuild"
META_SCHEMA_VERSION = "schema_version"
SCHEMA_VERSION = CURRENT_SCHEMA_VERSION   # 唯一来源见 migrations.py；此处保留别名供既有引用

# --------------------------------------------------------------------------
# 物理 Schema（PRD 5.1）+ chunks 内容表（PRD 4.3 短词 LIKE 降级所需）
# --------------------------------------------------------------------------
SCHEMA_STATEMENTS: tuple[str, ...] = (
    "PRAGMA journal_mode = WAL;",
    "PRAGMA synchronous = NORMAL;",
    """CREATE TABLE IF NOT EXISTS sys_meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );""",
    """CREATE TABLE IF NOT EXISTS documents (
        doc_id     TEXT PRIMARY KEY,
        rel_path   TEXT NOT NULL UNIQUE,
        file_size  INTEGER NOT NULL,
        mtime      REAL NOT NULL,
        title      TEXT,
        status     TEXT DEFAULT 'success',
        sha1      TEXT,
        indexed_at REAL
    );""",
    """CREATE TABLE IF NOT EXISTS chunk_metadata (
        chunk_id  TEXT PRIMARY KEY,
        doc_id    TEXT NOT NULL,
        parent_id TEXT NOT NULL,
        char_len  INTEGER NOT NULL,
        FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
    );""",
    """CREATE TABLE IF NOT EXISTS parent_blocks (
        parent_id TEXT PRIMARY KEY,
        doc_id    TEXT NOT NULL,
        content   TEXT NOT NULL,
        ord       INTEGER DEFAULT 0,
        section_path TEXT DEFAULT '',
        FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
    );""",
    # 子切片正文表：短词降级走 LIKE 全表扫描（PRD 4.3）
    # P0-4：`content` 是**展示原文**（UI / 引用 / 摘录用，永不被改写）；
    #       `retrieval_text` = section_path + content，供 FTS / LIKE / embedding 使用。
    """CREATE TABLE IF NOT EXISTS chunks (
        chunk_id  TEXT PRIMARY KEY,
        doc_id    TEXT NOT NULL,
        parent_id TEXT NOT NULL,
        content   TEXT NOT NULL,
        section_path TEXT DEFAULT '',
        retrieval_text TEXT DEFAULT ''
    );""",
    # 注意：这里的 `content` 列存的是 retrieval_text（章节路径 + 原文），不是展示原文。
    """CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
        chunk_id UNINDEXED,
        doc_id UNINDEXED,
        content,
        tokenize = 'trigram'
    );""",
    # 入库分析的元数据：关键词 / 来源域名 / 语言 / 摘要 / 实体
    # 由 indexer 在索引时从 frontmatter 抄写过来 —— 星图据此建「术语重合」与
    # 「同源」边，不必逐文件读 frontmatter；关键词顺带可用于检索与界面展示。
    """CREATE TABLE IF NOT EXISTS doc_meta (
        doc_id    TEXT PRIMARY KEY,
        keywords  TEXT DEFAULT '',
        host      TEXT DEFAULT '',
        language  TEXT DEFAULT '',
        summary   TEXT DEFAULT '',
        entities  TEXT DEFAULT '',
        normalized_url TEXT DEFAULT '',
        display_source TEXT DEFAULT ''
    );""",
    """CREATE INDEX IF NOT EXISTS idx_doc_meta_host ON doc_meta(host);""",
    """CREATE INDEX IF NOT EXISTS idx_doc_meta_normurl ON doc_meta(normalized_url);""",
    # 索引加速
    "CREATE INDEX IF NOT EXISTS idx_chunk_meta_doc ON chunk_metadata(doc_id);",
    "CREATE INDEX IF NOT EXISTS idx_parent_doc ON parent_blocks(doc_id);",
    "CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);",
    "PRAGMA foreign_keys = ON;",
)

VEC_TABLE_TMPL = """CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
    chunk_id TEXT PRIMARY KEY,
    embedding FLOAT[{dim}]
);"""


# --------------------------------------------------------------------------
# 启动前自愈：物理文件层面检测 -wal 残留
# --------------------------------------------------------------------------
def wal_residue_bytes(db_path: Path | None = None) -> int:
    """返回 ``cache.db-wal`` 的字节大小（不存在返回 0）。"""
    p = Path(db_path or paths.CACHE_DB)
    wal = Path(str(p) + "-wal")
    try:
        return wal.stat().st_size if wal.exists() else 0
    except OSError:
        return 0


def wal_self_heal(db_path: Path | None = None) -> bool:
    """PRD 4.1：启动连接主库前，将上次异常断电遗留的 WAL 强制刷回主库。

    返回 True 表示检测到残留并已执行检查点自愈。
    """
    p = Path(db_path or paths.CACHE_DB)
    if not p.exists():
        return False
    size = wal_residue_bytes(p)
    if size <= 0:
        return False
    try:
        conn = sqlite3.connect(str(p), timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            # RESTART: 刷回主库并重置 WAL 头，但不删除文件（保持连接可用）
            conn.execute("PRAGMA wal_checkpoint(RESTART);")
            conn.commit()
        finally:
            conn.close()
        remaining = wal_residue_bytes(p)
        log.warning(
            "检测到 WAL 残留 %d 字节，已执行启动自愈 (剩下 %d 字节)", size, remaining
        )
        return True
    except sqlite3.Error as exc:
        log.error("WAL 自愈失败（将交由 SQLite 自动恢复）: %s", exc)
        return False


# --------------------------------------------------------------------------
# 数据库门面
# --------------------------------------------------------------------------
class Database:
    """线程安全的 SQLite 门面：写操作串行化，读操作各用各的连接。"""

    def __init__(self, db_path: Path | None = None, embedding_dim: int = 512) -> None:
        self.path = Path(db_path or paths.CACHE_DB)
        self.embedding_dim = int(embedding_dim or 512)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._all_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._vec_table_lock = threading.Lock()
        self._vec_module = None          # sqlite_vec 模块对象（进程内缓存）
        self._vec_module_failed = False
        self._vec_error: str | None = None
        self.vec_table_ready = False
        self.signature_mismatch: str | None = None
        self.self_healed = False

    # ---------------- 连接管理 ----------------
    def _new_conn(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 8000;")
        self._try_load_vec(conn)
        with self._conns_lock:
            self._all_conns.append(conn)
        return conn

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._new_conn()
            self._local.conn = c
        return c

    def _load_vec_module(self):
        """进程内只 import 一次 sqlite_vec；导入失败永久降级。"""
        if self._vec_module is not None:
            return self._vec_module
        if self._vec_module_failed:
            return None
        try:
            import sqlite_vec  # type: ignore

            self._vec_module = sqlite_vec
            self._vec_error = None
            return sqlite_vec
        except ImportError as exc:
            self._vec_module_failed = True
            self._vec_error = f"未安装 sqlite-vec ({exc})"
            log.info("sqlite-vec 不可用：%s", self._vec_error)
            return None

    def _try_load_vec(self, conn: sqlite3.Connection) -> bool:
        """为**每一个新连接**挂载 vec0 扩展。

        SQLite 的可加载扩展是连接级状态，不是全局状态 —— 早期只在首个连接上
        加载会导致后续线程连接报 `no such module: vec0`。
        """
        mod = self._load_vec_module()
        if mod is None:
            return False
        try:
            conn.enable_load_extension(True)
            mod.load(conn)
            conn.enable_load_extension(False)
            return True
        except (sqlite3.Error, OSError, AttributeError) as exc:
            self._vec_error = f"sqlite-vec 扩展加载失败 ({exc})"
            log.error(self._vec_error)
            return False

    @property
    def vec_available(self) -> bool:
        return self._vec_module is not None

    # ---------------- 初始化 ----------------
    def init_schema(self) -> None:
        c = self.conn()
        with self._write_lock:
            for stmt in SCHEMA_STATEMENTS:
                try:
                    c.execute(stmt)
                except sqlite3.Error as exc:
                    log.warning("Schema 语句跳过 (%s): %.60s", exc, stmt)
            self._init_vec_table(c)
            # 版本号**只写一次**，之后永不覆盖。它记录的是「索引是用哪版结构建出来的」；
            # 一旦每次连接都改写，就再也查不出旧库原本的版本（这正是此前缺陷的根因）。
            c.execute(
                "INSERT OR IGNORE INTO sys_meta(key, value) VALUES(?, ?)",
                (META_SCHEMA_VERSION, SCHEMA_VERSION),
            )
            c.commit()

    def _init_vec_table(self, c: sqlite3.Connection) -> None:
        """按 config 维度创建/校验 chunks_vec。维度变更时置 mismatch 标志但不自动清库。"""
        dim = self.embedding_dim
        stored = self.get_meta(META_VEC_DIM)
        existing = self._table_exists(c, "chunks_vec")

        if existing and stored not in (None, str(dim)):
            self.signature_mismatch = (
                f"向量维度冲突：库内为 {stored} 维，当前配置为 {dim} 维"
            )
            log.warning("%s —— 已阻断向量召回，需全量重建索引", self.signature_mismatch)
            self.vec_table_ready = True  # 表存在但维度不匹配，查询层会拒绝使用
            return

        if not existing:
            if not self._try_load_vec(c):
                log.info("sqlite-vec 不可用（%s），向量召回将逐级降级", self._vec_error)
                self.vec_table_ready = False
                return
            try:
                c.execute(VEC_TABLE_TMPL.format(dim=dim))
                self.set_meta(META_VEC_DIM, str(dim))
            except sqlite3.Error as exc:
                self._vec_error = f"vec0 虚拟表创建失败 ({exc})"
                log.error(self._vec_error)
                self.vec_table_ready = False
                return
        self.vec_table_ready = True

    @staticmethod
    def _table_exists(c: sqlite3.Connection, name: str) -> bool:
        row = c.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (name,)
        ).fetchone()
        return row is not None

    def recreate_vec_table(self, dim: int | None = None) -> bool:
        """全量重建向量表（维度过期/重建索引时由上层调用）。"""
        dim = int(dim or self.embedding_dim)
        self.embedding_dim = dim
        c = self.conn()
        with self._write_lock:
            try:
                c.execute("DROP TABLE IF EXISTS chunks_vec;")
                if self._try_load_vec(c):
                    c.execute(VEC_TABLE_TMPL.format(dim=dim))
                    c.execute(
                        "INSERT OR REPLACE INTO sys_meta(key, value) VALUES(?, ?)",
                        (META_VEC_DIM, str(dim)),
                    )
                    c.commit()
                    self.vec_table_ready = True
                    self.signature_mismatch = None
                    return True
            except sqlite3.Error as exc:
                log.error("向量表重建失败: %s", exc)
                c.rollback()
            self.vec_table_ready = False
            return False

    # ---------------- sys_meta ----------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        try:
            row = self.conn().execute(
                "SELECT value FROM sys_meta WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default
        except sqlite3.Error:
            return default

    def set_meta(self, key: str, value: str) -> None:
        c = self.conn()
        with self._write_lock:
            c.execute(
                "INSERT OR REPLACE INTO sys_meta(key, value) VALUES(?, ?)", (key, value)
            )
            c.commit()

    def get_signature(self) -> dict | None:
        raw = self.get_meta(META_EMBED_SIGNATURE)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    def set_signature(self, source: str, model: str, dim: int,
                      extra: dict | None = None) -> None:
        payload = {"source": source, "model": model, "dim": int(dim)}
        for k, v in (extra or {}).items():
            if v:
                payload[str(k)] = str(v)
        self.set_meta(META_EMBED_SIGNATURE, json.dumps(payload))

    def check_signature(self, source: str, model: str, dim: int,
                        extra: dict | None = None) -> str | None:
        """比对本次嵌入签名与库内签名，返回告警文案（None 表示一致/首次写入）。

        *extra* 是**字节级**字段（bundled local_onnx 传 artifact / tokenizer 的
        SHA256 与精度）。只有「模型名 + 维度」时，同一个名字换了字节的 artifact
        会被误判为「没变」，旧向量就会静默失真。
        """
        current = self.get_signature()
        extra = {str(k): str(v) for k, v in (extra or {}).items() if v}
        if current is None:
            self.set_signature(source, model, dim, extra)
            return None
        if current.get("dim") != int(dim):
            return (
                f"嵌入模型维度已变更（{current.get('dim')} → {dim}），"
                "向量召回已被强制阻断"
            )
        if current.get("model") != model or current.get("source") != source:
            return (
                f"嵌入模型已更换（{current.get('model')} → {model}），"
                "向量相似度可能失真"
            )
        changed = sorted(k for k, v in extra.items()
                         if current.get(k) and current.get(k) != v)
        if changed:
            return (
                "随包嵌入资源已更换（" + "、".join(changed) + " 与库内记录不一致），"
                "向量相似度可能失真"
            )
        # A4.2b 之前写入的旧签名没有字节级字段 → 补齐，不当作 mismatch
        if any(k not in current for k in extra):
            merged = dict(current)
            merged.update({k: v for k, v in extra.items() if k not in current})
            self.set_meta(META_EMBED_SIGNATURE, json.dumps(merged))
        return None

    # ---------------- 事务辅助 ----------------
    def write(self, sql: str, params: tuple | list = ()) -> int:
        c = self.conn()
        with self._write_lock:
            cur = c.execute(sql, params)
            c.commit()
            return cur.rowcount

    def write_many(self, sql: str, seq: list[tuple]) -> int:
        if not seq:
            return 0
        c = self.conn()
        with self._write_lock:
            cur = c.executemany(sql, seq)
            c.commit()
            return cur.rowcount or 0

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, params).fetchone()

    def stats(self) -> dict:
        out = {
            "docs": 0,
            "chunks": 0,
            "parents": 0,
            "vec_ready": bool(self.vec_table_ready and not self.signature_mismatch),
            "vec_error": self._vec_error,
            "signature_mismatch": self.signature_mismatch,
        }
        try:
            out["docs"] = self.conn().execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
            out["chunks"] = self.conn().execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
            out["parents"] = self.conn().execute(
                "SELECT COUNT(*) c FROM parent_blocks"
            ).fetchone()["c"]
        except sqlite3.Error:
            pass
        return out

    # ---------------- 关闭 ----------------
    def checkpoint_and_close(self, vacuum_if_fragmented: bool = True) -> dict:
        """PRD 4.1 第 3~4 步：TRUNCATE 检查点 + 按需 VACUUM，彻底释放文件锁。"""
        report = {"checkpoint": False, "vacuum": False, "removed": []}
        with self._write_lock:
            with self._conns_lock:
                conns = list(self._all_conns)
                self._all_conns.clear()
            fired = False
            for c in conns:
                try:
                    if not fired:
                        c.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                        c.commit()
                        report["checkpoint"] = True
                        fired = True
                    if vacuum_if_fragmented and self._fragmented(c):
                        c.execute("VACUUM;")
                        c.commit()
                        report["vacuum"] = True
                except sqlite3.Error as exc:
                    log.debug("关闭检查点异常（忽略）: %s", exc)
                finally:
                    try:
                        c.close()
                    except sqlite3.Error:
                        pass
            self._local = threading.local()

        for suffix in ("-wal", "-shm"):
            f = Path(str(self.path) + suffix)
            try:
                if f.exists() and f.stat().st_size >= 0:
                    f.unlink(missing_ok=True)
                    report["removed"].append(f.name)
            except OSError:
                pass
        log.info(
            "数据库已安全关闭 (checkpoint=%s vacuum=%s 清理=%s)",
            report["checkpoint"],
            report["vacuum"],
            report["removed"] or "无残留",
        )
        return report

    @staticmethod
    def _fragmented(c: sqlite3.Connection) -> bool:
        """碎片率 > 25% 才值得付出 VACUUM 的整库重写代价（U 盘写放大考量）。"""
        try:
            free = c.execute("PRAGMA freelist_count;").fetchone()[0]
            total = c.execute("PRAGMA page_count;").fetchone()[0]
            return bool(total) and (free / total) > 0.25
        except sqlite3.Error:
            return False


# --------------------------------------------------------------------------
# 全局单例
# --------------------------------------------------------------------------
_db: Database | None = None
_db_lock = threading.Lock()


def get_db(db_path: Path | None = None, embedding_dim: int | None = None) -> Database:
    global _db
    with _db_lock:
        if _db is None or db_path is not None:
            dim = embedding_dim
            if dim is None:
                try:
                    from . import config as _cfg

                    dim = _cfg.get_int("AI", "embedding_dim", 512)
                except Exception:  # noqa: BLE001
                    dim = 512
            _db = Database(db_path=db_path, embedding_dim=dim)
        return _db


def close_db() -> dict:
    global _db
    with _db_lock:
        if _db is None:
            return {}
        report = _db.checkpoint_and_close()
        _db = None
        return report
