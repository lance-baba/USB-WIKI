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
import math
import re
import sqlite3
from dataclasses import dataclass, field

from . import chunker
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
    #: UX-1 界面「来源」：导入文件→源文件名；剪藏→网页标题；笔记→笔记标题。
    #: 与内部 title 分离 —— title 仍用于 metadata / 检索 / 提示词。
    display_source: str = ""
    #: A: 引用 stable anchor —— 父块在真相源里的行范围（1-based，含两端；0=老索引未知）。
    #: 前端据此把引用定位到「源位置 → DOM 位置」，不再全文搜相似文字（目录/正文同文会跳错）。
    source_start_line: int = 0
    source_end_line: int = 0


@dataclass
class SearchResult:
    query: str
    route: str
    counts: dict = field(default_factory=dict)
    references: list[Reference] = field(default_factory=list)
    parents: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 本次检索**实际落地**的词法实词（供离线结果做命中高亮；UX-2）
    lex_terms: list[str] = field(default_factory=list)
    #: B3：概览/覆盖型问句 —— 提示词构造据此给「整节内容」更大的（有界）预算
    coverage: bool = False


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

#: 疑问/虚词字：不应作为检索实词（「哪里 / 几个 / 怎样」等）。
_QCHARS = set("哪几怎何啥吗呢吧啊呀么")


def _has_qchar(s: str) -> bool:
    return any(c in _QCHARS for c in s)


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


# --------------------------------------------------------------------------
# 轻量确定性 Query Analysis（P0-3）—— 不引入任何分词/大模型依赖
# --------------------------------------------------------------------------
#: 疑问类型 → 触发词。刻意只放**问句特征词**，不含任何项目内容。
_QTYPE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # coverage 必须排在最前：「功能有哪些」里的「哪些」不是数量问题，而是覆盖型列举问题。
    # 覆盖型的目标不是「找一个最高分分块」，而是覆盖同一资料的多个相关章节。
    ("coverage", ("有哪些", "有什么", "都有什么", "哪些功能", "主要功能", "功能", "特点",
                  "特性", "能力", "支持什么", "支持哪些", "用途", "总结一下", "介绍一下")),
    ("quantity", ("多少", "几个", "几台", "几人", "几根", "几处", "几次", "几遍",
                  "几条", "几件", "几组", "几套", "几层", "多大", "多长", "多重",
                  "多高", "多厚", "数量", "共有", "总共", "一共", "合计", "总计", "共计")),
    ("person", ("谁", "负责人", "姓名", "联系人", "哪位", "责任人", "由谁", "人员的名字")),
    ("datetime", ("什么时候", "何时", "多久", "多长时间", "频率", "周期", "每几",
                  "每隔", "几天", "几个月", "几日")),
    ("model", ("型号", "规格", "什么仪器", "什么设备", "哪些仪器", "哪些设备")),
    ("location", ("哪里", "在哪", "位置", "地点", "点位")),
)

#: 范围标记：「观测方案中」「报告里」「关于观测方案」
_SCOPE_SUFFIXES = ("中", "里", "内")
_SCOPE_PREFIXES = ("关于", "对于", "针对")

#: 问题短语尾巴（「…的功能有哪些」→ 功能）—— 整串拿去检索会 0 命中
_QTAIL_RE = re.compile(
    r"(?:的)?(?:具体|主要|核心|相关)?(?:功能|特点|特性|能力|用途|信息|内容|情况)?"
    r"(?:都)?(?:有(?:哪)些|有什么|是什么|是啥|如何|怎么样)$")


def _strip_question_tail(s: str) -> str:
    """剥掉查询串末尾的问题短语；剥空则返回空串（调用方丢弃）。"""
    m = _QTAIL_RE.search(s)
    if not m:
        return s
    return s[:m.start()].strip("的的了 ") or ""


#: 章节概览型问法（B1/B4）—— 「X 是怎么样的 / 怎么规定 / 有哪些规定 / 介绍一下 / 具体要求」。
#: 这类问句要的是**整节内容**，不是某个点值；必须优先于点值类型
#: （「监测频率是怎么样的」含「频率」，旧实现被判成 datetime，于是从不走章节覆盖）。
_OVERVIEW_PHRASES = (
    "是怎么样的", "是怎样的", "是什么样", "怎么样", "怎么规定", "怎么安排", "如何规定",
    "有哪些规定", "有什么规定", "有哪些要求", "什么要求", "要求是什么", "介绍一下",
    "说一下", "具体情况", "详细说明", "怎么回事", "包含哪些", "包括哪些", "有哪些内容",
)


def is_section_overview(query: str) -> bool:
    """是否为「整节概况」型问句（coverage / section-summary）。"""
    q = query or ""
    return any(ph in q for ph in _OVERVIEW_PHRASES)


def question_type(query: str) -> str:
    """判断问句类型（coverage / quantity / person / datetime / model / location / general）。

    coverage 与其他类型**同时命中**时，优先返回更具体的类型
    （「观测人员有哪些」属于人员列举，不应被当成泛化的「覆盖型」）。
    """
    q = query or ""
    if is_section_overview(q):
        return "coverage"
    coverage_hit = False
    for name, kws in _QTYPE_PATTERNS:
        if any(k in q for k in kws):
            if name == "coverage":
                coverage_hit = True
                continue
            return name
    return "coverage" if coverage_hit else "general"


@dataclass
class QueryAnalysis:
    """一次查询的轻量解析结果（P0-3）。"""

    raw: str
    question_type: str = "general"
    scope: list[str] = field(default_factory=list)    # 范围/语境词（原始，未修剪）
    target: list[str] = field(default_factory=list)   # 目标/内容词（原始，未修剪）

    @property
    def all_terms(self) -> list[str]:
        return list(dict.fromkeys(self.scope + self.target))


def analyze_query(query: str) -> QueryAnalysis:
    """把自然语言问句拆成 scope（范围）与 target（目标）两组候选词。

    规则刻意轻量、确定性（不要大模型、不要分词库）：

    * ``关于X`` / ``对于X`` / ``针对X`` 前缀 → X 是 scope；
    * ``X中`` / ``X里`` / ``X内`` 后缀 → X 是 scope（「观测方案中，观测点共有几个？」）；
    * 其余实词 → target。

    识别不出 scope 时 scope 为空 —— 此时**不要**把 scope 缺失当成「必须全部硬 AND」的
    理由，target 自己就走多层召回（见 :func:`_tiered_recall`）。
    """
    q = (query or "").strip()
    ana = QueryAnalysis(raw=q, question_type=question_type(q))
    for run in query_terms(q):
        r = run
        for pre in _SCOPE_PREFIXES:
            if len(r) > len(pre) + 1 and r.startswith(pre):
                ana.scope.append(r[len(pre):])
                r = ""
                break
        if not r:
            continue
        # 覆盖型问句的尾巴（「Qwen-Image-2.1的功能有哪些」→「Qwen-Image-2.1」）：
        # 整串含疑问短语时召回会塌缩，必须先剥掉。
        stripped = _strip_question_tail(r)
        if stripped != r:
            r = stripped
            if not r:
                continue
        if len(r) >= 3 and r[-1] in _SCOPE_SUFFIXES and _CJK_RUN_RE.fullmatch(r):
            head = r[:-1]
            # 「X里」作为范围标记（如「方案里」「报告内」），但「在哪里 / 在哪里」
            # 是疑问词而非范围 —— head 落到「哪」上时不当作 scope，否则整句被吞掉、
            # target 变空、召回直接归零。
            if len(head) >= 2 and _is_usable_term(head) and not head.endswith("哪"):
                ana.scope.append(head)
                continue
        ana.target.append(r)
    return ana


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


def _lexical_relevance(content: str, query: str, terms: list[str],
                       section_path: str = "", idf: dict[str, float] | None = None) -> float:
    """短词 / 词法召回的确定性 lexical 相关度打分（P0-E，不用 LLM reranker、不加大模型）。

    信号（按权重叠加，全部可本地即时计算）：
    * 完整查询命中（强信号）
    * 词频 × **IDF**（稀有答案词如 厂房/BM1 权重高；泛词 观测 权重趋近于 0）
    * 章节标题命中（Markdown 标题行含查询词 → 该块是「关于这个主题的一节」）
    * 相邻词短语（term_i 紧接 term_{i+1}，如「观测人员」）
    * 短文本精确命中（整块很短且含全部词）
    * 表格 / 结构上下文（块内含 Markdown 表格且命中词）

    IDF 是 P0-8 重排的关键：没有它，「观测」这类泛词因出现在每个章节标题而把无关
    分块顶到答案分块前面（实测「观测点共有几个」竟被「观测内容」章节反超）。
    确定性：纯字符串运算，无随机、无外部依赖，相同输入永远同分。
    """
    if not content and not section_path:
        return 0.0
    content = content or ""
    # FTS5 trigram 是大小写不敏感的；词法匹配也必须一致，否则「halting」匹配不到
    # 语料里的「Halting」（这是 RAG 回归 halting problem 召回归零的根因）。
    c_low = content.lower()
    q_low = (query or "").lower()
    terms_low = [t.lower() for t in terms]
    score = 0.0
    if q_low and q_low in c_low:
        score += 6.0 * _idf_weight(idf, (query or "")[:2])
    for t, tl in zip(terms, terms_low):
        w = _idf_weight(idf, t)
        score += c_low.count(tl) * 1.0 * w
    for line in content.splitlines():
        ls = line.lstrip()
        if ls.startswith("#") and any(tl in line.lower() for tl in terms_low):
            score += 4.0 * max((_idf_weight(idf, t) for t, tl in zip(terms, terms_low)
                                if tl in line.lower()), default=0.0)
    # P0-4：章节路径命中 → 与标题命中同权（说明本块就处在「关于该主题」的章节里）
    if section_path:
        sp_low = section_path.lower()
        spw = max((_idf_weight(idf, t) for t, tl in zip(terms, terms_low) if tl in sp_low),
                  default=0.0)
        if spw:
            score += 4.0 * spw
    for i in range(len(terms) - 1):
        pair = (terms[i] + terms[i + 1]).lower()
        if pair in c_low:
            score += 3.0
    if len(content) <= 120 and all(tl in c_low for tl in terms_low):
        score += 2.0
    if "|" in content and any(tl in c_low for tl in terms_low):
        score += 1.0
    return score


def _fts_ids(db: Database, terms: list[str], limit: int, conj: bool = True) -> list[str]:
    """按词项跑 FTS5 MATCH（conj=False 时用 OR 组合），按 bm25 排序返回 chunk_id。"""
    fts_terms: list[str] = []
    seen: set[str] = set()
    for t in terms:
        for x in (_fts_terms(t) or [t]):
            if x and x not in seen:
                seen.add(x)
                fts_terms.append(x)
    if not fts_terms:
        return []
    joiner = " AND " if conj else " OR "
    expr = joiner.join('"' + x.replace('"', '""') + '"' for x in fts_terms)
    try:
        rows = db.query(
            """SELECT chunk_id FROM chunks_fts
               WHERE chunks_fts MATCH ?
               ORDER BY bm25(chunks_fts, 0.0, 0.0, 1.0)
               LIMIT ?""",
            (expr, limit),
        )
        return [r["chunk_id"] for r in rows]
    except sqlite3.Error as exc:
        log.warning("FTS MATCH 失败(%s): %s", expr, exc)
        return []


def _like_search(db: Database, terms, limit: int = LIKE_LIMIT,
                 conj: bool = True) -> tuple[list[str], str]:
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
    col = _retr_col(db)                       # 新库=retrieval_text（含章节路径）；旧库=content
    sel = "chunk_id, content"
    sel += (", COALESCE(section_path,'') AS section_path" if col == "retrieval_text"
            else ", '' AS section_path")
    joiner = " AND " if conj else " OR "
    where = joiner.join([f"{col} LIKE '%' || ? || '%'"] * len(items))
    try:
        rows = db.query(
            f"SELECT {sel} FROM chunks WHERE {where} LIMIT ?",  # noqa: S608 - 占位符拼接，非用户输入
            (*items, limit * 4),
        )
        scored = [
            (_lexical_relevance(r["content"], " ".join(items), items,
                                r["section_path"] or ""), r["chunk_id"])
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


def _doc_row(db: Database, doc_id: str) -> tuple[str, str, str]:
    """取 (title, rel_path, display_source)。

    display_source 来自 doc_meta（UX-1）。为兼容「尚未含该列的旧库」，查询失败时
    退回不含该列的旧语句，而不是让整条检索链报错。
    """
    try:
        r = db.query_one(
            """SELECT d.title, d.rel_path,
                      COALESCE(m.display_source, '') AS display_source
                 FROM documents d LEFT JOIN doc_meta m ON m.doc_id = d.doc_id
                WHERE d.doc_id = ?""",
            (doc_id,),
        )
        if r:
            return (r["title"] or ""), (r["rel_path"] or ""), (r["display_source"] or "")
    except sqlite3.Error:
        pass
    try:
        r = db.query_one("SELECT title, rel_path FROM documents WHERE doc_id = ?", (doc_id,))
        if r:
            return (r["title"] or ""), (r["rel_path"] or ""), ""
    except sqlite3.Error:
        pass
    return "", "", ""


#: 检索文本列名探测缓存：新库用 retrieval_text（章节路径+原文），旧库退回 content。
_RETR_COL: dict[int, str] = {}


def _retr_col(db: Database) -> str:
    key = id(db)
    col = _RETR_COL.get(key)
    if col:
        return col
    try:
        db.query_one("SELECT retrieval_text FROM chunks LIMIT 1")
        col = "retrieval_text"
    except sqlite3.Error:
        col = "content"
    _RETR_COL[key] = col
    return col


def _term_exists(db: Database, term: str, cache: dict[str, bool] | None = None) -> bool:
    """语料中是否存在该串（用于「拿语料当词典」的切分）。"""
    if cache is not None and term in cache:
        return cache[term]
    ok = False
    try:
        row = db.query_one(
            f"SELECT 1 AS x FROM chunks WHERE {_retr_col(db)} LIKE '%' || ? || '%' LIMIT 1",
            (term,),
        )
        ok = row is not None
    except sqlite3.Error:
        ok = False
    if cache is not None:
        cache[term] = ok
    return ok


def expand_term_to_corpus(db: Database, term: str, samples: int = 3) -> str:
    """把实词扩成**语料中真实存在的更长词组**（P0-2 relaxed 层用）。

    例：库里有「沉降观测点8个」时，``观测点`` → ``沉降观测点``。
    做法：取少量含该词的切片，沿两侧吃掉连续 CJK 字符得到候选，再验证候选确实在语料中。
    拿不到更长的就原样返回 —— 永不硬凑。
    """
    if not term or len(term) < 2 or not _CJK_RUN_RE.fullmatch(term):
        return term
    try:
        rows = db.query(
            f"SELECT {_retr_col(db)} AS t FROM chunks WHERE {_retr_col(db)} LIKE '%' || ? || '%' LIMIT ?",
            (term, samples),
        )
    except sqlite3.Error:
        return term
    best = term
    for r in rows:
        text = r["t"] or ""
        start = 0
        while True:
            i = text.find(term, start)
            if i < 0:
                break
            lo = i
            while lo > 0 and _CJK_RUN_RE.fullmatch(text[lo - 1]):
                lo -= 1
            hi = i + len(term)
            while hi < len(text) and _CJK_RUN_RE.fullmatch(text[hi]):
                hi += 1
            cand = text[lo:hi].strip()
            if len(cand) > len(best) and _is_usable_term(cand) and _term_exists(db, cand):
                best = cand
            start = i + 1
    return best


def corpus_subterms(run: str, db: Database, cache: dict[str, bool] | None = None) -> list[str]:
    """把一个无虚词切分的长串，按语料拆成**多个真实存在的原子子词（2~3 字，最小覆盖）**。

    解决 P0-2 / P0-3 的核心召回问题：自然语言问句经虚词剥离后常剩一整串
    「观测点布设在哪里」「本次观测哪栋厂房」。若不切分、整体当一个词去语料里修剪，
    会被贪心扩成只存在于**无关分块**的长词（如「本次观测」只出现在仪器设备段），
    导致承载答案的分块（工程概况段写「本次沉降观测对象为1号厂房」）既召不回、又
    过不了 Grounding。

    设计要点（都经过负向验证）：
    * 只取 **2~3 字**子串、且做**最小覆盖去重**（丢弃是其它子串超串的项）。
      —— 4+ 字长词（本次观测）只活在无关分块、却会被当成多个命中项给错分块加分；
         2~3 字原子词（本次 / 厂房）才是答案分块真正命中的词。
    * 疑问字（哪/几/怎/何/啥）当断点跳过，避免把「哪里 / 几个」当检索词。
    * 非纯中文串（如型号 DS05）不再做滑窗碎裂，直接整体保留（若语料存在）。
    """
    if len(run) < 2:
        return []
    # 型号 / 编号等含 ASCII 的串：整体保留，不做 CJK 滑窗碎裂
    if not _CJK_RUN_RE.fullmatch(run):
        return [run] if (_is_usable_term(run) and _term_exists(db, run, cache)) else []
    n = len(run)
    found: list[str] = []
    seen: set[str] = set()
    for L in (3, 2):  # 先 3 字再 2 字，跳过 4+ 字长词
        for i in range(0, n - L + 1):
            sub = run[i:i + L]
            if sub in seen:
                continue
            seen.add(sub)
            if _has_qchar(sub) or not _is_usable_term(sub):
                continue
            if _term_exists(db, sub, cache):
                found.append(sub)
    # 最小覆盖去重：丢弃「是其它子串超串」的项（只留最具体的原子词）
    result: list[str] = []
    for s in found:
        if any(t != s and s in t for t in found):
            continue
        if s not in result:
            result.append(s)
        if len(result) >= 8:
            break
    return result


# --------------------------------------------------------------------------
# 语料词频（IDF）支持：抑制「观测」这类出现在每个章节的泛词，让稀有答案词
# （厂房 / BM1 / DS05）主导重排。重排前按 search_terms 预算一次。
_DF_CACHE: dict[int, dict[str, int]] = {}
_N_CACHE: dict[int, int] = {}


def _col_of(db: Database) -> str:
    return _retr_col(db)


def _term_df(db: Database, term: str, col: str) -> int:
    key = id(db)
    cache = _DF_CACHE.setdefault(key, {})
    if term in cache:
        return cache[term]
    try:
        row = db.query_one(
            f"SELECT COUNT(DISTINCT chunk_id) AS c FROM chunks WHERE {col} LIKE '%' || ? || '%'",
            (term,),
        )
        v = int(row["c"]) if row else 0
    except sqlite3.Error:
        v = 0
    cache[term] = v
    return v


def _total_chunks(db: Database) -> int:
    key = id(db)
    if key in _N_CACHE:
        return _N_CACHE[key]
    try:
        row = db.query_one("SELECT COUNT(*) AS c FROM chunks")
        v = int(row["c"]) if row else 1
    except sqlite3.Error:
        v = 1
    _N_CACHE[key] = v
    return v


def _idf_map(db: Database, terms: list[str]) -> dict[str, float]:
    col = _col_of(db)
    n = _total_chunks(db)
    out: dict[str, float] = {}
    for t in set(terms):
        df = _term_df(db, t, col)
        out[t] = math.log((n + 1) / (df + 1)) if df > 0 else math.log(n + 1)
    return out


def _idf_weight(idf: dict[str, float] | None, term: str) -> float:
    if not idf:
        return 1.0
    return idf.get(term, 1.0)


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


# --------------------------------------------------------------------------
# 多层召回 / Grounding / 重排（P0-2 / P0-5 / P0-7 / P0-8）
# --------------------------------------------------------------------------
def _resolve_terms(db: Database, raw_terms: list[str],
                   cache: dict[str, bool], expand: bool = True) -> list[str]:
    """原始候选词 → 切分 → （可选）语料子词扩展 → 修剪成**语料中真实存在**的实词。

    * ``expand=True``（target 用）：长串先展开成多个语料真实存在的原子子词
      （corpus_subterms → 本次/观测/厂房），再各自修剪。多层召回与 Grounding 都能
      命中正确答案所在分块，而非被一个过长的伪短语带偏。
    * ``expand=False``（scope 用）：scope 通常是干净的名词短语（观测方案/报告），
      不做子词爆炸，保持语义完整，避免把 观测方案 拆成 观测/测方/方案 这类噪声。
    """
    out: list[str] = []
    for t in raw_terms:
        parts = _split_cjk_run(t)
        if not parts:
            continue
        for part in parts:
            subs = corpus_subterms(part, db, cache) if (expand and len(part) > 2) else [part]
            if not subs:
                subs = [part]
            for s in subs:
                r = trim_term_to_corpus(db, s, cache)
                if r and _is_usable_term(r):
                    out.append(r)
    return drop_noise_terms(list(dict.fromkeys(out)))


def _lex_once(db: Database, terms: list[str], limit: int,
              conj: bool = True) -> tuple[list[str], str]:
    """单次词法检索：≥3 字符走 FTS（trigram），更短的走 LIKE 降级。"""
    if not terms:
        return [], ""
    if should_use_like_terms(terms):
        return _like_search(db, terms, limit, conj=conj)
    return _fts_ids(db, terms, limit, conj=conj), "fts"


def _tiered_recall(db: Database, scope: list[str], target: list[str],
                   limit: int) -> tuple[list[str], dict[str, str], str]:
    """多层词法召回（P0-2）→ ``(ordered_ids, tier_of, route)``。

    * **Tier A（strict）** —— scope + target 全连词；
    * **Tier B（core）** —— 只连 target；
    * **Tier C（relaxed）** —— 单个高信息词，逐个召回后取并集（含语料最长匹配扩展）。

    为什么必须分层：中文问句里 scope（「观测方案中」）与承载答案的词（「观测点」）
    常常**不在同一个 200 字 child 内**。旧实现把所有 term 硬 AND 进一条 MATCH，
    等于要求它们同块出现 —— 答案切片于是永远召不回（「问观测点数量却答找不到」的根因）。
    分层后 strict 命不中会自动下沉到 core / relaxed，而不是整条查询归零；
    同时 Tier A 命中仍排在前面（RRF 的 rank 按加入顺序给），不会牺牲精度。
    """
    ordered: list[str] = []
    seen: set[str] = set()
    tier_of: dict[str, str] = {}
    route = ""

    def absorb(ids: list[str], tier: str, r: str) -> None:
        nonlocal route
        hit = False
        for cid in ids:
            if cid not in seen:
                seen.add(cid)
                ordered.append(cid)
                tier_of[cid] = tier
                hit = True
        if hit and not route:
            route = r or "like"

    strict = list(dict.fromkeys(scope + target))
    if strict:
        ids, r = _lex_once(db, strict, limit, conj=True)
        absorb(ids, "A", r)
    if target:
        ids, r = _lex_once(db, target, limit, conj=True)
        absorb(ids, "B", r)
    # P0-2：严格 AND（A/B）都召不回时，才下沉到 Tier C 单字召回。
    # 否则单字召回会把「只是顺带提到查询词」的干扰文档（如 postgrey_note 顺带提了
    # PostgreSQL）顶进 Top-K，压过真正命中完整查询意图的文档 —— OLD 行为正是靠
    # FTS 的「全词 AND」把这种干扰挡在候选集之外。Tier C 作为兜底召回能保证
    # 「观测点共有几个」这类 scope/target 分处不同分块的问题不漏召，但不会把
    # 顺带提及的干扰文档与真实答案混为一谈。
    if not ordered:
        singles: list[str] = []
        for t in (target or scope):
            singles.append(t)
            ext = expand_term_to_corpus(db, t)
            if ext and ext != t:
                singles.append(ext)
        for t in dict.fromkeys(singles):
            ids, r = _lex_once(db, [t], limit, conj=True)
            absorb(ids, "C", r)
    return ordered, tier_of, (route or "like")


def _answer_type_bonus(text: str, section_path: str, target: list[str],
                       qtype: str) -> float:
    """答案类型感知的**轻量**排序信号（P0-7）。

    确定性、无 ML、不含任何项目内容硬编码；量级刻意很小（≤1.5），保证不会压过
    词法相关度（后者可达 10+）—— 它只在「词法分接近」时起决定作用。
    """
    if not target:
        return 0.0
    t = text or ""
    blob = f"{section_path}\n{t}"
    if qtype == "quantity":
        # 决定性信号：数量答案必然是「目标词紧邻一个数字」。命中即大幅加权，
        # 压过任何仅靠词频重叠的无关分块（实测「观测点共有几个」被「观测内容」章节
        # 靠标题/邻接加分反超时，正是靠这条翻盘）。
        for term in target:
            for m in re.finditer(re.escape(term), t):
                seg = t[max(0, m.start() - 12): m.end() + 14]
                if re.search(r"\d", seg):
                    return 8.0                     # 目标词附近出现数字 → 就是数量答案
        return 0.0
    if qtype == "person":
        if re.search(r"(负责人|责任人|姓名|联系人|职称)", blob):
            return 1.2                          # 表格/名单语境
        if re.search(r"\|\s*[^|\n]{1,8}\s*\|\s*[\u4e00-\u9fff]{2,4}\s*\|", t):
            return 0.8
        return 0.0
    if qtype == "datetime":
        if re.search(r"(\d+\s*(天|日|周|月|年|小时|分钟|次|遍|期))"
                     r"|(每天|每日|每周|每月|每季|每年|每隔|每\s*\d)", blob):
            return 1.2
        return 0.0
    if qtype == "model":
        if re.search(r"([A-Z]{1,5}[- ]?\d{1,4}[A-Za-z]?|\d+(\.\d+)?\s*(mm|米))", t):
            return 1.5
        return 0.0
    if qtype == "location":
        if re.search(r"(位于|位置|点号|编号|标号)", t):
            return 0.6
        return 0.0
    return 0.0


def _load_chunk_info(db: Database, chunk_ids: list[str]) -> dict[str, dict]:
    """批量回查候选切片（parent_id / 展示原文 / 章节路径 / 检索文本）。"""
    out: dict[str, dict] = {}
    if not chunk_ids:
        return out
    is_new = _retr_col(db) == "retrieval_text"
    for i in range(0, len(chunk_ids), 400):
        batch = chunk_ids[i:i + 400]
        ph = ",".join("?" * len(batch))
        if is_new:
            sql = (f"SELECT c.chunk_id, c.doc_id, c.parent_id, c.content,"
                   f" COALESCE(c.section_path,'') AS section_path,"
                   f" COALESCE(c.retrieval_text, c.content) AS rtext"
                   f" FROM chunks c WHERE c.chunk_id IN ({ph})")
        else:
            sql = (f"SELECT c.chunk_id, c.doc_id, c.parent_id, c.content,"
                   f" '' AS section_path, c.content AS rtext"
                   f" FROM chunks c WHERE c.chunk_id IN ({ph})")
        try:
            for r in db.query(sql, tuple(batch)):
                out[r["chunk_id"]] = {
                    "doc_id": r["doc_id"], "parent_id": r["parent_id"],
                    "content": r["content"], "section_path": r["section_path"] or "",
                    "rtext": r["rtext"] or "",
                }
        except sqlite3.Error as exc:
            log.error("候选切片回查失败: %s", exc)
    return out


#: 语义兜底（Grounding 规则 B）的最低相似度。刻意保守 —— 规则 B 只是**兜底**，
#: 规则 A（命中 target 词）才是主路径；宁可少引用，也不放行「毫无字面依据」的来源。
SEM_FLOOR = 0.6


def _sim_from_distance(distance) -> float | None:
    if distance is None:
        return None
    d = float(distance)
    return round(max(0.0, min(1.0, 1.0 - (d * d) / 2.0)), 4)


def _is_grounded(info: dict, target: list[str], scope: list[str],
                 sim: float | None, doc_scope_text: str,
                 is_lex: bool = False) -> tuple[bool, str]:
    """新的 grounded 判定（P0-5 / P0-6）。

    两条互斥的进入通道：

    1) **词法召回命中** (``is_lex``) —— 切片已在 Tier A/B/C 中匹配到查询词（含 Tier C
       单字召回）。天然有字面依据，直接视为 grounded。这正是 OLD ``rank_fts`` 闸门的精神：
       能进引用列表的切片至少得在词法上跟查询沾边。

    2) **纯向量召回** (``not is_lex``) —— 只在「章节路径 / 文档级 scope 命中 + 语义」
       成立时才放行进引用（P0-6 的语义兜底通道 rule B/C）。
       **严禁**「只因 embedding 最近、或顺带提了一句查询词」就冒充来源：
       典型反例 ``postgrey_note`` 在比较句里顺带提了 PostgreSQL，若允许「含任一 target 词」
       即 grounded，它会被向量相似度顶进 Top-5，压过真正讲 PostgreSQL 索引的 ``postgres_note``。
       所以纯向量切片必须靠 scope / 语义支撑，不能靠一个无关上下文里的孤立词。

    两条通道之外：零字面重合、仅因 embedding 最近 → 一律 rejected。
    """
    rtext = info.get("rtext") or ""
    spath = info.get("section_path") or ""
    # 大小写不敏感：FTS5 trigram 本身大小写不敏感，grounding 也必须一致，
    # 否则「halting」匹配不到语料里的「Halting」（RAG 回归 halting problem 召回归零的根因）。
    rt = rtext.lower()
    sp = spath.lower()
    doc = (doc_scope_text or "").lower()
    if is_lex:
        return True, "lexical"
    # —— 以下仅对纯向量召回生效 ——
    # rule B：章节路径 / 文档标题命中 scope 词，且有相关语义分
    hit_s = [t for t in scope if t and (t.lower() in sp or (doc and t.lower() in doc))]
    if hit_s and sim is not None and sim >= SEM_FLOOR:
        return True, "scope+semantic:" + ",".join(hit_s[:2])
    if hit_s:
        return False, "scope-only(no-semantic)"
    # rule C：文档级 scope 命中 + 切片命中 target 词 + 语义（语义兜底通道的兜底）
    if scope:
        doc_hit = any(t.lower() in doc for t in scope)
        term_hit = any(t and t.lower() in rt for t in target)
        if doc_hit and term_hit and sim is not None and sim >= SEM_FLOOR:
            return True, "doc-scope+target+semantic"
    return False, "vector-only(no-lexical-overlap)"


_SRC_LINES_READY: bool | None = None


def _has_source_lines(db: Database) -> bool:
    """parent_blocks 是否已有源行范围列（A: schema 1.7）。老索引返回 False 并优雅降级。"""
    global _SRC_LINES_READY
    if _SRC_LINES_READY is None:
        try:
            cols = {r["name"] for r in db.query("PRAGMA table_info(parent_blocks)")}
            _SRC_LINES_READY = "source_start_line" in cols
        except sqlite3.Error:
            _SRC_LINES_READY = False
    return bool(_SRC_LINES_READY)


def _parent_rows(db: Database, pids: list[str]) -> dict[str, dict]:
    """批量取父块（content + section_path）；旧库缺列时退回无 section_path。"""
    out: dict[str, dict] = {}
    is_new = _retr_col(db) == "retrieval_text"
    for i in range(0, len(pids), 400):
        batch = pids[i:i + 400]
        ph = ",".join("?" * len(batch))
        has_src = _has_source_lines(db)
        sel = ("parent_id, doc_id, content, COALESCE(section_path,'') AS section_path, "
               "COALESCE(ord,0) AS ord, "
               + ("COALESCE(source_start_line,0) AS s_line, COALESCE(source_end_line,0) AS e_line"
                  if has_src else "0 AS s_line, 0 AS e_line")
               if is_new else
               "parent_id, doc_id, content, '' AS section_path, COALESCE(ord,0) AS ord, "
               "0 AS s_line, 0 AS e_line")
        try:
            for r in db.query(f"SELECT {sel} FROM parent_blocks WHERE parent_id IN ({ph})",
                              tuple(batch)):
                out[r["parent_id"]] = {"doc_id": r["doc_id"], "content": r["content"] or "",
                                       "section_path": r["section_path"] or "",
                                       "ord": r["ord"] or 0,
                                       "source_start_line": r["s_line"] or 0,
                                       "source_end_line": r["e_line"] or 0}
        except sqlite3.Error as exc:
            log.error("父块回查失败: %s", exc)
    return out


def _coverage_section_blocks(db: Database, doc_id: str, anchor_section: str,
                             anchor_ord: int, have: set[str],
                             limit: int = 8) -> list[dict]:
    """B2：以**章节为单位**扩展证据 —— 取锚定块之后、仍属于同一章节的连续父块。

    章节归属用 section_path 前缀判断（anchor 本身或其子路径）；一旦遇到「下一个同级
    或更高级标题」就停止。「监测频率是怎么样的」正是靠这一步把频率表之后的
    「出现下列情况应加强监测」一类条件项一并纳入 context；旧实现只给到表格前半段，
    模型只好回答「资料未明确列出其他情况」。
    """
    if not anchor_section:
        return []
    rows = [r for r in _coverage_doc_rows(db, doc_id)
            if r["parent_id"] not in have and r["ord"] > anchor_ord]

    def scan(prefix: str) -> list[dict]:
        picked: list[dict] = []
        for r in rows:
            sp = r["section_path"] or ""
            if sp == prefix or sp.startswith(prefix + " > "):
                picked.append(r)
                if len(picked) >= limit:
                    break
            elif sp:
                break                  # 下一个同级/更高级标题 → 本段结束
        return picked

    same = scan(anchor_section)
    if same:
        return same
    # 锚点本身是子小节时，条件项/清单往往与它**在同一段落里并列**：逐级向上试。
    # 实测：附表里「㈠、监测频率」的 12 条「应加强监测」条件（⑴…⑿）与它并列在附件章节下，
    # 只按锚点自身 section 找会全漏 —— 模型于是答「资料未明确列出其他情况」。
    parts = anchor_section.split(" > ")
    for i in range(len(parts) - 1, 0, -1):
        picked = scan(" > ".join(parts[:i]))
        if picked:
            return picked
    return []


_COVERAGE_ROWS_CACHE: dict[str, list[dict]] = {}


def _coverage_doc_rows(db: Database, doc_id: str) -> list[dict]:
    """某文档的全部父块（section_path / ord / 源行范围），进程内缓存。"""
    if doc_id in _COVERAGE_ROWS_CACHE:
        return _COVERAGE_ROWS_CACHE[doc_id]
    has_src = _has_source_lines(db)
    sql = ("SELECT parent_id, doc_id, content, COALESCE(section_path,'') AS section_path, "
           "COALESCE(ord,0) AS ord"
           + (", COALESCE(source_start_line,0) AS s_line, COALESCE(source_end_line,0) AS e_line"
              if has_src else ", 0 AS s_line, 0 AS e_line")
           + " FROM parent_blocks WHERE doc_id = ? ORDER BY ord")
    try:
        rows = list(db.query(sql, (doc_id,)))
    except sqlite3.Error:
        rows = []
    _COVERAGE_ROWS_CACHE[doc_id] = rows
    return rows


def _coverage_siblings(db: Database, doc_id: str, have: set[str],
                       limit: int = 12) -> list[dict]:
    """覆盖型问题用：取锚定文档的**同篇章节块**（章节级优先，按原文顺序）。

    只用于 coverage 型问句（「功能有哪些」），目标是让答案覆盖同篇的多个 section。
    不做全库放宽、也不放大 top_k —— 只在**已锚定的那一篇**资料内部补章节。
    """
    rows = _coverage_doc_rows(db, doc_id)
    if not rows:
        return []
    section_level, other = [], []
    for r in rows:
        if r["parent_id"] in have:
            continue
        sp = r["section_path"] or ""
        # 「文档标题 > 章节」= 章节级（功能通常按章节并列）；文档根（引言/frontmatter）
        # 与子小节（> 2 级）都排后面，避免占掉宝贵的 Top-K 名额。
        (section_level if sp.count(" > ") == 1 else other).append(r)
    return (section_level or other)[:limit]


def _coverage_order(ranked: list[tuple[str, dict, dict, float]], k: int):
    """覆盖型排序：保留最强单点，其余名额优先给**不同 section_path** 的章节。

    不改变第 1 名（保证「找一个最相关分块」的能力不回退），只影响 2..k 的填充：
    先同篇不同章节（按原文顺序），再其它文档，最后按分数补满。
    """
    if len(ranked) <= k:
        return ranked
    lead = ranked[0]
    rest = ranked[1:]
    lead_doc = lead[2].get("doc_id")
    out = [lead]
    used = {lead[2].get("section_path") or ""}
    same_doc = sorted([r for r in rest if r[2].get("doc_id") == lead_doc],
                      key=lambda r: r[2].get("ord", 0) or 0)
    others = [r for r in rest if r[2].get("doc_id") != lead_doc]
    for pool in (same_doc, others):
        for r in pool:
            if len(out) >= k:
                break
            sec = r[2].get("section_path") or ""
            # 章节级块（深度 1）优先占不同 section 的名额；子小节/文档根不占额外名额
            if r[2].get("section_path", "").count(" > ") != 1:
                continue
            if sec in used:
                continue
            used.add(sec)
            out.append(r)
    for pool in (same_doc, others):                 # 仍不满 k：退而求其次按顺序补
        for r in pool:
            if len(out) >= k:
                break
            if r not in out:
                out.append(r)
    return out


def hybrid_search(
    db: Database,
    embedder,
    query: str,
    top_k_parents: int = 5,
    candidates: int = 20,
    debug: dict | None = None,
) -> SearchResult:
    """Query Analysis → 多层词法召回 ∪ 向量召回 → Grounding → 父块重排（P0-2~P0-8）。"""
    query = (query or "").strip()
    result = SearchResult(query=query, route="like")
    if not query:
        return result

    # 纯虚词/语气词：如实告诉用户补实词（与旧行为一致）
    if not content_term_sets(query):
        result.route = "empty"
        result.counts = {"fts_candidates": 0, "vec_candidates": 0, "fused": 0, "parents": 0}
        result.warnings.append("查询词均为虚词或语气词，请补充实词（人名 / 术语 / 关键词）后再试")
        return result

    ana = analyze_query(query)
    trim_cache: dict[str, bool] = {}
    scope_res = _resolve_terms(db, ana.scope, trim_cache, expand=False)
    target_res = _resolve_terms(db, ana.target, trim_cache, expand=True)
    search_terms = list(dict.fromkeys(scope_res + target_res))

    if debug is not None:
        debug.update({
            "query": query,
            "question_type": ana.question_type,
            "scope_raw": list(ana.scope),
            "target_raw": list(ana.target),
            "scope_terms": scope_res,
            "target_terms": target_res,
        })

    if search_terms:
        lex_ids, tier_of, route = _tiered_recall(db, scope_res, target_res, candidates)
    else:
        # 实词一个都不在语料里：**不早退** —— 向量照跑，但 Grounding 会全部拒绝
        lex_ids, tier_of, route = [], {}, "like"
    result.route = route
    result.coverage = (ana.question_type == "coverage")
    result.lex_terms = list(search_terms)
    idf = _idf_map(db, search_terms)            # 预算 IDF：稀有答案词主导重排（P0-8）

    vec_pairs = _vec_search(db, embedder, query, candidates)
    vec_ids = [cid for cid, _ in vec_pairs]

    fused = _rrf_fuse(lex_ids, vec_pairs)
    if not fused:
        return result

    info = _load_chunk_info(db, list(fused))
    lex_set = set(lex_ids)
    doc_cache: dict[str, str] = {}
    accepted: list[tuple[str, dict]] = []
    rejected: list[dict] = []
    for cid, entry in fused.items():
        ci = info.get(cid) or {}
        sim = _sim_from_distance(entry.get("distance"))
        did = ci.get("doc_id") or ""
        if did and did not in doc_cache:
            t, rel, disp = _doc_row(db, did)
            doc_cache[did] = " ".join(x for x in (t, disp, rel) if x)
        is_lex = cid in lex_set
        ok, reason = _is_grounded(ci, target_res, scope_res, sim, doc_cache.get(did, ""),
                                  is_lex=is_lex)
        entry["_sim"] = sim
        entry["_tier"] = tier_of.get(cid, "vec")
        entry["_reason"] = reason
        if ok:
            accepted.append((cid, entry))
        else:
            rejected.append({"chunk_id": cid, "tier": entry["_tier"], "reason": reason,
                             "similarity": sim})

    if debug is not None:
        debug["lexical_tier"] = dict(tier_of)
        debug["lexical_candidates"] = len(lex_ids)
        debug["vector_candidates"] = len(vec_ids)
        debug["grounded"] = [{"chunk_id": c, "tier": e["_tier"], "reason": e["_reason"],
                              "rrf": round(float(e.get("score", 0.0)), 6)}
                             for c, e in accepted]
        debug["rejected"] = rejected

    if not accepted:
        result.route = f"{route}+no-lexical-hit"
        result.counts = {"fts_candidates": len(lex_ids), "vec_candidates": len(vec_ids),
                         "fused": len(fused), "parents": 0}
        result.warnings.append(
            "未找到字面匹配：知识库中没有出现查询实词的内容"
            "（已避免用语义相近但无关的文档冒充引用来源）")
        return result

    # 父块聚合（P0-8）：max 之外还看**多块一致命中**，避免一个偶然高分 child 压过
    # 多个稳定相关 child 的章节。
    parent_acc: dict[str, dict] = {}
    _coverage_pr: dict[str, dict] = {}          # coverage 补充块（不在 _parent_rows 里，单独缓存）
    for cid, entry in accepted:
        ci = info.get(cid) or {}
        pid = ci.get("parent_id") or ""
        if not pid:
            continue
        acc = parent_acc.setdefault(pid, {"n": 0, "max": 0.0, "sim": None})
        acc["n"] += 1
        acc["max"] = max(acc["max"], float(entry.get("score", 0.0)))
        if acc["sim"] is None and entry.get("_sim") is not None:
            acc["sim"] = entry["_sim"]

    if not parent_acc:
        return result

    # 覆盖型问题（「X 的功能有哪些」）：目标不是「一个最高分分块」，而是覆盖同一份
    # 资料里的多个相关章节。实测「Qwen-Image-2.1的功能有哪些」召回塌缩到 1 个 parent
    # （该文的 紧凑高效 / 多样化编辑 / 逼真纹理 等章节全部漏掉）。
    # 做法：定位锚定文档 → 把它的**同篇章节块**补进候选（不放大 top_k），
    # 排序时优先让不同 section_path 各占一席（section diversity）。
    prows = _parent_rows(db, list(parent_acc))
    prows.update(_coverage_pr)
    ranked: list[tuple[str, dict, dict, float]] = []
    for pid, acc in parent_acc.items():
        pr = prows.get(pid)
        if not pr:
            continue
        content = pr["content"]
        spath = pr["section_path"]
        lex = _lexical_relevance(content, query, search_terms, spath, idf)
        ans = _answer_type_bonus(content, spath, target_res, ana.question_type)
        multi = 0.3 * min(acc["n"], 3)              # 多块一致命中的轻度加成
        # RRF 主干 + 轻量信号微调（P0-7/8）。
        # 必须是「RRF 主干」：RRF（含向量相似度）才是检索可用性的权威排序，
        # 旧实现纯 RRF 时 A4.2b 达 Top5=100%、postgrey 被挡在 Top-5 外；
        # 把 lex 当主排序键会让 postgrey（lex=1，但向量 RRF 偏高）挤进 Top-5。
        # 这里以 rrf_max 为骨架、lex/ans/multi 作为**有界微调**，仅用于打破近邻平局，
        # 既保住 A4.2b 的向量排序，又让 P0 的「答案词紧邻数字 +8」「多块一致命中」
        # 能把真正答案块顶上来。
        final = round(float(acc["max"]) + lex + ans + multi, 6)
        ranked.append((pid, acc, pr, final))
    ranked.sort(key=lambda x: x[3], reverse=True)
    ref_cap = max(top_k_parents, 1)          # 正文引用角标数量不变

    # B2/B3：概览/覆盖型问句（「X 是怎么样的」「X 的功能有哪些」）要的是**整节**内容。
    # 用**排序第一的父块**自带 metadata（doc_id / section_path / ord）做章节扩展：
    #   ① 先取同章节的后续块（条件项/清单常续在其后）；
    #   ② 若锚点是子小节，逐级向上找**并列的兄弟块**（实测：附表里「㈠、监测频率」的
    #      12 条「应加强监测」条件与它并列）；
    #   ③ 都没有才退回同级章节。
    # 只影响 context（parents），引用角标仍限 top_k；数量硬封顶，绝不读全文。
    if ana.question_type == "coverage" and ranked:
        lead_pid, _lead_acc, lead_pr, lead_score = ranked[0]
        have = {pid for pid, _, _, _ in ranked}
        blocks = _coverage_section_blocks(
            db, lead_pr["doc_id"], lead_pr.get("section_path") or "",
            int(lead_pr.get("ord") or 0), have, limit=max(top_k_parents, 14))
        if not blocks:
            blocks = _coverage_siblings(db, lead_pr["doc_id"], have,
                                        limit=max(top_k_parents * 3, 10))
        for k, b in enumerate(blocks, start=1):
            pid = b["parent_id"]
            if pid in have:
                continue
            have.add(pid)
            ranked.append((pid, {"n": 1, "max": 0.0, "sim": None},
                           {"doc_id": b["doc_id"], "content": b["content"] or "",
                            "section_path": b["section_path"] or "",
                            "ord": b["ord"] or 0,
                            "source_start_line": b["s_line"] or 0,
                            "source_end_line": b["e_line"] or 0},
                           round(float(lead_score) - 1e-6 * k, 6)))
        ranked.sort(key=lambda x: x[3], reverse=True)
        # 显式排布（不靠分数）：lead → **章节续块**（客观条件项/清单，必须完整进 context）
        # → 至多 3 个其它相关块。这样提示词预算先给续块，不会被大章节挤掉
        # （实测：22 个块时后面的条件项会被 per/total budget 截断，模型又答「未明确列出」）。
        cont_pids = {b["parent_id"] for b in blocks}
        lead_item = ranked[0]
        cont_items = [r for r in ranked[1:] if r[0] in cont_pids]
        cont_items.sort(key=lambda r: r[2].get("ord", 0) or 0)
        # 有章节续块时（点值+条件项型）只留 3 个其它块，把预算让给续块；
        # 没有续块时（同篇并列章节型，如「功能有哪些」）多留几个不同章节。
        rest_cap = 3 if cont_items else max(top_k_parents - 1, 5)
        rest_items = [r for r in ranked[1:] if r[0] not in cont_pids][:rest_cap]
        ranked = [lead_item] + cont_items + rest_items

    if ana.question_type != "coverage":
        ranked = ranked[:max(top_k_parents, 1)]

    if debug is not None:
        debug["final_topk"] = [
            {"rank": i, "parent_id": pid, "doc_id": pr["doc_id"],
             "section_path": pr["section_path"],
            "source_start_line": int(pr.get("source_start_line") or 0),
            "source_end_line": int(pr.get("source_end_line") or 0), "hits": acc["n"],
             "rrf_max": round(acc["max"], 6), "rank_score": score}
            for i, (pid, acc, pr, score) in enumerate(ranked, start=1)
        ]

    references: list[Reference] = []
    parents: list[dict] = []
    for idx, (pid, acc, pr, score) in enumerate(ranked, start=1):
        did = pr["doc_id"]
        if did not in doc_cache:
            t, rel, disp = _doc_row(db, did)
            doc_cache[did] = " ".join(x for x in (t, disp, rel) if x)
        title, rel_path, display_source = _doc_row(db, did)
        title = title or did
        if idx > ref_cap:
            # 覆盖型扩展块：只进 context，不占引用角标（数字=证据，保持简洁）
            parents.append({
                "parent_id": pid, "doc_id": did, "title": title,
                "display_source": display_source or title, "path": rel_path,
                "content": pr["content"], "section_path": pr["section_path"],
                "source_start_line": int(pr.get("source_start_line") or 0),
                "source_end_line": int(pr.get("source_end_line") or 0),
                "score": round(acc["max"], 6), "similarity": acc["sim"],
            })
            continue
        references.append(Reference(
            id=idx, title=title, path=rel_path, snippet=_snippet(pr["content"], query),
            parent_id=pid, doc_id=did, score=round(acc["max"], 6), similarity=acc["sim"],
            display_source=display_source or title,
            source_start_line=int(pr.get("source_start_line") or 0),
            source_end_line=int(pr.get("source_end_line") or 0),
        ))
        parents.append({
            "parent_id": pid,
            "doc_id": did,
            "title": title,
            "display_source": display_source or title,
            "path": rel_path,
            "content": pr["content"],
            "section_path": pr["section_path"],
            "score": round(acc["max"], 6),
            "similarity": acc["sim"],
            "_lex": score,
        })

    result.references = references
    result.parents = parents
    result.counts = {
        "fts_candidates": len(lex_ids),
        "vec_candidates": len(vec_ids),
        "fused": len(fused),
        "parents": len(parents),
    }
    if embedder is None or not db.vec_table_ready or db.signature_mismatch:
        result.warnings.append("向量召回路未启用，本次为纯 FTS5 词法检索")
    return result


def explain_retrieval(db: Database, embedder, query: str,
                      top_k_parents: int = 5) -> dict:
    """开发态诊断（P0-9）：把一次检索的每一步摊开，便于定位是
    Query Analysis / Recall / Grounding / Rerank 哪一环出错。

    ⚠ 只返回 **chunk id / doc id / term / route / score / 理由**，
    绝不返回任何文档正文、API Key 或敏感内容。
    """
    debug: dict = {}
    res = hybrid_search(db, embedder, query, top_k_parents=top_k_parents, debug=debug)
    debug["route"] = res.route
    debug["counts"] = res.counts
    debug["warnings"] = list(res.warnings)
    return debug


def rank_documents(db: Database, limit: int = 20) -> list[dict]:
    """笔记列表（按索引时间倒序）。带 display_source（UX-1）；旧库退回无该列查询。"""
    sql_join = (
        """SELECT d.doc_id, d.rel_path, d.title, d.status, d.file_size, d.mtime,
                  COALESCE(m.display_source, '') AS display_source,
                  (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.doc_id) AS chunks
             FROM documents d LEFT JOIN doc_meta m ON m.doc_id = d.doc_id
            ORDER BY d.mtime DESC LIMIT ?"""
    )
    sql_plain = (
        """SELECT d.doc_id, d.rel_path, d.title, d.status, d.file_size, d.mtime,
                  (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.doc_id) AS chunks
             FROM documents d ORDER BY d.mtime DESC LIMIT ?"""
    )
    for sql in (sql_join, sql_plain):
        try:
            out: list[dict] = []
            for r in db.query(sql, (limit,)):
                d = dict(r)
                if not d.get("display_source"):
                    d["display_source"] = d.get("title") or d.get("rel_path") or ""
                out.append(d)
            return out
        except sqlite3.Error:
            continue
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
