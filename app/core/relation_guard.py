"""选择性归因守卫（生产）。

**核心观念：「相关」≠「存在事实关系」。** 同一父块共现、同一文档、相邻章节、名称相近的实体，
都**不构成**「A 导致 B / A 具有 B 属性 / A 负责 B」的证据。

**单一语义源**：一次解析 → 同时得到 路由（进不进 Guard）和 关系结构（anchor/target/type）。
Router 与 Parser **绝不允许各维护一套正则** —— 那是上一轮踩过的漂移坑
（router 判「进 Guard」而 parser 说「非关系型」，组合起来哑火）。

规模刻意做小：
  * 只覆盖 **causal / event / responsibility** 三类归因风险；
  * property / impact / confusion / quantity / coverage 一律 `PASS_THROUGH`，生产行为完全不变；
  * 纯确定性、零依赖、无模型、无网络；PASS_THROUGH 的额外开销 ≈ 0（单次正则扫描）。

接入位置：`llm.Gateway.stream_chat()` —— 检索之后、回答之前：
    rq = analyze_relation(query)
    if rq.route == PASS_THROUGH:      →  什么都不做（字节级保持）
    else: gr = evaluate_relation(rq, result.parents)
        gr.allow_relation_claim == False → 走受控回答（**不调用 LLM**），citation 照旧下发。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
ROUTE_GUARD = "ATTRIBUTION_GUARD"
ROUTE_PASS = "PASS_THROUGH"

VERDICT_YES = "YES"
VERDICT_NO = "NO"
VERDICT_INSUFFICIENT = "INSUFFICIENT_RELATION"

#: 因果谓词。**刻意不含「影响」** —— 影响属于 coverage/impact 语义，按设计一律 PASS。
_CAUSE = "导致|造成|引起|引发|致使"
#: 「后果类」宾语白名单；其余（影响/损失/地区/范围…）视为 coverage → PASS。
#: 取舍原则：**拿不准就 PASS** —— 误进 Guard 会破坏正常回答，比漏掉一次守卫更糟。
_CONSEQ_OBJ = "后果|结果|问题|危害|事故"

#: 先判的 PASS 结构覆盖：形式上像关系题、实质是属性/列表题。
#: 「负责人有哪些职责？」（含"负责"但问的是职责列表）必须在这里拦下。
_PASS_OVERRIDE = (
    re.compile(r"职责"),
    re.compile(r"负责[^？?]{0,6}(?:工作|工作内容|事项)"),
)

#: **唯一**的模式表：(route, relation_type, regex)。顺序即优先级，改这里就够了。
_PATTERNS: tuple[tuple[str, str, re.Pattern], ...] = (
    # -------- event：哪个（天气）过程/原因 造成 Y --------
    ("event", ROUTE_GUARD,
     re.compile(rf"^(?:哪个|什么)(?:天气)?(?:过程|事件|原因|因素)(?:{_CAUSE})(?P<t>.+?)[？?]?$")),
    # event（反向）：Y 是由哪个（天气）过程/原因 造成的 —— **必须排在 causal_by 之前**，
    # 否则「X 是由哪个天气过程造成的」会被 causal_by 当成 Y=「哪个天气过程」。
    ("event", ROUTE_GUARD,
     re.compile(rf"^(?P<t>.+?)(?:是)?(?:由|因为|由于)(?:哪个|什么)(?:天气)?(?:过程|事件|原因|因素)"
                rf"(?:{_CAUSE})的?[？?]?$")),
    # -------- causal --------
    ("causal", ROUTE_GUARD,
     re.compile(rf"^(?P<a>.+?)是否(?:会)?(?:{_CAUSE})(?P<t>.+?)[？?]?$")),
    ("causal", ROUTE_GUARD,
     re.compile(rf"^(?P<t>.+?)是否(?:是)?由(?P<a>.+?)(?:{_CAUSE})[？?]?$")),
    ("causal", ROUTE_GUARD,
     re.compile(rf"^(?P<a>.+?)(?:{_CAUSE})(?:了)?(?:哪些|什么)(?:{_CONSEQ_OBJ})[？?]?$")),
    # -------- responsibility --------
    # 「由谁负责 / 由哪个部门负责 / 谁负责的 / 谁负责 X」必须**先于**「由 <责任方> 负责」，
    # 否则「谁」会被解析成责任方。
    ("responsibility", ROUTE_GUARD,
     re.compile(r"^(?P<a>.+?)(?:是否|是)?由谁(?:来)?负责[？?]?$")),
    ("responsibility", ROUTE_GUARD,
     re.compile(r"^(?P<a>.+?)由哪(?:个|些)(?:部门|单位|人员|岗位)负责[？?]?$")),
    ("responsibility", ROUTE_GUARD,
     re.compile(r"^(?P<a>.+?)(?:是)?谁负责的[？?]?$")),
    ("responsibility", ROUTE_GUARD,
     re.compile(r"^谁负责(?P<a>.+?)[？?]?$")),
    ("responsibility", ROUTE_GUARD,
     re.compile(r"^(?P<a>.+?)(?:是否|是)?由(?P<t>.+?)负责[？?]?$")),
    # -------- 以下一律 PASS_THROUGH（仅做结构化记录，不进 Guard） --------
    ("property", ROUTE_PASS,
     re.compile(r"^(?P<a>.+?)的(?P<t>.+?)是(?P<v>.+?)吗[？?]?$")),
    ("property", ROUTE_PASS,
     re.compile(r"^(?P<a>.+?)的(?P<t>[^的]{2,8}?)(?:是多少|是什么|是谁|是什么型号)?[？?]?$")),
    ("impact", ROUTE_PASS,
     re.compile(r"^(?P<a>.+?)(?:造成了|造成|带来|产生了|产生)?(?:哪些|什么)?影响[？?]?$")),
)

_PARTY = re.compile(r"由([\u4e00-\u9fa5A-Za-z0-9\-－]{2,14}?)负责")
_CAUSE_SUBJ = re.compile(rf"([\u4e00-\u9fa5A-Za-z0-9\-－]{{2,14}}?)(?:{_CAUSE})")
_ENTITY = re.compile(r"[A-Z]{1,6}[-－]\d{1,4}")
_SENT = re.compile(r"[。！？；;]")


# --------------------------------------------------------------------------
# 结构化结果
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class RelationQuery:
    """一次解析的产物：`route` 与 `relation_type/anchor/target` **同源**。"""

    query: str
    route: str = ROUTE_PASS
    relation_type: str | None = None
    anchor: str | None = None
    target: str | None = None
    attr: str = ""
    value: str | None = None
    is_relation: bool = False

    @property
    def needs_guard(self) -> bool:
        return self.route == ROUTE_GUARD


@dataclass
class GuardResult:
    verdict: str = VERDICT_INSUFFICIENT
    reason: str = ""
    owner: str | None = None
    unit: str = ""
    preferred_parent_id: str = ""
    allow_relation_claim: bool = False

    def as_dict(self) -> dict:
        return {"guard_state": self.verdict, "guard_reason": self.reason,
                "guard_owner": self.owner, "preferred_parent_id": self.preferred_parent_id}


# --------------------------------------------------------------------------
# Query 分析（唯一入口）
# --------------------------------------------------------------------------
def analyze_relation(query: str) -> RelationQuery:
    """解析**一次**，同时产出路由与关系结构。生产与 Eval 共用这一份。"""
    s = (query or "").strip()
    if not s:
        return RelationQuery(query=s)
    for rx in _PASS_OVERRIDE:
        if rx.search(s):
            return RelationQuery(query=s)

    for rtype, route, rx in _PATTERNS:
        m = rx.match(s)
        if not m:
            continue
        g = m.groupdict()
        a = (g.get("a") or "").strip() or None
        t = (g.get("t") or "").strip() or None
        v = (g.get("v") or "").strip() or None
        # 属性断言题（「X 的 Y 是 Z 吗」）：真正要比对的是**断言值 Z**，不是属性名 Y，
        # 否则会去正文里找「量程=100m」这种根本不存在的串。
        if rtype == "property" and v:
            t = v
        if rtype == "impact" and a and a.endswith(("造成", "导致")):
            a = a[:-2]
        return RelationQuery(query=s, route=route, relation_type=rtype,
                             anchor=a, target=t,
                             attr=(g.get("t") or "").strip() if rtype == "property" else "",
                             value=v, is_relation=bool(a or t))
    return RelationQuery(query=s)


# --------------------------------------------------------------------------
# Evidence unit 切分
# --------------------------------------------------------------------------
def _is_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and len(s) > 2


def _is_sep(line: str) -> bool:
    s = line.strip()
    return bool(s) and set(s) <= set("|-: ")


def units_of(content: str) -> list[dict]:
    """把父块正文切成 evidence unit。

    表格特殊处理：**表头 + 数据行合并为一个单元** —— 否则「量程」这类属性名只出现在表头，
    数据行永远匹配不到（会把正确的属性题误判成无证据）。
    """
    units: list[dict] = []
    header = ""
    for i, ln in enumerate((content or "").splitlines()):
        if not ln.strip():
            continue
        if _is_sep(ln):
            continue
        if _is_row(ln):
            if not header:
                header = ln
                units.append({"text": ln, "kind": "table_header", "line": i})
            else:
                units.append({"text": header + " " + ln, "kind": "table_row", "line": i})
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


def owner_vocab(text: str) -> set[str]:
    """从文档**自身**推导「谁可能是事件/责任主体」——不硬编码任何业务词。"""
    v: set[str] = set()
    for m in re.finditer(rf"([\u4e00-\u9fa5A-Za-z0-9\-－]{{2,14}}?)(?:{_CAUSE})", text):
        v.add(m.group(1))
    for m in re.finditer(rf"由([\u4e00-\u9fa5A-Za-z0-9\-－]{{2,14}}?)(?:{_CAUSE})", text):
        v.add(m.group(1))
    for m in re.finditer(r"由([\u4e00-\u9fa5A-Za-z0-9\-－]{2,14}?)负责", text):
        v.add(m.group(1))
    return {t for t in v if len(t) >= 2}


# --------------------------------------------------------------------------
# Guard
# --------------------------------------------------------------------------
def evaluate_relation(rq: RelationQuery, parents: list[dict],
                      vocab: set[str] | None = None) -> GuardResult:
    """对已路由到 Guard 的关系题做 evidence 级判定。

    parents: `hybrid_search()` 的 `result.parents`（含 `content` / `section_path` / `parent_id`）。
    **不改动**排序、TopK、引用编号。
    """
    if not rq.needs_guard:
        return GuardResult(verdict=VERDICT_YES, reason="PASS_THROUGH（不进入归因守卫）",
                           allow_relation_claim=True)

    a, t, rt = rq.anchor, rq.target, rq.relation_type
    if vocab is None:
        vocab = set()
        for p in parents:
            vocab |= owner_vocab(p.get("content") or "")

    # (parent_id, section_path, unit)
    items: list[tuple[str, str, dict]] = [
        (p.get("parent_id") or "", p.get("section_path") or "", u)
        for p in parents for u in units_of(p.get("content") or "")
    ]

    def _ok(unit_text: str) -> str:
        for pid, _sec, u in items:
            if u["text"] == unit_text:
                return pid
        return ""

    # --- impact：只有 anchor（target 是被问的影响集合）→ 因果单元即成立 ---
    if rt == "impact" and a and not t:
        for pid, _sec, u in items:
            if a in u["text"] and re.search(rf"(?:{_CAUSE})", u["text"]):
                return GuardResult(VERDICT_YES, f"找到「{a}」的因果单元", None,
                                   u["text"], pid, True)
        return GuardResult(VERDICT_INSUFFICIENT, f"未找到「{a}」的因果证据单元")

    # --- event：只有 target（问「哪个事件造成 Y」）→ 指出主体 ---
    if rt == "event" and t and not a:
        for pid, _sec, u in items:
            if t in u["text"]:
                m = _CAUSE_SUBJ.search(u["text"])
                if m:
                    return GuardResult(VERDICT_YES, f"该结果由「{m.group(1)}」造成",
                                       m.group(1), u["text"], pid, True)
        return GuardResult(VERDICT_INSUFFICIENT, f"未找到「{t}」的成因单元")

    # --- 责任归属：看 anchor 所在单元的「由 X 负责」 ---
    if rt == "responsibility" and a:
        for pid, _sec, u in items:
            if a in u["text"]:
                m = _PARTY.search(u["text"])
                if m:
                    party = m.group(1)
                    if t and party != t:
                        return GuardResult(VERDICT_NO, f"责任方为「{party}」，不是「{t}」",
                                           party, u["text"], pid, False)
                    return GuardResult(VERDICT_YES, f"责任方「{party}」", party,
                                       u["text"], pid, True)
        # 找不到「由 X 负责」句式 → **不早退**，继续走 entity / 冲突判定
        # （表格型责任分工就是这样：责任人写在表列里，没有「由…负责」句式）

    # --- STRONG：同一 evidence unit 内同时出现 anchor 与 target ---
    if a and t:
        for pid, _sec, u in items:
            if a in u["text"] and t in u["text"]:
                return GuardResult(VERDICT_YES, f"同一 {u['kind']} 内共现", None,
                                   u["text"], pid, True)

    # --- MEDIUM：anchor 明确来自 section owner，且 target 位于该 section 内 ---
    # 规格允许的次强证据路径。为什么必须有：真实文档里常见「## 大雾过程」小节只列出影响
    # （「高速公路临时封闭。」）而不写「大雾导致…」，此时 STRONG（同句共现）拿不到，
    # 若只认 STRONG 会把**正确**的归因也误判成无证据。
    if a and t:
        for pid, sec, u in items:
            if t in u["text"] and sec and a in sec:
                return GuardResult(VERDICT_YES,
                                   f"target 位于 anchor 所属章节「{sec}」内",
                                   None, u["text"], pid, True)

    # --- 归因冲突：target 出现在**别的**主体 / 实体名下 ---
    if t:
        for pid, _sec, u in items:
            if t not in u["text"]:
                continue
            for w in vocab:
                if a and w in u["text"] and w != a and w not in a and a not in w:
                    return GuardResult(VERDICT_NO, f"该结果归属于「{w}」，不是「{a}」",
                                       w, u["text"], pid, False)
            if a:
                ents = set(_ENTITY.findall(u["text"]))
                if ents and ents != {a} and a not in ents:
                    other = sorted(ents - {a})[0]
                    return GuardResult(VERDICT_NO, f"该属性属于「{other}」，不是「{a}」",
                                       other, u["text"], pid, False)

    hit_a = any(a and a in u["text"] for _, _, u in items)
    hit_t = any(t and t in u["text"] for _, _, u in items)
    if hit_a or hit_t:
        return GuardResult(VERDICT_INSUFFICIENT,
                           "分别找到 anchor / target，但没有直接关系证据")
    return GuardResult(VERDICT_INSUFFICIENT, "检索证据中未出现 anchor/target")


# --------------------------------------------------------------------------
# 受控回答（blocked 时**不调用 LLM**）
# --------------------------------------------------------------------------
def blocked_message(rq: RelationQuery, gr: GuardResult) -> str:
    """受控自然语言：内容只来自 `anchor / target / guard owner`，**不让模型猜**。"""
    a = rq.anchor or "该主体"
    t = rq.target or "该结果"
    if gr.verdict == VERDICT_NO:
        if gr.owner:
            return (f"当前资料中的相关证据把「{t}」归因于「{gr.owner}」，"
                    f"不能据此认定它与「{a}」存在该关系。")
        return (f"当前资料中的相关证据把「{t}」归属于其他事件或主体，"
                f"不能据此归因给「{a}」。")
    return (f"当前资料中没有找到「{a}」与「{t}」之间的直接关系证据，"
            f"因此不能据此确认二者存在该关系。")


def evidence_hint(rq: RelationQuery) -> str:
    """提示用户下方 citation 可自行核验为什么被拒绝建立关系。"""
    return "下方已列出相关资料片段，你可以自行核验该判断依据。"
