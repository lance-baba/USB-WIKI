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
├─ run_eval.py            # Harness：baseline / compare
├─ build_gold.py          # 维护者工具：生成 gold 并做完整性校验
├─ datasets/
│  ├─ core_gold.jsonl     # 72 条 gold（14 类）
│  └─ private/            # 私人题库（gitignore；本地专用）
├─ fixtures/              # **gold 的唯一来源**：6 篇 Markdown 语料（4 主题 + 2 干扰）
└─ reports/
   ├─ baseline.json
   └─ baseline.md
```

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
