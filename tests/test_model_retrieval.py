"""D：型号/规格查询必须优先「目标实体 ↔ 型号」的直接关系。

回归门（D6）：
  合成 fixture 含设备表（电子水准仪 | 天宝DINI03）与测量要求
  （水位仪读数精度±1mm / 水准仪精度不低于N2级 / 水准仪检定有效期…）。

  Query「水准仪是什么型号的？」必须：
    - 命中 model 类型；
    - Top1 = 设备表（含 天宝DINI03）；
    - 证据含「电子水准仪 / 天宝DINI03」；
    - 不得把 ±1mm / N2 / 检定有效期 当型号（旧 bug）。

  实体边界（D5）：问「水准仪」不能因为「水位仪」字形接近而当作目标实体。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.core import chunker, db as db_mod, indexer, search as S
from app.core.embedder import HashEmbedder


def _build(q: str):
    fixture = """---
title: "设备型号 fixture"
---
### 测量要求

水位仪读数精度为±1mm。
管口高程用水准仪定期联测。
水准仪精度不低于N2级。
水准仪检定有效期至2026年5月14日。

### 设备表

| 设备名称 | 规格型号 | 数量 |
| 电子水准仪 | 天宝DINI03 | 1台 |
| 全站仪 | 徕卡TS09 | 2台 |
"""
    tmp = Path(tempfile.mkdtemp(prefix="modeltest_"))
    database = db_mod.get_db(db_path=tmp / "c.db", embedding_dim=512)
    database.init_schema()
    emb = HashEmbedder(512)
    parsed = chunker.parse(fixture, "notes/dev_fixture.md")
    indexer.index_parsed(database, parsed, len(fixture.encode()), 0.0, embedder=emb)
    return database, emb, q


def run(ctx, check, section, skip) -> None:
    section("D 型号/规格检索：实体 ↔ 型号直接关系")
    database, emb, q = _build("水准仪是什么型号的？")
    try:
        ana = S.analyze_query(q)
        dbg = {}
        res = S.hybrid_search(database, emb, q, top_k_parents=5, debug=dbg)
        top = dbg["final_topk"][0] if dbg.get("final_topk") else None
        check("D 问句被判为 model 类型", ana.question_type == "model", ana.question_type)
        check("D Top1 落在「设备表」章节（而非测量要求/精度）",
              bool(top) and "设备表" in (top.get("section_path") or ""),
              top.get("section_path") if top else None)
        snip = (res.references[0].snippet or "") if res.references else ""
        check("D 证据含真实型号 天宝DINI03", "天宝DINI03" in snip, snip[:40])
        check("D 证据含目标实体 电子水准仪", "电子水准仪" in snip, snip[:40])
        # 旧 bug：±1mm / N2（精度等级）/ 检定有效期 不得成为型号答案
        low = (res.references[0].snippet or "")
        check("D 旧 bug 未复发：证据不含 ±1mm/N2/检定有效期",
              not any(k in low for k in ("±1mm", "N2", "检定有效期")), low[:40])
        # D5：实体边界 —— 证据不得把水位仪当目标实体
        check("D5 目标实体为水准仪而非水位仪",
              "水准仪" in snip and "水位仪读数精度" not in snip, snip[:40])
    finally:
        database.checkpoint_and_close()
