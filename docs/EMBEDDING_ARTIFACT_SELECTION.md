# V1 嵌入 artifact 选型报告（A4.2a）

> **本阶段只选型，不做集成。** 未改 `build_release.py` / installer / `requirements-release.lock` /
> BUILD_INFO schema / Data Contract / embedding signature / UI，未下载任何 Chat LLM。
>
> 调研范围刻意收窄到 **BAAI/bge-small-zh-v1.5 的发行 artifact**：问题不是「哪个 embedding 模型好」，
> 而是「这个已经足够小、中文能力成熟的模型，应该采用哪个 ONNX 发行物」。

---

## 1. 结论

| | 选择 | 说明 |
| :--- | :--- | :--- |
| **V1 bundled artifact** | **`Xenova/bge-small-zh-v1.5` → `onnx/model_int8.onnx`** | 22.80 MB，量化 INT8 |
| **备用候选** | **`Qdrant/bge-small-zh-v1.5` → `model_optimized.onnx`** | 90.39 MB，FP32，**许可声明最干净**（显式 MIT） |

一句话理由：**在 1200 组对比里 INT8 的标注 Top-1 与 FP32 完全相同（87.5%），现有 RAG 回归零退化，
但体积少 67.6 MB、batch=1 快 2.5 倍、内存少 2.2 倍** —— 对 U 盘产品与低配 CPU 这三项是决定性的。
两个 FP32 转换（Xenova / Qdrant）互为等价，差别只在许可声明与 0.9 MB 体积。

`local_hash` 维持**显式 emergency/debug fallback**，未进入自动降级链（本阶段未动该逻辑）。

---

## 2. 候选调研表

四个仓库都查了 metadata；**只有一个被排除，且在下载之前排除**。

| 仓库 | revision | base model | license | ONNX 变体数 | 结论 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `Xenova/bge-small-zh-v1.5` | `75c43b069aac4d136ba6bc1122f995fedcfd2781` | BAAI/bge-small-zh-v1.5 | 转换仓库**未声明**（base 为 MIT） | 8 | ✅ 入选（FP32 + INT8） |
| `onnx-community/bge-small-zh-v1.5-ONNX` | `9507db33464b5da99a532ac26b2a251767cbc62b` | 同上 | 未声明 | 5 | ❌ **排除**（见下） |
| `Qdrant/bge-small-zh-v1.5` | `46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59` | 同上 | **mit（显式）** | 1 | ✅ 入选（FP32 optimized） |
| `BAAI/bge-small-zh-v1.5`（base 本体） | `7999e1d3359715c523056ef9478215996d62a620` | — | **mit** | 无 ONNX | 溯源用（不下权重） |

**`onnx-community` 的排除理由（架构层面，非质量问题）**：它把权重拆成 **external data** 格式 ——
`onnx/model.onnx` 只有 0.04 MB，真正的 90.38 MB 在 `onnx/model.onnx_data`。
这意味着「一个 artifact」实际是**两个必须同行的文件**，且加载时要能解析外部数据路径。
对一个要打进 U 盘发布包的固定字节集合来说，这是无谓的集成复杂度与失败面，
而它在 tokenizer（同一套字节）与权重来源上都与 Xenova 完全相同 —— 没有任何补偿性优势。

其余可记录事实：Xenova 48,598 下载 / 4 likes；Qdrant 52,535 下载 / 0 likes；
onnx-community 1,497 下载 / 1 like。**下载量只作参考，不作为安全评分**（§5）。

---

## 3. 下载与校验（shortlist）

只下了 **3 个** ONNX 文件（不是把 8 个变体全拉下来）：1 个 reference FP32 + 1 个最有希望的量化
+ 1 个第三方 FP32 用来判断转换差异。

| id | 文件 | precision | size | SHA256（与 HF `lfs.oid` 逐字节一致） |
| :--- | :--- | :--- | ---: | :--- |
| `xenova-fp32` | `onnx/model.onnx` | fp32 | 94,851,877 B (90.46 MB) | `69a0b846f4f116b5e6aabf9546ea6754d02264f3211a13a1bd69b31b8040749a` |
| `xenova-int8` | `onnx/model_int8.onnx` | int8 | 23,903,394 B (22.80 MB) | `b9837c19ce154ff0726d398ee77abbc03a7faf0476c6f93016c84e531be7ebb5` |
| `qdrant-fp32-opt` | `model_optimized.onnx` | fp32 | 94,781,076 B (90.39 MB) | `1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38` |

三个文件的本地 SHA256 与上游声明的 `lfs.oid` **全部一致**（`prepare.py` 逐文件校验）。
模型放在 gitignored 的 `runtime/models/_eval/`，**不入库**。

---

## 4. 统一运行协议（apples-to-apples）

```
official tokenizer semantics（tokenizer.json，BERT WordPiece）
    → enable_truncation(512) / enable_padding("[PAD]")
ONNX inference（CPUExecutionProvider）
    → last_hidden_state
CLS（token 位置 0）
    → L2 normalization
    → 512 维
```

依据：base model 官方 `1_Pooling/config.json` 是 `pooling_mode_cls_token: true`、
`pooling_mode_mean_tokens: false`。三个候选的 ONNX 输出都是 `last_hidden_state`
（`(batch, seq, 512)`），**统一取 CLS**；没有任何候选被允许用 mean pooling 来「凑」更好的数字。

---

## 5. tokenizer 取证

**三个候选使用完全相同的 tokenizer 字节** —— 逐文件 SHA256 对比（Xenova vs Qdrant）：

| 文件 | size | SHA256 | 一致 |
| :--- | ---: | :--- | :--- |
| `tokenizer.json` | 439,125 | `48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26` | ✅ |
| `vocab.txt` | 109,540 | `45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c` | ✅ |
| `tokenizer_config.json` | 367 | `e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a` | ✅ |
| `special_tokens_map.json` | 125 | `b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3` | ✅ |

词表 21,130 项。分词行为压力用例（三候选输出完全相同）：

| 用例 | token 数 | UNK |
| :--- | ---: | ---: |
| 纯中文 | 26 | 0 |
| 中英混合 | 20 | **4** |
| 数字 | 26 | 0 |
| URL | 46 | 1 |
| 标点 | 24 | 4 |
| 长文本（"知识库检索"×120） | **512（按 max_len 截断）** | 0 |
| 空白 | 2 | 0 |
| 特殊字符 | 31 | 1 |

### ⚠ 重要发现：该 tokenizer `lowercase=False` → **大写英文一律 `[UNK]`**

```
'USB'         -> ['[CLS]', '[UNK]', '[SEP]']        'usb'  -> ['[CLS]', 'usb', '[SEP]']
'API'         -> ['[CLS]', '[UNK]', '[SEP]']        'api'  -> ['[CLS]', 'api', '[SEP]']
'FTS5'        -> ['[CLS]', '[UNK]', '[SEP]']        'fts5' -> ['[CLS]', 'ft', '##s', '##5', '[SEP]']
'Transformer' -> ['[CLS]', '[UNK]', '[SEP]']        'sqlite' -> ['[CLS]', 'sql', '##ite', '[SEP]']
normalizer: BertNormalizer(clean_text=True, handle_chinese_chars=True, lowercase=False)
```

含义与处置：

- 这是 **base model 自带的官方语义**，三候选完全一致 → 不是选型差异因子，**不做任何改动**
  （给某个候选单独加 lowercasing 会破坏 apples-to-apples，也偏离官方用法）。
- 但它直接影响**用户预期管理**：USB-WIKI 的输入大量是中英混合（`USB`/`API`/`SQL`/`FTS5`），
  这些大写缩写拿不到向量语义 → **FTS 词法路必须继续作为一等公民**，embedding 只做补充召回，
  不能单独承担召回责任。
- 值得在 A4.2b 之后单独做一次小实验：比较 `do_lower_case=True` 时中英混合文档的召回差异
  （属于「是否偏离官方语义」的取舍，需单独拍板，不要在集成里顺手改）。

---

## 6. 基本正确性

三个候选全部满足（`bench.py --all`，每候选独立进程）：

| 检查 | 结果 |
| :--- | :--- |
| 输出维度 | 512（静态形状即 `(batch, sequence_length, 512)`） |
| 有限值 | ✅ 无 NaN / Inf |
| L2 范数 | 1.000000 |
| 确定性 | ✅ 同一输入两次结果逐位相同 |
| ONNX 输入 | `input_ids` / `attention_mask` / `token_type_ids`（三者一致） |
| ONNX 输出 | `last_hidden_state`（三者一致） |
| Execution Provider | `CPUExecutionProvider`（三者一致） |

**没有发现任何候选的行为异常。**

---

## 7. Windows CPU 性能（本机实测）

环境：Windows / `CPUExecutionProvider` / `intra_op_num_threads=2` / batch=1 取 5 次中位数（预热 2 次）。

| 候选 | cold load | batch=1 | batch=8 | ms/条(b8) | session 后内存 | 进程峰值 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| `xenova-fp32` | 126 ms | 5.3 ms | 23.9 ms | 3.0 | +102.3 MB | 198 MB |
| **`xenova-int8`** | **70 ms** | **2.1 ms** | **10.9 ms** | **1.4** | **+37.6 MB** | **108 MB** |
| `qdrant-fp32-opt` | 83 ms | 5.3 ms | 23.3 ms | 2.9 | +101.1 MB | 197 MB |

内存用 ctypes 读 `PROCESS_MEMORY_COUNTERS`；每候选独占进程，进程峰值才有可比性。

---

## 8. 检索质量

### 8.1 现有 RAG 回归用例（23 篇 / 43 例，其中负样本 4 条）

| 候选 | Top1 | Top3 | Top5 | MRR | 负样本误命中 |
| :--- | ---: | ---: | ---: | ---: | ---: |
| **baseline（无嵌入，纯 FTS）** | 94.9% | 100.0% | 100.0% | 0.974 | 0 |
| `xenova-fp32` | 97.4% | 100.0% | 100.0% | 0.983 | 0 |
| `xenova-int8` | 97.4% | 100.0% | 100.0% | 0.983 | 0 |
| `qdrant-fp32-opt` | 97.4% | 100.0% | 100.0% | 0.983 | 0 |

**三个候选完全相同，且相对纯词法基线 +2.5pp Top1 —— 没有任何退化。**
（这套用例以词法为主，区分度有限，所以必须配 8.2 与第 9 节。）

### 8.2 同一套用例、放行纯语义命中（`allow_semantic_only=1`）

指标不变，但**负样本误命中从 0 变成 3（三个候选一致）** —— 这是「纯语义命中必须被拦」这条
现成护栏的直接实测证据（见第 10 节）。

### 8.3 人工中文 sanity set（6 篇产品形态文档 / 12 条查询）

**生产默认（`allow_semantic_only=0`）**：

| 候选 | Top1 | Top3 | Top5 | MRR |
| :--- | ---: | ---: | ---: | ---: |
| baseline（无嵌入） | 25.0% | 25.0% | 25.0% | 0.250 |
| 三个候选 | 25.0% | 25.0% | 25.0% | 0.250 |

→ 与基线**完全一致**。原因不是嵌入没用，而是这些查询**没有任何词法命中**，
被 `allow_semantic_only=0` 的护栏整体丢弃（实测 route 为 `like+no-lexical-hit`、refs 为空）。

**放行纯语义命中后（评测用，不改生产默认）**：

| 候选 | Top1 | Top3 | Top5 | MRR |
| :--- | ---: | ---: | ---: | ---: |
| baseline（无嵌入） | 25.0% | 25.0% | 25.0% | 0.250 |
| `xenova-fp32` | **58.3%** | 66.7% | 66.7% | **0.625** |
| `xenova-int8` | **58.3%** | 66.7% | 66.7% | **0.625** |
| `qdrant-fp32-opt` | **58.3%** | 66.7% | 66.7% | **0.625** |

→ 嵌入把 Top1 从 25% 抬到 58.3%（MRR 0.250 → 0.625），**三个候选再次完全相同**。

查询类型覆盖：同义表达（过滤网↔滤芯、吵不吵↔噪音）、不含原关键词的语义查询、
简称与完整称呼（QPS↔每秒请求数）、中文自然问句。

> ⚠ 这 12 条样本量小（7/12 命中），只作 sanity，不作结论依据 ——
> 结论依据是第 9 节的 1200 组对比。

---

## 9. 量化影响（1200 组对比，独立于检索库）

用 30 篇文档 × 40 条查询构造 1200 组「查询→文档」对比，直接看**排序行为**是否改变
（§12：量化不要求逐元素数值一致，看 retrieval behaviour）。

| 候选 | 标注 Top-1 命中 |
| :--- | ---: |
| `xenova-fp32` | 35/40 = 87.5% |
| `xenova-int8` | **35/40 = 87.5%** |
| `qdrant-fp32-opt` | 35/40 = 87.5% |

（三者未命中的 5 条**完全相同**，属于该合成池的标注偏严，不构成候选差异。）

与 FP32 的排序一致率：

| 对比 | Top-1 一致 | Top-3 集合一致 | 成对顺序一致 |
| :--- | ---: | ---: | ---: |
| `xenova-int8` vs `xenova-fp32` | 38/40 = 95.0% | 25/40 = 62.5% | 92.83% |
| `qdrant-fp32-opt` vs `xenova-fp32` | 40/40 = 100.0% | 40/40 = 100.0% | 99.95% |

INT8 与 FP32 的差异是**轻微重排**（Top-1 有 2 条不同、Top-3 集合有 15 条不同），
但**标注准确率完全不变**；两个 FP32 转换之间几乎等同（99.95% 顺序一致），
印证它们是同一份权重、同一套 tokenizer 的产物。

---

## 10. 关键发现：`allow_semantic_only` 决定嵌入的实际价值

`SEARCH.allow_semantic_only` 默认 **0**，语义是：**没有词法命中的切片一律不进引用列表**
（防「问台风却引用基坑报告」，`app/core/search.py` 有明确注释与历史事故记录）。

实测含义非常直接：

- 默认配置下，**纯语义查询（不含任何查询词字面命中）拿不到任何引用** ——
  嵌入只能在一个已经由 FTS 命中的候选集合内部**重排**。
- 放行后，嵌入的召回价值才显现（sanity Top1 25% → 58.3%），
  代价是负样本误命中 0 → 3。

这不是缺陷，是**产品取舍**，而且证据表明默认值是合理的。但它意味着：

> **bundled embedding 在 V1 里的定位应表述为「提升已命中候选的排序质量 + 覆盖部分近义改写」，
> 而不是「让问句能搜到任何语义相近的内容」。**

若要获得更激进的语义召回，需要用户显式打开 `allow_semantic_only=1`（已有配置项与说明），
而不是选一个更大的模型 —— 换个模型并不会突破这条护栏。

---

## 11. 最终推荐的逐项对照（§17 优先级）

| # | 判据 | `xenova-int8` | `xenova-fp32` | `qdrant-fp32-opt` |
| ---: | :--- | :--- | :--- | :--- |
| 1 | **检索质量** | 87.5% / RAG 97.4% | 87.5% / 97.4% | 87.5% / 97.4% |
| 2 | **Windows CPU 稳定性** | ✅ 正确性全绿 | ✅ | ✅ |
| 3 | **provenance / license 可发行** | base MIT，转换仓库未声明 | 同左 | **显式 MIT（最干净）** |
| 4 | **体积** | **22.80 MB** ✅ | 90.46 MB | 90.39 MB |
| 5 | **性能** | **2.1 ms / +37.6 MB** ✅ | 5.3 ms / +102 MB | 5.3 ms / +101 MB |
| 6 | 是否官方 | 否 | 否 | 否 |

第 1、2 项三方并列；第 3 项 Qdrant 最优；第 4、5 项 INT8 大幅领先。
**取 INT8 为首选**，因为体积/性能/内存对「U 盘产品形态 + 低配 CPU」是产品级约束，
而它的许可链路是**可完整记录**的（MIT 权重 → 转换），并非「许可不明」。
若将来需要一份**声明最干净**的分发审计口径，`qdrant-fp32-opt` 是现成的等价替换（代价 +67.6 MB、~2.5 倍延迟）。

**INT8 为什么可以进 V1**（§12 四条判据逐条满足）：
体积明显下降 ✅（−74.8%）· CPU 性能有实际价值 ✅（2.5×）·
现有 regression 基本不退化 ✅（RAG Top1 完全相同）· 没有异常 case ✅（正确性全绿，1200 组标注准确率不变）。

---

## 12. Provenance 记录（§14 格式）

```json
{
  "model_id": "bge-small-zh-v1.5",
  "artifact_source": "https://huggingface.co/Xenova/bge-small-zh-v1.5",
  "artifact_revision": "75c43b069aac4d136ba6bc1122f995fedcfd2781",
  "base_model": "BAAI/bge-small-zh-v1.5",
  "base_model_revision": "7999e1d3359715c523056ef9478215996d62a620",
  "filename": "onnx/model_int8.onnx",
  "precision": "int8",
  "size": 23903394,
  "sha256": "b9837c19ce154ff0726d398ee77abbc03a7faf0476c6f93016c84e531be7ebb5",
  "license": "MIT",
  "license_note": "转换仓库未声明 license；权重来自 MIT 的 base model，故按 MIT 记录并保留完整链路",
  "tokenizer": {
    "files": {
      "tokenizer.json":         {"size": 439125, "sha256": "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26"},
      "vocab.txt":              {"size": 109540, "sha256": "45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c"},
      "tokenizer_config.json":  {"size": 367,    "sha256": "e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a"},
      "special_tokens_map.json":{"size": 125,    "sha256": "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3"}
    },
    "note": "与 Qdrant/bge-small-zh-v1.5 的同名文件逐字节相同"
  },
  "pooling": "cls",
  "normalization": "l2",
  "dimension": 512,
  "max_length": 512
}
```

**pin revision + pin SHA256 即可**，不需要在线可信服务器 / 公钥体系 / artifact 签名系统（§14）。
USB-WIKI 只为「实际随盘发行的那一组固定字节」负责，不建模型鉴伪系统。

---

## 13. 是否需要 USB-WIKI 自行转换

**不需要。**

三个社区 artifact 的来源可追溯、字节可固定、许可可记录、运行正确、检索质量通过 ——
§3 的四条判据全部满足，且两个 FP32 转换互相印证（99.95% 排序一致），
说明转换本身没有引入异常。

自建转换会引入 torch / transformers / optimum / exporter / quantization 全链路，
换来的只是「把已知可用的字节重新生成一遍」。**只在候选全部出问题时才启用**这条备选路径。

---

## 14. 移交 A4.2b 的待决项

1. **tokenizer 依赖路线**（必须拍板，影响发布体积与依赖数）：
   - `tokenizers` 库：0.23.2 wheel 2.90 MB，但**连带 huggingface_hub / httpx / anyio / h11 /
     httpcore / filelock / tqdm / typing_extensions / protobuf / flatbuffers ≈ +4.4 MB 与 10 个新依赖**；
   - 或用纯 Python 实现 WordPiece（vocab.txt + BasicTokenizer 归一化，约百行，**零依赖**），
     但必须对 `tokenizer.json` 的官方语义做逐条一致性测试（含本文档第 5 节的分词用例）。
2. **ONNX Runtime**：`onnxruntime` 当前是 `--with-onnx` 可选态；若要 bundle 必须进
   `requirements-release.lock`（连带 `dependency_lock_sha256` 变化）。
3. **artifact 落点**：`paths.ONNX_MODEL_FILE` 目前把文件名写死为 `bge-small-zh-q4.onnx`，
   需改为与新 artifact 名一致（或改成由清单解析）。
4. **payload 组装**：`build_release.py::_stage_payload` 目前只拷 `app/` 与 `python-runtime/`，
   需纳入模型文件（+ tokenizer），并把大小写进体积口径（当前 `--with-onnx` 文档写 +140MB，需实测更新）。
5. **许可入库**：走 A3.1 的 `vendor/licenses/<pkg>/<ver>/` + `PROVENANCE.json`（含 sha256），
   构建时只做本地拷贝、不联网。
6. **`--clean` / `--verify`**：确认 eval 目录（`runtime/models/_eval/`）不会被当成发布内容。

---

## 15. 复跑方式

```bash
P=<venv>/Scripts/python.exe
$P tools/embedding_eval/prepare.py --download      # 下载 + 逐文件 sha256 校验
$P tools/embedding_eval/bench.py --all             # 正确性 + tokenizer + CPU 性能
$P tools/embedding_eval/retrieval_eval.py --all --semantic-only   # 双模式检索对比
$P tools/embedding_eval/agreement.py               # 1200 组量化影响
```

细节与两条铁律（候选必须独立进程；协议必须统一）见 `tools/embedding_eval/README.md`。

---

## 16. 本阶段流量与边界

| 项 | 数值 |
| :--- | :--- |
| ONNX artifact 下载 | 203.64 MB（3 个文件） |
| tokenizer 下载 | 1.05 MB（2 仓库 × 4 文件） |
| 评测依赖（pip） | 31.6 MB（最大单文件 `onnxruntime` wheel 14.30 MB） |
| **合计下载** | **≈ 236.3 MB** |
| **单文件最大** | **90.46 MB（< 100 MB 上限）** |

未下载：PyTorch / CUDA / Optimum / Chat LLM / GGUF / Ollama runtime / 任何 >100MB 文件。
未修改 release/runtime 正式代码；未改 dependency lock；未改 BUILD_INFO schema；未加模型 UI。
模型与评测产物全部落在 gitignored 路径。
