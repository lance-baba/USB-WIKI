"""构建 eval/datasets/core_gold.jsonl（维护者工具，非产品代码）。

**Gold 的唯一来源是 eval/fixtures/ 下的 fixture 原文**，本脚本只做两件事：
  1. 把人工编写的问题与期望**机械地**写成 JSONL；
  2. **完整性校验**：`must_contain` 的每一项必须逐字出现在对应 fixture 中；
     负例题的 `must_not_contain` 必须**真的不出现**（否则该题根本不"无答案"，题就废了）。
     任何一条不满足 → 直接抛错，不产出数据集。

用法：python eval/build_gold.py
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
OUT = HERE / "datasets" / "core_gold.jsonl"

JKJ = "jkj_monitoring.md"
QWEN = "qwen_image.md"
WEA = "weather_events.md"
PROC = "procurement.md"

# (id, category, query, doc, must_contain, must_not_contain, relation, evidence_groups, answer_state)
C: list[tuple] = [
    # ---------------------------------------------------------------- direct_fact
    ("jkj_df_purpose", "direct_fact", "监测的目的是什么？", JKJ,
     ["变形规律", "施工安全"], [], None, None, "answered"),
    ("jkj_df_observer_skill", "direct_fact", "现场观测员应该熟悉什么操作？", JKJ,
     ["水准仪", "全站仪"], [], None, None, "answered"),
    ("jkj_df_baseline_use", "direct_fact", "基准点中其中一个点的用途是什么？", JKJ,
     ["放置全站仪", "后视点"], [], None, None, "answered"),
    ("jkj_df_mark", "direct_fact", "水平位移监测点是怎么设置的？", JKJ,
     ["测量钉", "红漆", "标志"], [], None, None, "answered"),
    ("jkj_df_structure", "direct_fact", "围护结构采用什么形式？", JKJ,
     ["钻孔灌注桩", "内支撑"], [], None, None, "answered"),
    ("jkj_df_data_person", "direct_fact", "谁负责数据处理？", JKJ,
     ["陈静", "数据处理"], [], None, None, "answered"),
    ("qwen_df_position", "direct_fact", "Qwen-Image-2.1 是什么产品？", QWEN,
     ["图像生成与编辑", "专业创作"], [], None, None, "answered"),
    ("proc_df_accept", "direct_fact", "设备验收的结论是什么？", PROC,
     ["合格", "附件齐全"], [], None, None, "answered"),

    # ---------------------------------------------------------------- quantity
    ("jkj_q_obs_points", "quantity", "观测点共有几个？", JKJ,
     ["8 个", "CJ1-CJ8"], [], ["观测点", "8"], None, "answered"),
    ("jkj_q_baseline_groups", "quantity", "基准点设置了几组？", JKJ,
     ["3 组基准点", "BM1-BM3"], [], ["基准点", "3 组"], None, "answered"),
    ("jkj_q_vert_points", "quantity", "竖向位移监测点的点号范围是什么？", JKJ,
     ["LZCJ1-LZCJ6"], [], None, None, "answered"),
    ("jkj_q_strut_groups", "quantity", "砼支撑轴力监测组点号是什么？", JKJ,
     ["ZL1-ZL8"], [], None, None, "answered"),
    ("jkj_q_depth", "quantity", "基坑开挖深度约多少米？", JKJ,
     ["9.8 米"], [], None, None, "answered"),
    ("jkj_q_alert_value", "quantity", "周边地表沉降累计预警值是多少？", JKJ,
     ["30mm"], [], ["累计预警值", "30mm"], None, "answered"),

    # ---------------------------------------------------------------- person
    ("jkj_p_leader", "person", "项目负责人是谁？", JKJ,
     ["张伟"], [], ["项目负责人", "张伟"], None, "answered"),
    ("jkj_p_group_leader", "person", "监测组长是谁？", JKJ,
     ["李明"], [], None, None, "answered"),
    ("jkj_p_list", "person", "观测人员有哪些？", JKJ,
     ["张伟", "李明", "王强", "赵磊", "陈静"], [], None,
     [["张伟"], ["李明"], ["王强", "赵磊"], ["陈静"]], "answered"),
    ("jkj_p_quality", "person", "人员素质有什么要求？", JKJ,
     ["专业培训", "资格", "平差"], [], None, None, "answered"),
    ("jkj_p_observers_count", "person", "现场观测员有几名？", JKJ,
     ["现场观测员 2 名", "王强"], [], None, None, "answered"),

    # ---------------------------------------------------------------- model_spec
    ("jkj_m_level_model", "model_spec", "水准仪是什么型号的？", JKJ,
     ["电子水准仪", "天宝DINI03"], ["±1mm", "N2", "SW-30"], ["水准仪", "天宝DINI03"], None, "answered"),
    ("jkj_m_total_station", "model_spec", "全站仪是什么型号？", JKJ,
     ["徕卡TS09"], [], ["全站仪", "徕卡TS09"], None, "answered"),
    ("jkj_m_waterlevel_model", "model_spec", "水位仪的型号是什么？", JKJ,
     ["SW-30"], ["天宝DINI03"], ["水位仪", "SW-30"], None, "answered"),
    ("jkj_m_device_count", "model_spec", "全站仪有几台？", JKJ,
     ["2台"], [], ["全站仪", "2台"], None, "answered"),
    ("jkj_m_origin", "model_spec", "电子水准仪的产地是哪里？", JKJ,
     ["美国"], [], None, None, "answered"),

    # ---------------------------------------------------------------- date_time
    ("jkj_d_calib", "date_time", "水准仪检定有效期到什么时候？", JKJ,
     ["2026 年 5 月 14 日"], [], None, None, "answered"),
    ("jkj_d_period_start", "date_time", "监测期从什么时候开始？", JKJ,
     ["第二阶段开挖"], [], None, None, "answered"),
    ("jkj_d_publish_window", "date_time", "达到预警值后多长时间内发布预警信息？", JKJ,
     ["2 小时"], [], None, None, "answered"),
    ("proc_d_arrival", "date_time", "设备是什么时候到货的？", PROC,
     ["2026 年 3 月"], [], None, None, "answered"),
    ("jkj_d_freq_top", "date_time", "顶板施工后的监测频率是多少？", JKJ,
     ["1次/10d"], [], ["顶板施工后", "1次/10d"], None, "answered"),

    # ---------------------------------------------------------------- location
    ("jkj_l_project", "location", "本工程位于哪里？", JKJ,
     ["镇海区"], [], None, None, "answered"),
    ("jkj_l_baseline_where", "location", "基准点设置在什么地方？", JKJ,
     ["通视条件良好", "不受基坑开挖影响"], [], None, None, "answered"),
    ("jkj_l_point_layout", "location", "观测点沿什么布设、间距多少？", JKJ,
     ["基坑边线", "20 米"], [], None, None, "answered"),
    ("wea_l_typhoon_area", "location", "台风海葵影响了哪些地区？", WEA,
     ["浙江", "福建"], [], None, None, "answered"),

    # ---------------------------------------------------------------- table_relation
    ("jkj_tr_device_table", "table_relation", "监测设备表里有哪些设备和型号？", JKJ,
     ["徕卡TS09", "SW-30"], [], ["徕卡TS09", "2台"], None, "answered"),
    ("jkj_tr_freq_table", "table_relation", "各施工阶段的监测频率分别是多少？", JKJ,
     ["1次/1d", "1次/3d", "1次/10d"], [], ["基坑开挖阶段", "1次/1d"], None, "answered"),
    ("jkj_tr_waterlevel_row", "table_relation", "水位仪的规格型号与数量是多少？", JKJ,
     ["SW-30", "2台"], [], ["SW-30", "2台"], None, "answered"),
    ("jkj_tr_level_row", "table_relation", "电子水准仪的型号和数量？", JKJ,
     ["天宝DINI03", "1台"], [], ["天宝DINI03", "1台"], None, "answered"),
    ("jkj_tr_alert_rate", "table_relation", "水平位移的变化速率预警值是多少？", JKJ,
     ["3mm/d"], [], ["预警值", "3mm/d"], None, "answered"),
    ("jkj_tr_multi_table", "table_relation", "监测频率表里包含了哪些施工阶段？", JKJ,
     ["基坑开挖阶段", "垫层施工后", "基础施工后"], [], None, None, "answered"),

    # ---------------------------------------------------------------- list_coverage
    ("jkj_lc_projects", "list_coverage", "本工程的监测项目包括哪些？", JKJ,
     ["水平位移", "竖向位移", "支撑轴力", "地表沉降", "建筑物沉降"], [], None,
     [["水平位移"], ["竖向位移"], ["支撑轴力"], ["地表沉降"], ["建筑物沉降"]], "answered"),
    ("jkj_lc_conditions", "list_coverage", "哪些情况下应提高监测频率？", JKJ,
     ["达到预警值", "速率加快", "连续降雨", "支护结构出现开裂", "管涌"], [], None,
     [["达到预警值"], ["速率加快"], ["连续降雨"], ["支护结构出现开裂"], ["管涌"]], "answered"),
    ("jkj_lc_quality_items", "list_coverage", "人员素质要求包括哪些？", JKJ,
     ["专业培训", "熟悉水准仪", "数据平差", "安全管理规定"], [], None,
     [["专业培训"], ["水准仪"], ["平差"], ["安全管理规定"]], "answered"),
    ("qwen_lc_features", "list_coverage", "Qwen-Image-2.1 的功能有哪些？", QWEN,
     ["紧凑高效", "透明", "多样化编辑", "纹理"], [], None,
     [["紧凑高效"], ["透明", "统一"], ["多样化编辑"], ["纹理", "美学"]], "answered"),
    ("qwen_lc_edit_modes", "list_coverage", "Qwen-Image-2.1 支持哪些编辑方式？", QWEN,
     ["局部重绘", "指令编辑", "风格迁移", "文字渲染"], [], None,
     [["局部重绘"], ["指令编辑"], ["风格迁移"], ["文字渲染"]], "answered"),

    # ---------------------------------------------------------------- section_overview
    ("jkj_so_freq", "section_overview", "监测频率是怎么样的？", JKJ,
     ["1次/1d", "1次/3d", "1次/5～7d", "1次/10d", "达到预警值", "速率加快"], [], None,
     [["1次/1d"], ["1次/3d"], ["1次/5～7d"], ["1次/10d"], ["达到预警值"]], "answered"),
    ("jkj_so_device", "section_overview", "监测设备情况怎么样？", JKJ,
     ["天宝DINI03", "徕卡TS09", "SW-30"], [], None, None, "answered"),
    ("jekj_so_alert", "section_overview", "预警值是怎么规定的？", JKJ,
     ["30mm", "3mm/d", "2 小时"], [], None, None, "answered"),
    ("qwen_so_edit", "section_overview", "Qwen-Image-2.1 的编辑能力怎么样？", QWEN,
     ["局部重绘", "指令编辑", "风格迁移"], [], None, None, "answered"),
    ("jkj_so_person", "section_overview", "人员配置情况怎么样？", JKJ,
     ["张伟", "李明", "王强", "陈静"], [], None, None, "answered"),

    # ---------------------------------------------------------------- similar_entity
    ("jkj_se_level_vs_water", "similar_entity", "水准仪的精度要求是什么？", JKJ,
     ["N2"], ["±1mm"], None, None, "answered"),
    ("jkj_se_waterlevel_prec", "similar_entity", "水位仪的读数精度是多少？", JKJ,
     ["±1mm"], ["N2", "DINI03"], None, None, "answered"),
    ("jkj_se_base_purpose", "similar_entity", "基准点的作用是什么？", JKJ,
     ["放置全站仪", "后视点"], ["测量钉"], None, None, "answered"),
    ("jkj_se_alert_freq", "similar_entity", "预警时对应的监测频率是多少？", JKJ,
     ["2次/1d"], ["1次/1d"], None, None, "answered"),

    # ---------------------------------------------------------------- cross_section
    ("jkj_cs_freq_and_alert", "cross_section", "监测频率和预警值分别是怎么规定的？", JKJ,
     ["1次/1d", "30mm"], [], None, None, "answered"),
    ("jkj_cs_model_and_prec", "cross_section", "水准仪的型号和精度要求分别是什么？", JKJ,
     ["天宝DINI03", "N2"], [], None, None, "answered"),
    ("jkj_cs_person_and_device", "cross_section", "人员配置和监测设备分别是什么？", JKJ,
     ["张伟", "天宝DINI03"], [], None, None, "answered"),
    ("qwen_cs_feature_and_edit", "cross_section", "Qwen-Image-2.1 有哪些功能？编辑能力如何？", QWEN,
     ["透明", "局部重绘"], [], None, None, "answered"),

    # ---------------------------------------------------------------- negative_no_answer
    ("proc_neg_price", "negative_no_answer", "全站仪的采购单价是多少钱？", PROC,
     [], ["单价", "元"], None, None, "insufficient"),
    ("proc_neg_warranty", "negative_no_answer", "这批设备的质保期是多久？", PROC,
     [], ["质保", "保修"], None, None, "insufficient"),
    ("proc_neg_maker_level", "negative_no_answer", "电子水准仪的生产厂家是哪家？", PROC,
     [], ["厂家"], None, None, "insufficient"),
    ("wea_neg_casualties", "negative_no_answer", "台风海葵造成了多少人员伤亡？", WEA,
     [], ["伤亡", "死亡"], None, None, "insufficient"),
    ("jkj_neg_total_cost", "negative_no_answer", "本项目的监测总费用是多少？", JKJ,
     [], ["费用", "万元", "预算"], None, None, "insufficient"),

    # ---------------------------------------------------------------- attribution
    ("wea_at_cold_area", "attribution", "冷空气影响了哪些地区？", WEA,
     ["长江中下游", "华北北部"], ["浙江东部"], None, None, "answered"),
    ("wea_at_huaxi_area", "attribution", "华西秋雨影响了哪些地区？", WEA,
     ["四川盆地", "陕西南部"], ["浙江"], None, None, "answered"),
    ("wea_at_temp_drop", "attribution", "哪个天气过程导致气温下降 6～8℃？", WEA,
     ["冷空气"], ["台风"], None, None, "answered"),
    ("wea_at_frost_cause", "attribution", "华北北部出现初霜冻是哪个天气过程造成的？", WEA,
     ["冷空气"], ["台风"], None, None, "answered"),
    ("wea_at_typhoon_frost", "attribution", "台风海葵是否导致华北北部出现初霜冻？", WEA,
     [], ["初霜冻"], None, None, "insufficient"),

    # ---------------------------------------------------------------- citation
    ("jkj_cit_freq", "citation", "监测频率是怎么规定的？", JKJ,
     ["1次/1d", "达到预警值"], [], None, None, "answered"),
    ("jkj_cit_level_model", "citation", "水准仪是什么型号？", JKJ,
     ["天宝DINI03"], ["±1mm"], None, None, "answered"),
    ("jkj_cit_obs_points", "citation", "观测点共有多少个？", JKJ,
     ["CJ1-CJ8"], [], None, None, "answered"),
    ("qwen_cit_features", "citation", "Qwen-Image-2.1 有哪些功能？", QWEN,
     ["紧凑高效", "多样化编辑"], [], None, None, "answered"),
    ("wea_cit_typhoon", "citation", "台风海葵影响了哪些地区？", WEA,
     ["浙江", "福建"], [], None, None, "answered"),
]


def main() -> int:
    docs = {p.name: p.read_text(encoding="utf-8") for p in FIXTURES.glob("*.md")}
    rows, problems = [], []
    for (cid, cat, q, doc, must, mustnot, rel, groups, state) in C:
        text = docs.get(doc)
        if text is None:
            problems.append(f"{cid}: fixture {doc} 不存在")
            continue
        for t in must:
            if t not in text:
                problems.append(f"{cid}: must_contain {t!r} 不在 {doc} 中")
        if state == "answered" and not must:
            problems.append(f"{cid}: answered 题必须有 must_contain")
        # must_not_contain 有两种语义，必须分开：
        #   negative_no_answer ：「语料里根本没有」→ 才叫无答案，必须真的不出现；
        #   attribution / 其它 ：「不许被当作本题证据」→ 词可以在别的章节存在
        #                        （华西秋雨的"初霜冻"就在冷空气那节），只要求
        #                        检索出来的 top-1 证据里不得出现。
        if cat == "negative_no_answer":
            if not mustnot:
                problems.append(f"{cid}: 负例题必须有 must_not_contain")
            for t in mustnot:
                if t in text:
                    problems.append(f"{cid}: must_not_contain {t!r} 竟然出现在 {doc} 中（该题不成立）")
        if rel:
            e, v = rel
            # 关系必须在**同一行**上成立（表格行 / 同一句），否则只是"同文共现"
            if not any((e in ln and v in ln) for ln in text.splitlines()):
                problems.append(f"{cid}: 关系 {rel} 未在任何同一行共现")
        for grp in (groups or []):
            for t in grp:
                if t not in text:
                    problems.append(f"{cid}: evidence_group 词 {t!r} 不在 {doc} 中")
        rows.append({
            "id": cid, "category": cat, "query": q,
            "expected": {
                "doc": doc, "must_contain": must, "must_not_contain": mustnot,
                "answer_state": state, "relation": rel,
            },
            "evidence_groups": groups or [],
        })

    if problems:
        print("GOLD INTEGRITY FAILED:")
        for p in problems:
            print("  -", p)
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    from collections import Counter
    cnt = Counter(r["category"] for r in rows)
    print(f"OK 写出 {len(rows)} 条 → {OUT}")
    for k, v in sorted(cnt.items()):
        print(f"   {k:20s} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
