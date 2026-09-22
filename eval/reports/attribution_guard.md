# USB-WIKI · Attribution Guard Spike V1

- 生成：2026-09-22 21:50:25
- Attribution DEV Pack：**33 题**（{'causal': 15, 'impact': 3, 'property': 4, 'responsibility': 6, 'confusion': 3, 'event': 2}）
- 生产代码未改动；**未加载任何 reranker**；guard 为纯确定性逻辑。

## Attribution 指标（baseline = 只看共现 / candidate = guard）

| 指标 | Baseline（共现） | Candidate（guard） |
| --- | --- | --- |
| Positive Relation Accuracy | 100.0% | **100.0%** |
| Negative Relation Accuracy | 0.0% | **100.0%** |
| Abstention Accuracy | 0.0% | **100.0%** |
| Attribution Precision | 58.1% | **100.0%** |
| Attribution Recall | 100.0% | **100.0%** |
| False Association Rate | 86.7% | **0.0%** |

（正例 18 / 负例 12 / 无答案 3）

## 逐题判定（guard）

| id | category | 期望 | guard | 说明 |
| --- | --- | --- | --- | --- |
| ad_c01 | causal | yes | `YES` | 同一 line 内共现 |
| ad_c02 | causal | yes | `YES` | 同一 line 内共现 |
| ad_c03 | causal | yes | `YES` | 同一 line 内共现 |
| ad_c06 | causal | yes | `YES` | 同一 line 内共现 |
| ad_c07 | causal | yes | `YES` | 同一 line 内共现 |
| ad_c10 | causal | yes | `YES` | 同一 line 内共现 |
| ad_c12 | causal | yes | `YES` | 同一 line 内共现 |
| ad_c04 | causal | no | `NO` | 该结果归属于「大风」，不是「强降雨」 |
| ad_c05 | causal | no | `NO` | 该结果归属于「强降雨」，不是「大风」 |
| ad_c08 | causal | no | `NO` | 该结果归属于「雷电」，不是「暴雨」 |
| ad_c09 | causal | no | `NO` | 该结果归属于「暴雨」，不是「高温」 |
| ad_c11 | causal | no | `NO` | 该结果归属于「冻融循环」，不是「车辆超载」 |
| ad_n01 | causal | insufficient | `INSUFFICIENT_RELATION` | 分别找到 anchor / target，但没有直接关系证据 |
| ad_n02 | causal | insufficient | `INSUFFICIENT_RELATION` | 分别找到 anchor / target，但没有直接关系证据 |
| ad_n03 | causal | insufficient | `INSUFFICIENT_RELATION` | 分别找到 anchor / target，但没有直接关系证据 |
| ad_i01 | impact | yes | `YES` | 找到「强降雨」的因果单元 |
| ad_i02 | impact | yes | `YES` | 找到「暴雨」的因果单元 |
| ad_i03 | impact | yes | `YES` | 找到「雷电」的因果单元 |
| ad_p01 | property | yes | `YES` | 同一 table_row 内共现 |
| ad_p02 | property | yes | `YES` | 同一 table_row 内共现 |
| ad_p03 | property | yes | `YES` | 同一 table_row 内共现 |
| ad_p06 | property | no | `NO` | 该属性属于「AQ-110」，不是「AQ-100」 |
| ad_r01 | responsibility | yes | `YES` | 责任方「水库管理局」 |
| ad_r02 | responsibility | yes | `YES` | 责任方「养护一班」 |
| ad_r03 | responsibility | yes | `YES` | 责任方「结构检测中心」 |
| ad_r05 | responsibility | no | `NO` | 责任方为「属地乡镇」，不是「水库管理局」 |
| ad_r06 | responsibility | no | `NO` | 责任方为「质量管理部」，不是「测绘队」 |
| ad_r07 | responsibility | no | `NO` | 责任方为「值班工程师」，不是「结构检测中心」 |
| ad_x01 | confusion | no | `NO` | 该属性属于「AQ-100」，不是「AQ-110」 |
| ad_x02 | confusion | no | `NO` | 该属性属于「AQ-100」，不是「AQ-110」 |
| ad_x03 | confusion | no | `NO` | 该属性属于「AQ-110」，不是「AQ-100」 |
| ad_e01 | event | yes | `YES` | 该结果由「高温」造成 |
| ad_e02 | event | yes | `YES` | 该结果由「大风」造成 |

## 原 DEV 72 回归（guard 是加法标注层，不重排）

基线取 **depth_control.A0（同口径）**（与本次同口径）。
| 指标 | 值 | delta |
| --- | --- | --- |
| recall@1 | 69.7% | +0.0pp |
| recall@3 | 92.4% | +0.0pp |
| recall@5 | 93.9% | +0.0pp |
| mrr | 80.4% | +0.0pp |
| coverage_group_recall | 79.3% | +0.0pp |
| coverage_full_rate | 57.1% | +0.0pp |
| relation_accuracy | 100.0% | +0.0pp |
| wrong_document_rate | 3.0% | +0.0pp |
| no_answer_fp_rate | 0.0% | +0.0pp |
| attribution_violation_rate | 0.0% | +0.0pp |

- 关系型查询数：24 / 72
- ⚠ **guard 在 DEV 72 上误拦了 15 道本可回答的普通题** → 说明 guard 不能无差别地套在所有关系型查询上
- 端到端耗时（检索+guard）：2.41 ms/query
- **guard 自身耗时：2.36 ms/query**（目标 < 20ms）

## Holdout A 验证（算法定稿后仅跑一次）

| id | 期望 | guard | baseline | 说明 |
| --- | --- | --- | --- | --- |
| ho_df_calib | answered | `INSUFFICIENT_RELATION` | INSUFFICIENT_RELATION | 分别找到 anchor / target，但没有直接关系证据 |
| ho_df_who_report | answered | `INSUFFICIENT_RELATION` | INSUFFICIENT_RELATION | 检索证据中未出现 anchor/target |
| ho_q_temp_range | answered | `INSUFFICIENT_RELATION` | INSUFFICIENT_RELATION | 分别找到 anchor / target，但没有直接关系证据 |
| ho_p_detectors | answered | `INSUFFICIENT_RELATION` | INSUFFICIENT_RELATION | 分别找到 anchor / target，但没有直接关系证据 |
| ho_p_tunnel_leader | answered | `INSUFFICIENT_RELATION` | INSUFFICIENT_RELATION | 分别找到 anchor / target，但没有直接关系证据 |
| ho_m_bridge_station | answered | `INSUFFICIENT_RELATION` | INSUFFICIENT_RELATION | 检索证据中未出现 anchor/target |
| ho_m_crack_model | answered | `NO` | INSUFFICIENT_RELATION | 该属性属于「LF-20」，不是「桥梁监测」 |
| ho_m_tunnel_station | answered | `INSUFFICIENT_RELATION` | INSUFFICIENT_RELATION | 分别找到 anchor / target，但没有直接关系证据 |
| ho_m_level_bridge | answered | `NO` | INSUFFICIENT_RELATION | 该属性属于「JZ-5」，不是「桥梁监测使用」 |
| ho_tr_bridge_table | answered | `NO` | INSUFFICIENT_RELATION | 该属性属于「JZ-5」，不是「桥梁监测仪器表中静力水准仪」 |
| ho_tr_tunnel_table | answered | `INSUFFICIENT_RELATION` | INSUFFICIENT_RELATION | 检索证据中未出现 anchor/target |
| ho_tr_lf_resolution | answered | `NO` | YES | 该属性属于「LF-30」，不是「LF-20」 |
| ho_tr_lf30_weight | answered | `YES` | YES | 同一 line 内共现 |
| ho_lc_bridge_items | answered | `INSUFFICIENT_RELATION` | INSUFFICIENT_RELATION | 分别找到 anchor / target，但没有直接关系证据 |
| ho_se_lf_resolution_diff | answered | `INSUFFICIENT_RELATION` | INSUFFICIENT_RELATION | 检索证据中未出现 anchor/target |
| ho_se_jz5_what | answered | `NO` | INSUFFICIENT_RELATION | 该属性属于「JZ-5」，不是「JZ-5 是什么类型」 |
| ho_at_cold_area | answered | `PASS_THROUGH` | PASS_THROUGH | 非关系型查询 |
| ho_at_sand_area | answered | `PASS_THROUGH` | PASS_THROUGH | 非关系型查询 |
| ho_at_fog_effect | answered | `YES` | YES | 找到「大雾」的因果单元 |
| ho_at_sand_fog | insufficient | `INSUFFICIENT_RELATION` | INSUFFICIENT_RELATION | 分别找到 anchor / target，但没有直接关系证据 |
| ho_neg_waterproof | insufficient | `INSUFFICIENT_RELATION` | INSUFFICIENT_RELATION | 分别找到 anchor / target，但没有直接关系证据 |
| ho_so_alert | answered | `INSUFFICIENT_RELATION` | INSUFFICIENT_RELATION | 检索证据中未出现 anchor/target |

- `ho_at_sand_fog` 是否自然修复：**True**

---

> 只测量，未修改生产代码；未使用 Holdout A 做调参反馈。
