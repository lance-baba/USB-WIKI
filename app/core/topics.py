"""主题分组 —— 按关键词把笔记自动归题，替代「节点图」形态。

## 为什么换掉节点图

实测本项目的真实语料：12 篇笔记分成 **9 个互不相连的孤岛**（6 个还是单篇），
3 条边里只有 1 条算真关系（另外两条是「同一篇文章抓了两次」和「同属 x.com」）。

根因不是算法差，是**形态不匹配**：本项目是「剪藏一批互不相关页面 + 提问」的
稍后读/问答工具，内容天然没有主题簇。而 Obsidian 的图好看是因为用户在持续手写
链接、Connected Papers 能看是因为论文之间有真实引用 —— 我们两者都没有。

**需要的是「这些笔记在讲什么」的自动索引，不是让人自己在网里找关系。**

## 做法

以**关键词为轴**分组（而不是聚类成互斥的簇）：

- 某一关键词出现在 ≥2 篇笔记里 → 就是一个主题组，组下列出这些笔记
- 一篇笔记可以同时属于多个主题（「台风」与「气象」），这符合直觉；
  强行聚成互斥的簇在稀疏语料下只会再次退化成一堆单篇孤岛
- 每个组标注「为什么归到一起」与笔记数，点进去就是笔记列表

零模型、离线可用，且随笔记增多自然长出更多主题 —— 与检索层的关键词同源。
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from .db import Database
from .log_util import get_logger

log = get_logger()

# 太泛的词做主题没有导航价值（「数据」「内容」这种几乎每篇都有）
_TOO_GENERIC = {
    "数据", "内容", "信息", "问题", "情况", "方面", "方式", "方法", "时间", "项目",
    "公司", "工作", "使用", "进行", "包括", "相关", "主要", "重要", "目前", "结果",
    "data", "content", "information", "project", "system", "using", "based",
}
_MIN_TERM_LEN = 2
_MAX_TERM_LEN = 12


@dataclass
class Topic:
    term: str
    docs: list[dict] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.docs)


def _usable(term: str) -> bool:
    t = (term or "").strip()
    if not (_MIN_TERM_LEN <= len(t) <= _MAX_TERM_LEN):
        return False
    if t in _TOO_GENERIC or t.lower() in _TOO_GENERIC:
        return False
    # 纯数字/纯符号做不了主题
    return bool(re.search(r"[^\W\d_]", t, re.UNICODE))


def build_topics(db: Database, min_docs: int = 2, limit: int = 40) -> dict:
    """按关键词把笔记归成主题组。

    返回 ``{topics: [{term, count, docs:[{title, path, language}]}], stats: {...}}``。
    """
    try:
        rows = db.query(
            """SELECT d.doc_id, d.rel_path, d.title,
                      COALESCE(m.keywords, '') AS keywords,
                      COALESCE(m.language, '') AS language
                 FROM documents d
                 LEFT JOIN doc_meta m ON m.doc_id = d.doc_id
                 ORDER BY d.mtime DESC"""
        )
    except Exception as exc:  # noqa: BLE001
        log.error("主题分组查询失败: %s", exc)
        return {"topics": [], "stats": {"error": str(exc)}}

    docs = [
        {
            "doc_id": r["doc_id"],
            "path": r["rel_path"] or "",
            "title": (r["title"] or r["rel_path"] or "").strip(),
            "language": r["language"] or "",
            "keywords": [k for k in (r["keywords"] or "").split() if k],
        }
        for r in rows
    ]

    buckets: dict[str, list[dict]] = defaultdict(list)
    for d in docs:
        for term in d["keywords"]:
            if _usable(term):
                buckets[term].append(d)

    # 只保留出现在 ≥2 篇笔记里的词：单篇独有的词做不成"主题"，
    # 放进来只会让每组都是一个孤岛（这正是节点图失败的原因，不能重蹈）。
    topics = [
        Topic(term=t, docs=ds)
        for t, ds in buckets.items()
        if len(ds) >= min_docs
    ]
    topics.sort(key=lambda x: (-x.count, x.term))

    covered = {d["doc_id"] for t in topics for d in t.docs}
    ungrouped = [d for d in docs if d["doc_id"] not in covered]

    return {
        "topics": [
            {
                "term": t.term,
                "count": t.count,
                "docs": [
                    {"title": d["title"], "path": d["path"], "language": d["language"]}
                    for d in sorted(t.docs, key=lambda x: x["title"])
                ],
            }
            for t in topics[:limit]
        ],
        "ungrouped": [
            {"title": d["title"], "path": d["path"]} for d in ungrouped
        ],
        "stats": {
            "docs": len(docs),
            "topics": len(topics),
            "grouped_docs": len(covered),
            "ungrouped_docs": len(ungrouped),
            "min_docs": min_docs,
        },
    }
