"""文档级索引流水线 —— 切片入库、文档级局部清空重建、孤儿切片回收。

遵循 PRD 4.3 的「文档级局部清空与重建」策略：以 doc_id 为单位整体清除再写回，
从业务层绕开 vec0 虚拟表在动态行级 DELETE 上的历史缺陷。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse

from . import chunker, paths
from .db import Database
from .log_util import get_logger
from . import urls as _urls

log = get_logger()


def derive_display_source(meta: dict, title: str, rel_path: str) -> str:
    """统一「来源显示名」（UX-1）—— **只服务界面**，不影响内部 title。

    规则（按优先级）：
      * 本地导入文件  → frontmatter 的 ``source_file``（用户上传时的原始文件名）
      * 网页剪藏      → 网页标题（即内部 ``title``）
      * 手写笔记      → 笔记标题（即内部 ``title``）
      * 兜底          → ``rel_path`` 的文件名

    背景：真实 DOCX 的 ``docProps/core.xml`` 里 title/subject 常是公司名之类，
    拿它当检索来源会让用户看到「宁波孙氏开发有限公司太阳广场」而不是自己上传的
    文件名。内部 ``title`` 继续用于 metadata / 检索 / 提示词，不改动。
    """
    meta = meta or {}
    src_file = str(meta.get("source_file") or "").strip()
    if src_file:
        return src_file
    t = str(title or "").strip()
    if t:
        return t
    try:
        return Path(rel_path or "").name
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------
def _vec_chunk_ids_for(db: Database, doc_id: str) -> list[str]:
    """扫描 ``chunks_vec`` 取回属于该文档的全部 chunk_id。

    vec0 是虚拟表，没有 doc_id 列也无法用 LIKE 过滤；而 ``chunk_metadata`` 可能因为
    历史异常写入而缺行。因此这里以**向量表的真实内容**为准做前缀归属判定，杜绝
    「元数据已清、向量残留」导致的主键冲突。
    """
    if not db.vec_table_ready:
        return []
    prefix = f"{doc_id}:"
    try:
        rows = db.query("SELECT chunk_id FROM chunks_vec")
    except sqlite3.Error:
        return []
    return [r["chunk_id"] for r in rows if str(r["chunk_id"] or "").startswith(prefix)]


def purge_document(db: Database, doc_id: str) -> int:
    """彻底清除某文档的全部索引痕迹（切片 / 父块 / FTS / 向量）。

    返回被清除的子切片数量。任何一步失败都不得留下孤儿切片。
    """
    conn = db.conn()
    with db._write_lock:  # noqa: SLF001 - 同包内部协作
        try:
            rows = conn.execute(
                "SELECT chunk_id FROM chunk_metadata WHERE doc_id = ?", (doc_id,)
            ).fetchall()
            chunk_ids = {r["chunk_id"] for r in rows}
            # 以向量表真实内容补齐（可能多于元数据）
            chunk_ids.update(_vec_chunk_ids_for(db, doc_id))

            if chunk_ids and db.vec_table_ready:
                try:
                    conn.executemany(
                        "DELETE FROM chunks_vec WHERE chunk_id = ?",
                        [(cid,) for cid in chunk_ids],
                    )
                except sqlite3.Error as exc:
                    log.warning("向量行删除失败，转为整表清空: %s", exc)
                    try:
                        conn.execute("DELETE FROM chunks_vec")
                    except sqlite3.Error as exc2:
                        log.error("向量表清空失败: %s", exc2)

            conn.execute("DELETE FROM chunks_fts WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM parent_blocks WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM chunk_metadata WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            conn.commit()
            return len(chunk_ids)
        except sqlite3.Error as exc:
            conn.rollback()
            log.error("purge_document(%s) 失败: %s", doc_id, exc)
            return 0


def purge_by_rel_path(db: Database, rel_path: str) -> int:
    """文件被删除/重命名时调用，防止孤儿切片污染检索与图谱。"""
    did = chunker.doc_id_for(rel_path)
    row = db.query_one("SELECT doc_id FROM documents WHERE doc_id = ?", (did,))
    if not row:
        row = db.query_one("SELECT doc_id FROM documents WHERE rel_path = ?", (rel_path,))
    if not row:
        return 0
    return purge_document(db, row["doc_id"])


def purge_orphans(db: Database, valid_rel_paths: set[str]) -> list[str]:
    """扫描 documents 表，清除磁盘上已不存在的文档索引（TC-HARD-05 核心）。"""
    removed: list[str] = []
    try:
        rows = db.query("SELECT doc_id, rel_path FROM documents")
    except sqlite3.Error:
        return removed
    for r in rows:
        if r["rel_path"] not in valid_rel_paths:
            purge_document(db, r["doc_id"])
            removed.append(r["rel_path"])
    if removed:
        log.info("回收孤儿切片: %d 个文档 (%s)", len(removed), ", ".join(removed[:5]))
    return removed


# --------------------------------------------------------------------------
def _embed_in_batches(embedder, texts: list[str], batch: int = 16) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        out.extend(embedder.embed(chunk))
    return out


def index_parsed(
    db: Database,
    parsed: chunker.ParsedDoc,
    file_size: int,
    mtime: float,
    embedder=None,
    prefix_hash: str = "",
) -> dict:
    """把一个已解析文档整体写入索引（先清空后写入，保证幂等）。"""
    purge_document(db, parsed.doc_id)

    conn = db.conn()
    vectors: list[list[float]] = []
    vec_error: str | None = None
    if embedder is not None and parsed.children:
        try:
            # P0-4：向量也建在 retrieval_text（含章节路径）上 —— 章节语境对语义召回同样有用
            vectors = _embed_in_batches(embedder, [c.retrieval_text for c in parsed.children])
        except Exception as exc:  # noqa: BLE001 - 向量失败必须降级而非中断索引
            vec_error = f"向量化失败，已仅建词法索引: {type(exc).__name__}: {exc}"
            log.warning("[%s] %s", parsed.rel_path, vec_error)
            vectors = []

    with db._write_lock:  # noqa: SLF001
        try:
            conn.execute(
                """INSERT OR REPLACE INTO documents
                   (doc_id, rel_path, file_size, mtime, title, status, sha1, indexed_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    parsed.doc_id, parsed.rel_path, int(file_size), float(mtime),
                    parsed.title, parsed.status, prefix_hash or None, time.time(),
                ),
            )
            # 入库分析元数据（frontmatter -> doc_meta），供星图与界面使用
            _m = parsed.meta or {}
            _kws = _m.get("keywords")
            _kw_text = " ".join(str(x) for x in _kws) if isinstance(_kws, (list, tuple)) else str(_kws or "")
            if not _kw_text.strip():
                # 回退：frontmatter 里没有关键词（本次功能上线前入库的旧笔记）时
                # **就地现算**并存进 doc_meta。刻意不回头改用户的 .md 文件 ——
                # doc_meta 是衍生物，随索引重建即可再生；动源文件则是越权。
                try:
                    from . import analyzer as _ana  # noqa: PLC0415

                    _body = "\n".join(c.content for c in parsed.children)
                    _kw_text = " ".join(_ana.extract_terms(_body, parsed.title, limit=12).keys())
                except Exception as exc:  # noqa: BLE001
                    log.warning("关键词回退计算失败 %s: %s", parsed.rel_path, exc)
            _host = ""
            _src = str(_m.get("source_url") or "")
            if _src:
                try:
                    _host = (urlparse(_src).hostname or "").lower()
                except ValueError:
                    _host = ""
            # 归一化来源 URL：抓取前用它判重（同一页面的 utm / fragment / 尾斜杠
            # 等差异不应产生多篇笔记）。见 core/urls.py。
            _norm_url = ""
            if _src:
                try:
                    _norm_url = _urls.normalize_url(_src)
                except Exception:  # noqa: BLE001 - 规范化失败不影响索引
                    _norm_url = ""
            # UX-1：统一「来源显示名」（导入文件→源文件名；剪藏/笔记→标题）
            _display = derive_display_source(_m, parsed.title, parsed.rel_path)
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO doc_meta
                       (doc_id, keywords, host, language, summary, entities,
                        normalized_url, display_source)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        parsed.doc_id, _kw_text[:600], _host,
                        str(_m.get("language") or "")[:16],
                        str(_m.get("summary") or "")[:300],
                        json.dumps(_m.get("entities") or {}, ensure_ascii=False)[:900],
                        _norm_url, _display[:300],
                    ),
                )
            except sqlite3.Error as exc:  # 元数据写入失败不该影响索引
                log.warning("doc_meta 写入失败 %s: %s", parsed.rel_path, exc)

            conn.executemany(
                "INSERT OR REPLACE INTO parent_blocks(parent_id, doc_id, content, ord, "
                "section_path, source_start_line, source_end_line) VALUES (?,?,?,?,?,?,?)",
                [(p.parent_id, p.doc_id, p.content, p.ord, getattr(p, "section_path", ""),
                  getattr(p, "source_start_line", 0), getattr(p, "source_end_line", 0))
                 for p in parsed.parents],
            )
            conn.executemany(
                "INSERT OR REPLACE INTO chunk_metadata(chunk_id, doc_id, parent_id, char_len) VALUES (?,?,?,?)",
                [(c.chunk_id, c.doc_id, c.parent_id, len(c.content)) for c in parsed.children],
            )
            # P0-4：content=展示原文（永不被改写），retrieval_text=章节路径+原文（建索引用）
            conn.executemany(
                "INSERT OR REPLACE INTO chunks"
                "(chunk_id, doc_id, parent_id, content, section_path, retrieval_text)"
                " VALUES (?,?,?,?,?,?)",
                [(c.chunk_id, c.doc_id, c.parent_id, c.content,
                  c.section_path, c.retrieval_text) for c in parsed.children],
            )
            # FTS5 的 INSERT OR REPLACE 不会替换，而是静默追加重复行 —— 必须显式先删
            conn.execute("DELETE FROM chunks_fts WHERE doc_id = ?", (parsed.doc_id,))
            conn.executemany(
                "INSERT INTO chunks_fts(chunk_id, doc_id, content) VALUES (?,?,?)",
                [(c.chunk_id, c.doc_id, c.retrieval_text) for c in parsed.children],
            )

            vec_written = 0
            if vectors and db.vec_table_ready and not db.signature_mismatch:
                dim = len(vectors[0])
                if dim != db.embedding_dim:
                    vec_error = (
                        f"向量维度 {dim} 与库内约定 {db.embedding_dim} 不一致，已跳过向量写入"
                    )
                    log.warning(vec_error)
                else:
                    for child, vec in zip(parsed.children, vectors):
                        try:
                            conn.execute(
                                "INSERT INTO chunks_vec(chunk_id, embedding) VALUES (?, ?)",
                                (child.chunk_id, json.dumps(vec)),
                            )
                        except sqlite3.IntegrityError:
                            # vec0 不支持 INSERT OR REPLACE —— 手动先删后插自愈
                            conn.execute(
                                "DELETE FROM chunks_vec WHERE chunk_id = ?", (child.chunk_id,)
                            )
                            conn.execute(
                                "INSERT INTO chunks_vec(chunk_id, embedding) VALUES (?, ?)",
                                (child.chunk_id, json.dumps(vec)),
                            )
                        vec_written += 1
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            log.error("索引写入失败 [%s]: %s", parsed.rel_path, exc)
            return {"ok": False, "error": str(exc), "chunks": 0}

    return {
        "ok": True,
        "doc_id": parsed.doc_id,
        "rel_path": parsed.rel_path,
        "title": parsed.title,
        "status": parsed.status,
        "parents": len(parsed.parents),
        "chunks": len(parsed.children),
        "vectors": len(vectors),
        "links": parsed.links,
        "warning": vec_error,
    }


def index_file(db: Database, abs_path: Path, embedder=None) -> dict:
    """从磁盘读取单一 Markdown 文件并索引。"""
    p = Path(abs_path)
    try:
        stat = p.stat()
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.error("读取失败 %s: %s", p, exc)
        return {"ok": False, "error": str(exc), "path": str(p)}

    rel = paths.rel_to_data(p)
    parsed = chunker.parse(text, rel)
    result = index_parsed(
        db, parsed, stat.st_size, stat.st_mtime, embedder,
        prefix_hash=_prefix_hash(p),
    )
    result["path"] = str(p)
    return result


def _prefix_hash(p: Path, size: int = 4096) -> str:
    """前 4KB 快速哈希，作为 exFAT 等长编辑的第二指纹（与 sync.prefix_hash 同算法）。"""
    try:
        data = Path(p).open("rb").read(size)
    except OSError:
        return ""
    try:
        import xxhash  # type: ignore

        return xxhash.xxh64(data).hexdigest()
    except ImportError:
        import hashlib

        return hashlib.blake2b(data, digest_size=8).hexdigest()


def scan_notes(abs_dir: Path | None = None) -> list[Path]:
    root = Path(abs_dir or paths.NOTES_DIR)
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.md") if p.is_file())


def find_by_normalized_url(db: Database, url: str) -> dict | None:
    """按**归一化**来源 URL 查已抓过的笔记。

    返回 ``{doc_id, rel_path, title, normalized_url}`` 或 ``None``。
    用 doc_meta 的索引列做 O(1) 查找，避免每次抓取都去扫 notes 目录。
    """
    norm = _urls.normalize_url(url)
    if not norm:
        return None
    try:
        row = db.query_one(
            """SELECT d.doc_id, d.rel_path, d.title, m.normalized_url
                 FROM doc_meta m JOIN documents d ON d.doc_id = m.doc_id
                WHERE m.normalized_url = ?
                ORDER BY d.mtime DESC LIMIT 1""",
            (norm,),
        )
    except Exception as exc:  # noqa: BLE001 - 表可能尚未建出（老库未重建）
        log.warning("按 URL 查重失败（将视为未重复）: %s", exc)
        return None
    if not row:
        return None
    return {
        "doc_id": row["doc_id"],
        "rel_path": row["rel_path"] or "",
        "title": row["title"] or row["rel_path"] or "",
        "normalized_url": norm,
    }


def rebuild_all(db: Database, embedder=None, recreate_vec: bool = False) -> dict:
    """全量重建：清空索引 -> 扫描 notes -> 逐文件重建（可重建向量表维度）。"""
    started = time.time()
    if recreate_vec and embedder is not None:
        db.recreate_vec_table(getattr(embedder, "dim", db.embedding_dim))

    # 全清
    conn = db.conn()
    with db._write_lock:  # noqa: SLF001
        for table in ("chunks_fts", "chunks", "chunk_metadata", "parent_blocks", "documents"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except sqlite3.Error as exc:
                log.debug("清空 %s 跳过: %s", table, exc)
        if db.vec_table_ready:
            try:
                conn.execute("DELETE FROM chunks_vec")
            except sqlite3.Error:
                pass
        conn.commit()

    files = scan_notes()
    ok, failed, chunks = 0, 0, 0
    for f in files:
        r = index_file(db, f, embedder)
        if r.get("ok"):
            ok += 1
            chunks += int(r.get("chunks", 0))
        else:
            failed += 1

    elapsed = time.time() - started
    db.set_meta("last_rebuild", str(time.time()))
    report = {
        "ok": True,
        "files": len(files),
        "indexed": ok,
        "failed": failed,
        "chunks": chunks,
        "elapsed": round(elapsed, 2),
        "vec_ready": bool(db.vec_table_ready and not db.signature_mismatch),
    }
    log.info(
        "全量索引重建完成: %d/%d 文件, %d 切片, 耗时 %.2fs",
        ok, len(files), chunks, elapsed,
    )
    return report
