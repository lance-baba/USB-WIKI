"""恢复脚本：从 cache.db.v16r*.bak 的父块正文重建 data/notes/*.md。

背景：本轮在**未隔离 workspace** 下跑 targeted tests，tests 的 reset_workspace()
删除了 data/notes/*.md 与 cache.db（这两者在 .gitignore 里，git 无法恢复）。
schema 升级时自动留下的 cache.db.v*R*.bak 仍保存着每篇笔记的全部父块正文 → 据此重建。

保真度说明：
* 正文按 parent_blocks.ord 顺序拼接，段落文本与原文一致；
* super-long 块（>PARENT_HARD_LIMIT）曾被硬切，拼接处可能出现表格行被切成两行的情况；
* frontmatter 不在索引里 → 用 documents.title + doc_meta.display_source 生成最小 frontmatter。
"""
from __future__ import annotations

import datetime as _dt
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTES = ROOT / "data" / "notes"
BACKUP = ROOT / "data" / "cache.db.v16r20260920-185807.bak"

NOTES.mkdir(parents=True, exist_ok=True)
con = sqlite3.connect(str(BACKUP))
con.row_factory = sqlite3.Row

docs = con.execute("SELECT doc_id, rel_path, title FROM documents ORDER BY rel_path").fetchall()
meta = {r["doc_id"]: r for r in con.execute("SELECT * FROM doc_meta")}
restored, skipped = [], []

for d in docs:
    rel = (d["rel_path"] or "").replace("\\", "/")
    name = Path(rel).name
    if not name.endswith(".md"):
        skipped.append(rel)
        continue
    rows = con.execute(
        "SELECT content FROM parent_blocks WHERE doc_id = ? ORDER BY ord", (d["doc_id"],)
    ).fetchall()
    body = "\n\n".join((r["content"] or "").strip() for r in rows if (r["content"] or "").strip())
    if not body:
        skipped.append(rel)
        continue
    m = meta.get(d["doc_id"])
    disp = (m["display_source"] if m and m["display_source"] else "") or ""
    title = (d["title"] or "").strip() or Path(name).stem
    fm = ["---", f'title: "{title}"']
    if disp:
        fm.append(f'source_file: "{disp}"')
    fm.append('source_type: "restored"')
    fm.append(f'restored_at: "{_dt.datetime.now():%Y-%m-%d %H:%M:%S}"')
    fm.append("restored_from: \"" + BACKUP.name + "\"")
    fm.append("---")
    (NOTES / name).write_text("\n".join(fm) + "\n\n" + body + "\n", encoding="utf-8")
    restored.append((name, len(body)))

con.close()
print(f"restored {len(restored)} notes:")
for n, chars in restored:
    print(f"  {n}  ({chars} chars)")
if skipped:
    print("skipped:", skipped)
