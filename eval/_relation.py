"""Attribution Spike 的共享确定性逻辑：query 关系解析 + evidence unit 切分。

**无 LLM、无模型、纯正则与字符串** —— 先证明确定性规则有没有价值，再谈更重的东西。

核心观念：
  「相关」 != 「存在事实关系」。同一 parent、同一文档、相邻章节的共现，
  **不构成** A 导致 B / A 具有 B 属性 的证据。

evidence unit 优先级（与 Spike 规格一致）：
  STRONG  : 同一 table row（表头 + 该行算一个单元）/ 同一 list item / 同一句
  MEDIUM  : 同一短段落，且 section_path 明确归属 query anchor
  INVALID : 只在同一 parent、只在同一文档、分属不同 sibling section
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------
# Query 关系解析（确定性）
# --------------------------------------------------------------------------
#: 关系类型 → 触发词
_REL_WORDS = {
    "causal": ("导致", "造成", "引起"),
    "responsibility": ("负责",),
    "property": ("型号", "量程", "参数", "分辨力", "责任人", "检定周期", "用途", "状态", "精度"),
    "impact": ("影响", "哪些影响"),
    "event": ("哪个过程", "哪个事件", "什么造成", "什么引起"),
}

_P = [
    # X 是否（会）导致/造成/引起 Y
    ("causal", re.compile(r"^(?P<a>.+?)是否(?:会)?(?:导致|造成|引起)(?P<t>.+?)[？?]?$")),
    # X 是否由 Y 引起/导致/造成   → Y 是因，X 是果（角色互换）
    ("causal_by", re.compile(r"^(?P<t>.+?)是否(?:是)?由(?P<a>.+?)(?:引起|导致|造成)[？?]?$")),
    # X 是由什么引起的 / Y 是由哪个（天气）过程/原因造成的
    # ⚠ 词表必须与 Router 一致（天气/原因/因素），否则 router 判「进 Guard」而 parser 说
    #   「非关系型查询」，组合起来就会出现「进了 Guard 却什么都不做」的哑火。
    ("event", re.compile(r"^(?P<t>.+?)(?:是)?由(?:哪个|什么)(?:天气)?(?:过程|事件|原因|因素)"
                         r"(?:造成|导致|引起)的?[？?]?$")),
    # 哪个（天气）过程/原因造成 Y
    ("event", re.compile(r"^(?:哪个|什么)(?:天气)?(?:过程|事件|原因|因素)"
                         r"(?:造成|导致|引起)(?P<t>.+?)[？?]?$")),
    # X（是否）由谁负责 —— 必须排在「由 <责任方> 负责」之前，否则会把「谁」解析成责任方。
    # 且 anchor 里不能带上「是否」（否则 anchor='农田排涝是否'）。
    ("responsibility", re.compile(r"^(?P<a>.+?)(?:是否|是)?由谁负责[？?]?$")),
    ("responsibility", re.compile(r"^谁负责(?P<a>.+?)[？?]?$")),
    ("responsibility", re.compile(r"^(?P<a>.+?)(?:是否|是)?由(?P<t>.+?)负责[？?]?$")),
    # X 的 Y 是 Z 吗（属性断言）
    ("property", re.compile(r"^(?P<a>.+?)的(?P<t>.+?)是(?P<v>.+?)吗[？?]?$")),
    ("property", re.compile(r"^(?P<a>.+?)的(?P<t>[^的]{2,8}?)(?:是多少|是什么|是谁|是什么型号)?[？?]?$")),
    # X 造成了哪些影响 / 影响了哪些
    ("impact", re.compile(r"^(?P<a>.+?)(?:造成了|造成|带来|产生了|产生)?(?:哪些|什么)?影响[？?]?$")),
]


# --------------------------------------------------------------------------
# Attribution Scope Router（V1.1+）
# --------------------------------------------------------------------------
#: 因果动词。**刻意不含「影响」**—— 影响属于 coverage/impact 语义，按规格一律 PASS。
_CAUSE_V = "导致|造成|引起|引发|致使"
#: 「后果类」宾语白名单。其余宾语（影响/损失/地区/范围/部门/人员…）视为 coverage → PASS。
#: 取舍原则：**拿不准就 PASS** —— 误进 Guard 会破坏正常回答，比漏掉一次守卫更糟。
_CONSEQ_OBJ = "后果|结果|问题|危害|事故"

#: 先匹配的 **PASS 结构覆盖**：形式上像关系题、实质是属性/列表题。
_PASS_OVERRIDE = [
    re.compile(r"职责"),                                  # 「负责人有哪些职责？」≠ 责任归因
    re.compile(r"负责[^？?]{0,6}(?:工作|工作内容|事项)"),      # 「数据处理员负责什么工作？」
]

#: 高三 risk 归因结构（进入 Attribution Guard）
_GUARD_PATTERNS = [
    # causal 断言：X 是否（会）导致/造成/引起 Y
    re.compile(rf"^.+?是否(?:会)?(?:{_CAUSE_V}).+?[？?]?$"),
    # causal 断言（角色互换）：X 是否（是）由 Y 造成/引起
    re.compile(rf"^.+?是否(?:是)?由.+?(?:{_CAUSE_V})[？?]?$"),
    # causal 后果询问：X 导致了哪些「后果类」？——宾语必须是后果类
    re.compile(rf"^.+?(?:{_CAUSE_V})(?:了)?(?:哪些|什么)(?:{_CONSEQ_OBJ})[？?]?$"),
    # event：哪个（天气）过程/事件/原因 导致 Y
    re.compile(rf"^(?:哪个|什么)(?:天气)?(?:过程|事件|原因|因素)(?:{_CAUSE_V}).+?[？?]?$"),
    # event（反向）：Y 是由哪个（天气）过程/原因 造成的
    re.compile(rf"^.+?(?:是)?(?:由|因为|由于)(?:哪个|什么)(?:天气)?(?:过程|事件|原因|因素)(?:{_CAUSE_V})的?[？?]?$"),
    # responsibility 断言：X 是否（是）由 Y 负责
    re.compile(r"^.+?是否(?:是)?由.+?负责[？?]?$"),
    # responsibility 询问：X 由谁/由哪个部门负责
    re.compile(r"^.+?由谁(?:来)?负责[？?]?$"),
    re.compile(r"^.+?由哪(?:个|些)(?:部门|单位|人员|岗位)负责[？?]?$"),
    re.compile(r"^.+?(?:是)?谁负责的[？?]?$"),
    re.compile(r"^谁负责.+?[？?]?$"),
]


def route_attribution(q: str) -> str:
    """确定性路由：`ATTRIBUTION_GUARD` 或 `PASS_THROUGH`。

    **只看问句结构，不看单个关键词** —— 出现「负责/原因/影响」不等于要进 Guard：
      「负责人有哪些职责？」      → PASS（属性/职责列表）
      「采集中断的常见原因有哪些？」→ PASS（没有因果谓词，只是原因字段查询）
      「台风造成了哪些影响？」      → PASS（影响=coverage 语义）
      「X 是否导致 Y？」           → GUARD
      「X 是否由 Y 负责？」        → GUARD
    零依赖、零模型、纯正则；单次调用微秒级。
    """
    s = (q or "").strip()
    if not s:
        return "PASS_THROUGH"
    for rx in _PASS_OVERRIDE:
        if rx.search(s):
            return "PASS_THROUGH"
    for rx in _GUARD_PATTERNS:
        if rx.match(s):
            return "ATTRIBUTION_GUARD"
    return "PASS_THROUGH"


def analyze_query(q: str) -> dict:
    """把查询解析成 {anchor, target, verb, relation_type, is_relation}。

    确定性、零依赖；**不引入 LLM**。解析不出关系时 is_relation=False（该题不进入 guard）。
    """
    s = (q or "").strip()
    for kind, rx in _P:
        m = rx.match(s)
        if not m:
            continue
        g = m.groupdict()
        a = (g.get("a") or "").strip()
        t = (g.get("t") or "").strip()
        v = (g.get("v") or "").strip()
        if kind == "property" and v:
            # 属性断言题（「X 的 Y 是 Z 吗」）：真正要比对的是**断言值 Z**，
            # 不是属性名 Y —— 否则 guard 会去正文里找「量程=100m」这种不存在的串。
            t = v
        if kind == "impact" and a.endswith(("造成", "导致")):
            a = a[:-2]
        # 关系类型归并
        rtype = {"causal_by": "causal"}.get(kind, kind)
        return {
            "anchor": a or None,
            "target": t or None,
            "attr": (g.get("t") or "").strip() if kind == "property" else "",
            "value": v or None,
            "relation_type": rtype,
            "is_relation": bool(a or t),
            "raw": kind,
        }
    return {"anchor": None, "target": None, "attr": "", "value": None,
            "relation_type": None, "is_relation": False, "raw": None}


# --------------------------------------------------------------------------
# Evidence unit 切分
# --------------------------------------------------------------------------
_SENT = re.compile(r"[。！？；;]")
_ENTITY = re.compile(r"[A-Z]{1,6}[-－]\d{1,4}")


def is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and len(s) > 2


def is_table_sep(line: str) -> bool:
    s = line.strip()
    return bool(s) and set(s) <= set("|-: ")


def lines_of(content: str) -> list[str]:
    return [ln for ln in (content or "").splitlines() if ln.strip()]


def units_of(content: str) -> list[dict]:
    """把 parent 正文切成 evidence unit。

    表格特殊处理：**表头 + 数据行合并为一个单元** —— 否则「量程」这种属性名只出现在
    表头里，数据行永远匹配不到（会把正确的属性题误判成无证据）。
    """
    ls = lines_of(content)
    units: list[dict] = []
    header = ""
    for i, ln in enumerate(ls):
        if is_table_sep(ln):
            continue
        if is_table_row(ln):
            if not header:
                header = ln
                units.append({"text": ln, "kind": "table_header", "line": i})
                continue
            units.append({"text": (header + " " + ln), "kind": "table_row", "line": i})
            continue
        header = ""
        body = ln.lstrip("-•*\t 　").strip()
        parts = [p.strip() for p in _SENT.split(body) if p.strip()]
        if len(parts) <= 1:
            units.append({"text": body, "kind": "line", "line": i})
        else:
            for p in parts:
                units.append({"text": p, "kind": "sentence", "line": i})
    return units


def owner_vocab(doc_text: str) -> set[str]:
    """从文档自身推导「谁可能是事件/责任主体」——不硬编码任何业务词。

    两类来源：
      1. 因果句里的主语：`X 导致/造成/引起`、`由 X 引起`；
      2. 责任句里的责任方：`由 X 负责`。
    """
    v: set[str] = set()
    for m in re.finditer(r"([\u4e00-\u9fa5A-Za-z0-9\-－]{2,14}?)(?:导致|造成|引起)", doc_text):
        v.add(m.group(1))
    for m in re.finditer(r"由([\u4e00-\u9fa5A-Za-z0-9\-－]{2,14}?)(?:引起|导致|造成)", doc_text):
        v.add(m.group(1))
    for m in re.finditer(r"由([\u4e00-\u9fa5A-Za-z0-9\-－]{2,14}?)负责", doc_text):
        v.add(m.group(1))
    return {t for t in v if len(t) >= 2}


def entities_in(text: str) -> set[str]:
    return set(_ENTITY.findall(text or ""))


def contains(hay: str, needle: str) -> bool:
    return bool(needle) and needle in (hay or "")
