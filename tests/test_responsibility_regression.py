"""Pre-Release Final Patch · 责任归属守卫 L1/L2 —— Mini Responsibility Regression Pack。

纯确定性、零依赖、无模型、无网络。覆盖：
  * 责任归属 8 种句式（L1 统一抽取）：
      ① 由 X 负责  ② X，负责…  ③ X负责…  ④ 负责人：X  ⑤ 负责人为 X
      ⑥ 责任人为 X  ⑦ 责任单位：X  ⑧ 责任单位为 X
  * positive → YES + owner
  * wrong-owner → NO + 正确 owner（防汛/责任单位错指）
  * insufficient → INSUFFICIENT（anchor 不在资料）
  * PASS_THROUGH 边界（问职责列表 / 问工作内容，Router 不得误进 Guard）
  * blocked_message 责任类分支（「由…负责，而不是…」，无「归因于」）

可独立运行（python tests/test_responsibility_regression.py），也可被 test_suite 接入。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import relation_guard as RG  # noqa: E402

# 与 test_relation_guard 不同的合成场景，扩充句式覆盖（不依赖真实资料）。
PARENTS = [
    {"parent_id": "m1", "doc_id": "m", "section_path": "施工分工",
     "content": "## 施工分工\n\n- 基坑支护由中建八局负责。\n- 钢筋绑扎由中铁建工负责。"},
    {"parent_id": "m2", "doc_id": "m", "section_path": "专班",
     "content": "## 专班\n\n- 张伟，负责综合协调。\n- 刘洋负责后勤保障。"},
    {"parent_id": "m3", "doc_id": "m", "section_path": "主体责任",
     "content": "## 主体责任\n\n本项目安全负责人：黄志强。\n质量负责人为王丽娟。"},
    {"parent_id": "m4", "doc_id": "m", "section_path": "防汛责任",
     "content": "## 防汛责任\n\n堤防巡查由长江委负责，地方水务局配合。\n泵站调度由地方水务局负责。"},
    {"parent_id": "m5", "doc_id": "m", "section_path": "事故责任",
     "content": "## 事故责任\n\n本次坍塌事故的责任人为陈国庆，责任单位为城投集团。"},
    {"parent_id": "m6", "doc_id": "m", "section_path": "专项",
     "content": "## 专项\n\n本次演练责任单位：应急管理局。\n技术顾问责任人为孙明。"},
]


def _eval(q):
    rq = RG.analyze_relation(q)
    return rq, RG.evaluate_relation(rq, PARENTS)


def run(ctx, check, section, skip) -> None:
    section("责任归属守卫 L1/L2 · Mini 回归包（8 句式 + 错指 + 不足 + PASS 边界）")

    # ---- 8 句式 positive ----
    rq, gr = _eval("基坑支护由谁负责？")
    check("句式①由X负责 → GUARD/responsibility/YES owner=中建八局",
          rq.route == RG.ROUTE_GUARD and rq.relation_type == "responsibility"
          and gr.verdict == RG.VERDICT_YES and gr.owner == "中建八局")

    rq, gr = _eval("谁负责钢筋绑扎？")
    check("句式③X负责… → YES owner=中铁建工",
          gr.verdict == RG.VERDICT_YES and gr.owner == "中铁建工")

    rq, gr = _eval("综合协调由谁负责？")
    check("句式②X，负责… → YES owner=张伟",
          gr.verdict == RG.VERDICT_YES and gr.owner == "张伟")

    rq, gr = _eval("后勤保障由谁负责？")
    check("句式③X负责…（刘洋）→ YES owner=刘洋",
          gr.verdict == RG.VERDICT_YES and gr.owner == "刘洋")

    rq, gr = _eval("本项目安全由谁负责？")
    check("句式④负责人：X → YES owner=黄志强",
          gr.verdict == RG.VERDICT_YES and gr.owner == "黄志强")

    rq, gr = _eval("质量由谁负责？")
    check("句式⑤负责人为X → YES owner=王丽娟",
          gr.verdict == RG.VERDICT_YES and gr.owner == "王丽娟")

    rq, gr = _eval("堤防巡查由哪个单位负责？")
    check("句式①由X负责（单位）→ YES owner=长江委",
          gr.verdict == RG.VERDICT_YES and gr.owner == "长江委")

    rq, gr = _eval("本次演练由哪个单位负责？")
    check("句式⑦责任单位：X → YES owner=应急管理局",
          gr.verdict == RG.VERDICT_YES and gr.owner == "应急管理局")

    rq, gr = _eval("本次坍塌事故由谁负责？")
    check("句式⑥责任人为X → YES owner=陈国庆",
          gr.verdict == RG.VERDICT_YES and gr.owner == "陈国庆")

    rq, gr = _eval("技术顾问由谁负责？")
    check("句式⑥责任人为X（孙明）→ YES owner=孙明",
          gr.verdict == RG.VERDICT_YES and gr.owner == "孙明")

    # ---- wrong-owner（错指）----
    rq, gr = _eval("基坑支护是否由中铁建工负责？")
    check("wrong-owner ① → NO owner=中建八局",
          gr.verdict == RG.VERDICT_NO and not gr.allow_relation_claim
          and gr.owner == "中建八局")

    rq, gr = _eval("堤防巡查是否由地方水务局负责？")
    check("wrong-owner ①单位错指 → NO owner=长江委",
          gr.verdict == RG.VERDICT_NO and gr.owner == "长江委")

    rq, gr = _eval("本次演练是否由城投集团负责？")
    check("wrong-owner ⑦单位错指 → NO owner=应急管理局",
          gr.verdict == RG.VERDICT_NO and gr.owner == "应急管理局")

    # ---- insufficient（anchor 不在资料）----
    rq, gr = _eval("绿化养护由谁负责？")
    check("insufficient → INSUFFICIENT（不误判 YES/NO）",
          gr.verdict == RG.VERDICT_INSUFFICIENT and not gr.allow_relation_claim)

    # ---- PASS_THROUGH 边界（不得误进 Guard）----
    for q, label in (
        ("负责人有哪些职责？", "问职责列表"),
        ("张伟负责什么工作？", "问工作内容"),
    ):
        rq, gr = _eval(q)
        check(f"PASS 边界「{label}」→ 不进 Guard（行为完全不变）",
              rq.route == RG.ROUTE_PASS and gr.allow_relation_claim)

    # ---- L2：责任类 blocked_message 分支 ----
    rq, gr = _eval("基坑支护是否由中铁建工负责？")
    msg = RG.blocked_message(rq, gr)
    check("L2 责任类 blocked_message 用『由…负责，而不是…』",
          "由" in msg and "负责" in msg and "而不是" in msg
          and "中建八局" in msg and "中铁建工" in msg and "归因于" not in msg)
    rq2, gr2 = _eval("堤防巡查是否由地方水务局负责？")
    msg2 = RG.blocked_message(rq2, gr2)
    check("L2 责任类 blocked_message（单位错指）同样用归属措辞",
          "由" in msg2 and "负责" in msg2 and "而不是" in msg2
          and "长江委" in msg2 and "地方水务局" in msg2)


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
