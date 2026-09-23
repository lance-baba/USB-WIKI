"""生产级确定性测试：选择性归因守卫（Router + Guard）。

覆盖（A–J）与一个隔离库 smoke（§13）：
  A causal positive      → GUARD → YES  → allow
  B causal conflict      → GUARD → NO   → block
  C causal insufficient  → GUARD → INSUFFICIENT → block
  D event positive / 负向
  E responsibility positive / negative
  F 普通 property        → PASS_THROUGH
  G coverage             → PASS_THROUGH
  H model/spec           → PASS_THROUGH
  I 「负责人有哪些职责？」 → PASS_THROUGH
  J 「谁负责数据处理？」   → GUARD → 直接证据下 YES
  K smoke：沙尘/大雾 两问（不得回答 YES / 正常回答 YES，citation 都保留）

**不依赖模型、不依赖网络**；PASS_THROUGH 用例不走任何额外分支。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import relation_guard as RG  # noqa: E402

# --------------------------------------------------------------------------
# 合成证据（覆盖「同文档共现但无关系」的结构）
# --------------------------------------------------------------------------
PARENTS = [
    {"parent_id": "p_rain", "doc_id": "d1", "section_path": "强降雨过程",
     "content": "## 强降雨过程\n\n- 强降雨导致河水水位上涨。\n- 强降雨导致 3 处堤防渗水。"},
    {"parent_id": "p_fog", "doc_id": "d1", "section_path": "大雾过程",
     "content": "## 大雾过程\n\n- 大雾导致高速公路临时封闭。"},
    {"parent_id": "p_loss", "doc_id": "d1", "section_path": "灾情汇总",
     "content": "## 灾情汇总\n\n本轮过程直接经济损失约 100 万元。"},
    {"parent_id": "p_duty", "doc_id": "d1", "section_path": "分工",
     "content": "## 分工\n\n- 泄洪调度由水库调度中心负责。\n- 数据处理由陈静负责。"},
]

# 责任归属 8 句式合成 fixture（L1 扩展回归）。覆盖：
#   由 X 负责 / X，负责… / X负责… / 负责人：X / 负责人为 X /
#   责任人为 X / 责任单位：X / 责任单位为 X，外加 防汛指挥部 vs 排水管理处 错指。
PARENTS_RESP = [
    {"parent_id": "rr1", "doc_id": "d2", "section_path": "分工",
     "content": "## 分工\n\n- 数据平差与报表编制由李岚负责。\n- 外业测量由王强负责。"},
    {"parent_id": "rr2", "doc_id": "d2", "section_path": "责任清单",
     "content": "## 责任清单\n\n- 陈涛，负责设备采购与验收。\n- 赵敏负责合同管理。"},
    {"parent_id": "rr3", "doc_id": "d2", "section_path": "职责说明",
     "content": "## 职责说明\n\n本项目负责人：周建国。\n现场安全负责人为市应急管理局。"},
    {"parent_id": "rr4", "doc_id": "d2", "section_path": "调度",
     "content": "## 调度\n\n水库调度由防汛指挥部负责，排水管理处配合。\n排涝泵站运行由排水管理处负责。"},
    {"parent_id": "rr5", "doc_id": "d2", "section_path": "责任认定",
     "content": "## 责任认定\n\n本次事故的责任人为李建国。\n现场处置责任单位为市应急管理局。"},
    {"parent_id": "rr6", "doc_id": "d2", "section_path": "防汛",
     "content": "## 防汛\n\n本次防汛责任单位：防汛指挥部。\n技术支撑责任人为吴磊。"},
]


def _eval(q: str, parents=None):
    rq = RG.analyze_relation(q)
    return rq, RG.evaluate_relation(rq, parents if parents is not None else PARENTS)


# --------------------------------------------------------------------------
def _unit_cases(check) -> None:
    # ---- A. causal positive ----
    rq, gr = _eval("强降雨是否导致河水水位上涨？")
    check("A causal positive → 路由进 Guard", rq.route == RG.ROUTE_GUARD
          and rq.relation_type == "causal")
    check("A causal positive → 解析 anchor/target", rq.anchor == "强降雨"
          and rq.target == "河水水位上涨")
    check("A causal positive → YES 且允许声称关系",
          gr.verdict == RG.VERDICT_YES and gr.allow_relation_claim)

    # ---- B. causal conflict（target 归属别的事件）----
    rq, gr = _eval("强降雨是否导致高速公路临时封闭？")
    check("B causal conflict → 路由进 Guard", rq.route == RG.ROUTE_GUARD)
    check("B causal conflict → NO（阻止建立关系）",
          gr.verdict == RG.VERDICT_NO and not gr.allow_relation_claim)
    check("B causal conflict → owner 从直接证据提取（不是模型猜）", gr.owner == "大雾")

    # ---- C. causal insufficient（两者分别存在，无直接关系证据）----
    rq, gr = _eval("强降雨是否造成直接经济损失？")
    check("C causal insufficient → 路由进 Guard", rq.route == RG.ROUTE_GUARD)
    check("C causal insufficient → INSUFFICIENT",
          gr.verdict == RG.VERDICT_INSUFFICIENT and not gr.allow_relation_claim)

    # ---- D. event positive / 负向 ----
    rq, gr = _eval("哪个过程导致高速公路临时封闭？")
    check("D event positive → 路由进 Guard", rq.route == RG.ROUTE_GUARD
          and rq.relation_type == "event")
    check("D event positive → YES 且指出主体",
          gr.verdict == RG.VERDICT_YES and gr.owner == "大雾")
    rq, gr = _eval("哪个过程导致航班取消？")
    check("D event 负向（target 不在资料）→ INSUFFICIENT",
          gr.verdict == RG.VERDICT_INSUFFICIENT and not gr.allow_relation_claim)

    # ---- E. responsibility positive / negative ----
    rq, gr = _eval("泄洪调度由谁负责？")
    check("E responsibility positive → 路由进 Guard",
          rq.route == RG.ROUTE_GUARD and rq.relation_type == "responsibility")
    check("E responsibility positive → YES", gr.verdict == RG.VERDICT_YES
          and gr.allow_relation_claim)
    rq, gr = _eval("泄洪调度是否由河道管理站负责？")
    check("E responsibility negative → NO",
          gr.verdict == RG.VERDICT_NO and not gr.allow_relation_claim
          and gr.owner == "水库调度中心")

    # ---- E2. 责任归属 8 句式（L1 统一扩展）----
    def _ev(q):
        return _eval(q, PARENTS_RESP)

    # 1 由 X 负责
    rq, gr = _ev("数据平差与报表编制由谁负责？")
    check("E2 ①由X负责 → YES owner=李岚",
          rq.route == RG.ROUTE_GUARD and rq.relation_type == "responsibility"
          and gr.verdict == RG.VERDICT_YES and gr.owner == "李岚")
    # 3 X负责…
    rq, gr = _ev("谁负责外业测量？")
    check("E2 ③X负责… → YES owner=王强",
          gr.verdict == RG.VERDICT_YES and gr.owner == "王强")
    # 2 X，负责…
    rq, gr = _ev("设备采购与验收由谁负责？")
    check("E2 ②X，负责… → YES owner=陈涛",
          gr.verdict == RG.VERDICT_YES and gr.owner == "陈涛")
    # 3（另一例）赵敏负责合同管理
    rq, gr = _ev("合同管理由谁负责？")
    check("E2 ③X负责…（赵敏）→ YES owner=赵敏",
          gr.verdict == RG.VERDICT_YES and gr.owner == "赵敏")
    # 4 负责人：X
    rq, gr = _ev("本项目由谁负责？")
    check("E2 ④负责人：X → YES owner=周建国",
          gr.verdict == RG.VERDICT_YES and gr.owner == "周建国")
    # 5 负责人为 X（撰写 负责单位为）
    rq, gr = _ev("现场安全由谁负责？")
    check("E2 ⑤负责人为X → YES owner=市应急管理局",
          gr.verdict == RG.VERDICT_YES and gr.owner == "市应急管理局")
    # 7 责任单位：X
    rq, gr = _ev("本次防汛由哪个单位负责？")
    check("E2 ⑦责任单位：X → YES owner=防汛指挥部",
          gr.verdict == RG.VERDICT_YES and gr.owner == "防汛指挥部")
    # 6 责任人为 X
    rq, gr = _ev("本次事故由谁负责？")
    check("E2 ⑥责任人为X → YES owner=李建国",
          gr.verdict == RG.VERDICT_YES and gr.owner == "李建国")
    # 8 责任单位为 X
    rq, gr = _ev("现场处置由哪个单位负责？")
    check("E2 ⑧责任单位为X → YES owner=市应急管理局",
          gr.verdict == RG.VERDICT_YES and gr.owner == "市应急管理局")

    # ---- wrong-owner：防汛指挥部 vs 排水管理处（两种句式都拦）----
    rq, gr = _ev("水库调度是否由排水管理处负责？")
    check("E2 wrong-owner（由X负责） → NO owner=防汛指挥部",
          gr.verdict == RG.VERDICT_NO and not gr.allow_relation_claim
          and gr.owner == "防汛指挥部")
    rq, gr = _ev("本次防汛是否由排水管理处负责？")
    check("E2 wrong-owner（责任单位：X） → NO owner=防汛指挥部",
          gr.verdict == RG.VERDICT_NO and not gr.allow_relation_claim
          and gr.owner == "防汛指挥部")
    rq, gr = _ev("本次事故是否由王强负责？")
    check("E2 wrong-owner（责任人为X 错指） → NO owner=李建国",
          gr.verdict == RG.VERDICT_NO and gr.owner == "李建国")

    # ---- L2：责任类 blocked_message 用归属措辞，不用「归因于」----
    rq, gr = _ev("水库调度是否由排水管理处负责？")
    msg = RG.blocked_message(rq, gr)
    check("E2 责任类 blocked_message 用『由…负责，而不是…』",
          "由" in msg and "负责" in msg and "而不是" in msg
          and "防汛指挥部" in msg and "排水管理处" in msg
          and "归因于" not in msg)
    # 因果类 blocked_message 仍用「归因于」（回归保护）
    rq2, gr2 = _eval("强降雨是否导致高速公路临时封闭？")
    msg2 = RG.blocked_message(rq2, gr2)
    check("E2 因果类 blocked_message 仍用『归因于』（不回归）",
          "归因于" in msg2 and "大雾" in msg2 and "强降雨" in msg2)

    # ---- F/G/H/I. 必须 PASS_THROUGH ----
    for q, label in (
        ("水准仪是什么型号？", "F 普通 property"),
        ("电子水准仪的产地是哪里？", "F 普通 property"),
        ("本工程的监测项目包括哪些？", "G coverage"),
        ("Qwen-Image-2.1 的功能有哪些？", "G coverage"),
        ("RT-100 的通道数是多少？", "H model/spec"),
        ("观测点共有几个？", "H quantity"),
        ("负责人有哪些职责？", "I 含『负责』但问职责列表"),
        ("数据处理员负责什么工作？", "I 含『负责』但问工作内容"),
        ("台风过程造成了哪些影响？", "impact（按设计 PASS）"),
        ("采集中断的常见原因有哪些？", "含『原因』但无因果谓词 → PASS"),
    ):
        rq = RG.analyze_relation(q)
        gr = RG.evaluate_relation(rq, PARENTS)
        check(f"{label} → PASS_THROUGH（不进 Guard）", rq.route == RG.ROUTE_PASS)
        check(f"{label} → 守卫绝不拦截", gr.allow_relation_claim)

    # ---- J. 责任边界：允许进 Guard，直接证据存在 → YES ----
    rq, gr = _eval("谁负责数据处理？")
    check("J 谁负责数据处理？ → 路由进 Guard", rq.route == RG.ROUTE_GUARD)
    check("J 谁负责数据处理？ → 直接证据存在时 YES（不做 case-specific 绕过）",
          gr.verdict == RG.VERDICT_YES and gr.allow_relation_claim)

    # ---- 受控回答模板（内容只来自 anchor/target/owner）----
    rq, gr = _eval("强降雨是否导致高速公路临时封闭？")
    msg = RG.blocked_message(rq, gr)
    check("受控回答（NO）点名真实 owner，不出现 YES 措辞",
          "大雾" in msg and "强降雨" in msg and "不能据此" in msg)
    rq2, gr2 = _eval("强降雨是否造成直接经济损失？")
    msg2 = RG.blocked_message(rq2, gr2)
    check("受控回答（INSUFFICIENT）说明无直接关系证据",
          "没有找到" in msg2 and "直接关系证据" in msg2)


# --------------------------------------------------------------------------
def _smoke(check) -> None:
    """隔离库 smoke：沙尘 / 大雾 两问（§13）。"""
    tmp = Path(tempfile.mkdtemp(prefix="usb-wiki-rg-smoke-"))
    try:
        from app.core import chunker, db as db_mod, indexer
        from app.core.embedder import HashEmbedder

        text = (
            "---\ntitle: \"天气过程\"\nstatus: success\n---\n\n"
            "# 沙尘\n\n能见度下降。\n\n"
            "# 大雾\n\n高速公路临时封闭。\n"
        )
        db = db_mod.get_db(tmp / "cache.db", embedding_dim=512)
        db.init_schema()
        parsed = chunker.parse(text, "notes/weather.md")
        indexer.index_parsed(db, parsed, len(text.encode()), time.time(),
                             embedder=HashEmbedder(512))

        # 直接走 Gateway.stream_chat —— 但把 provider 固定为 offline，
        # 保证「被守卫拦截」这条路径**完全不触碰模型**（也证明不会让 LLM 自由生成）。
        from app.core import llm as llm_mod

        gw = llm_mod.Gateway(db, HashEmbedder(512))
        gw.resolve_provider = lambda: ("offline", [])  # type: ignore[method-assign]

        def _frames(q):
            return list(gw.stream_chat(q))

        # Q1：沙尘是否导致高速公路临时封闭？ → **不得回答 YES**
        fr1 = _frames("沙尘是否导致高速公路临时封闭？")
        refs1 = [f for f in fr1 if f.get("type") == "references"]
        meta1 = next((f for f in fr1 if f.get("type") == "meta"), {})
        text1 = "".join(f.get("content", "") for f in fr1 if f.get("type") == "delta")
        check("Smoke Q1（沙尘→高速封闭）被守卫拦截",
              meta1.get("allow_relation_claim") is False)
        check("Smoke Q1 不声称存在该关系",
              ("不能据此" in text1 or "没有找到" in text1) and "沙尘" in text1)
        check("Smoke Q1 **仍保留 citation**（用户可自行核验）",
              bool(refs1) and len(refs1[0].get("refs") or []) >= 1)
        check("Smoke Q1 未产生 LLM 正文之外的自由生成（provider=offline 也未走兜底）",
              "本地搜索结果" not in text1)

        # Q2：大雾是否导致高速公路临时封闭？ → 正常回答（守卫放行）
        fr2 = _frames("大雾是否导致高速公路临时封闭？")
        meta2 = next((f for f in fr2 if f.get("type") == "meta"), {})
        refs2 = [f for f in fr2 if f.get("type") == "references"]
        check("Smoke Q2（大雾→高速封闭）守卫放行",
              meta2.get("allow_relation_claim") is True
              and meta2.get("guard_state") == RG.VERDICT_YES)
        check("Smoke Q2 引用了正确证据",
              bool(refs2) and len(refs2[0].get("refs") or []) >= 1)

        # ---- §8 PASS_THROUGH 必须字节级行为保持 ----
        fr3 = _frames("大雾过程有哪些影响？")
        meta3 = next((f for f in fr3 if f.get("type") == "meta"), {})
        types3 = [f.get("type") for f in fr3]
        check("PASS_THROUGH：meta 帧不带任何 relation 字段（行为完全不变）",
              "relation_route" not in meta3 and "allow_relation_claim" not in meta3
              and "guard_state" not in meta3)
        check("PASS_THROUGH：帧序列仍是 references → meta → … → done",
              types3[0] == "references" and "meta" in types3 and types3[-1] == "done")

        try:
            db.checkpoint_and_close()
        except Exception:  # noqa: BLE001
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
def run(ctx, check, section, skip) -> None:
    section("选择性归因守卫（生产 · 确定性）")
    _unit_cases(check)
    section("选择性归因守卫 · 隔离库 smoke")
    _smoke(check)


if __name__ == "__main__":
    _tally = {"ok": 0, "fail": 0}

    def _check(name, cond):
        if cond:
            _tally["ok"] += 1
            print(f"  PASS  {name}")
        else:
            _tally["fail"] += 1
            print(f"  FAIL  {name}")

    def _sec(name):
        print(f"\n=== {name} ===")

    run(None, _check, _sec, lambda *a, **k: None)
    print(f"\nPASS {_tally['ok']}  FAIL {_tally['fail']}")
    raise SystemExit(1 if _tally["fail"] else 0)
