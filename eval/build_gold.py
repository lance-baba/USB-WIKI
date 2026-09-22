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


# --------------------------------------------------------------------------
# HOLDOUT（V1.1 新增）：**独立语料 + 独立表达**，不得简单改写 DEV 问题。
# 用于阶段性 release decision；开发期默认锁定（见 README 防泄漏规则）。
# --------------------------------------------------------------------------
FIXTURES_HOLDOUT = HERE / "fixtures_holdout"
OUT_HOLDOUT = HERE / "datasets" / "holdout" / "holdout_gold.jsonl"

BR = "bridge_health.md"          # 桥梁健康监测
TU = "tunnel_monitor.md"         # 隧道监测（干扰件，全站仪型号不同）
LF = "crack_gauge_manual.md"     # 仪器技术手册（含型号命名规则）
NW = "november_weather_log.md"   # 11 月天气日志（三个过程并存）

HO_C: list[tuple] = [
    # ---- direct_fact
    ("ho_df_span_type", "direct_fact", "本桥是什么桥型？", BR,
     ["连续箱梁桥", "预应力"], [], None, None, "answered"),
    ("ho_df_light_test", "direct_fact", "本桥是否已开展荷载试验？", BR,
     ["尚未开展荷载试验"], [], None, None, "answered"),
    ("ho_df_calib", "direct_fact", "裂缝观测仪的校准有效期是多久？", LF,
     ["12 个月", "返厂校准"], [], None, None, "answered"),
    ("ho_df_storage", "direct_fact", "LF-21 是否带数据存储功能？", LF,
     ["带存储", "LF-21"], [], None, None, "answered"),
    ("ho_df_who_report", "direct_fact", "桥梁监测报告的编制由谁负责？", BR,
     ["赵敏", "报告编制"], [], None, None, "answered"),
    # ---- quantity
    ("ho_q_deflect_points", "quantity", "主梁挠度测点共有几个？", BR,
     ["12 个", "LD-01 至 LD-12"], [], ["挠度测点", "12"], None, "answered"),
    ("ho_q_pier_points", "quantity", "墩顶位移测点有多少个？", BR,
     ["6 个", "DD-01 至 DD-06"], [], None, None, "answered"),
    ("ho_q_temp_range", "quantity", "结构温度测点的编号范围是什么？", BR,
     ["WD-01 至 WD-08"], [], None, None, "answered"),
    ("ho_q_tunnel_settle", "quantity", "隧道拱顶下沉测点有几个？", TU,
     ["15 个", "SD-01 至 SD-15"], [], None, None, "answered"),
    # ---- person
    ("ho_p_team_size", "person", "桥梁监测小组由几个人组成？", BR,
     ["3 人", "赵敏"], [], None, None, "answered"),
    ("ho_p_detectors", "person", "桥梁的现场检测员是谁？", BR,
     ["周晨", "钱磊"], [], None, None, "answered"),
    ("ho_p_tunnel_leader", "person", "隧道监测的负责人是谁？", TU,
     ["孙航"], [], ["负责人", "孙航"], None, "answered"),
    # ---- model_spec
    ("ho_m_bridge_station", "model_spec", "桥梁监测使用的是哪种全站仪？", BR,
     ["徕卡TS16"], ["徕卡TS09"], ["全站仪", "徕卡TS16"], None, "answered"),
    ("ho_m_crack_model", "model_spec", "桥梁监测的裂缝观测仪是什么型号？", BR,
     ["LF-20"], [], None, None, "answered"),
    ("ho_m_tunnel_station", "model_spec", "隧道监测使用的全站仪是什么型号？", TU,
     ["徕卡TS09"], ["TS16"], ["全站仪", "徕卡TS09"], None, "answered"),
    ("ho_m_level_bridge", "model_spec", "桥梁监测使用的静力水准仪是什么型号？", BR,
     ["JZ-5"], [], None, None, "answered"),
    # ---- table_relation
    ("ho_tr_bridge_table", "table_relation", "桥梁监测仪器表中静力水准仪的数量是多少？", BR,
     ["JZ-5", "4台"], [], ["JZ-5", "4台"], None, "answered"),
    ("ho_tr_tunnel_table", "table_relation", "隧道监测仪器表中收敛计的型号与数量？", TU,
     ["SLJ-3", "5台"], [], ["收敛计", "5台"], None, "answered"),
    ("ho_tr_lf_resolution", "table_relation", "LF-20 的分辨率是多少？", LF,
     ["0.01mm"], [], ["分辨率", "0.01mm"], None, "answered"),
    ("ho_tr_lf30_weight", "table_relation", "LF-30 的整机重量是多少？", LF,
     ["480g"], [], ["LF-30", "480g"], None, "answered"),
    # ---- list_coverage
    ("ho_lc_bridge_items", "list_coverage", "本桥的监测项目包括哪些？", BR,
     ["主梁挠度", "墩顶位移", "支座位移", "结构温度", "车辆荷载", "裂缝宽度"], [], None,
     [["主梁挠度"], ["墩顶位移"], ["支座位移"], ["结构温度"], ["车辆荷载"], ["裂缝宽度"]], "answered"),
    ("ho_lc_tunnel_items", "list_coverage", "隧道监测项目有哪些？", TU,
     ["拱顶下沉", "周边收敛", "地表沉降", "锚杆轴力"], [], None,
     [["拱顶下沉"], ["周边收敛"], ["地表沉降"], ["锚杆轴力"]], "answered"),
    ("ho_lc_lf_params", "list_coverage", "裂缝观测仪的主要技术参数有哪些？", LF,
     ["量程", "分辨率", "工作温度", "供电", "整机重量"], [], None,
     [["量程"], ["分辨率"], ["工作温度"], ["供电"], ["整机重量"]], "answered"),
    # ---- similar_entity
    ("ho_se_lf_resolution_diff", "similar_entity", "LF-20 与 LF-30 的分辨率有什么区别？", LF,
     ["0.01mm", "0.005mm"], [], None, None, "answered"),
    ("ho_se_jz5_what", "similar_entity", "JZ-5 是什么类型的仪器？", BR,
     ["静力水准仪"], ["裂缝观测仪"], None, None, "answered"),
    ("ho_se_sand_area", "similar_entity", "扬沙天气发生在哪些地区？", NW,
     ["甘肃西部", "宁夏北部"], ["江苏南部"], None, None, "answered"),
    # ---- attribution
    ("ho_at_cold_area", "attribution", "寒潮影响了哪些地区？", NW,
     ["内蒙古中部", "河北北部"], ["江苏南部"], None, None, "answered"),
    ("ho_at_sand_area", "attribution", "沙尘过程影响了哪些地区？", NW,
     ["甘肃西部", "宁夏北部"], ["内蒙古"], None, None, "answered"),
    ("ho_at_fog_effect", "attribution", "大雾造成了什么影响？", NW,
     ["高速公路临时封闭", "追尾事故 3 起"], ["PM10"], None, None, "answered"),
    ("ho_at_sand_fog", "attribution", "沙尘天气是否导致高速公路临时封闭？", NW,
     [], ["高速公路"], None, None, "insufficient"),
    # ---- negative_no_answer
    ("ho_neg_airport", "negative_no_answer", "大雾过程影响了哪些机场？", NW,
     [], ["机场"], None, None, "insufficient"),
    ("ho_neg_economic", "negative_no_answer", "寒潮过程造成了多少经济损失？", NW,
     [], ["经济损失", "亿元"], None, None, "insufficient"),
    ("ho_neg_waterproof", "negative_no_answer", "LF 系列裂缝观测仪的防水等级是多少？", LF,
     [], ["防水", "IP"], None, None, "insufficient"),
    # ---- section_overview
    ("ho_so_freq", "section_overview", "桥梁监测频次是怎么规定的？", BR,
     ["1 次/月", "1 次/周", "15℃"], [], None, None, "answered"),
    ("ho_so_alert", "section_overview", "桥梁监测的预警指标有哪些？", BR,
     ["1/600", "5mm", "0.2mm"], [], None, None, "answered"),
]


# --------------------------------------------------------------------------
# ATTRIBUTION DEV PACK（V1.1+）：专门测「相关 ≠ 存在事实关系」。
# **与 Holdout 语料完全不同**；本轮算法开发只允许看这一套 + 原 DEV 72。
# 正负样本接近 1:1；正例必须有 STRONG evidence unit，负例必须**不能**有。
# --------------------------------------------------------------------------
FIXTURES_ATTR = HERE / "fixtures_attribution"
OUT_ATTR = HERE / "datasets" / "attribution_dev.jsonl"

RF = "reservoir_flood.md"
EQ = "equipment_ledger.md"
SI = "storm_impact.md"
BI = "bridge_inspection.md"

# (id, category, query, doc, answer_state, anchor, target)
AT_C: list[tuple] = [
    # ---- causal：正例（同一 evidence unit 内成立）
    ("ad_c01", "causal", "强降雨是否导致河流水位上涨？", RF, "yes", "强降雨", "河流水位上涨"),
    ("ad_c02", "causal", "大风是否导致航班取消？", RF, "yes", "大风", "航班取消"),
    ("ad_c03", "causal", "开闸泄洪是否导致下游水位下降？", RF, "yes", "开闸泄洪", "下游水位下降"),
    ("ad_c06", "causal", "暴雨是否造成供电线路故障？", SI, "yes", "暴雨", "供电线路故障"),
    ("ad_c07", "causal", "雷电是否造成通信基站受损？", SI, "yes", "雷电", "通信基站受损"),
    ("ad_c10", "causal", "伸缩缝渗水是否由排水管堵塞引起？", BI, "yes", "排水管堵塞", "伸缩缝渗水"),
    ("ad_c12", "causal", "主桥支座开裂是否由车辆超载引起？", BI, "yes", "车辆超载", "主桥支座开裂"),
    # ---- causal：负例（两者都在文档里，但**不在同一 evidence unit**，且 target 归属他事件）
    ("ad_c04", "causal", "强降雨是否导致航班取消？", RF, "no", "强降雨", "航班取消"),
    ("ad_c05", "causal", "大风是否导致河流水位上涨？", RF, "no", "大风", "河流水位上涨"),
    ("ad_c08", "causal", "暴雨是否造成通信基站受损？", SI, "no", "暴雨", "通信基站受损"),
    ("ad_c09", "causal", "高温是否造成道路积水？", SI, "no", "高温", "道路积水"),
    ("ad_c11", "causal", "引桥桥面铺装破损是否由车辆超载引起？", BI, "no", "车辆超载", "引桥桥面铺装破损"),
    # ---- causal：INSUFFICIENT（anchor/target 都在，但没有任何关系证据）
    ("ad_n01", "causal", "强降雨是否造成直接经济损失？", RF, "insufficient", "强降雨", "直接经济损失"),
    ("ad_n02", "causal", "暴雨是否造成人员伤亡？", SI, "insufficient", "暴雨", "人员伤亡"),
    ("ad_n03", "causal", "高温是否造成人员伤亡？", SI, "insufficient", "高温", "人员伤亡"),
    # ---- impact
    ("ad_i01", "impact", "强降雨造成了哪些影响？", RF, "yes", "强降雨", "农田受淹"),
    ("ad_i02", "impact", "暴雨造成了哪些影响？", SI, "yes", "暴雨", "道路积水"),
    ("ad_i03", "impact", "雷电造成了哪些影响？", SI, "yes", "雷电", "停电"),
    # ---- property ownership
    ("ad_p01", "property", "AQ-100 的量程是多少？", EQ, "yes", "AQ-100", "量程"),
    ("ad_p02", "property", "AQ-110 的量程是多少？", EQ, "yes", "AQ-110", "量程"),
    ("ad_p03", "property", "AQ-100 的责任人是谁？", EQ, "yes", "AQ-100", "责任人"),
    ("ad_p06", "property", "AQ-100 的检定周期是 24 个月吗？", EQ, "no", "AQ-100", "24 个月"),
    # ---- responsibility
    ("ad_r01", "responsibility", "汛期水位调度由谁负责？", RF, "yes", "汛期水位调度", "水库管理局"),
    ("ad_r02", "responsibility", "日常巡检由谁负责？", BI, "yes", "日常巡检", "养护一班"),
    ("ad_r03", "responsibility", "专项检测由谁负责？", BI, "yes", "专项检测", "结构检测中心"),
    ("ad_r05", "responsibility", "农田排涝是否由水库管理局负责？", RF, "no", "农田排涝", "水库管理局"),
    ("ad_r06", "responsibility", "检定送检是否由测绘队负责？", EQ, "no", "检定送检", "测绘队"),
    ("ad_r07", "responsibility", "应急检查是否由结构检测中心负责？", BI, "no", "应急检查", "结构检测中心"),
    # ---- entity confusion（名称接近的实体，属性不同）
    ("ad_x01", "confusion", "AQ-110 是否由张工负责？", EQ, "no", "AQ-110", "张工"),
    ("ad_x02", "confusion", "AQ-110 的量程是 50m 吗？", EQ, "no", "AQ-110", "50m"),
    ("ad_x03", "confusion", "AQ-100 的责任人是李工吗？", EQ, "no", "AQ-100", "李工"),
    # ---- event attribution
    ("ad_e01", "event", "哪个过程造成沥青软化？", SI, "yes", "高温", "沥青软化"),
    ("ad_e02", "event", "航班取消是由哪个过程造成的？", RF, "yes", "大风", "航班取消"),
]


def _build_attr() -> int:
    """Attribution Pack 专用校验：不仅要词在，还要**关系结构**成立。"""
    from _relation import analyze_query, units_of           # noqa: PLC0415

    docs = {p.name: p.read_text(encoding="utf-8") for p in FIXTURES_ATTR.glob("*.md")}
    rows, problems = [], []
    for (cid, cat, q, doc, state, anchor, target) in AT_C:
        text = docs.get(doc)
        if text is None:
            problems.append(f"{cid}: fixture {doc} 不存在")
            continue
        for t in (anchor, target):
            if t not in text:
                problems.append(f"{cid}: {t!r} 不在 {doc} 中（题目不成立）")
        units = units_of(text)
        same_unit = [u for u in units
                     if anchor in u["text"] and target in u["text"]]
        if state == "yes" and not same_unit:
            problems.append(f"{cid}: 正例但找不到同一 evidence unit 内共现 —— "
                            f"anchor={anchor!r} target={target!r}")
        if state in ("no", "insufficient") and same_unit:
            problems.append(f"{cid}: 负例/无证据题却在同一 unit 内共现 "
                            f"（{same_unit[0]['kind']}: {same_unit[0]['text'][:40]!r}）")
        # 解析器自检：确定性解析必须能从 query 里还原出 anchor / target
        pa = analyze_query(q)
        if anchor is not None and pa["anchor"] and pa["anchor"] != anchor:
            problems.append(f"{cid}: 解析 anchor={pa['anchor']!r} != 声明 {anchor!r}")
        if anchor is not None and not pa["anchor"] and cat != "event":
            problems.append(f"{cid}: 解析不出 anchor（声明 {anchor!r}）")
        pt = pa.get("value") or pa.get("target")
        if target and pt and pt != target and cat not in ("impact",):
            problems.append(f"{cid}: 解析 target={pt!r} != 声明 {target!r}")
        rows.append({
            "id": cid, "category": cat, "split": "attribution_dev", "query": q,
            "expected": {
                "doc": doc, "answer_state": state,
                "anchor": anchor, "target": target,
                "relation_type": pa.get("relation_type"),
                "must_contain": [anchor, target], "must_not_contain": [],
            },
            "evidence_groups": [],
        })

    if problems:
        print("ATTRIBUTION GOLD INTEGRITY FAILED:")
        for p in problems:
            print("  -", p)
        return 1
    OUT_ATR_SAFE = OUT_ATTR
    OUT_ATR_SAFE.parent.mkdir(parents=True, exist_ok=True)
    with OUT_ATTR.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    from collections import Counter
    cnt = Counter(r["category"] for r in rows)
    st = Counter(r["expected"]["answer_state"] for r in rows)
    print(f"OK [attribution_dev] 写出 {len(rows)} 条 → {OUT_ATTR}")
    for k, v in sorted(cnt.items()):
        print(f"   {k:20s} {v}")
    print(f"   answer_state: {dict(st)}  → 正例 {st['yes']} / 非正例 "
          f"{st['no'] + st['insufficient']}")
    return 0


# --------------------------------------------------------------------------
# ROUTER DEV PACK（V1.1+）：测 Scope Router —— 该不该进 Attribution Guard。
# ≥50% 是**边界负例**：看起来像关系问题，实际必须 PASS_THROUGH。
# 覆盖 工程 / 设备 / 天气 / 制度 四个领域。
# --------------------------------------------------------------------------
FIXTURES_ROUTER = HERE / "fixtures_router"
OUT_ROUTER = HERE / "datasets" / "router_dev.jsonl"

RR = "router_reservoir.md"
RD = "router_device.md"
RW = "router_weather.md"
RP = "router_policy.md"

G = "ATTRIBUTION_GUARD"
P_ = "PASS_THROUGH"

# (id, domain, query, doc, route, relation_type, anchor, target, answer_state)
RT_C: list[tuple] = [
    # ================= causal（进 Guard） =================
    ("rt_c01", "工程", "强降雨是否导致河水水位上涨？", RR, G, "causal", "强降雨", "河水水位上涨", "yes"),
    ("rt_c02", "工程", "大风是否导致航班取消？", RR, G, "causal", "大风", "航班取消", "yes"),
    ("rt_c03", "工程", "强降雨是否导致堤防渗水？", RR, G, "causal", "强降雨", "堤防渗水", "yes"),
    ("rt_c04", "工程", "强降雨是否导致航班取消？", RR, G, "causal", "强降雨", "航班取消", "no"),
    ("rt_c05", "工程", "大风是否导致河水水位上涨？", RR, G, "causal", "大风", "河水水位上涨", "no"),
    ("rt_c06", "天气", "高温是否导致用电负荷上升？", RW, G, "causal", "高温", "用电负荷上升", "yes"),
    ("rt_c07", "天气", "台风是否导致乡镇停电？", RW, G, "causal", "台风", "乡镇停电", "yes"),
    ("rt_c08", "天气", "高温是否导致停航？", RW, G, "causal", "高温", "停航", "no"),
    ("rt_c09", "天气", "台风是否导致路面软化？", RW, G, "causal", "台风", "路面软化", "no"),
    ("rt_c10", "工程", "强降雨是否导致广告牌坠落？", RR, G, "causal", "强降雨", "广告牌坠落", "no"),
    ("rt_c11", "设备", "采集中断是否由供电不稳引起？", RD, G, "causal", "供电不稳", "采集中断", "yes"),
    ("rt_c12", "设备", "数据跳变是否由供电不稳引起？", RD, G, "causal", "供电不稳", "数据跳变", "no"),
    ("rt_c13", "设备", "数据跳变是否由接地不良引起？", RD, G, "causal", "接地不良", "数据跳变", "yes"),
    ("rt_c14", "工程", "强降雨是否导致直接经济损失？", RR, G, "causal", "强降雨", "直接经济损失", "insufficient"),
    # ================= event（进 Guard） =================
    ("rt_e01", "天气", "哪个过程导致停航？", RW, G, "event", "台风", "停航", "yes"),
    ("rt_e02", "工程", "航班取消是由哪个过程造成的？", RR, G, "event", "大风", "航班取消", "yes"),
    ("rt_e03", "天气", "哪个过程导致路面软化？", RW, G, "event", "高温", "路面软化", "yes"),
    ("rt_e04", "天气", "停电是由哪个天气过程造成的？", RW, G, "event", "台风", "停电", "yes"),
    ("rt_e05", "设备", "采集中断是由什么原因引起的？", RD, G, "event", "供电不稳", "采集中断", "yes"),
    ("rt_e06", "设备", "哪个原因导致数据跳变？", RD, G, "event", "接地不良", "数据跳变", "yes"),
    ("rt_e07", "工程", "堤防渗水是由哪个过程造成的？", RR, G, "event", "强降雨", "堤防渗水", "yes"),
    ("rt_e08", "工程", "广告牌坠落是由哪个过程造成的？", RR, G, "event", "大风", "广告牌坠落", "yes"),
    # ================= responsibility（进 Guard） =================
    ("rt_r01", "工程", "泄洪调度由谁负责？", RR, G, "responsibility", "泄洪调度", "水库调度中心", "yes"),
    ("rt_r02", "工程", "堤防巡查由谁负责？", RR, G, "responsibility", "堤防巡查", "河道管理站", "yes"),
    ("rt_r03", "设备", "日常维护由谁负责？", RD, G, "responsibility", "日常维护", "现场组", "yes"),
    ("rt_r04", "设备", "故障返修由谁负责？", RD, G, "responsibility", "故障返修", "厂家技术支持", "yes"),
    ("rt_r05", "制度", "设备采购由谁负责？", RP, G, "responsibility", "设备采购", "综合管理部", "yes"),
    ("rt_r06", "制度", "成果归档由谁负责？", RP, G, "responsibility", "成果归档", "资料室", "yes"),
    ("rt_r07", "制度", "安全培训由谁负责？", RP, G, "responsibility", "安全培训", "安全办", "yes"),
    ("rt_r08", "制度", "安全培训是否由安全办负责？", RP, G, "responsibility", "安全培训", "安全办", "yes"),
    ("rt_r09", "制度", "设备采购是否由资料室负责？", RP, G, "responsibility", "设备采购", "资料室", "no"),
    ("rt_r10", "制度", "成果归档是否由综合管理部负责？", RP, G, "responsibility", "成果归档", "综合管理部", "no"),
    ("rt_r11", "工程", "泄洪调度是否由河道管理站负责？", RR, G, "responsibility", "泄洪调度", "河道管理站", "no"),
    ("rt_r12", "设备", "日常维护是否由厂家技术支持负责？", RD, G, "responsibility", "日常维护", "厂家技术支持", "no"),
    ("rt_r13", "工程", "堤防巡查是否由水库调度中心负责？", RR, G, "responsibility", "堤防巡查", "水库调度中心", "no"),
    ("rt_r14", "设备", "故障返修是否由现场组负责？", RD, G, "responsibility", "故障返修", "现场组", "no"),
    # ============ 边界负例：必须 PASS_THROUGH ============
    ("rt_p01", "工程", "大坝的设计库容是多少？", RR, P_, None, None, None, None),
    ("rt_p02", "工程", "坝顶高程是多少？", RR, P_, None, None, None, None),
    ("rt_p03", "设备", "RT-100 的通道数是多少？", RD, P_, None, None, None, None),
    ("rt_p04", "设备", "RT-200 的采样率是多少？", RD, P_, None, None, None, None),
    ("rt_p05", "设备", "RT-100 的责任工程师是谁？", RD, P_, None, None, None, None),
    ("rt_p06", "制度", "项目负责人的职责是什么？", RP, P_, None, None, None, None),
    ("rt_p07", "制度", "监测组长的职责是什么？", RP, P_, None, None, None, None),
    ("rt_p08", "制度", "数据处理员负责什么工作？", RP, P_, None, None, None, None),
    ("rt_p09", "设备", "RT 系列采集仪是什么型号？", RD, P_, None, None, None, None),
    ("rt_p10", "设备", "采集仪支持哪些功能？", RD, P_, None, None, None, None),
    ("rt_p11", "设备", "RT 采集仪用于什么？", RD, P_, None, None, None, None),
    ("rt_p12", "设备", "采集仪有哪些型号？", RD, P_, None, None, None, None),
    ("rt_p13", "工程", "本轮共接报多少起灾情？", RR, P_, None, None, None, None),
    ("rt_p14", "天气", "8 月共发布多少次预警？", RW, P_, None, None, None, None),
    ("rt_p15", "天气", "停电影响了多少个村？", RW, P_, None, None, None, None),
    ("rt_p16", "设备", "RT-200 有多少个通道？", RD, P_, None, None, None, None),
    ("rt_p17", "制度", "监测项目有哪些岗位？", RP, P_, None, None, None, None),
    ("rt_p18", "设备", "RT 系列的责任工程师有哪些人？", RD, P_, None, None, None, None),
    ("rt_p19", "天气", "8 月天气过程包括哪些？", RW, P_, None, None, None, None),
    ("rt_p20", "制度", "本项目有哪些管理人员？", RP, P_, None, None, None, None),
    ("rt_p21", "设备", "RT 系列采集仪有哪些功能？", RD, P_, None, None, None, None),
    ("rt_p22", "制度", "监测项目的工作流程包括哪些环节？", RP, P_, None, None, None, None),
    ("rt_p23", "工程", "强降雨过程造成了哪些损失？", RR, P_, None, None, None, None),
    ("rt_p24", "天气", "台风过程造成了哪些影响？", RW, P_, None, None, None, None),
    ("rt_p25", "制度", "责任划分是怎么规定的？", RP, P_, None, None, None, None),
    ("rt_p26", "制度", "岗位职责是怎么规定的？", RP, P_, None, None, None, None),
    ("rt_p27", "天气", "8 月天气过程情况怎么样？", RW, P_, None, None, None, None),
    ("rt_p28", "制度", "本制度的目的是什么？", RP, P_, None, None, None, None),
    ("rt_p29", "天气", "本轮天气过程的预警发布情况如何？", RW, P_, None, None, None, None),
    ("rt_p30", "设备", "采集中断的常见原因有哪些？", RD, P_, None, None, None, None),
    ("rt_p31", "工程", "停航影响范围是什么？", RW, P_, None, None, None, None),
    ("rt_p32", "设备", "数据跳变的常见原因是什么？", RD, P_, None, None, None, None),
    ("rt_p33", "工程", "强降雨影响了哪些地区？", RR, P_, None, None, None, None),
    ("rt_p34", "天气", "台风影响了哪些地区？", RW, P_, None, None, None, None),
    ("rt_p35", "天气", "高温有哪些影响？", RW, P_, None, None, None, None),
    ("rt_p36", "天气", "停电影响了哪些对象？", RW, P_, None, None, None, None),
]


def _build_router() -> int:
    """Router Pack 专用校验：**路由期望必须与确定性解析器一致**。"""
    from _relation import route_attribution  # noqa: PLC0415

    docs = {p.name: p.read_text(encoding="utf-8") for p in FIXTURES_ROUTER.glob("*.md")}
    rows, problems = [], []
    n_guard = n_pass = 0
    for (cid, dom, q, doc, rte, rtype, anchor, target, state) in RT_C:
        text = docs.get(doc)
        if text is None:
            problems.append(f"{cid}: fixture {doc} 不存在")
            continue
        got = route_attribution(q)
        if got != rte:
            problems.append(f"{cid}: 路由期望 {rte} 但解析器给出 {got} —— {q}")
        if rte == G:
            n_guard += 1
            if rtype not in ("causal", "event", "responsibility"):
                problems.append(f"{cid}: 进 Guard 的 relation_type 必须是 causal/event/responsibility")
            if state not in ("yes", "no", "insufficient"):
                problems.append(f"{cid}: 进 Guard 必须有 answer_state")
            a, t = anchor, target
            if a not in text or t not in text:
                problems.append(f"{cid}: anchor/target 不在 {doc} 中")
            same = any(a in u and t in u for u in _units_of(text))
            if state == "yes" and not same:
                problems.append(f"{cid}: 正例但无同一 evidence unit 共现")
            if state == "no" and same:
                problems.append(f"{cid}: 负例却在同一 unit 共现")
        else:
            n_pass += 1
        rows.append({
            "id": cid, "split": "router_dev", "domain": dom, "query": q,
            "expected_route": rte,
            "expected": {"doc": doc, "relation_type": rtype, "anchor": anchor,
                         "target": target, "answer_state": state},
        })

    if problems:
        print("ROUTER GOLD INTEGRITY FAILED:")
        for p in problems:
            print("  -", p)
        return 1
    OUT_ROUTER.parent.mkdir(parents=True, exist_ok=True)
    with OUT_ROUTER.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    from collections import Counter
    dom = Counter(r["domain"] for r in rows)
    print(f"OK [router_dev] 写出 {len(rows)} 条 → {OUT_ROUTER}")
    print(f"   GUARD {n_guard} / PASS_THROUGH {n_pass} "
          f"（边界负例占比 {n_pass/len(rows)*100:.0f}%）")
    print(f"   领域分布: {dict(dom)}")
    return 0


def _units_of(text: str) -> list[str]:
    from _relation import units_of  # noqa: PLC0415
    return [u["text"] for u in units_of(text)]


def main() -> int:
    rc = _build(C, FIXTURES, OUT, "dev")
    if rc != 0:
        return rc
    print()
    if _build(HO_C, FIXTURES_HOLDOUT, OUT_HOLDOUT, "holdout") != 0:
        return 1
    print()
    if _build_attr() != 0:
        return 1
    print()
    return _build_router()


def _build(case_list, fixtures_dir: Path, out: Path, split: str) -> int:
    docs = {p.name: p.read_text(encoding="utf-8") for p in fixtures_dir.glob("*.md")}
    rows, problems = [], []
    for (cid, cat, q, doc, must, mustnot, rel, groups, state) in case_list:
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
            "id": cid, "category": cat, "query": q, "split": split,
            "expected": {
                "doc": doc, "must_contain": must, "must_not_contain": mustnot,
                "answer_state": state, "relation": rel,
            },
            "evidence_groups": groups or [],
        })

    if problems:
        print(f"GOLD INTEGRITY FAILED [{split}]:")
        for p in problems:
            print("  -", p)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    from collections import Counter
    cnt = Counter(r["category"] for r in rows)
    print(f"OK [{split}] 写出 {len(rows)} 条 → {out}")
    for k, v in sorted(cnt.items()):
        print(f"   {k:20s} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
