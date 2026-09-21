"""Retrieval Benchmark（P0：多层召回 / Grounding / Heading-aware / 重排）。

背景（真机反馈）：问「观测方案中，观测点共有几个？」时，原文明确写着
「经现场踏勘布设沉降观测点8个，点号CJ1-CJ8。」，但系统没有把该段落送进 LLM，
Ollama 只能基于错误 Context 回答「知识库中没有找到」。**这不是 LLM failure，
是 Retrieval failure。**

根因：旧实现把 scope（观测方案）与 target（观测点）连同各自的 trigram 一起
**全部硬 AND** 进一条 FTS MATCH —— 等于要求它们出现在同一个 200 字 child 里。
两者天然分处不同章节，于是答案切片永远召不回。

本文件用**脱敏合成**的《沉降观测方案》构建 25+ 个事实问题，度量 Top1/Top3
父块召回率，并对最硬的那一道设硬门。所有数据均为虚构，不含任何真实项目内容。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from app.core import db as db_mod, indexer, search as search_mod
from app.core.embedder import HashEmbedder

DOC_NAME = "沉降观测方案.md"
SOURCE_FILE = "年产160万新能源汽车前幅车架及后悬部件项目降观测方案260916(2).docx"

# 全部为脱敏合成的虚构内容（人名/编号/数值都是编的）
BENCH_DOC = f"""---
title: 宁波某某开发有限公司某某广场
source_file: {SOURCE_FILE}
source_type: docx
doc_type: imported
status: success
---

# 沉降观测方案

## 目录

一、工程概况
二、观测目的和内容
三、观测人员配备
四、仪器设备
五、观测方法与步骤
六、观测频率与周期
七、报警值与稳定判定
八、成果提交

## 一、工程概况

本次沉降观测对象为1号厂房，建筑面积约2万平方米，结构形式为框架结构。

## 二、观测目的和内容

### 1、观测内容

沉降观测的主要内容包括：基准点联测、沉降观测点观测、数据处理与成果分析。

### 2、沉降点布设

经现场踏勘布设沉降观测点8个，点号CJ1-CJ8。观测点沿建筑物周边均匀布设，相邻观测点间距不超过15米。

### 3、基准点布设

在场区稳定位置布设3个基准点，编号BM1、BM2、BM3。其中BM1高程为+12.500m。

## 三、观测人员配备

| 职责 | 姓名 | 职称 | 联系电话 |
| --- | --- | --- | --- |
| 现场观测负责人 | 张三 | 工程师 | 13800000001 |
| 内业资料整理 | 李四 | 助理工程师 | 13800000002 |
| 观测辅助人员 | 王五 | 技术员 | 13800000003 |

人员素质要求：观测人员须持有相应资格证书，熟悉观测方案与操作规程，并经技术交底后方可上岗。

## 四、仪器设备

本次观测采用DS05型电子水准仪，配套铟钢尺使用。仪器必须在检定有效期内使用。

## 五、观测方法与步骤

观测步骤为：埋设观测点、首次观测、按期复测、数据平差、成果分析。沉降观测点测站高差中误差应不超过±0.15mm。

## 六、观测频率与周期

结构封顶后每3个月观测一次，共计观测12次。施工期间可根据荷载变化情况加密观测。

## 七、报警值与稳定判定

连续两次沉降量超过2mm/d时应停止施工并分析原因。累计沉降量达到30mm时报警。连续两次观测沉降量小于0.01mm/月时可判定为稳定。

## 八、成果提交

每期观测结束后7日内提交观测报告。观测点应设置保护盖与警示标志。
"""

#: (问题, 期望章节片段, 关键证据片段, 类型)
CASES: list[tuple[str, str, str, str]] = [
    ("观测方案中，观测点共有几个？", "沉降点布设", "沉降观测点8个", "quantity"),
    ("观测点共有几个？", "沉降点布设", "沉降观测点8个", "quantity"),
    ("沉降观测点编号是什么？", "沉降点布设", "CJ1-CJ8", "model"),
    ("观测点布设间距是多少？", "沉降点布设", "15米", "quantity"),
    ("观测点布设在哪里？", "沉降点布设", "沿建筑物周边", "location"),
    ("基准点有几个？", "基准点布设", "3个基准点", "quantity"),
    ("基准点编号有哪些？", "基准点布设", "BM1、BM2、BM3", "model"),
    ("BM1高程是多少？", "基准点布设", "+12.500m", "quantity"),
    ("本次观测哪栋厂房？", "工程概况", "1号厂房", "general"),
    ("沉降观测使用什么仪器？", "仪器设备", "DS05", "model"),
    ("电子水准仪型号是什么？", "仪器设备", "DS05", "model"),
    ("现场观测负责人是谁？", "观测人员配备", "张三", "person"),
    ("内业资料谁负责？", "观测人员配备", "李四", "person"),
    ("观测辅助人员是谁？", "观测人员配备", "王五", "person"),
    ("人员素质有什么要求？", "观测人员配备", "资格证书", "person"),
    ("封顶后多久观测一次？", "观测频率与周期", "每3个月观测一次", "datetime"),
    ("观测频率是多少？", "观测频率与周期", "每3个月", "datetime"),
    ("观测周期是多久？", "观测频率与周期", "3个月", "datetime"),
    ("共计观测多少次？", "观测频率与周期", "12次", "quantity"),
    ("连续沉降量超过多少应停止施工？", "报警值与稳定判定", "2mm/d", "quantity"),
    ("累计沉降量达到多少时报警？", "报警值与稳定判定", "30mm", "quantity"),
    ("稳定判定标准是什么？", "报警值与稳定判定", "0.01mm/月", "general"),
    ("观测点测站高差中误差是多少？", "观测方法与步骤", "±0.15mm", "quantity"),
    ("观测步骤有哪些？", "观测方法与步骤", "埋设观测点", "general"),
    ("观测报告几天内提交？", "成果提交", "7日内", "quantity"),
    ("观测点需要什么保护措施？", "成果提交", "保护盖", "general"),
]

#: 硬门（用户指定）：Top1/Top3 必须包含 沉降观测点8个 与 CJ1-CJ8
HARD_QUERY = "观测方案中，观测点共有几个？"

#: coverage 回归 fixture（脱敏合成）：一篇「功能介绍型」文章，多个并列功能章节。
#: 真实失败场景 = 问「X 的功能有哪些？」只答其中一个章节。
COVERAGE_DOC = """---
title: "Qwen-Image-2.1 功能介绍"
source_type: "web"
---

# Qwen-Image-2.1 功能介绍

## 紧凑高效

该模型体积紧凑，推理高效，适合本地部署。

## 原生透明度

原生支持透明图像，统一了创作与编辑流程。

## 多样化编辑

支持多参考、局部编辑等多种编辑方式。

## 逼真质感与精致美学

生成结果纹理逼真，美学精致。
"""


def _bench_db(tmp: Path):
    db = db_mod.get_db(db_path=tmp / "cache.db", embedding_dim=512)
    db.init_schema()
    notes = tmp / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    f = notes / DOC_NAME
    f.write_text(BENCH_DOC, encoding="utf-8")
    emb = HashEmbedder(512)
    indexer.index_file(db, f, emb)
    return db, emb


def run(ctx, check, section, skip) -> None:  # noqa: ARG001
    section("Retrieval Benchmark（多层召回 / Grounding / 重排）")
    tmp = Path(tempfile.mkdtemp(prefix="bench_"))
    try:
        db, emb = _bench_db(tmp)

        # ---------------------------------------------------------------- P0-1
        # 先证明「硬 AND」确实召不回答案切片，再证明分层召回能召回。
        ans_chunks = [r["chunk_id"] for r in db.query(
            "SELECT chunk_id FROM chunks WHERE content LIKE '%沉降观测点8个%'")]
        check("P0-1 答案切片确实在库里（原文含「沉降观测点8个」）", bool(ans_chunks),
              f"{len(ans_chunks)} 个")
        ans_cid = ans_chunks[0] if ans_chunks else ""
        # 旧实现的本质 bug：把 scope(观测方案) 与 target(观测点) 一起**硬 AND**，
        # 等于要求两者出现在同一分块的「自身内容」里。这里用确定性的子串 AND 复现
        # 该思想，但只查**子切片自身展示内容**（chunks.content，即旧 FTS 索引的列，
        # 不含章节路径）—— 答案分块自身只含「观测点」不含「观测方案」，故召不回。
        # （注：不能查 retrieval_text，因为文档标题「沉降观测方案」会沿章节路径下灌到
        #   每个分块，使「观测方案」成为子串而掩盖了该 bug；那不是旧实现的语义。）
        rows = db.query(
            "SELECT chunk_id FROM chunks "
            "WHERE content LIKE '%观测方案%' AND content LIKE '%观测点%'")
        strict_ids = [r["chunk_id"] for r in rows]
        check("P0-1 复现：scope+target 必须同块（旧硬 AND 思想）召不回答案切片",
              ans_cid not in strict_ids, f"strict 命中 {len(strict_ids)} 条")
        tier_ids, tier_of, _route = search_mod._tiered_recall(db, ["观测方案"], ["观测点"], 20)
        check("P0-1 修复：分层召回把答案切片召回了",
              ans_cid in tier_ids, f"tier={tier_of.get(ans_cid)} 共 {len(tier_ids)} 条")

        # ---------------------------------------------------------------- 章节路径
        row = db.query_one("SELECT section_path, retrieval_text FROM chunks WHERE chunk_id = ?",
                           (ans_cid,))
        spath = (row["section_path"] if row else "") or ""
        check("P0-4 章节路径已生成（Heading-aware）",
              "沉降点布设" in spath, spath)
        check("P0-4 retrieval_text = 章节路径 + 原文",
              bool(row) and spath in row["retrieval_text"]
              and "沉降观测点8个" in row["retrieval_text"])

        # ---------------------------------------------------------------- 批量评测
        rows = []
        hit1 = hit3 = 0
        for q, want_section, evidence, kind in CASES:
            res = search_mod.hybrid_search(db, emb, q, top_k_parents=5)
            tops = res.parents
            h1 = bool(tops) and evidence in (tops[0]["content"] or "")
            h3 = any(evidence in (p["content"] or "") for p in tops[:3])
            sec_ok = any(want_section in (p.get("section_path") or "") + (p["content"] or "")
                         for p in tops[:3])
            hit1 += int(h1)
            hit3 += int(h3)
            rows.append((q, h1, h3, sec_ok, res.route,
                         (tops[0].get("section_path") or "")[:40] if tops else "（无召回）",
                         kind))
        n = len(CASES)
        top1 = hit1 / n
        top3 = hit3 / n

        for q, h1, h3, sec_ok, route, top_sec, kind in rows:
            if not h3:
                print(f"    [MISS] {q}  route={route} top1_sec={top_sec} kind={kind}")
            elif not h1:
                print(f"    [top3] {q}  top1_sec={top_sec}")

        check(f"Benchmark 题数 >= 25（实际 {n}）", n >= 25, str(n))
        check(f"Top1 父块召回率 >= 90%（实际 {top1:.0%}）", top1 >= 0.90, f"{hit1}/{n}")
        check(f"Top3 父块召回率 >= 96%（实际 {top3:.0%}）", top3 >= 0.96, f"{hit3}/{n}")

        # ---------------------------------------------------------------- 硬门
        res = search_mod.hybrid_search(db, emb, HARD_QUERY, top_k_parents=5)
        blob = "\n".join((p["content"] or "") for p in res.parents[:3])
        check("硬门：Top3 含「沉降观测点8个」", "沉降观测点8个" in blob,
              str([p.get("section_path") for p in res.parents[:3]]))
        check("硬门：Top3 含「CJ1-CJ8」", "CJ1-CJ8" in blob)
        check("硬门：Top1 就是该章节",
              bool(res.parents) and "沉降观测点8个" in (res.parents[0]["content"] or ""),
              (res.parents[0].get("section_path") if res.parents else "无召回") or "")

        # 明确存在于原文的事实，绝不能回答「找不到」
        check("事实性问题不得落空（引用数 > 0）", len(res.references) > 0, str(res.counts))

        # 来源显示名（UX-1 联动）
        check("引用来源显示为源文件名",
              bool(res.references) and res.references[0].display_source == SOURCE_FILE,
              res.references[0].display_source if res.references else "")

        # ---------------------------------------------------------------- P0-9 诊断
        trace = search_mod.explain_retrieval(db, emb, HARD_QUERY, top_k_parents=5)
        check("P0-9 诊断含 question_type=quantity",
              trace.get("question_type") == "quantity", str(trace.get("question_type")))
        check("P0-9 诊断区分 scope / target",
              "观测方案" in (trace.get("scope_terms") or [])
              and "观测点" in (trace.get("target_terms") or []),
              f"scope={trace.get('scope_terms')} target={trace.get('target_terms')}")
        check("P0-9 诊断含 final_topk 且只暴露 id/score（无正文）",
              bool(trace.get("final_topk"))
              and all(set(x) <= {"rank", "parent_id", "doc_id", "section_path", "hits",
                                 "rrf_max", "rank_score",
                                 "source_start_line", "source_end_line"}
                      for x in trace["final_topk"]))
        check("P0-9 诊断含 rejected 理由（便于定位召回/Grounding/重排）",
              isinstance(trace.get("rejected"), list) and bool(trace.get("grounded")))

        # ---------------------------------------------------------------- 不变量
        check("top_k 未被暴力放大（默认仍为 5）", search_mod.hybrid_search.__defaults__[0] == 5,
              str(search_mod.hybrid_search.__defaults__))
        check("语料里不存在的词仍然如实返回空（不硬凑）",
              len(search_mod.hybrid_search(db, emb, "量子纠缠态装置", top_k_parents=3).references) == 0)

        # ---------------------------------------------------------- coverage 覆盖型
        # 真实失败样本：「Qwen-Image-2.1 的功能有哪些？」召回塌缩到 1 个 parent，
        # 同篇的其它功能章节（紧凑高效/多样化编辑/逼真纹理）全被漏掉。
        # 修复 = coverage 问题类型 + 同篇章节补充 + section 多样性排序（不放大 top_k）。
        check("coverage：问句类型识别为 coverage",
              search_mod.analyze_query("Qwen-Image-2.1的功能有哪些").question_type == "coverage")
        check("coverage：问句尾巴被剥掉（不整串去检索）",
              search_mod.analyze_query("Qwen-Image-2.1的功能有哪些").target
              == ["Qwen", "Image", "2.1"],
              str(search_mod.analyze_query("Qwen-Image-2.1的功能有哪些").target))
        check("更具体的类型优先于 coverage（人员列举不当成泛化覆盖）",
              search_mod.question_type("现场观测负责人是谁") == "person",
              search_mod.question_type("现场观测负责人是谁"))

        cov_dbg = {}
        cov_doc = tmp / "notes" / "coverage-fixture.md"
        cov_doc.write_text(COVERAGE_DOC, encoding="utf-8")
        indexer.index_file(db, cov_doc, emb)
        cov = search_mod.hybrid_search(db, emb, "Qwen-Image-2.1的功能有哪些",
                                       top_k_parents=5, debug=cov_dbg)
        cov_secs = {(r.get("section_path") or "").split(" > ")[-1].strip("# ")
                    for r in cov_dbg.get("final_topk", [])
                    if (r.get("section_path") or "").count(" > ") == 1}
        checks_needed = {"紧凑高效", "原生透明度", "多样化编辑", "逼真质感与精致美学"}
        check("coverage：Top5 覆盖 ≥4 个不同章节（功能列举不再只答一节）",
              len(cov_secs) >= 4, str(sorted(cov_secs)))
        check("coverage：用户点名的功能章节全部进 Top5",
              checks_needed <= cov_secs, str(sorted(checks_needed - cov_secs)))
        # B2/B3 起：coverage 的 **context（parents）** 允许有界扩展（整节覆盖），
        # 但**引用角标**仍严格限 top_k —— 数字=证据，保持简洁。
        check("coverage：引用角标仍 ≤ top_k（未放大）",
              len(cov.references) <= 5, str(len(cov.references)))
        check("coverage：context 按章节扩展（parents ≥ references）",
              len(cov.parents) >= len(cov.references), str((len(cov.parents), len(cov.references))))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
