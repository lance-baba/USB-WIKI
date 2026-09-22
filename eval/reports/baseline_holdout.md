# USB-WIKI · RAG Eval Lab V1 — Baseline Report

- 生成时间：2026-09-22 21:23:08
- 题量：**35**（top_k=5，embedder=local_hash(512)  # 离线可复现；不依赖 ollama）
- 隔离库：`C:\Users\huqih\AppData\Local\Temp\usb-wiki-eval-uryfis93`（TEMP，未触碰真实 Library）
- 耗时：0.26s

## 总指标

| 指标 | 值 |
| --- | --- |
| Evidence Recall@1 | 90.3% |
| Evidence Recall@3 | 100.0% |
| Evidence Recall@5 | 100.0% |
| MRR | 0.941 |
| Coverage Evidence Recall（证据组覆盖） | 100.0% |
| Coverage 全覆盖率 | 100.0% |
| Relation Accuracy（同行共现） | 100.0% |
| Wrong-document Rate | 0.0% |
| No-answer False-positive Rate | 0.0% |
| Attribution 误归因率 | 25.0% |
| Citation 结构合法率 | 100.0% |
| Citation 与证据区间相交率 | 100.0% |

## 各类别

| category | n | Recall@5 | 通过 |
| --- | --- | --- | --- |
| attribution | 4 | 100.0% | 3/4 |
| direct_fact | 5 | 100.0% | 5/5 |
| list_coverage | 3 | 100.0% | 3/3 |
| model_spec | 4 | 100.0% | 4/4 |
| negative_no_answer | 3 | — | 3/3 |
| person | 3 | 100.0% | 3/3 |
| quantity | 4 | 100.0% | 4/4 |
| section_overview | 2 | 100.0% | 2/2 |
| similar_entity | 3 | 100.0% | 3/3 |
| table_relation | 4 | 100.0% | 4/4 |

## 三个点名 Baseline


## 最差失败 case（共 1）

| id | category | 归因层 | 查询 |
| --- | --- | --- | --- |
| ho_at_sand_fog | attribution | `RECALL` | 沙尘天气是否导致高速公路临时封闭？ |

---

> 本报告仅**测量**，未对 search.py / chunker / prompt / embedding / top_k 做任何改动。Baseline 越真实越有价值。
