# USB-WIKI · RAG Eval Lab V1 — Baseline Report

- 生成时间：2026-09-22 20:29:49
- 题量：**72**（top_k=5，embedder=local_hash(512)  # 离线可复现；不依赖 ollama）
- 隔离库：`C:\Users\huqih\AppData\Local\Temp\usb-wiki-eval-8gevup7k`（TEMP，未触碰真实 Library）
- 耗时：24.74s

## 总指标

| 指标 | 值 |
| --- | --- |
| Evidence Recall@1 | 69.7% |
| Evidence Recall@3 | 92.4% |
| Evidence Recall@5 | 93.9% |
| MRR | 0.807 |
| Coverage Evidence Recall（证据组覆盖） | 79.3% |
| Coverage 全覆盖率 | 57.1% |
| Relation Accuracy（同行共现） | 100.0% |
| Wrong-document Rate | 1.5% |
| No-answer False-positive Rate | 0.0% |
| Attribution 误归因率 | 0.0% |
| Citation 结构合法率 | 100.0% |
| Citation 与证据区间相交率 | 93.9% |

## 各类别

| category | n | Recall@5 | 通过 |
| --- | --- | --- | --- |
| attribution | 5 | 100.0% | 5/5 |
| citation | 5 | 100.0% | 5/5 |
| cross_section | 4 | 100.0% | 4/4 |
| date_time | 5 | 100.0% | 5/5 |
| direct_fact | 8 | 87.5% | 7/8 |
| list_coverage | 5 | 100.0% | 5/5 |
| location | 4 | 75.0% | 3/4 |
| model_spec | 5 | 100.0% | 5/5 |
| negative_no_answer | 5 | — | 5/5 |
| person | 5 | 80.0% | 4/5 |
| quantity | 6 | 100.0% | 6/6 |
| section_overview | 5 | 80.0% | 4/5 |
| similar_entity | 4 | 100.0% | 4/4 |
| table_relation | 6 | 100.0% | 6/6 |

## 三个点名 Baseline

- **level_model_query**：`水准仪是什么型号的？` → Recall@1=❌ Recall@3=✅ Recall@5=✅ RR=0.333 route=like
- **qwen_features**：`Qwen-Image-2.1 的功能有哪些？` → Recall@1=❌ Recall@3=✅ Recall@5=✅ RR=0.5 route=fts
- **typhoon_attribution**：`冷空气影响了哪些地区？` → Recall@1=✅ Recall@3=✅ Recall@5=✅ RR=1.0 route=like

## 最差失败 case（共 6）

| id | category | 归因层 | 查询 |
| --- | --- | --- | --- |
| jkj_l_point_layout | location | `RECALL` | 观测点沿什么布设、间距多少？ |
| jkj_df_purpose | direct_fact | `RERANK` | 监测的目的是什么？ |
| jkj_p_list | person | `RERANK` | 观测人员有哪些？ |
| jkj_so_person | section_overview | `RERANK` | 人员配置情况怎么样？ |
| qwen_lc_features | list_coverage | `COVERAGE` | Qwen-Image-2.1 的功能有哪些？ |
| jkj_so_freq | section_overview | `COVERAGE` | 监测频率是怎么样的？ |

## LLM answer smoke

```json
{
  "cases": [
    {
      "id": "jkj_df_purpose",
      "category": "direct_fact",
      "provider": "ollama",
      "must_contain_hit": 0,
      "must_contain_total": 2,
      "forbidden_leak": [],
      "has_citation": false,
      "refs": 5,
      "answer_head": "现有资料没有明确说明监测的具体目的，但提供了不同项目的监测频率和人员配置等信息。"
    },
    {
      "id": "jkj_df_observer_skill",
      "category": "direct_fact",
      "provider": "ollama",
      "must_contain_hit": 2,
      "must_contain_total": 2,
      "forbidden_leak": [],
      "has_citation": true,
      "refs": 1,
      "answer_head": "现场观测员应熟悉水准仪与全站仪的操作 [1]。"
    },
    {
      "id": "jkj_df_baseline_use",
      "category": "direct_fact",
      "provider": "ollama",
      "must_contain_hit": 2,
      "must_contain_total": 2,
      "forbidden_leak": [],
      "has_citation": true,
      "refs": 13,
      "answer_head": "基准点中的一个点可用于放置全站仪，另一个作为后视点 [1]。"
    },
    {
      "id": "jkj_df_mark",
      "category": "direct_fact",
      "provider": "ollama",
      "must_contain_hit": 3,
      "must_contain_total": 3,
      "forbidden_leak": [],
      "has_citation": true,
      "refs": 1,
      "answer_head": "水平位移监测点是用测量钉打入设置的，并且用红漆做了明显的标志 [1]。"
    },
    {
      "id": "jkj_df_structure",
      "category": "direct_fact",
      "provider": "ollama",
      "must_contain_hit": 2,
      "must_contain_total": 2,
      "forbidden_leak": [],
      "has_citation": true,
      "refs": 2,
      "answer_head": "*   BLD0912 高新区商业综合体贴坑监测方案中的工程：围护结构采用地下连续墙加三道混凝土支撑 [1]。 *   JKJ2307 镇海区基坑围护施工监测方案中的工程：围护结构采用钻孔灌注桩加内支撑 [2]。"
    },
    {
      "id": "jkj_df_data_person",
      "category": "direct_fact",
      "provider": "ollama",
      "must_contain_hit": 2,
      "must_contain_total": 2,
      "forbidden_leak": [],
      "has_citation": true,
      "refs": 1,
      "answer_head": "数据处理员是陈静 [1]。"
    },
    {
      "id": "qwen_df_position",
      "category": "direct_fact",
      "provider": "ollama",
      "must_contain_hit": 2,
      "must_contain_total":
```

---

> 本报告仅**测量**，未对 search.py / chunker / prompt / embedding / top_k 做任何改动。Baseline 越真实越有价值。
