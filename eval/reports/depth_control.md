# USB-WIKI · Candidate Depth Control（split = dev，72 题）

同一套生产 `hybrid_search()`，**未改任何算法**。A1/A2 只加深候选池并**保持原始排序**，
再截 Top5；rerank 两列来自上一轮 Spike（pool=N20）。

| 指标 | A0_prod_top5 | A2_pool10 | A1_pool20 | rerank:bge-reranker-base | rerank:bge-reranker-v2-m3 |
| --- | --- | --- | --- | --- | --- |
| Recall@1 | 69.7% | 69.7% | 69.7% | 69.7% | 71.2% |
| Recall@3 | 92.4% | 92.4% | 92.4% | 95.5% | 97.0% |
| Recall@5 | 93.9% | 93.9% | 93.9% | 98.5% | 98.5% |
| MRR | 0.804 | 0.804 | 0.804 | 0.824 | 0.839 |
| Coverage group | 79.3% | 79.3% | 79.3% | 96.4% | 96.4% |
| Coverage full | 57.1% | 57.1% | 57.1% | 85.7% | 85.7% |
| Relation | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| Wrong-doc | 3.0% | 3.0% | 3.0% | 1.5% | 1.5% |
| No-answer FP | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| Attribution err | 0.0% | 0.0% | 0.0% | 20.0% | 20.0% |
| Candidate Recall@pool | 93.9% | 95.5% | 98.5% | — | — |
| CPU p50 | — | — | — | 26ms | 74ms |
| RAM(预热) | — | — | — | 713MB | 1270MB |
| 磁盘 | — | — | — | 288MB | 561MB |

## 关键证据：加深 pool 是否改变了 top-5（含顺序）

- `A0_prod_top5__vs__A2_pool10`：**完全相同**
- `A0_prod_top5__vs__A1_pool20`：**完全相同**

## 结论：pool depth 的净贡献

- Recall@1：pool 10→20 带来 **+0.0pp**
- Recall@3：pool 10→20 带来 **+0.0pp**
- Coverage full：pool 10→20 带来 **+0.0pp**
- Candidate Recall@pool：A0 93.9% / A2 95.5% / A1 98.5%

> 若上表显示 pool 加深后 **top-5 与指标完全不变**，则上一轮 reranker 报告里
> Recall@3/5 与 Coverage 的全部提升都来自**重排**，而不是候选池加深。

## reranker 的净贡献（相对 A1，同 pool=20）

- base： Recall@1 +0.0pp · Recall@3 +3.0pp · Coverage full +28.6pp
- v2-M3：Recall@1 +1.5pp · Recall@3 +4.5pp · Coverage full +28.6pp

---

> 只测量，未修改 search.py / query analysis / Gold。
