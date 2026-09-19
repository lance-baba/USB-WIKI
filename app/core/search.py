"""双路混合检索（PRD 4.3）：FTS5 Trigram 词法 + sqlite-vec 向量 + RRF 倒排融合。

检索路由：
* 满足「纯英文/数字 ≥ 3 字符」或「≥ 3 个中文字符」→ FTS5 MATCH（trigram 索引最小可用粒度）
* 更短的查询（单汉字、"AI"、"C#" 等）→ 自动退化普通表扫描 ``LIKE '%kw%'``，保证 100% 召回
* 另加「MATCH 空结果自动回落 LIKE」的兜底，杜绝短查询静默丢召回

融合公式（PRD 4.3）：
    Score(d) = 1/(60 + Rank_fts(d)) + 1/(60 + Rank_vec(d))
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from . import chunker, config
from .db import Database
from .log_util import get_logger

log = get_logger()

RRF_K = 60
_TERM_RE = re.compile(r"[A-Za-z0-9_+#.]+|[\u3400-\u4dbf\u4e00-\u9fff]+")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
LIKE_LIMIT = 50


@dataclass
class Hit:
    chunk_id: str
    doc_id: str
    parent_id: str = ""
    content: str = ""
    score: float = 0.0
    rank_fts: int | None = None
    rank_vec: int | None = None
    distance: float | None = None


@dataclass
class Reference:
    id: int
    title: str
    path: str
    snippet: str
    parent_id: str
    doc_id: str
    score: float
    similarity: float | None = None


@dataclass
class SearchResult:
    query: str
    route: str
    counts: dict = field(default_factory=dict)
    references: list[Reference] = field(default_factory=list)
    parents: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
def query_terms(query: str) -> list[str]:
    return [m.group(0) for m in _TERM_RE.finditer(query or "")]


# --------------------------------------------------------------------------
# 中文虚词 / 英文停用词：它们几乎出现在每篇文档里，一旦参与检索只会把无关文档
# 拉进候选。同级项目实测过「黄仁勋说了什么」因虚词劫持返回一堆无关结果。
_STOPWORDS = {
    "的", "了", "是", "在", "有", "和", "与", "或", "及", "就", "也", "都", "而", "吗", "呢",
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "这", "那", "这个", "那个",
    "什么", "怎么", "怎样", "如何", "为什么", "哪些", "哪个", "多少", "是否", "能否",
    "吧", "啊", "呀", "么", "哦", "嗯", "说", "讲", "提到", "关于", "对于", "以及",
    "还有", "可以", "一下", "一些", "有点", "比较", "非常", "很", "太", "更", "最",
    "请", "帮", "帮我", "告诉", "介绍", "总结", "概括", "列出", "看看",
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "of", "to", "in", "on",
    "at", "by", "for", "with", "from", "about", "as", "that", "this", "these", "those",
    "what", "which", "who", "whom", "whose", "how", "why", "when", "where",
    "do", "does", "did", "can", "could", "should", "would", "will", "may", "might",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us", "them",
    "and", "or", "not", "no", "yes", "please", "tell", "show", "list",
}

_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")

# 问句的虚词几乎总是出现在两端（…吗 / …是什么 / …说了什么）。
# 按长度降序，先长后短，避免「是什么」被「是」抢先切掉。
_CJK_EDGE_STOPWORDS = (
    "有什么", "是什么", "说了什么", "怎么样", "怎么用", "干什么",
    "这个", "那个", "这些", "那些", "一些", "一下",
    "怎么", "什么", "如何", "哪些", "哪个",
    "吗", "呢", "吧", "啊", "呀", "么", "的", "了", "是", "有", "在", "和", "与", "及",
)


_CJK_SPLIT_CHARS = "的了吗呢吧啊呀么是在和与及有"


def _is_usable_term(t: str) -> bool:
    """剥离后剩下的东西必须还是个实词。

    反例：「是什么」剥到只剩「什么」、「的了吗呢」剥到只剩「吗」——
    这些本身就是虚词，拿去 LIKE 只会把无关文档全捞出来，必须丢弃。
    """
    return bool(t) and t not in _STOPWORDS and t.lower() not in _STOPWORDS


def _has_content_chars(run: str) -> bool:
    """整串是否含实词字（全是「的了吗呢」这种就属于无实词）。"""
    return any(c not in _CJK_SPLIT_CHARS for c in run)


def _split_cjk_run(run: str) -> list[str]:
    """把一个 CJK 连续串切成实词片段。

    两步：先剥两端虚词（「台风有吗」→「台风」），
    再按**中间**的虚词字切段（「台风杜鹃的路径」→「台风杜鹃」+「路径」）。
    两段用 AND 组合命中同一文档 —— 这比整串当子串可靠得多。
    """
    core = _strip_edges(run)
    if not core:
        return []
    parts = re.split(f"[{_CJK_SPLIT_CHARS}]", core)
    return [p for p in parts if _is_usable_term(p)]


def query_terms(query: str) -> list[str]:
    return [m.group(0) for m in _TERM_RE.finditer(query or "")]


def _strip_edges(run: str) -> str:
    """剥掉 CJK 串两端的虚词，返回中间的实词主体。

    最多剥三层：既要处理「台风有吗 → 台风」（尾），
    也要处理「这个台风是什么 → 台风」（首尾兼有），又不能无限剥下去把词切碎。
    """
    s = run
    for _ in range(3):
        before = s
        for w in _CJK_EDGE_STOPWORDS:
            if len(s) > len(w) and s.endswith(w):
                s = s[: -len(w)]
            if len(s) > len(w) and s.startswith(w):
                s = s[len(w):]
        if s == before:
            break
    return s


def content_term_sets(query: str) -> list[list[str]]:
    """给出**多套候选实词**（按优先级），供词法检索逐个尝试。

    为什么需要多套：中文没有词边界，正则切不出「台风有吗」里的「台风」。
    这里刻意不引入分词器（保持零依赖），而是：

    1. 首选「剥掉两端虚词后的实词主体」—— 覆盖「台风有吗 / 台风是什么」这类问句；
    2. 若首选零命中，再退回**未剥离的原串** —— 这样「有机食品」这种以虚词字
       开头的真词不会被切坏（剥成「机食品」查不到，回退到原串就能查到）。

    语料驱动的精确分词（用库内实际出现的词做最长匹配）留作后续升级项。
    """
    primary: list[str] = []
    fallback: list[str] = []
    for t in query_terms(query):
        if _CJK_RUN_RE.fullmatch(t):
            parts = _split_cjk_run(t)
            if not parts:
                # 整段都是虚词（如「是什么」「的了吗呢」）→ 丢弃。
                # 若把它当实词留下，AND 组合里会出现一个永不命中的条件，
                # 把整个查询的正确结果一起清零（「KrevixAi 是什么」就是这么被坑的）。
                continue
            if parts != [t]:
                primary.extend(parts)
                if _has_content_chars(t):
                    fallback.append(t)
                continue
        if not _is_usable_term(t):
            continue
        primary.append(t)
        fallback.append(t)

    sets: list[list[str]] = []
    if primary:
        sets.append(primary)
    if fallback and fallback != primary:
        sets.append(fallback)
    return sets


def content_terms(query: str) -> list[str]:
    """首选实词集合（多套候选的第一套）。"""
    sets = content_term_sets(query)
    return sets[0] if sets else []


def should_use_like_terms(terms: list[str]) -> bool:
    """trigram 索引最小可用粒度为 3 字符，更短的必须走 LIKE 降级通道。"""
    if not terms:
        return True
    return any(len(t) < 3 for t in terms)


def should_use_like(query: str) -> bool:
    """检索路由判定：True 表示必须走 LIKE 降级（trigram 需 ≥3 字符）。"""
    return should_use_like_terms(content_terms(query))
    return False


def _fts_terms(query: str) -> list[str]:
    """构造 trigram 可匹配的查询词项。

    trigram 索引只能回答「连续子串」类问题。把「位置编码有什么用」整句当一个短语
    去匹配，会因为笔记里只有「位置编码」而 0 命中。因此对中文长串额外切出
    滑动 3-gram（同时保留原串以加权完整命中），用 OR 语义召回 —— 命中越多
    trigram 的文档 bm25 排名越靠前。
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(t: str) -> None:
        if t and t not in seen:
            seen.add(t)
            out.append(t)

    for t in query_terms(query):
        if len(t) < 3:
            continue
        if _CJK_RE.search(t) and len(t) > 3:
            for i in range(len(t) - 2):
                add(t[i:i + 3])
        add(t)  # 完整词项权重更高，放最后以便排序稳定
    return out


def _match_expr(terms: list[str]) -> str:
    """构造 FTS5 MATCH 表达式。

    用 **AND 而非 OR**：OR 的语义是「任一词命中就召回」，一个虚词或一个泛词
    就能把大量无关文档拉进候选（同级项目实测「黄仁勋说了什么」正是被这样劫持、
    返回一堆无关结果的）。精确优先；召回不足时由 longest-term 与 LIKE 两级兜底。
    """
    return " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)


def _serialize_vector(vec: list[float]):
    try:
        import sqlite_vec  # type: ignore

        if hasattr(sqlite_vec, "serialize_float32"):
            return sqlite_vec.serialize_float32(vec)
    except Exception:  # noqa: BLE001
        pass
    return json.dumps([float(v) for v in vec])


# --------------------------------------------------------------------------
def _fts_search(db: Database, query: str, limit: int) -> tuple[list[str], str]:
    terms = _fts_terms(query)
    if not terms:
        return [], "like"
    expr = _match_expr(terms)
    try:
        rows = db.query(
            """SELECT chunk_id FROM chunks_fts
               WHERE chunks_fts MATCH ?
               ORDER BY bm25(chunks_fts, 0.0, 0.0, 1.0)
               LIMIT ?""",
            (expr, limit),
        )
        ids = [r["chunk_id"] for r in rows]
        if ids:
            return ids, "fts"
    except sqlite3.Error as exc:
        log.warning("FTS5 MATCH 失败(%s)，回落 LIKE: %s", expr, exc)

    # 兜底一：仅用最长词项重试
    longest = max(terms, key=len)
    try:
        rows = db.query(
            "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT ?",
            ('"' + longest.replace('"', '""') + '"', limit),
        )
        ids = [r["chunk_id"] for r in rows]
        if ids:
            return ids, "fts"
    except sqlite3.Error:
        pass

    return _like_search(db, longest, limit)[0], "like"


def _lexical_relevance(content: str, query: str, terms: list[str]) -> float:
    """短词 / 词法召回的确定性 lexical 相关度打分（P0-E，不用 LLM reranker、不加大模型）。

    信号（按权重叠加，全部可本地即时计算）：
    * 完整查询命中（强信号）
    * 词频（查询词出现次数）
    * 章节标题命中（Markdown 标题行含查询词 → 该块是「关于这个主题的一节」）
    * 相邻词短语（term_i 紧接 term_{i+1}，如「观测人员」）
    * 短文本精确命中（整块很短且含全部词）
    * 表格 / 结构上下文（块内含 Markdown 表格且命中词）

    确定性：纯字符串运算，无随机、无外部依赖，相同输入永远同分。
    """
    if not content:
        return 0.0
    score = 0.0
    if query and query in content:
        score += 6.0
    for t in terms:
        score += content.count(t) * 1.0
    for line in content.splitlines():
        ls = line.lstrip()
        if ls.startswith("#") and any(t in line for t in terms):
            score += 4.0
    for i in range(len(terms) - 1):
        pair = terms[i] + terms[i + 1]
        if pair in content:
            score += 3.0
    if len(content) <= 120 and all(t in content for t in terms):
        score += 2.0
    if "|" in content and any(t in content for t in terms):
        score += 1.0
    return score


def _like_search(db: Database, terms, limit: int = LIKE_LIMIT) -> tuple[list[str], str]:
    """短词 / 特殊缩写降级通道（P0-E：结果按 lexical 相关度排序）。

    ⚠ 必须按**实词**做子串匹配，**不能拿整句去 LIKE**：自然语言问句
    （如「台风有吗」）整句作为子串永远匹配不到 —— 那等于静默丢掉全部召回，
    只能靠向量路兜底，也正是「引用来源与提问无关」的成因之一。

    多个实词用 AND 组合（宁可少召回，也不要召回无关内容）；
    实词过多时取最长的几个，避免条件爆炸。命中后**本地打分**并确定性排序，
    不再「LIMIT 后无脑取」，保证「人员配备」章节排到泛化描述前列。
    """
    if isinstance(terms, str):
        terms = content_terms(terms)
    items = [t for t in (terms or []) if t]
    if not items:
        return [], "like"
    items = sorted(items, key=len, reverse=True)[:4]
    where = " AND ".join(["content LIKE '%' || ? || '%'"] * len(items))
    try:
        rows = db.query(
            f"SELECT chunk_id, content FROM chunks WHERE {where} LIMIT ?",  # noqa: S608 - 占位符拼接，非用户输入
            (*items, limit * 4),
        )
        scored = [
            (_lexical_relevance(r["content"], " ".join(items), items), r["chunk_id"])
            for r in rows
        ]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [cid for _, cid in scored[:limit]], "like"
    except sqlite3.Error as exc:
        log.error("LIKE 降级检索失败: %s", exc)
        return [], "like"


def _vec_search(db: Database, embedder, query: str, limit: int) -> list[tuple[str, float]]:
    if embedder is None or not db.vec_table_ready or db.signature_mismatch:
        return []
    try:
        qvec = embedder.embed([query])[0]
    except Exception as exc:  # noqa: BLE001
        log.warning("查询向量化失败: %s", exc)
        return []
    if len(qvec) != db.embedding_dim:
        log.warning("查询向量维度 %d 与库内 %d 不符，跳过向量召回", len(qvec), db.embedding_dim)
        return []
    try:
        rows = db.query(
            """SELECT chunk_id, distance FROM chunks_vec
               WHERE embedding MATCH ? AND k = ?
               ORDER BY distance""",
            (_serialize_vector(qvec), limit),
        )
        return [(r["chunk_id"], float(r["distance"])) for r in rows]
    except sqlite3.Error as exc:
        log.warning("sqlite-vec KNN 检索失败: %s", exc)
        return []


# --------------------------------------------------------------------------
def _rrf_fuse(fts_ids: list[str], vec_ids: list[str]) -> dict[str, dict]:
    fused: dict[str, dict] = {}
    for rank, cid in enumerate(fts_ids, start=1):
        e = fused.setdefault(cid, {"rank_fts": None, "rank_vec": None, "distance": None})
        e["rank_fts"] = rank
    for rank, (cid, dist) in enumerate(vec_ids, start=1):
        e = fused.setdefault(cid, {"rank_fts": None, "rank_vec": None, "distance": None})
        e["rank_vec"] = rank
        e["distance"] = dist
    for cid, e in fused.items():
        score = 0.0
        if e["rank_fts"]:
            score += 1.0 / (RRF_K + e["rank_fts"])
        if e["rank_vec"]:
            score += 1.0 / (RRF_K + e["rank_vec"])
        e["score"] = score
    return fused


def _snippet(text: str, query: str, width: int = 140) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return ""
    for term in query_terms(query):
        pos = t.find(term)
        if pos >= 0:
            start = max(0, pos - width // 3)
            return ("…" if start > 0 else "") + t[start:start + width] + (
                "…" if start + width < len(t) else ""
            )
    return t[:width] + ("…" if len(t) > width else "")


def _term_exists(db: Database, term: str, cache: dict[str, bool] | None = None) -> bool:
    """语料中是否存在该串（用于「拿语料当词典」的切分）。"""
    if cache is not None and term in cache:
        return cache[term]
    ok = False
    try:
        row = db.query_one(
            "SELECT 1 AS x FROM chunks WHERE content LIKE '%' || ? || '%' LIMIT 1",
            (term,),
        )
        ok = row is not None
    except sqlite3.Error:
        ok = False
    if cache is not None:
        cache[term] = ok
    return ok


def trim_term_to_corpus(db: Database, term: str, cache: dict[str, bool] | None = None) -> str:
    """把实词修剪成**语料中真实存在**的最长片段。

    中文没有词边界，正则切不出「位置编码有什么用」里的「位置编码」。
    这里不引入分词器，而是**拿库内实际内容当词典**：从长到短试，命中即采用。

    为什么这样反而比通用分词器更合适：个人知识库里的术语（产品名、人名、行话、
    缩写）通用词典本来就不认识，而它们恰恰是检索时最有价值的词 —— 用自家语料
    当词典，这些词天然被认识。代价是每次查询多几次存在性探测，用 LIMIT 1
    加结果缓存把开销压到可忽略。

    仅对 CJK 串修剪：ASCII 词（KrevixAi、FTS5）一旦截断就失去意义。
    """
    if len(term) < 2:
        return term
    if not _CJK_RUN_RE.fullmatch(term):
        return term

    # 预算跟随串长：长自然语言问句里的术语可能在**最前面**，需要砍很多刀才露出来。
    # 实测反例：「索引重建会不会改笔记文件」（12 字）里的「索引重建」在最前面，
    # 而固定 6 刀的预算只能砍到「索引重建会不会」→ 永远命中不到，
    # 于是这类问句在「引用必须有词法依据」的规则下会被如实判为「没找到」。
    budget = max(6, min(24, len(term) - 1))

    # 先尾部修剪：问句虚词多在尾部（「…有什么用」「…怎么用」）
    for cut in range(0, budget):
        cand = term[: len(term) - cut] if cut else term
        if len(cand) < 2:
            break
        if _term_exists(db, cand, cache):
            return cand
    # 再首部修剪：处理「请问台风」这类前缀缀语
    for cut in range(1, budget):
        cand = term[cut:]
        if len(cand) < 2:
            break
        if _term_exists(db, cand, cache):
            return cand
    return ""


def drop_noise_terms(terms: list[str]) -> list[str]:
    """多词 AND 时丢弃单字 CJK 噪声。

    「位置编码有什么用」经虚词切分后会剩下 位置编码 / 什 / 用 —— 单字的「什」
    在库里几乎不可能出现，放进 AND 会把正确结果一起清零。
    单个字独立成查询（如「注」）仍保留，那是合法的短查询，走 LIKE 通道。
    """
    if len(terms) <= 1:
        return terms
    return [t for t in terms if not (len(t) == 1 and _CJK_RUN_RE.fullmatch(t))] or terms


def resolve_lexical_terms(db: Database, term_sets: list[list[str]]) -> list[str]:
    """把候选实词集合落到**语料里真实存在的词**上。

    逐套尝试，返回第一套能落地的词；都落不了地就返回空表 ——
    调用方据此走「如实返回没找到」，而不是拿语义相近的无关文档硬凑。
    """
    cache: dict[str, bool] = {}
    for terms in term_sets:
        resolved = [t for t in (trim_term_to_corpus(db, t, cache) for t in terms) if t]
        if resolved:
            return resolved
    return []


def hybrid_search(
    db: Database,
    embedder,
    query: str,
    top_k_parents: int = 5,
    candidates: int = 20,
) -> SearchResult:
    """执行双路召回 + RRF 融合，返回 Top-K 父分块与引用溯源。"""
    query = (query or "").strip()
    result = SearchResult(query=query, route="like")
    if not query:
        return result

    # 路 1：词法（含短词降级）—— 传**实词**，不传整句。
    # 多套候选按优先级逐个尝试，第一套有命中即采用：
    # 首选 = 剥掉虚词后的实词主体；回退 = 未剥离的原串（保护「有机食品」这类真词）。
    term_sets = content_term_sets(query)
    if not term_sets:
        result.route = "empty"
        result.counts = {"fts_candidates": 0, "vec_candidates": 0, "fused": 0, "parents": 0}
        result.warnings.append("查询词均为虚词或语气词，请补充实词（人名 / 术语 / 关键词）后再试")
        return result

    # 落到「语料里真实存在的词」上，并**逐套候选试到命中为止**。
    #
    # 注意必须是「试到命中」而不是「试到能解析」：首选切分可能得到
    # ['位置编码','什','用'] 这种含噪声词的组合（能解析、但永不命中），
    # 只有真正搜出结果才算这一套成立，否则继续试下一套 ——
    # 最终退到「整串修剪」（『位置编码有什么用』→『位置编码』）就能命中。
    # 全部试完仍无命中 = 库里确实没有相关内容 → 如实返回空，绝不硬凑。
    trim_cache: dict[str, bool] = {}
    lex_terms: list[str] = []
    fts_ids: list[str] = []
    route = "like"
    for terms in term_sets:
        resolved = drop_noise_terms(
            [t for t in (trim_term_to_corpus(db, t, trim_cache) for t in terms) if t]
        )
        if not resolved:
            continue
        if should_use_like_terms(resolved):
            ids, r = _like_search(db, resolved, candidates)
        else:
            ids, r = _fts_search(db, " ".join(resolved), candidates)
        if ids:
            lex_terms, fts_ids, route = resolved, ids, r
            break
    result.route = route

    # 路 2：向量
    vec_pairs = _vec_search(db, embedder, query, candidates)
    vec_ids = [cid for cid, _ in vec_pairs]

    fused = _rrf_fuse(fts_ids, vec_pairs)
    if not fused:
        return result

    ordered = sorted(fused.items(), key=lambda kv: kv[1]["score"], reverse=True)

    # ---- 引用必须有词法依据 ----
    # 向量检索的天性就是「永远返回 k 个最近邻」，哪怕全都相距甚远。实测：
    # 库里根本没有「杜苏芮」，它最近邻的距离（0.768）甚至比真正存在的
    # 「台风」（0.804）还小 —— 距离阈值区分不了，于是无关文档被当成出处引用
    # （用户截图里「问台风却引用基坑围护报告」就是这么来的）。
    # 因此：没有词法命中的切片一律不进引用列表。宁可回答「没找到」，
    # 也不用语义相近的无关内容冒充来源。
    grounded = {cid for cid, info in fused.items() if info.get("rank_fts")}
    if grounded:
        ordered = [(cid, info) for cid, info in ordered if cid in grounded]
    elif not config.get_bool("SEARCH", "allow_semantic_only", False):
        result.route = f"{route}+no-lexical-hit"
        result.counts = {
            "fts_candidates": len(fts_ids), "vec_candidates": len(vec_ids),
            "fused": len(fused), "parents": 0,
        }
        result.warnings.append(
            "未找到字面匹配：知识库中没有出现查询实词的内容"
            "（已避免用语义相近但无关的文档冒充引用来源）"
        )
        return result

    # chunk -> parent 聚合（父分块去重，保留最高分）
    chunk_ids = [cid for cid, _ in ordered]
    meta_rows = {}
    for i in range(0, len(chunk_ids), 400):
        batch = chunk_ids[i:i + 400]
        placeholders = ",".join("?" * len(batch))
        try:
            for r in db.query(
                f"""SELECT cm.chunk_id, cm.doc_id, cm.parent_id, c.content
                    FROM chunk_metadata cm LEFT JOIN chunks c ON c.chunk_id = cm.chunk_id
                    WHERE cm.chunk_id IN ({placeholders})""",
                tuple(batch),
            ):
                meta_rows[r["chunk_id"]] = r
        except sqlite3.Error as exc:
            log.error("切片元数据回查失败: %s", exc)

    parent_scores: dict[str, float] = {}
    for cid, info in ordered:
        row = meta_rows.get(cid)
        if not row:
            continue
        pid = row["parent_id"]
        parent_scores[pid] = max(parent_scores.get(pid, 0.0), info["score"])

    top_parents = sorted(parent_scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k_parents]

    doc_cache: dict[str, sqlite3.Row | None] = {}
    references: list[Reference] = []
    parents: list[dict] = []

    for idx, (pid, score) in enumerate(top_parents, start=1):
        prow = db.query_one(
            "SELECT parent_id, doc_id, content FROM parent_blocks WHERE parent_id = ?", (pid,)
        )
        if not prow:
            continue
        did = prow["doc_id"]
        drow = doc_cache.get(did, "__miss__")  # type: ignore[assignment]
        if drow == "__miss__":  # type: ignore[comparison-overlap]
            drow = db.query_one("SELECT title, rel_path, status FROM documents WHERE doc_id = ?", (did,))
            doc_cache[did] = drow
        title = (drow["title"] if drow else None) or did
        rel_path = (drow["rel_path"] if drow else "") or ""

        # 该父块下得分最高的子切片用于计算相似度提示
        sim: float | None = None
        for cid, info in ordered:
            row = meta_rows.get(cid)
            if row and row["parent_id"] == pid and info.get("distance") is not None:
                # vec0 默认 L2 距离；对归一化向量 cos = 1 - d²/2
                d = float(info["distance"])
                sim = round(max(0.0, min(1.0, 1.0 - (d * d) / 2.0)), 4)
                break

        snippet = _snippet(prow["content"], query)
        ref = Reference(
            id=idx, title=title, path=rel_path, snippet=snippet,
            parent_id=pid, doc_id=did, score=round(score, 6), similarity=sim,
        )
        references.append(ref)
        parents.append(
            {
                "parent_id": pid,
                "doc_id": did,
                "title": title,
                "path": rel_path,
                "content": prow["content"],
                "score": round(score, 6),
                "similarity": sim,
            }
        )

    # P0-E：词法召回的确定性 lexical 重排（保持 allow_semantic_only=0，不用 LLM reranker）。
    # 仅当有真实词法命中词时生效；以 lexical 分为主、RRF 分为辅，保证短词查询
    # 「人员配备」章节稳定排在泛化描述前列，且不影响 FTS 高质量排序（分相同则 RRF 兜底）。
    if lex_terms:
        for p in parents:
            p["_lex"] = _lexical_relevance(p.get("content") or "", query, lex_terms)
        parents.sort(key=lambda p: (p["_lex"], p["score"]), reverse=True)
        order_pids = [p["parent_id"] for p in parents]
        references.sort(key=lambda r: order_pids.index(r.parent_id) if r.parent_id in order_pids else 1 << 30)
        for i, r in enumerate(references, start=1):
            r.id = i

    result.references = references
    result.parents = parents
    result.counts = {
        "fts_candidates": len(fts_ids),
        "vec_candidates": len(vec_ids),
        "fused": len(fused),
        "parents": len(parents),
    }
    if embedder is None or not db.vec_table_ready or db.signature_mismatch:
        result.warnings.append("向量召回路未启用，本次为纯 FTS5 词法检索")
    return result


def rank_documents(db: Database, limit: int = 20) -> list[dict]:
    """笔记列表（按索引时间倒序）。"""
    try:
        rows = db.query(
            """SELECT d.doc_id, d.rel_path, d.title, d.status, d.file_size, d.mtime,
                      (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.doc_id) AS chunks
               FROM documents d ORDER BY d.mtime DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def keyword_search_chunks(db: Database, query: str, limit: int = 30) -> list[dict]:
    """供图谱/调试使用的轻量切片检索。"""
    if should_use_like(query):
        ids, route = _like_search(db, query, limit)
    else:
        ids, route = _fts_search(db, query, limit)
    out = []
    for cid in ids:
        row = db.query_one(
            "SELECT chunk_id, doc_id, parent_id, content FROM chunks WHERE chunk_id = ?", (cid,)
        )
        if row:
            out.append({**dict(row), "route": route})
    return out
