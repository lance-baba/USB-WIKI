"""入库语义分析：确定性抽取（常开） + 可选 AI 摘要（降级可用）。

## 为什么要有它

本项目的入库流程此前只有「转 Markdown → 切片 → 索引」，**没有任何语义理解**。
后果不止是「看不到摘要」：笔记之间无从比较，关键词 / 实体也就没有数据来源。
所以这块是入库内容分析的地基。

## 两层设计（与项目既有的降级理念一致）

- **确定性层（零依赖、离线可用、永远执行）**：关键词 / 实体 / 语言 / 规模。
- **增强层（可选）**：本地 Ollama 生成一句话摘要与分类标签；不在线就自动跳过，
  绝不影响入库，也绝不阻塞。

## 刻意不做的事

不宣称 NER、不做实体链接。只做**模式可验证**的抽取（日期、金额、百分比、URL、
邮箱、以机构后缀结尾的名称）。抽不到的就不写 —— 宁可少给，不用模型幻觉填补。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

# 关键词提取用的虚词/功能词：它们出现频率最高，却毫无区分度
_NOISE = {
    "我们", "你们", "他们", "自己", "什么", "怎么", "这样", "那样", "因为", "所以",
    "但是", "如果", "可以", "已经", "还是", "就是", "这个", "那个", "一个", "没有",
    "以及", "并且", "或者", "而且", "然后", "目前", "现在", "今天", "昨天", "明天",
    "进行", "通过", "对于", "关于", "由于", "根据", "按照", "包括", "其中", "同时",
    "表示", "认为", "指出", "介绍", "相关", "方面", "情况", "问题", "内容", "方式",
    "以上", "以下", "之间", "左右", "之后", "之前", "目前", "当日", "次日", "部分",
    "一般", "全部", "大量", "少量", "多数", "少数", "其中", "随后", "此外", "另外",
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "has",
    "have", "had", "not", "but", "you", "your", "our", "their", "its", "can", "will",
    "would", "should", "could", "about", "into", "than", "then", "they", "them",
}

_CJK = r"\u3400-\u4dbf\u4e00-\u9fff"
# frontmatter 必须剥掉再分析：否则 captured_at / source_url 这类字段名
# 会因为「重复出现」而被当成关键词 —— 实测第一版就踩了这个坑。
_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)


def strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text or "", count=1)

_CJK_RUN_RE = re.compile(f"[{_CJK}]+")
_CJK_SEG_RE = re.compile(f"[^{_CJK}]+")
_ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+#.\-]{2,}")

# 关键词里最没用的东西：纯数字、单字、超长串
_MIN_CJK = 2
_MAX_CJK = 8
_MIN_ASCII = 2
_MAX_ASCII = 24

# 实体抽取：只做模式可验证的类别
_PATTERNS: dict[str, re.Pattern] = {
    "日期": re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?"),
    "金额": re.compile(r"(?:[¥￥$]\s*\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s*(?:万元|亿元|万|亿|元|美元|人民币))"),
    "百分比": re.compile(r"\d+(?:\.\d+)?\s*%"),
    "网址": re.compile(r"https?://[^\s<>\"'）)】\]]+"),
    "邮箱": re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    "机构": re.compile(f"[{_CJK}]{{2,12}}(?:公司|集团|研究院|研究所|大学|学院|医院|银行|事务所|管理局|管理局|委员会|协会|中心)"),
}
_ENTITY_LIMIT = 6


@dataclass
class Analysis:
    """一次入库分析的结果。"""

    keywords: list[str] = field(default_factory=list)      # 供界面展示的 top-k
    entities: dict[str, list[str]] = field(default_factory=dict)
    language: str = "unknown"
    chars: int = 0
    summary: str = ""            # 仅增强层会填
    tags: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# 确定性层
# --------------------------------------------------------------------------
# 只用于「内部包含」判定的中文虚词（长度 ≥2，避免误伤）
_NOISE_CJK_INNER = frozenset(
    w for w in _NOISE if len(w) >= 2 and _CJK_RUN_RE.fullmatch(w)
)


def _looks_like_url(token: str) -> bool:
    low = token.lower()
    return low.startswith(("http", "www.")) or "/" in token or "://" in token


def _cjk_ngrams(run: str, sizes: Iterable[int] = (2, 3, 4)) -> Counter:
    """取 CJK 串的 n-gram 频次。中文无词边界，n-gram 是零依赖下最稳的候选生成方式。"""
    out: Counter = Counter()
    for n in sizes:
        for i in range(len(run) - n + 1):
            out[run[i:i + n]] += 1
    return out


def _segments(text: str) -> list[str]:
    """把文本切成候选片段：CJK 连续串 + ASCII 词。"""
    parts: list[str] = []
    for run in _CJK_RUN_RE.findall(text or ""):
        parts.append(run)
    for w in _ASCII_WORD_RE.findall(text or ""):
        parts.append(w)
    return parts


def extract_terms(text: str, title: str = "", limit: int = 40) -> dict[str, float]:
    """抽取加权术语向量。

    做法（全确定性、零依赖）：

    1. 对每个 CJK 连续串取 2/3/4-gram 频次；
    2. **包含过滤**：若某个 n-gram 被一个更长且同样高频的 n-gram 包含，丢短的
       —— 否则「台风」「风杜」「杜鹃」会同时上榜，后者是跨词边界的噪声；
    3. 只保留**在文中重复出现**的 n-gram（频次 ≥ 2）：出现一次的词拿来做关键词
       区分度太低，放进去等于噪声；
    4. 标题里的词额外加权 —— 标题是作者亲自挑的，信噪比最高；
    5. 权重 = 频次 × (1 + log 长度)：长词通常更具体。

    刻意不引入分词器：通用词典不认识个人知识库里的产品名、人名、行话，
    而那恰恰是最该被抽出来的词。n-gram + 频次 + 包含过滤已足够。
    """
    text = strip_frontmatter(text or "")
    counter: Counter = Counter()

    # ⚠ 必须**跨串全局累加**：中文被标点切成大量短串，「台风」在 5 个不同的串里
    # 各出现一次 —— 若按单串统计，每个串内计数都是 1，再按「频次 ≥2」过滤就会
    # 全军覆没（第一版正是这么写的，结果中文关键词一个都出不来）。
    for run in _CJK_RUN_RE.findall(text):
        counter.update(_cjk_ngrams(run))

    for w in _ASCII_WORD_RE.findall(text):
        if _looks_like_url(w):
            continue          # 网址交给实体抽取，不该混进关键词
        counter[w] += 1
        counter[w.lower()] += 1

    # 包含过滤的正确方向：
    #   只有当**更长的词频次不低于它**时，短词才算被完全包含，可以丢弃。
    # 反例（第一版踩的坑）：若一律「被更长词包含就丢」，「台风杜鹃准备」这类
    # 4-gram 会先把「台风」吞掉 —— 而「台风」出现 5 次、那个 4-gram 只出现 2 次，
    # 明显是「台风」更该留下。实测正是这个方向错误导致中文关键词全部消失。
    # 先按频次 ≥2 收窄候选（低频繁词太多，直接两两比较会很慢）。
    freq = {g: n for g, n in counter.items() if n >= 2}
    survivors: dict[str, int] = {}
    for gram, n in sorted(freq.items(), key=lambda kv: (-len(kv[0]), -kv[1])):
        if any(gram != longer and gram in longer and ln >= n for longer, ln in survivors.items()):
            continue
        survivors[gram] = n

    # 标题加权
    title = title or ""
    title_terms = set()
    for run in _CJK_RUN_RE.findall(title):
        for gram in _cjk_ngrams(run, sizes=(2, 3)).keys():
            title_terms.add(gram)
    for w in _ASCII_WORD_RE.findall(title):
        title_terms.add(w)
        title_terms.add(w.lower())

    import math

    lang = detect_language(text)
    scored: dict[str, float] = {}
    for term, n in survivors.items():
        is_cjk = bool(_CJK_RUN_RE.fullmatch(term))
        if is_cjk:
            if not (_MIN_CJK <= len(term) <= _MAX_CJK):
                continue
        elif not (_MIN_ASCII <= len(term) <= _MAX_ASCII):
            continue
        if term in _NOISE or term.lower() in _NOISE:
            continue
        # n-gram 是跨词边界生成的，会拼出「度以上」这类产物：
        # 只要内部含有 ≥2 字的虚词，就说明它是边界噪声而不是真词。
        if is_cjk and any(w in term for w in _NOISE_CJK_INNER):
            continue
        if term.isdigit():
            continue
        weight = n * (1.0 + math.log(len(term)))
        if term in title_terms:
            weight *= 2.0
        if lang == "zh" and not is_cjk:
            # 中文文档里的英文多是页脚服务条款 / 版权声明之类的模板文本
            # （实测中文文章的关键词一度被 information / including / services 占据），
            # 降权让真正的主题词浮上来。
            weight *= 0.5
        # 同一术语的大小写变体只留一个
        key = term if is_cjk else term.lower()
        scored[key] = max(scored.get(key, 0.0), weight)

    # 收尾去碎片：n-gram 会切出「化速率」这种跨词边界的碎片，它其实是
    # 「变化速率」的一部分。按权重从高到低取，**若某词被已选中的更长词包含则丢弃**。
    # 等频次下长词权重更高（weight 含 log 长度项），所以长词会先被选中。
    picked: dict[str, float] = {}
    for term, weight in sorted(scored.items(), key=lambda kv: kv[1], reverse=True):
        if any(term != p and term in p for p in picked):
            continue
        picked[term] = weight
        if len(picked) >= limit:
            break
    return picked


def extract_entities(text: str) -> dict[str, list[str]]:
    """按可验证的模式抽取实体（不宣称 NER）。"""
    out: dict[str, list[str]] = {}
    for label, rx in _PATTERNS.items():
        seen: list[str] = []
        for m in rx.finditer(text or ""):
            val = m.group(0).strip()
            if val and val not in seen:
                seen.append(val)
            if len(seen) >= _ENTITY_LIMIT:
                break
        if seen:
            out[label] = seen
    return out


def detect_language(text: str) -> str:
    """粗粒度语种判定（用于提示，不参与检索）。"""
    t = text or ""
    if not t.strip():
        return "unknown"
    cjk = len(_CJK_RUN_RE.findall(t))
    cjk_chars = sum(len(x) for x in _CJK_RUN_RE.findall(t))
    ascii_words = len(_ASCII_WORD_RE.findall(t))
    if cjk_chars >= 20 and cjk_chars >= ascii_words * 2:
        return "zh"
    if ascii_words >= 10 and cjk_chars < ascii_words:
        return "en"
    if cjk_chars and ascii_words:
        return "mixed"
    return "zh" if cjk else "en"


def analyze(text: str, title: str = "", top_k: int = 6) -> Analysis:
    """确定性分析入口（永远可用，不依赖任何模型）。"""
    text = strip_frontmatter(text or "")
    terms = extract_terms(text, title)
    return Analysis(
        keywords=list(terms.keys())[:top_k],
        entities=extract_entities(text),
        language=detect_language(text),
        chars=len(text or ""),
    )


# --------------------------------------------------------------------------
# 增强层（可选）
# --------------------------------------------------------------------------
_SUMMARY_PROMPT = (
    "用一句中文概括下面这篇笔记的**核心信息**，不超过 60 字。"
    "只输出这句话本身，不要任何前缀、引号或解释。\n\n标题：{title}\n\n正文：\n{body}"
)


def summarize(text: str, title: str, gateway) -> str:
    """用本地模型生成一句话摘要；失败或不可用时返回空串（绝不阻塞入库）。"""
    if not text or gateway is None:
        return ""
    try:
        body = text[:4000]
        reply = gateway.complete(_SUMMARY_PROMPT.format(title=title or "(无)", body=body))
    except Exception:  # noqa: BLE001 - 摘要失败不该影响入库
        return ""
    line = (reply or "").strip().splitlines()[0].strip() if reply else ""
    # 去掉模型爱加的前后缀
    for prefix in ("摘要：", "摘要:", "概括：", "概括:"):
        if line.startswith(prefix):
            line = line[len(prefix):].strip()
    return line[:120]
