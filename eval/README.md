# USB-WIKI · RAG Eval Lab V1

可重复运行的检索评测实验室。**只测量，不调参。** 本目录**不进入用户安装包**
（`scripts/build_release.py` 只 stage `app/` 与 `runtime/`，永远不会复制 `eval/`）。

## 它回答四个问题

1. 当前 Retrieval 到底有多准？
2. 哪些问题类型容易失败？
3. 错误发生在 **Recall / Rerank / Coverage / Grounding / Citation** 哪一层？
4. 后续加 Reranker 是否真的值得？（看 Recall@1 与 Recall@3 的差距 —— 差得越大越值得）

## 目录

```text
eval/
├─ README.md
├─ run_eval.py            # Harness：baseline（默认 DEV）/ baseline --holdout / compare
├─ depth_control.py       # V1.1：A0/A1/A2 候选池深度对照（隔离"加深 pool"与"rerank"）
├─ reranker_spike.py      # V1：reranker 离线 A/B（fetch / verify / run）
├─ build_gold.py          # 维护者工具：生成 DEV+HOLDOUT gold 并做完整性校验
├─ datasets/
│  ├─ core_gold.jsonl     # **DEV SET**：72 条（14 类，每条带 "split": "dev"）
│  ├─ holdout/
│  │  └─ holdout_gold.jsonl  # **HOLDOUT**：35 条（10 类，"split": "holdout"）
│  └─ private/            # 私人题库（gitignore；本地专用）
├─ fixtures/              # **DEV gold 的唯一来源**：6 篇语料（4 主题 + 2 干扰）
├─ fixtures_holdout/      # HOLDOUT 语料：4 篇（桥梁 / 隧道 / 仪器手册 / 天气日志）
├─ models/                # reranker 权重（gitignored；持久保存，可长期复用）
└─ reports/
   ├─ baseline.{json,md}            # DEV
   ├─ baseline_holdout.{json,md}    # HOLDOUT（显式 --holdout 才生成）
   ├─ reranker_benchmark.{json,md}
   └─ depth_control.{json,md}
```

## DEV / HOLDOUT 分离（V1.1）

| split | 文件 | 语料 | 用途 |
| --- | --- | --- | --- |
| **DEV** | `datasets/core_gold.jsonl`（72 题） | `fixtures/` | 日常开发、bug 分析、方案选择 |
| **HOLDOUT** | `datasets/holdout/holdout_gold.jsonl`（35 题） | `fixtures_holdout/` | **仅阶段性 release decision** |

⚠ **现有 72 题已正式标记为 DEV SET**（每条带 `"split": "dev"`）。它们参与过 bug 分析、reranker
选择与架构决策，**今后不得称其为 unbiased test**，也不得据此宣称泛化能力。

### Holdout 防泄漏规则（必须遵守）

1. 开发阶段**只输出 DEV 指标**；Holdout **默认锁定**。
2. 只有显式开关才跑：`... run_eval.py baseline --holdout`
3. `compare` **不会**自动跑 Holdout（它只读已产出的报告文件）。
4. **不要根据 Holdout 的逐题失败去调算法** —— 那就是过拟合。Holdout 只用于阶段性 release decision。
5. Holdout 语料与 DEV 语料**完全分离**：不同领域、不同表达、不同结构，**不是 DEV 问题的改写**。
6. 每次动完算法，**先看 DEV**；只有到"要决定是否发布"时才跑一次 Holdout。

### Holdout 首次运行（35 题，locked → 显式开启）

Recall@1 **90.3%** · Recall@3/@5 **100%** · MRR 0.941 · Coverage 100% · Relation 100% ·
Wrong-doc 0% · No-answer FP 0% · **Attribution 误归因 25%（1/4）**

> 值得注意：**Attribution 在完全独立的语料上依然出问题**
> （`ho_at_sand_fog`：把"沙尘"错配到"高速公路临时封闭"，那是大雾的影响）。
> 这是 holdout 独立确认的真实弱点，不是 DEV 的偶然。

## Candidate Depth Control（V1.1）

同一套生产 `hybrid_search()`，**算法一行未改**。A2/A1 只加深候选池、**保持原始排序**，再截 Top5。

| 指标 | A0 生产 Top5 | A2 pool10 | A1 pool20 | rerank base | rerank v2-M3 |
| --- | --- | --- | --- | --- | --- |
| Recall@1 | 69.7% | 69.7% | 69.7% | 69.7% | **71.2%** |
| Recall@3 | 92.4% | 92.4% | 92.4% | 95.5% | **97.0%** |
| Recall@5 | 93.9% | 93.9% | 93.9% | 98.5% | 98.5% |
| MRR | 0.804 | 0.804 | 0.804 | 0.824 | 0.839 |
| Coverage group | 79.3% | 79.3% | 79.3% | 96.4% | 96.4% |
| Coverage full | 57.1% | 57.1% | 57.1% | 85.7% | 85.7% |
| Relation | 100% | 100% | 100% | 100% | 100% |
| Wrong-doc | 3.0% | 3.0% | 3.0% | 1.5% | 1.5% |
| Attribution err | 0.0% | 0.0% | 0.0% | 20.0% | 20.0% |
| **Candidate Recall@pool** | **93.9%** | **95.5%** | **98.5%** | — | — |

**结论要分两层说，不能只说一句「pool 没用」：**

1. **pool 加深不改变头部**：A0 / A2 / A1 的 top-5 **逐题完全相同（含顺序）**，所有头部指标一字未动。
   → 上一轮 reranker 报告里的 Recall@3/5 与 Coverage 提升，**100% 来自重排**，而不是候选池加深。
2. **但 pool 加深确实抬高了「天花板」**：Candidate Recall@pool 93.9% → 95.5% → **98.5%**。
   多出来的那部分证据**全部排在 5 名之后**，没有重排就永远浮不上来。
   → 这既解释了 reranker 为什么有效（它能把 5 名后的证据提上来），也说明
   **pool 的红利只有重排才能兑现**；单纯加深 pool 不改变任何用户可见指标。

**本轮未构建 Evidence Selector**（MMR / section diversity / 新打分 / prompt 改动），
按要求只把实验口径搞干净。

## 运行

```bash
# 推荐：用随包嵌入式运行时（自带 sqlite-vec，向量路可用；与用户环境一致）
runtime/python-3.11-embed/python.exe eval/run_eval.py baseline

# 可选：额外抽 12 道代表题跑**真实** LLM answer smoke
runtime/python-3.11-embed/python.exe eval/run_eval.py baseline --llm-smoke 12

# 对比（V1 只预留接口，不下载/不集成 reranker）
python3 eval/run_eval.py compare --candidate reranker
```

**安全**：`run_eval.py` 第一步就 `tests/test_env.activate_test_library()`（复用测试
安全保险丝），在 `%TEMP%\usb-wiki-eval-*` 建独立库后把 fixtures 拷进去索引。
**绝不触碰真实 Library，也不读真实 API Key**（检索路径完全不碰 AI 配置）。

## Gold 数据契约

**Gold 只来自 `fixtures/` 原文**，`build_gold.py` 机械产出并强制校验：

* 每条 `must_contain` 必须**逐字出现**在对应 fixture 中；
* `negative_no_answer` 的 `must_not_contain` 必须**真的不存在**于语料（否则该题不成立）；
* `relation` 的实体与值必须**在同一行**共现（表格行 / 同一句）。

任何一条不满足 → 直接报错，不产出数据集。

```jsonc
{
  "id": "jkj_m_level_model",
  "category": "model_spec",
  "query": "水准仪是什么型号的？",
  "expected": {
    "doc": "jkj_monitoring.md",
    "must_contain": ["电子水准仪", "天宝DINI03"],
    "must_not_contain": ["±1mm", "N2", "SW-30"],   // 归因/干扰：不许被当成本题证据
    "answer_state": "answered",
    "relation": ["水准仪", "天宝DINI03"]            // 必须在同一行成立
  },
  "evidence_groups": []                            // coverage 题：分组证据
}
```

### 类别（14 类 / 72 题）

`direct_fact` `quantity` `person` `model_spec` `date_time` `location` `table_relation`
`list_coverage` `section_overview` `similar_entity` `cross_section`
`negative_no_answer` `attribution` `citation`

### 已进入永久 Regression 的真实失败

| 真实问题 | Gold 要求 |
| --- | --- |
| 观测人员有哪些？ | 5 人全部覆盖（分组证据） |
| 观测点共有几个？ | `8 个` + `CJ1-CJ8`，关系同行 |
| 人员素质有什么要求？ | `专业培训` / `资格` / `平差` |
| 监测频率是怎么样的？ | 各阶段频率 + 「加强监测」条件（整节覆盖） |
| Qwen-Image-2.1 的功能有哪些？ | 4 个功能章节全覆盖 |
| 台风影响哪些地区？ | 归因不得把冷空气/华西秋雨算给台风 |
| 水准仪是什么型号？ | `水准仪/电子水准仪` ↔ `天宝DINI03`；**不得**把 `±1mm` / `N2` / 检定有效期当型号 |

## 指标定义

| 指标 | 含义 |
| --- | --- |
| Evidence Recall@k | top-k 引用里是否出现「gold 证据父块」（同文档 + 命中期望词） |
| MRR | 首个命中证据的排名倒数均值 |
| Coverage Evidence Recall | `evidence_groups` 的证据组覆盖率（组内词全中即该组覆盖） |
| Coverage 全覆盖率 | 该题**所有**证据组都被覆盖的比例 |
| Relation Accuracy | 实体与值**同一行**共现（不是"同文共现"就算过） |
| Wrong-document Rate | top-5 里完全没有期望文档 |
| No-answer FP Rate | 负例题 top-1 命中了「不该出现」的干扰词 |
| Attribution 误归因率 | 归因题 top-1 命中了他事件的词 |
| Citation 结构合法率 | 全库引用的 doc_id/parent_id 存在、range 合法、parent 属于该 doc |
| Citation 相交率 | 引用区间与 gold 证据区间相交 |

## 失败归因

`QUERY_ANALYSIS` / `RECALL` / `RERANK` / `COVERAGE` / `GROUNDING` / `CITATION` / `UNKNOWN`。
`RECALL` 与 `RERANK` 的区分方式：用 `top_k=60` 宽召回探一次 —— gold 在宽召回里但不在 top-5
判为 **RERANK**（排序问题），宽召回里就没有判为 **RECALL**（召回问题）。

## 基线诚实性声明

* **只测不调**：本 Lab 未修改 `search.py` / `chunker` / prompt / embedding / top_k。
  分数是多少就报多少。
* **语料规模限制**：fixtures 只有 6 篇（4 主题 + 2 干扰）。**绝对分数会被小语料抬高**，
  真正有意义的是**同一套 gold 下 baseline 与 candidate 的相对差异**，以及**各类别的失败结构**。
* 词表匹配（`must_contain`）是**代理指标**，不等于人判断的相关性。

## Reranker Spike V1 结论（2026-09-22，`reranker_spike.py`）

两个候选（**ONNX int8 导出**，纯 CPU，Windows）：

| | bge-reranker-base | bge-reranker-v2-m3 |
| --- | --- | --- |
| 上游 / license | BAAI/bge-reranker-base · MIT | BAAI/bge-reranker-v2-m3 · Apache-2.0 |
| 本地路径 | `eval/models/bge-reranker-base` | `eval/models/bge-reranker-v2-m3` |
| 磁盘(int8) / fp32 参考 | 287.5MB / 1061MB | 560.6MB / 2166MB |
| 加载 / 首问 / p50 / p95 | 2.1s / 434ms / **28ms** / 320ms | 4.9s / 1634ms / **81ms** / 1022ms |
| RAM 增量（加载 → 预热） | +532MB → **+713MB** | +866MB → **+1270MB** |
| Recall@1 Δ（主 pool N20） | **+0.0pp** | **+1.5pp** |
| 标签 | `WEAK` | `WEAK`（N10 时 +3.0pp → `MARGINAL`） |

**结论：目前不值得进产品。** 依据：
1. **净收益约等于零**：base 修好 5 题 / 改坏 5 题（净 0）；v2-M3 修好 7 / 改坏 6（净 +1）。
   R@1 的 +1.5pp 是"换来换去"的结果，不是稳定增益。
2. **引入真实回退**：`attribution_violation_rate` **0% → 20%**（`wea_at_typhoon_frost`
   被重排成命中陷阱词）；base 的 `table_relation` R@1 66.7% → **50%**。
3. **代价很大**：磁盘 +288MB/+561MB（当前精简包仅 122MB）、常驻 RAM +0.7~1.3GB，
   而 USB-WIKI 定位是**便携、不要求独显**。

**它确实修好了什么**（局部有效，aggregate 被抵消）：基线 3 个 RERANK 失败题
（监测目的 / 观测人员有哪些 / 人员配置）全部被拉回 top-5；`水准仪是什么型号？`
被 v2-M3 从第 3 名**提到第 1 名**（DINI03 证据）。这印证了"正确证据已在候选里、只是没排第一"
的判断 —— 但 cross-encoder 同时把它认为"更像"的其它段落也提了上来。

⚠ **指标口径提醒**：`trap_hit` 的判据是"top-1 证据父块里出现了 must_not_contain 词"。
对**表格型证据**会过严 —— 例如 `jkj_m_level_model` 的正确证据父块是整张设备表，同一块里
本来就含有干扰行 `水位仪 SW-30`，于是被判为"命中陷阱"。这类需按表格行级别判定，
本轮未细分，故把它与真正的归因回退（`wea_at_typhoon_frost`）区分看待。

**若将来要重试 reranker**，优先验证：① 只在 `model_spec` / `person` / `table_relation`
这类"实体↔值"问题上启用（这两类确实 +20~40pp）；② 按需加载 + 高配才开，避免常驻内存；
③ 先解决 rerank 后 top-1 反而变差的 5~6 题再谈上线。

## 下一实验（仅登记设计，本轮不实现）

```text
Baseline   : Hybrid(FTS5 + vec) + RRF              → Top 5
Candidate  : Hybrid(FTS5 + vec) + RRF → Top 20~30 → local reranker → Top 3~5
```

候选模型（择一，需与中文语料实测对比）：
`BAAI/bge-reranker-v2-m3`、`BAAI/bge-reranker-base`，或其它 multilingual cross-encoder。

**未来必须一起比较的维度**（准确率之外，便携设备上尤其重要）：

| 维度 | 关注点 |
| --- | --- |
| 准确率 | Recall@1 / Recall@3 / MRR / Coverage 全覆盖率 的 delta |
| CPU latency | 单次 rerank 的 p50 / p95（无 GPU 时） |
| RAM | 常驻内存增量 |
| 模型大小 | 是否把运行时分发体积再推高一档（当前精简包 ~122MB） |
| 首次加载 | 冷启动额外耗时（与「冷启动 → 页面渲染」体验直接相关） |
| 低端电脑 | 无 AVX2 / 4GB 内存机器是否可用 |

> 判断依据：本轮 baseline 中 **Recall@3 (92.4%) 明显高于 Recall@1 (69.7%)**，
> RR 最低的正是 `水准仪是什么型号的？`（0.333）这类 **model_spec / similar_entity** 题 ——
> 即「正确的证据已经在候选里，只是没排到第一」。这正是 reranker 的**理论收益区间**。
> 但必须先用上面的维度实测，确认收益**大于**分发体积与延迟代价。
