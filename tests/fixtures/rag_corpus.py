"""RAG 检索回归语料（**完全人工构造、可公开**）。

## 为什么要有它

现有测试集中在「有没有崩 / 数据库对不对 / 路由通不通」，一旦改动
chunker / FTS / LIKE 兜底 / embedding / RRF / parent block / query normalization /
citation，**不知道真实检索效果有没有被改坏**。这里建立一套稳定的**行为基线**。

## 定性：这是回归套件，不是 benchmark

不追求「准确率 92%」这类人为评分。每条 case 写明预期文档，
断言的是：

    expected_doc ∈ Top-K          （优先）
    forbidden_doc ∉ Top-K         （负样本，防「什么都召回」）

只有非常确定的精确查询才要求 `top1`。**不把排名写死到脆弱**——
排序细节会随合理改动变化，但「正确的文档必须能被搜到」不该变。

## 刻意不含真实资料

这里的文档全部人工撰写，任何真实知识库 / 私人文档都不得进入仓库。
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# 语料：25 篇，覆盖不同语言 / 体裁 / 干扰关系
# --------------------------------------------------------------------------
DOCS: dict[str, str] = {
    # —— 中文长文（技术）——
    "cn_rag_long.md": """---
title: "检索增强生成的分块与召回策略"
---

# 检索增强生成的分块与召回策略

检索增强生成的核心在于把长文档切成合适的片段，再按查询召回最相关的几段。
分块粒度直接影响召回质量：切得太碎会丢失上下文，切得太粗会让噪声挤占上下文窗口。

## 分块粒度

实践中常用「父块 + 子块」两层结构。子块粒度小、便于精确匹配；父块粒度大、
便于给模型足够上下文。检索时先用子块命中，再回溯到父块注入提示词，
这样既保证命中率，也保证上下文完整。

## 召回融合

词法召回与向量召回各有所长。词法召回对专有名词与精确术语更敏感，
向量召回对同义表达与语义改写更宽容。把两路结果用倒数排名融合（RRF）
合并，可以在两类查询上都保持稳定，而不必为某一类查询手工调权重。

## 上下文预算

注入给模型的上下文必须有硬上限。实测在本地小模型上，单段过长会显著降低
回答质量：模型会把注意力分散到无关段落上。因此需要按段封顶并按总量封顶。

## 常见退化

降级链是这类系统的必备设计：本地嵌入引擎不可用时退到远程嵌入服务，
再不可用则退到纯词法检索。任何一级降级都必须在界面上如实告知，
否则用户会以为「搜不到就是没有」。
""",
    "cn_chunker_short.md": """---
title: "切片长度与重叠"
---

# 切片长度与重叠

切片长度建议控制在数百字量级，相邻切片之间保留少量重叠，
以免关键句子正好被切在边界上而两边都匹配不全。
重叠过多会让同一内容被重复召回，反而稀释排序。
""",
    # —— 中文短文（叙述）——
    "cn_story_short.md": """---
title: "城南旧书店"
---

城南那条巷子里有一家旧书店，老板姓陈，做了三十年。店里最贵的书放在玻璃柜里，
最便宜的摞在门口的木箱上，任人翻。陈老板说，书是拿来读的，不是拿来供的。
""",
    # —— 英文 ——
    "en_vector_index.md": """---
title: "Vector Index Trade-offs"
---

# Vector Index Trade-offs

Approximate nearest neighbour indexes trade recall for latency. An HNSW graph
gives excellent recall at low latency but costs memory, while IVF with product
quantisation shrinks the index at the price of accuracy.

Exact search is only practical for small collections. For a personal knowledge
base with a few thousand chunks, an exhaustive scan is often fast enough and
avoids the tuning burden of an approximate index entirely.
""",
    "en_short_note.md": """---
title: "On Writing Notes"
---

# On Writing Notes

A note that cannot be found later is a note that was never written. Prefer
searchable phrasing over clever phrasing.
""",
    # —— 中英混合 ——
    "mixed_dev_note.md": """---
title: "SQLite FTS5 与 BM25 调优笔记"
---

# SQLite FTS5 与 BM25 调优笔记

FTS5 的 trigram tokenizer 对中文很友好，但**最小可用粒度是 3 个字符**，
两个汉字的查询会匹配不到，需要回落 LIKE。

BM25 的 k1 与 b 参数决定词频饱和与长度归一化的强度。默认值在短文档集上
通常够用，但若语料里文档长度差异很大，建议适当调低 b。

Caveat: the `bm25()` function takes column weights, so remember the leading
positional arguments are the weights, not the query.
""",
    # —— 技术文档：代码 ——
    "code_python_snippet.md": """---
title: "把长文本切片的 Python 实现"
---

# 把长文本切片的 Python 实现

```python
def split_blocks(text, size=800, overlap=80):
    step = size - overlap
    for i in range(0, len(text), step):
        yield text[i:i + size]
```

注意 `overlap` 必须小于 `size`，否则步长会变成 0 或负数，导致死循环。
""",
    "code_shell_snippet.md": """---
title: "常用 shell 片段"
---

# 常用 shell 片段

```bash
grep -rn "TODO" ./src | wc -l
```

统计待办数量时注意排除 `.git` 目录，否则会把历史提交也数进去。
""",
    "code_csharp_snippet.md": """---
title: "C# 里的异步等待"
---

# C# 里的异步等待

C# 用 async/await 表达异步流程。await 会挂起当前方法而不阻塞线程，
底层由状态机实现。注意不要在循环里忘记 await，否则异常会被吞掉。
""",
    # —— 人名 ——
    "person_zhang.md": """---
title: "张伟的项目复盘"
---

# 张伟的项目复盘

张伟在复盘里提到，这次延误主要是因为需求变更没有走评审流程。
他建议后续所有变更都留下书面记录，并在周会上同步。
""",
    "person_alan.md": """---
title: "Alan Turing and the Halting Problem"
---

# Alan Turing and the Halting Problem

Alan Turing proved that no general algorithm can decide, for every program and
input, whether that program halts. The proof uses a diagonal argument that
assumes such a decider exists and then constructs a program that contradicts it.
""",
    # —— 产品名（含易混干扰）——
    "product_krevix.md": """---
title: "KrevixAI 使用说明"
---

# KrevixAI 使用说明

KrevixAI 的默认并发上限是 4，超过会排队。若需要提高上限，请在配置里
显式调大 concurrency，并确认下游服务能承受。
""",
    "product_krevix_clone.md": """---
title: "KrevixAi 与 KreviX 的区别"
---

# KrevixAi 与 KreviX 的区别

KreviX 是一个模型名称，KrevixAi 是围绕它构建的产品。两者容易被混写，
但指的并不是同一件事。
""",
    # —— 数字 ——
    "numbers_report.md": """---
title: "2026 年第三季度运营数据"
---

# 2026 年第三季度运营数据

第三季度新增用户 12480 人，环比增长 17.3%；付费转化率 4.6%。
客单价 396 元，退款率 1.2%。库存周转天数从 58 天降到 41 天。
""",
    # —— 干扰：同主题不同含义 ——
    "typhoon_weather.md": """---
title: "台风杜鹃的路径与降雨"
---

# 台风杜鹃的路径与降雨

台风杜鹃在近海加强，预计带来大范围强降雨，沿海地区需防范风暴潮。
气象部门提醒，降雨集中时段应避免前往山区。
""",
    "typhoon_fighter.md": """---
title: "台风战斗机的气动布局"
---

# 台风战斗机的气动布局

台风战斗机采用鸭式三角翼布局，强调瞬时盘旋能力。
它的气动设计与气象现象「台风」没有任何关系，只是译名相同。
""",
    "postgres_note.md": """---
title: "PostgreSQL 的索引选择"
---

# PostgreSQL 的索引选择

PostgreSQL 里 B-tree 适合等值与范围查询，GIN 适合数组与全文检索。
选择索引前先看执行计划，不要凭感觉加索引。
""",
    "postgrey_note.md": """---
title: "Postgrey 邮件灰名单"
---

# Postgrey 邮件灰名单

Postgrey 是 Postfix 的灰名单插件，对首次来信延迟放行以拦截垃圾邮件。
注意它和 PostgreSQL 只差几个字母，但完全是两回事。
""",
    # —— 普通叙述 ——
    "essay_reading.md": """---
title: "关于慢读"
---

# 关于慢读

读书的速度不该由页数决定，而该由难度决定。难的地方值得停下来，
简单的地方可以略过。把每本书都读成同一速度，等于放弃了判断。
""",
    # —— 弱结构 / 无标题 ——
    "plain_notes.md": """---
title: "随手记"
---

没有小标题的一段记录：今天把索引重建了一次，耗时比预期长，
主要花在向量写入上。下次可以先只重建词法索引，向量部分延后。
""",
    # —— 长文（章节多，验证 parent block）——
    "multi_section_manual.md": """---
title: "便携应用部署手册"
---

# 便携应用部署手册

## 运行时形态

便携版把解释器与依赖一起打包，使用者无需预装任何东西。
代价是体积变大，因此重型可选依赖默认不随包分发。

## 首次启动

首次启动会生成默认配置并扫描数据目录。若上一轮异常退出，
启动时会检查并修复残留的数据库日志文件。

## 索引重建

索引由笔记文件派生，随时可以整库重建。重建不会修改笔记文件本身，
这一点是数据安全的前提。

## 故障排查

检索不到内容时，先确认索引是否已建立，再确认查询词是否过短。
两个字符以下的中文查询依赖兜底路径，命中质量会略低。
""",
    "index_fallback.md": """---
title: "短查询的兜底路径"
---

# 短查询的兜底路径

三元组索引最少需要三个字符才能匹配，两字词与单词缩写必须走子串兜底。
兜底路径没有排序能力，因此只作为召回保底，不参与排名融合。
""",
    "citation_note.md": """---
title: "引用溯源的做法"
---

# 引用溯源的做法

回答里的每个角标都应当指向真实的来源片段。引用列表需要去重，
同一个父块被多次命中时只保留一次，否则脚注会重复得很难看。
""",
}

# --------------------------------------------------------------------------
# 用例：每条写明模式、预期文档、禁止文档
# --------------------------------------------------------------------------
# mode:
#   fts    —— 直接走 FTS5 词法
#   like   —— 短查询兜底（子串）
#   hybrid —— 双路 + RRF（用确定性哈希嵌入）
#   noemb  —— 无嵌入源时的降级（应等价于纯词法）
CASES: list[dict] = [
    # —— 正常中文短查询 ——
    {"q": "检索", "mode": "fts", "expect": "cn_rag_long", "note": "两字中文"},
    {"q": "分块", "mode": "fts", "expect": "cn_rag_long"},
    {"q": "重叠", "mode": "fts", "expect": "cn_chunker_short"},
    # —— 中文长查询 ——
    {"q": "父块和子块分别用来做什么", "mode": "hybrid", "expect": "cn_rag_long"},
    {"q": "上下文预算为什么要封顶", "mode": "hybrid", "expect": "cn_rag_long"},
    {"q": "索引重建会不会改笔记文件", "mode": "hybrid", "expect": "multi_section_manual"},
    # —— 英文 ——
    {"q": "HNSW", "mode": "fts", "expect": "en_vector_index"},
    {"q": "vector index recall latency", "mode": "hybrid", "expect": "en_vector_index"},
    {"q": "halting problem", "mode": "hybrid", "expect": "person_alan"},
    # —— 中英混合 ——
    {"q": "FTS5 的 bm25 参数", "mode": "hybrid", "expect": "mixed_dev_note"},
    {"q": "trigram 最小粒度", "mode": "hybrid", "expect": "mixed_dev_note"},
    {"q": "SQLite 全文检索", "mode": "hybrid", "expect": "mixed_dev_note"},
    # —— 短词（LIKE 兜底）——
    {"q": "AI", "mode": "like", "expect": "product_krevix", "note": "两字符英文"},
    {"q": "C#", "mode": "like", "expect": "code_csharp_snippet", "note": "符号"},
    {"q": "陈", "mode": "like", "expect": "cn_story_short", "note": "单汉字（独特字）"},
    {"q": "书", "mode": "like", "expect": "cn_story_short", "note": "单汉字"},
    # —— 专有名词 ——
    {"q": "KrevixAI", "mode": "fts", "expect": "product_krevix"},
    {"q": "张伟", "mode": "fts", "expect": "person_zhang"},
    {"q": "PostgreSQL", "mode": "fts", "expect": "postgres_note"},
    # —— 数字 ——
    {"q": "12480", "mode": "fts", "expect": "numbers_report"},
    {"q": "转化率", "mode": "fts", "expect": "numbers_report"},
    {"q": "第三季度", "mode": "fts", "expect": "numbers_report"},
    # —— 代码符号 ——
    {"q": "overlap", "mode": "fts", "expect": "code_python_snippet"},
    {"q": "grep", "mode": "fts", "expect": "code_shell_snippet"},
    {"q": "def split_blocks", "mode": "hybrid", "expect": "code_python_snippet"},
    # —— 同义 / 语义相近但关键词不同 ——
    {"q": "切得太碎会有什么问题", "mode": "hybrid", "expect": "cn_rag_long"},
    {"q": "引用列表怎么去重", "mode": "hybrid", "expect": "citation_note",
     "note": "自然语言问句 + 词法锚点；纯语义改写不在此测，避免依赖哈希嵌入的语义能力"},
    {"q": "脚注重复", "mode": "hybrid", "expect": "citation_note"},
    # —— 干扰项：同主题不同含义（负样本）——
    {"q": "台风杜鹃的路径", "mode": "hybrid", "expect": "typhoon_weather",
     "forbid": ["typhoon_fighter"], "note": "气象 vs 战机"},
    {"q": "台风战斗机的气动布局", "mode": "fts", "expect": "typhoon_fighter",
     "forbid": ["typhoon_weather"]},
    {"q": "PostgreSQL 索引", "mode": "hybrid", "expect": "postgres_note",
     "forbid": ["postgrey_note"], "note": "只差几个字母"},
    {"q": "邮件灰名单", "mode": "hybrid", "expect": "postgrey_note",
     "forbid": ["postgres_note"]},
    {"q": "KrevixAI 并发上限", "mode": "fts", "expect": "product_krevix",
     "forbid": ["product_krevix_clone"]},
    # —— 精确查询：可以要求 Top-1 ——
    {"q": "12480", "mode": "fts", "expect": "numbers_report", "top1": "numbers_report"},
    {"q": "halting problem", "mode": "fts", "expect": "person_alan", "top1": "person_alan"},
    # —— 应搜不到（负样本：防「什么都召回」）——
    {"q": "量子纠缠退相干", "mode": "hybrid", "expect": None, "note": "语料里没有"},
    {"q": "zzqqxx9988", "mode": "like", "expect": None},
    {"q": "Blockchain consensus", "mode": "hybrid", "expect": None},
    {"q": "的了吗呢", "mode": "hybrid", "expect": None, "note": "纯虚词"},
    # —— 重复来源不应霸榜 ——
    {"q": "台风", "mode": "hybrid", "expect": "typhoon_weather", "max_per_doc": 1,
     "note": "同一文档不应占据多个 Top-K 名额"},
    # —— citation ——
    {"q": "引用要指向真实来源", "mode": "hybrid", "expect": "citation_note", "cites": True},
    {"q": "父块与子块", "mode": "hybrid", "expect": "cn_rag_long", "cites": True},
    {"q": "HNSW", "mode": "hybrid", "expect": "en_vector_index", "cites": True},
]
