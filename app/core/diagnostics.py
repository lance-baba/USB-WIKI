"""只读系统诊断（Core 层）—— 看病检查，不做任何修复。

## 设计目标

**必须在「部分系统已经坏掉」时仍能运行**：cache.db 损坏、sqlite-vec 不可用、
config 异常、manifest 损坏、FTS 缺失…… 诊断本身不能跟着崩。

* 每个 probe 独立 try/except，失败记为 issue 继续，最后尽量返回完整报告。
* **纯只读**：SQLite 用 ``mode=ro`` URI 打开；不调 ensure()、不写 manifest、
  不建目录、不改 config —— diagnostics 是检查，repair 是另一回事。
* 独立于 Server：Core 函数可直接被 CLI / 修复工具 / 启动器调用，
  ``/api/diagnostics`` 只做薄包装。

## 状态与 issue

状态统一 ``OK / WARN / ERROR / UNKNOWN``（不发明 healthy/good/failed 等同义词）。

每个 issue 是机器可读的，给未来 Repair Engine 用：

    {"code": "CACHE_CORRUPTED", "severity": "WARN",
     "repairable": true, "component": "database", "message": "..."}

**严重度语义（Data Contract 的直接体现）**：

* B 类可重建层异常（cache/FTS/vector）→ ``WARN`` + ``repairable=true``
* A 类永久资料异常（笔记不可读等）→ ``ERROR`` + ``repairable=false``

所有异常文本统一过 ``redact.sanitize_text`` —— 第三方异常可能回显
``Authorization: Bearer sk-…``，不允许落进诊断输出。
"""

from __future__ import annotations

# ⚠ 被当普通脚本直接运行时（embedded runtime 无包上下文）先自举包路径，
# 否则 `python app/core/diagnostics.py` 会因相对导入失败 —— 而 CLI 出口的
# 存在意义恰恰是「server/launcher 都起不来时也能诊断」。
if __name__ == "__main__" and not __package__:
    import os as _os
    import sys as _sys

    _here = _os.path.dirname(_os.path.abspath(__file__))
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_here)))
    __package__ = "app.core"

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

from . import redact
from .log_util import get_logger

log = get_logger()

OK = "OK"
WARN = "WARN"
ERROR = "ERROR"
UNKNOWN = "UNKNOWN"

# Data Contract 状态码（未来恢复工具直接按 code 分流）
NEW_LIBRARY = "NEW_LIBRARY"
LEGACY_V0 = "LEGACY_V0"
V1_OK = "V1_OK"
MANIFEST_CORRUPTED = "MANIFEST_CORRUPTED"
DATA_FORMAT_TOO_NEW = "DATA_FORMAT_TOO_NEW"
INVALID_LIBRARY = "INVALID_LIBRARY"

PERMANENT_DIRS = ("notes", "originals", "assets", "snapshots")
MANIFEST_NAME = "library.json"


def _issue(code: str, severity: str, repairable: bool, component: str, message: str) -> dict:
    """构造机器可读的 issue。message 一律过 secret sanitizer。"""
    return {
        "code": code,
        "severity": severity,
        "repairable": repairable,
        "component": component,
        "message": redact.sanitize_text(str(message or "")),
    }


def _sha_file(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return ""


def _dir_stats(root: Path, name: str) -> dict:
    """统计某个永久子目录：数量 + 总大小 + 可读性。只读，不返回文件名。"""
    out = {"count": 0, "bytes": 0, "readable": True}
    d = root / name
    if not d.is_dir():
        out["readable"] = False
        return out
    try:
        for f in d.iterdir():
            if not f.is_file():
                continue
            out["count"] += 1
            try:
                out["bytes"] += f.stat().st_size
            except OSError as exc:
                out["readable"] = False
                log.warning("诊断读取 %s 失败: %s", name, redact.sanitize_text(str(exc)))
    except OSError:
        out["readable"] = False
    return out


# --------------------------------------------------------------------------
# Library / Data Contract 探针
# --------------------------------------------------------------------------
def _library_probe(root: Path, issues: list) -> dict:
    out = {"status": UNKNOWN, "code": None, "exists": root.is_dir(),
           "writable": os.access(root, os.W_OK) if root.is_dir() else False,
           "fingerprint": None, "counts": {}}
    if not root.is_dir():
        out["status"] = UNKNOWN
        out["code"] = "LIBRARY_NOT_FOUND"
        issues.append(_issue("LIBRARY_NOT_FOUND", "UNKNOWN", False, "library",
                             "资料库目录不存在"))
        return out

    manifest_path = root / MANIFEST_NAME
    m = None
    if manifest_path.exists():
        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(m, dict):
                raise ValueError("顶层不是 JSON 对象")
            out["fingerprint"] = _sha_file(manifest_path)[:12]
        except (OSError, ValueError) as exc:
            # 损坏：绝不自动改写（Data Contract），标记 ERROR 交由显式修复
            out["status"] = ERROR
            out["code"] = MANIFEST_CORRUPTED
            issues.append(_issue(MANIFEST_CORRUPTED, ERROR, True, "library",
                                 f"library.json 损坏：{exc}；可用修复功能显式重建该文件"))
            out["counts"] = _counts_all(root)
            return out
    else:
        out["fingerprint"] = None

    dv = m.get("data_version") if m else None
    fmt = m.get("format") if m else None
    has_data = _has_permanent_data(root)

    if m is None:
        if has_data:
            out["status"] = WARN
            out["code"] = LEGACY_V0
            issues.append(_issue(LEGACY_V0, WARN, True, "library",
                                 "检测到旧版资料库（无 manifest），程序已按 Data Format 0 识别，"
                                 "打开时会自动接管为 V1"))
        else:
            out["status"] = OK
            out["code"] = NEW_LIBRARY
        # fingerprint 对无 manifest 的库：用永久资料目录树的轻量指纹
        out["fingerprint"] = _tree_fingerprint(root)

    elif fmt != "usb-wiki-library":
        out["status"] = ERROR
        out["code"] = INVALID_LIBRARY
        issues.append(_issue(INVALID_LIBRARY, ERROR, False, "library",
                             "该目录不是 USB-WIKI 资料库，程序不会改动它"))
    elif isinstance(dv, int) and dv > 1:
        out["status"] = WARN
        out["code"] = DATA_FORMAT_TOO_NEW
        issues.append(_issue(DATA_FORMAT_TOO_NEW, WARN, False, "library",
                             f"资料格式版本 {dv} 高于当前程序支持的 1，请升级程序"))
    else:
        out["status"] = OK
        out["code"] = V1_OK

    out["data_version"] = dv if isinstance(dv, int) else (0 if (m is None and has_data) else None)
    out["counts"] = _counts_all(root)
    return out


def _has_permanent_data(root: Path) -> bool:
    for d in PERMANENT_DIRS:
        p = root / d
        if p.is_dir() and any(p.iterdir()):
            return True
    return False


def _counts_all(root: Path) -> dict:
    counts = {}
    total = 0
    for d in PERMANENT_DIRS:
        st = _dir_stats(root, d)
        counts[d] = st
        total += st["bytes"]
    counts["total_bytes"] = total
    return counts


def _tree_fingerprint(root: Path) -> str:
    h = hashlib.sha256()
    for d in PERMANENT_DIRS:
        for f in sorted((root / d).glob("*")):
            h.update(str(f.name).encode("utf-8", "replace"))
            h.update(str(f.stat().st_size if f.is_file() else -1).encode())
    return h.hexdigest()[:12]


# --------------------------------------------------------------------------
# Database / Index 探针
# --------------------------------------------------------------------------
_EXPECTED_TABLES = ("documents", "chunks", "chunks_fts", "parent_blocks", "chunk_metadata")


def _db_probe(root: Path, issues: list) -> dict:
    db_path = root / "cache.db"
    out = {"status": UNKNOWN, "code": None, "exists": db_path.exists(),
           "schema_version": None, "sqlite_version": sqlite3.sqlite_version,
           "tables": [], "documents": None, "chunks": None, "fts_rows": None,
           "vec_available": _vec_available()}
    if not db_path.exists():
        # Data Contract：cache 是 B 类可重建数据 → WARN 而非 ERROR
        out["status"] = WARN
        out["code"] = "CACHE_MISSING"
        issues.append(_issue("CACHE_MISSING", WARN, True, "database",
                             "索引数据库不存在，可从资料库重新建立索引"))
        return out

    # ⚠ 只读打开：mode=ro 从根上保证不会产生 WAL/锁文件、不会修改任何内容
    try:
        con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        out["status"] = WARN
        out["code"] = "CACHE_CORRUPTED"
        issues.append(_issue("CACHE_CORRUPTED", WARN, True, "database",
                             f"索引数据库无法打开（可整库重建）：{exc}"))
        return out

    try:
        out["tables"] = sorted(
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall())
        tables = set(out["tables"])

        try:
            row = con.execute(
                "SELECT value FROM sys_meta WHERE key='schema_version'").fetchone()
            out["schema_version"] = row[0] if row else None
        except sqlite3.Error:
            pass

        for t in _EXPECTED_TABLES:
            if t not in tables:
                issues.append(_issue(f"{t.upper()}_MISSING", WARN, True, "database",
                                     f"索引表 {t} 缺失，可从资料库重建"))
                out["status"] = WARN
                out["code"] = "CACHE_INCOMPLETE"
        if "chunks_fts" not in tables:
            issues.append(_issue("FTS_MISSING", WARN, True, "search",
                                 "全文索引缺失，可从资料库重建"))
        if "chunks_vec" not in tables:
            issues.append(_issue("VECTOR_INDEX_MISSING", WARN, True, "search",
                                 "向量索引缺失，可从资料库重建"))

        if "documents" in tables:
            out["documents"] = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        if "chunks" in tables:
            out["chunks"] = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if "chunks_fts" in tables:
            out["fts_rows"] = con.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]

        try:
            qc = con.execute("PRAGMA quick_check").fetchone()
            if not qc or qc[0] != "ok":
                out["status"] = WARN
                out["code"] = "CACHE_CORRUPTED"
                issues.append(_issue("CACHE_CORRUPTED", WARN, True, "database",
                                     f"索引完整性检查未通过：{qc[0] if qc else '无结果'}"))
            elif out["status"] == UNKNOWN:
                out["status"] = OK
        except sqlite3.Error as exc:
            out["status"] = WARN
            out["code"] = "CACHE_CORRUPTED"
            issues.append(_issue("CACHE_CORRUPTED", WARN, True, "database",
                                 f"索引完整性检查失败：{exc}"))
        return out
    except sqlite3.DatabaseError as exc:
        # 连接成功但读不了（文件不是数据库 / 页损坏）——仍属 B 类可重建
        out["status"] = WARN
        out["code"] = "CACHE_CORRUPTED"
        issues.append(_issue("CACHE_CORRUPTED", WARN, True, "database",
                             f"索引数据库无法读取（可整库重建）：{exc}"))
        return out
    finally:
        con.close()


def _vec_available() -> bool:
    try:
        import sqlite_vec  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# AI / Runtime 探针
# --------------------------------------------------------------------------
def _ai_probe(get_cfg, ollama_probe, issues: list) -> dict:
    provider = str(get_cfg("AI", "provider", "") or "")
    api_key_set = bool(str(get_cfg("AI", "api_key", "") or ""))
    ollama_base = redact.sanitize_url_userinfo(str(get_cfg("AI", "ollama_base", "") or ""))
    api_base = redact.sanitize_url_userinfo(str(get_cfg("AI", "api_base_url", "") or ""))
    emb_src = str(get_cfg("SYSTEM", "embedding_source", "") or "")

    out = {
        "provider": provider or None,
        "ollama_configured": bool(ollama_base),
        "ollama_reachable": None,          # 只在显式提供 probe 时才探测
        "model_configured": bool(str(get_cfg("AI", "ollama_model", "") or "")),
        "embedding_source": emb_src or None,
        "cloud_api_configured": bool(api_base and api_key_set),
        "api_key_set": api_key_set,
    }

    if out["ollama_configured"] and callable(ollama_probe):
        try:
            out["ollama_reachable"] = bool(ollama_probe())
        except Exception as exc:  # noqa: BLE001 - 探测失败不阻塞诊断
            out["ollama_reachable"] = False
            issues.append(_issue("OLLAMA_PROBE_FAILED", WARN, False, "ai",
                                 f"Ollama 探测失败（不影响诊断）：{exc}"))
    return out


def _runtime_probe() -> dict:
    import platform

    portable = (Path(sys.executable).resolve().parent.parent / "python-3.11-embed").is_dir() or \
               "python-3.11-embed" in str(Path(sys.executable).resolve())
    return {
        "python_version": sys.version.split()[0],
        "os": os.name,
        "machine": platform.machine() or None,
        "portable_runtime": bool(portable),
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def _app_probe() -> dict:
    # ⚠ 用绝对导入：version 在 app/version.py（包 app.version），
    # CLI shim 只把仓库根加进 sys.path，包内相对导入 .version 会指向不存在的
    # app.core.version —— CLI 冒烟当场抓到这个 bug。
    from app.version import APP_VERSION  # noqa: PLC0415
    schema = None
    try:
        from app.core.migrations import CURRENT_SCHEMA_VERSION  # noqa: PLC0415
        schema = CURRENT_SCHEMA_VERSION
    except Exception:  # noqa: BLE001 - 即使版本模块异常也要继续
        pass
    return {
        "app_version": APP_VERSION,
        "schema_version": schema,
        "data_format_version": 1,
    }


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
def collect(*, library_root: Path | None = None, ollama_probe=None) -> dict:
    """收集只读诊断报告。

    任何 probe 抛异常都不会中断整体收集 —— 该 probe 记为 ERROR issue，
    状态 UNKNOWN，其余 probe 照常执行。

    :param library_root: 资料库根目录；缺省用当前配置的 ``paths.DATA_DIR``。
    :param ollama_probe: 可注入的 Ollama 可达性探测（返回 bool）；
        传 None 则跳过探测（CI / 离线环境确定性）。
    """
    issues: list = []
    report: dict = {"status": UNKNOWN, "app": {}, "library": {}, "database": {},
                    "search": {}, "ai": {}, "runtime": {}, "issues": issues}

    # --- App（版本源必须可用；这里失败本身就是重大信息）---
    try:
        report["app"] = _app_probe()
    except Exception as exc:  # noqa: BLE001
        issues.append(_issue("APP_PROBE_FAILED", ERROR, False, "app",
                             f"版本信息读取失败：{exc}"))
        report["app"] = {"status": UNKNOWN}

    # --- Library ---
    try:
        root = Path(library_root) if library_root else _default_library_root()
        report["library"] = _library_probe(root, issues)
    except Exception as exc:  # noqa: BLE001
        issues.append(_issue("LIBRARY_PROBE_FAILED", ERROR, False, "library",
                             f"资料库诊断失败：{exc}"))
        report["library"] = {"status": UNKNOWN}

    # --- Database ---
    try:
        root = Path(library_root) if library_root else _default_library_root()
        report["database"] = _db_probe(root, issues)
    except Exception as exc:  # noqa: BLE001
        issues.append(_issue("DB_PROBE_FAILED", ERROR, False, "database",
                             f"数据库诊断失败：{exc}"))
        report["database"] = {"status": UNKNOWN}

    # --- Search（索引层一致性概览；细项已在 db probe）---
    try:
        dbp = report["database"]
        report["search"] = {
            "status": dbp.get("status", UNKNOWN),
            "documents": dbp.get("documents"),
            "chunks": dbp.get("chunks"),
            "fts_rows": dbp.get("fts_rows"),
            "vec_available": dbp.get("vec_available"),
        }
    except Exception as exc:  # noqa: BLE001
        report["search"] = {"status": UNKNOWN}
        issues.append(_issue("SEARCH_PROBE_FAILED", ERROR, False, "search", str(exc)))

    # --- AI（只查能力；Ollama 探测必须由调用方显式注入）---
    try:
        report["ai"] = _ai_probe(_cfg_get, ollama_probe, issues)
    except Exception as exc:  # noqa: BLE001
        issues.append(_issue("AI_PROBE_FAILED", WARN, False, "ai",
                             f"AI 配置读取失败：{exc}"))
        report["ai"] = {"status": UNKNOWN}

    # --- Runtime ---
    try:
        report["runtime"] = _runtime_probe()
    except Exception as exc:  # noqa: BLE001
        issues.append(_issue("RUNTIME_PROBE_FAILED", UNKNOWN, False, "runtime", str(exc)))
        report["runtime"] = {"status": UNKNOWN}

    # --- 汇总状态：ERROR > WARN > OK > UNKNOWN ---
    severities = [i["severity"] for i in issues]
    if "ERROR" in severities:
        report["status"] = ERROR
    elif "WARN" in severities:
        report["status"] = WARN
    else:
        report["status"] = OK
    return report


def _default_library_root() -> Path:
    try:
        from .paths import DATA_DIR  # noqa: PLC0415
        return Path(DATA_DIR)
    except Exception:  # noqa: BLE001 - paths 失败也要能诊断
        return Path(os.environ.get("WIKIUSB_LIBRARY") or Path.cwd()) / "data"


def _cfg_get(section: str, key: str, default: str = "") -> str:
    try:
        from . import config  # noqa: PLC0415
        return config.get_str(section, key, default)
    except Exception:  # noqa: BLE001 - config 坏了诊断也要能跑
        return default


if __name__ == "__main__":  # pragma: no cover - CLI 出口：server 起不来也能诊断
    import argparse

    _ap = argparse.ArgumentParser(description="USB-WIKI 只读系统诊断")
    _ap.add_argument("--json", action="store_true", help="输出完整 JSON")
    _ap.add_argument("--library", default=None, help="资料库根目录（缺省用当前配置）")
    _args = _ap.parse_args()
    _r = collect(library_root=Path(_args.library) if _args.library else None)
    if _args.json:
        print(json.dumps(_r, ensure_ascii=False, indent=2))
    else:
        print(f"status={_r['status']}  "
              f"app={_r['app'].get('app_version')}  "
              f"library={_r['library'].get('code')}  "
              f"database={_r['database'].get('code') or _r['database'].get('status')}")
        for i in _r["issues"]:
            print(f"  [{i['severity']}] {i['code']} ({i['component']}) "
                  f"repairable={i['repairable']}: {i['message']}")
