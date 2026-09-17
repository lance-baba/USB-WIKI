# tools/embedding_eval —— 嵌入 artifact 选型 / 复验工具（A4.2a）

**这些脚本不属于 release 运行时**，也不进 `requirements-release.lock`。
用途只有一个：把「选哪个 ONNX 发行物」这件事变成可复跑的实测，而不是凭感觉。

## 一次性准备

```bash
# 独立评测环境（不污染项目 runtime，也不改正式依赖锁）
python -m venv <venv>
<venv>/Scripts/python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
    numpy onnxruntime tokenizers sqlite-vec
```

`sqlite-vec` 是为了让**向量召回真的生效**（否则 `hybrid_search` 会因
`vec_table_ready=False` 静默降级成纯词法，测出来的不是候选的真实召回）。

模型与 tokenizer 落在 **gitignored** 的 `runtime/models/_eval/`：

```
runtime/models/_eval/
├─ xenova-fp32/onnx/model.onnx          90.46 MB
├─ xenova-int8/onnx/model_int8.onnx     22.80 MB
├─ qdrant-fp32-opt/model_optimized.onnx 90.39 MB
├─ tokenizers/<repo>/                   各仓库自带 tokenizer
├─ selection_inputs.json                候选表 + 实际 sha256/size
├─ bench.json                           性能/正确性结果
└─ retrieval.json                       检索质量结果
```

## 复跑

```bash
P=<venv>/Scripts/python.exe
$P tools/embedding_eval/prepare.py --list                  # 候选表（含上游 sha256）
$P tools/embedding_eval/prepare.py --download              # 下载并逐文件校验 sha256
$P tools/embedding_eval/bench.py --all                     # 正确性 + tokenizer + CPU 性能
$P tools/embedding_eval/retrieval_eval.py --all            # FTS 基线 vs 各候选
```

单候选跑：

```bash
$P tools/embedding_eval/bench.py --only xenova-int8
$P tools/embedding_eval/retrieval_eval.py --embedder xenova-int8 --suite sanity
```

## 两条铁律（别再踩）

1. **候选必须在独立进程里测。** 同进程顺序加载多个 ONNX session，
   「冷加载时间」不再冷，「进程峰值内存」被前一个候选污染。
   `--all` 已自动为每个候选起子进程。
2. **运行协议必须一致。** 见 `runner.py`：official tokenizer → ONNX →
   `last_hidden_state` 的 **CLS** → L2 → 512 维。
   一个候选用 CLS、另一个用 mean pooling，比出来的数字没有意义。
   依据：`BAAI/bge-small-zh-v1.5` 官方 `1_Pooling/config.json` 是
   `pooling_mode_cls_token: true`。

## 内存测量

`_mem_mb()` 用 ctypes 读 Windows `PROCESS_MEMORY_COUNTERS`。
⚠ 必须传 `c_void_p(-1)` 作为进程句柄：`kernel32.GetCurrentProcess()` 返回的是
Python int，不声明 `argtypes` 时会被当 32 位截断，API 静默返回 0 —— 量出来永远是 0。
