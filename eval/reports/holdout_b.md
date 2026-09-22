# USB-WIKI · Holdout B —— Release Candidate Gate 1

- 生成：2026-09-23 07:50:34　冻结：2026-09-23 07:46:54
- 规模：**57 题 / 8 篇独立文档**，hard case **32%**
- gold sha256：`a244cd8693c9fe9d…`
- 运行时：随包嵌入式 Python + TEMP 隔离库；**未触碰真实 Library**。
- 本轮**只验收未调算法**，生产代码零改动。

## Retrieval

| 指标 | 值 |
| --- | --- |
| Recall@1 | **97.5%** |
| Recall@3 | **100.0%** |
| Recall@5 | **100.0%** |
| MRR | **0.988** |
| Coverage group | **100.0%** |
| Coverage full | **100.0%** |
| Relation | 71.4% |

## Safety

| 指标 | 值 |
| --- | --- |
| Wrong-document Rate | **0.0%** |
| No-answer FP Rate | **0.0%** |
| Attribution err（旧口径） | — |
| False Association | **0.0%** |
| Unnecessary Guard（**真的被拦**） | **2.3%** （1/44） |
| 普通题进入 Guard（含责任形态） | 2.3% （1/44） |

## Relation（Guard）

| 指标 | 值 |
| --- | --- |
| Positive Relation Accuracy | **100.0%** |
| Negative Relation Accuracy | **100.0%** |
| Abstention Accuracy | **100.0%** |

## Citation

| 指标 | 值 |
| --- | --- |
| 结构合法率 | **100.0%**（125 条引用） |
| source-range overlap（每题至少一条） | **98.1%**（53 题） |
| └ 逐条引用口径（诊断，天然偏低） | 55.6%（117 条引用） |

## 各类型 Recall@1

| 类别 | Recall@1 | n |
| --- | --- | --- |
| citation | 100% | 3 |
| direct_fact | 100% | 5 |
| list_coverage | 100% | 4 |
| model_spec | 100% | 5 |
| person | 100% | 4 |
| quantity | 100% | 5 |
| section_overview | 100% | 4 |
| similar_entity | 100% | 5 |
| table_relation | 80% | 5 |

## Blocker 判定

- **无 Blocker**
- 触发阈值但判定**非 Blocker**：D —— 普通题被 Guard 误拦 1/44 = 2.3%；non-blocking（1~2 例、根因明确，不构成『大面积误拦』）

## 最终结论

# **RC_READY_WITH_LIMITATIONS**

> 无安全/数据 Blocker（无错误归因、无编造、无引用错位）；存在明确长尾限制，已记录进 KNOWN_LIMITATIONS.md，记录后可进入 Pilot

## LLM smoke

| id | 类别 | route | guard | 引用 | 回答摘要 |
| --- | --- | --- | --- | --- | --- |
| hb_df_struct | direct_fact | PASS | - | ✅ | **本地搜索结果**（未调用生成式 AI，以下均为知识库原文摘录）：  ### [^1] 地铁 3 号线车站基坑监测方案 |
| hb_m_level | model_spec | PASS | - | ✅ | **本地搜索结果**（未调用生成式 AI，以下均为知识库原文摘录）：  ### [^1] 地铁 3 号线车站基坑监测方案 |
| hb_q_inclino_tube | quantity | PASS | - | ✅ | **本地搜索结果**（未调用生成式 AI，以下均为知识库原文摘录）：  ### [^1] 地铁 3 号线车站基坑监测方案 |
| hb_lc_clinic_items | list_coverage | PASS | - | ✅ | **本地搜索结果**（未调用生成式 AI，以下均为知识库原文摘录）：  ### [^1] 体检中心操作规程，相似度 0. |
| hb_tr_a1001 | table_relation | PASS | - | ✅ | **本地搜索结果**（未调用生成式 AI，以下均为知识库原文摘录）：  ### [^1] 2026 年一季度仓储盘点表， |
| hb_neg_cost | negative_no_answer | PASS | - | ✅ | **本地搜索结果**（未调用生成式 AI，以下均为知识库原文摘录）：  ### [^1] 地铁 3 号线车站基坑监测方案 |
| hb_c_pos1 | causal | ATTRIBUTION_GUARD | YES | ✅ | **本地搜索结果**（未调用生成式 AI，以下均为知识库原文摘录）：  ### [^1] 6 月汛情与调度记录，相似度  |
| hb_c_neg1 | causal | ATTRIBUTION_GUARD | NO | ✅ | 当前资料中的相关证据把「下游滩地受淹」归因于「泄洪」，不能据此认定它与「强降雨」存在该关系。下方已列出相关资料片段，你可 |
| hb_c_ins1 | causal | ATTRIBUTION_GUARD | INSUFFICIENT_RELATION | ✅ | 当前资料中没有找到「强降雨」与「人员伤亡」之间的直接关系证据，因此不能据此确认二者存在该关系。下方已列出相关资料片段，你 |
| hb_e_pos1 | event | ATTRIBUTION_GUARD | YES | ✅ | **本地搜索结果**（未调用生成式 AI，以下均为知识库原文摘录）：  ### [^1] 6 月汛情与调度记录，相似度  |
| hb_r_neg1 | responsibility | ATTRIBUTION_GUARD | NO | ✅ | 当前资料中的相关证据把「排水管理处」归因于「防汛指挥部」，不能据此认定它与「水库调度」存在该关系。下方已列出相关资料片段 |
| hb_se_tube_range | similar_entity | PASS | - | ✅ | **本地搜索结果**（未调用生成式 AI，以下均为知识库原文摘录）：  ### [^1] 地铁 3 号线车站基坑监测方案 |

---

> 本轮只验收不调算法；失败只记录，不现场修。
