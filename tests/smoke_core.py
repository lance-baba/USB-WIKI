"""核心链路冒烟测试：切片 -> 索引 -> FTS/向量/RRF 检索。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import context, indexer, paths, search  # noqa: E402

NOTE = """---
title: "Attention Is All You Need 研读笔记"
status: success
---

# 注意力机制与 Transformer

Transformer 模型的核心是自注意力（Self-Attention）结构，它能够捕捉序列中的长距离依赖，
彻底摆脱了循环神经网络逐时间步计算的串行瓶颈。多头注意力允许模型在不同子空间中并行关注
不同位置的信息。

## 位置编码

由于自注意力本身不具备序列顺序感知能力，Transformer 通过位置编码（Positional Encoding）
向输入注入位置信息。原始论文采用正弦余弦函数构造位置编码。

参考 [[Transformer 架构通俗指南]] 与 [[多头注意力详解]]。

## 工程实践

在 AI 应用中，Transformer 已成为事实标准。落地时需要注意显存占用与推理延迟的平衡，
量化与蒸馏是两条常用压缩路线。
"""


def main() -> int:
    ctx = context.get_ctx()
    rep = ctx.boot(start_syncer=False, probe_ollama=False)
    print("BOOT:", json.dumps(rep, ensure_ascii=False, indent=2))
    print("DB stats:", json.dumps(ctx.db.stats(), ensure_ascii=False))

    p = paths.NOTES_DIR / "papers_attention.md"
    p.write_text(NOTE, encoding="utf-8")

    r = indexer.index_file(ctx.db, p, ctx.embedder)
    print("INDEX:", json.dumps(r, ensure_ascii=False))

    ok = 0
    for q in ["注意力机制与 Transformer 的关系", "AI", "注", "位置编码"]:
        res = search.hybrid_search(ctx.db, ctx.embedder, q, top_k_parents=3, candidates=20)
        print(f"\nQ={q!r} route={res.route} counts={res.counts}")
        for ref in res.references:
            print(f"   [{ref.id}] {ref.title} score={ref.score} sim={ref.similarity}")
            print(f"       {ref.snippet[:80]}")
        if res.references:
            ok += 1

    print(f"\n召回成功 {ok}/4")
    return 0 if ok == 4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
