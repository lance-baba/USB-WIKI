# USB-WIKI · Reranker Offline A/B Spike V1

- 生成：2026-09-22 21:09:09
- 题量：**72**；embedder=`local_hash(512)`
- 实测产物：**ONNX int8 (quantized) — CPU-only 可分发形态；fp32 体积见 models**
- 第一阶段：生产 `hybrid_search()`（FTS+Dense+RRF）取 Top N，**算法未改**
- LLM smoke 本轮**未跑**（隔离变量）。

## 主表（pool = N20）

| 指标 | Baseline | bge-reranker-base | bge-reranker-v2-m3 |
| --- | --- | --- | --- |
| Recall@1 | 69.7% | 69.7% | 71.2% |
| Recall@3 | 92.4% | 95.5% | 97.0% |
| Recall@5 | 93.9% | 98.5% | 98.5% |
| MRR | 0.804 | 0.824 | 0.839 |
| Coverage group | 79.3% | 96.4% | 96.4% |
| Coverage full | 57.1% | 85.7% | 85.7% |
| model_spec R@1 | 40.0% | 40.0% | 60.0% |
| person R@1 | 20.0% | 40.0% | 60.0% |
| similar_entity R@1 | 100.0% | 100.0% | 100.0% |
| table_relation R@1 | 66.7% | 50.0% | 66.7% |
| direct_fact R@1 | 75.0% | 87.5% | 87.5% |
| Wrong-doc | 3.0% | 1.5% | 1.5% |
| No-answer FP | 0.0% | 0.0% | 0.0% |
| Attribution err | 0.0% | 20.0% | 20.0% |
| CPU p50 | — | 28ms | 81ms |
| CPU p95 | — | 320ms | 1022ms |
| 模型加载 | — | 2.1s | 4.9s |
| RAM 增量(加载) | — | 532MB | 866MB |
| RAM 增量(预热后) | — | 713MB | 1269MB |
| 磁盘(int8) | — | 288MB | 561MB |

## 模型信息 / 标签

| 模型 | 上游 | license | revision | int8 磁盘 | fp32 参考 | 标签 |
| --- | --- | --- | --- | --- | --- | --- |
| bge-reranker-base | BAAI/bge-reranker-base | MIT | `280bcc27a84e` | 288MB | 1061MB | **WEAK** |
| bge-reranker-v2-m3 | BAAI/bge-reranker-v2-m3 | Apache-2.0 | `6f5ff6529851` | 561MB | 2166MB | **WEAK** |

### 本地持久路径 / 校验（可直接复用，无需重新下载）

- **bge-reranker-base** → `F:\项目\USB-WIKI\eval\models\bge-reranker-base`
  - 权重 `model_quantized.onnx` · sha256 `dd98f3e67837d23210a6b7550c08cced4f61845b940ac45be3565840a10f3244`
  - 上游 `BAAI/bge-reranker-base` · revision `280bcc27a84e0b898c251e06fddb25171bd9b101` · license **MIT** · 磁盘 288MB（fp32 导出参考 1061MB）
- **bge-reranker-v2-m3** → `F:\项目\USB-WIKI\eval\models\bge-reranker-v2-m3`
  - 权重 `model_quantized.onnx` · sha256 `912fc1215c2dbff6499700534bd8d31253af01573861abbfc43afd1fab6cce5d`
  - 上游 `BAAI/bge-reranker-v2-m3` · revision `6f5ff65298512715a1e669753bc754d2bc8f367b` · license **Apache-2.0** · 磁盘 561MB（fp32 导出参考 2166MB）

> 校验：`runtime/python-3.11-embed/python.exe eval/reranker_spike.py verify`

主表 pool = **N20**（N20 已饱和：更深的 pool 指标完全相同，无需继续加深）。

### ⚠ 内存与分发影响（便携场景的硬约束）

| 模型 | int8 磁盘 | 加载后 RAM | 预热后 RAM |
| --- | --- | --- | --- |
| bge-reranker-base | 288MB | +532MB | +713MB |
| bge-reranker-v2-m3 | 561MB | +866MB | +1269MB |

> USB-WIKI 是**便携、不要求独显**的产品：准确率之外，RAM 增量与分发体积是同等硬约束。把一个 288~561MB 的模型塞进 122MB 的精简包、并常驻数百 MB 内存，需要单独决策（默认关闭 / 按需加载 / 仅高配启用），本轮不替产品做这个决定。

## Candidate pool 深度

| N | bge-reranker-base R@1/3/5 | bge-reranker-v2-m3 R@1/3/5 | bge-reranker-base p50 | bge-reranker-v2-m3 p50 |
| --- | --- | --- | --- | --- |
| 10 | 71.2/93.9/95.5% | 72.7/93.9/95.5% | 27ms | 77ms |
| 20 | 69.7/95.5/98.5% | 71.2/97.0/98.5% | 28ms | 81ms |
| 30 | 69.7/95.5/98.5% | 71.2/97.0/98.5% | 38ms | 77ms |

## 逐题 diff（主 pool）—— 谁被修好、谁被改坏

- **bge-reranker-base**：Recall@1 修好 5 题 ['jkj_df_structure', 'jkj_q_depth', 'jkj_p_leader', 'jkj_l_project', 'jkj_tr_multi_table']；**改坏 5 题** ['jkj_d_calib', 'jkj_tr_device_table', 'jkj_tr_alert_rate', 'jkj_so_device', 'jekj_so_alert']；新增命中陷阱词 ['wea_at_typhoon_frost']
- **bge-reranker-v2-m3**：Recall@1 修好 7 题 ['jkj_df_structure', 'jkj_p_group_leader', 'jkj_p_list', 'jkj_m_level_model', 'jkj_tr_freq_table', 'jkj_tr_multi_table', 'jkj_cit_level_model']；**改坏 6 题** ['jkj_q_obs_points', 'jkj_tr_device_table', 'jkj_tr_alert_rate', 'jkj_so_device', 'jekj_so_alert', 'jkj_cit_obs_points']；新增命中陷阱词 ['jkj_m_level_model', 'wea_at_typhoon_frost']

## 重点 Case（before → after）

| case | 查询 | Baseline 名次 | bge-reranker-base | bge-reranker-v2-m3 |
| --- | --- | --- | --- | --- |
| jkj_m_level_model | 水准仪是什么型号的？ | 3 | 2 | ✅1 |
| jkj_df_purpose | 监测的目的是什么？ | ❌未进前5 | 2 | 2 |
| jkj_p_list | 观测人员有哪些？ | ❌未进前5 | 2 | ✅1 |
| jkj_so_person | 人员配置情况怎么样？ | ❌未进前5 | 5 | 2 |
| qwen_lc_features | Qwen-Image-2.1 的功能有哪些？ | 2 | 3 | 2 |
| jkj_so_freq | 监测频率是怎么样的？ | ✅1 | ✅1 | ✅1 |

## Coverage 特别说明

> **不要把 Recall@1 提升误认为 Coverage 已修好。** 覆盖度单列在重点 case 里（`groups_covered`），本轮**不修** coverage。

---

> 本轮只测量，未修改任何生产代码。
