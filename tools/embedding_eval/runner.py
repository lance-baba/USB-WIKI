#!/usr/bin/env python3
"""统一的候选运行协议（A4.2a §10）—— 三个候选必须 apples-to-apples。

固定协议（不得逐候选私改）：

    official tokenizer semantics（tokenizer.json，BERT WordPiece）
        ↓  enable_truncation(max_length=512) / enable_padding
    ONNX inference
        ↓
    last_hidden_state 的 **CLS**（token 位置 0）
        ↓
    L2 normalization
        ↓
    512 维

依据：BAAI/bge-small-zh-v1.5 官方 `1_Pooling/config.json` 为
`pooling_mode_cls_token: true` / `pooling_mode_mean_tokens: false`。
所以任何候选都取 CLS —— **不允许**一个用 CLS、另一个用 mean 再比较结果。

⚠ 本模块属于**选型阶段工具**，不是 release 运行时代码：
正式 tokenizer 集成放在 A4.2b，不在此处进 lock。
"""
from __future__ import annotations

import time
from pathlib import Path


class OnnxBertEmbedder:
    """与运行时 `BaseEmbedder` 同形状（dim/source/embed），但用真实 tokenizer。"""

    source = "local_onnx"

    def __init__(self, model_path: Path, tokenizer_dir: Path, *,
                 model_name: str, max_len: int = 512) -> None:
        self.model_path = Path(model_path)
        self.tokenizer_dir = Path(tokenizer_dir)
        self.model = model_name
        self.max_len = int(max_len)
        self.dim: int | None = None
        self.load_seconds: float | None = None
        self.io_names: dict = {}
        self._session = None
        self._tok = None
        self._np = None

    # ------------------------------------------------------------------
    def _lazy(self) -> None:
        if self._session is not None:
            return
        t0 = time.perf_counter()
        import numpy as np  # noqa: PLC0415
        import onnxruntime as ort  # noqa: PLC0415
        from tokenizers import Tokenizer  # noqa: PLC0415

        tok_path = self.tokenizer_dir / "tokenizer.json"
        if not tok_path.is_file():
            raise RuntimeError(f"缺 tokenizer.json：{tok_path}")
        tok = Tokenizer.from_file(str(tok_path))
        pad_id = tok.token_to_id("[PAD]")
        tok.enable_truncation(max_length=self.max_len)
        tok.enable_padding(pad_id=pad_id if pad_id is not None else 0,
                           pad_token="[PAD]")

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.intra_op_num_threads = 2

        t_load = time.perf_counter()
        sess = ort.InferenceSession(str(self.model_path), sess_options=so,
                                    providers=["CPUExecutionProvider"])
        self.load_seconds = time.perf_counter() - t_load
        _ = t0

        outs = sess.get_outputs()
        self.io_names = {
            "inputs": [i.name for i in sess.get_inputs()],
            "outputs": [o.name for o in outs],
            "shapes": {o.name: o.shape for o in outs},
            "providers": sess.get_providers(),
        }
        # 静态形状时立刻定下维度：调用方（DB 签名、检索）在首次 embed 之前就需要它，
        # 等到 embed() 才赋值会让 set_signature 拿到 None（实测踩过）。
        for name in self.io_names["outputs"]:
            shape = self.io_names["shapes"].get(name) or []
            if len(shape) == 3 and isinstance(shape[-1], int):
                self.dim = int(shape[-1])
                break
        self._np, self._tok, self._session = np, tok, sess

    # ------------------------------------------------------------------
    def embed(self, texts: list[str]) -> list[list[float]]:
        self._lazy()
        np = self._np
        encs = self._tok.encode_batch(list(texts))
        ids = np.array([e.ids for e in encs], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.io_names["inputs"]:
            feeds["token_type_ids"] = np.zeros_like(ids)
        # 只喂模型声明过的输入（不同导出可能省略 token_type_ids）
        feeds = {k: v for k, v in feeds.items() if k in self.io_names["inputs"]}

        outs = self._session.run(None, feeds)
        last_hidden = None
        for name, arr in zip(self.io_names["outputs"], outs):
            if "last_hidden_state" in name.lower():
                last_hidden = arr
                break
        if last_hidden is None:
            cand = [a for a in outs if getattr(a, "ndim", 0) == 3]
            if not cand:
                raise RuntimeError(
                    f"没有 3D 输出可取 CLS；实际输出="
                    f"{ {n: getattr(a, 'shape', None) for n, a in zip(self.io_names['outputs'], outs)} }")
            last_hidden = cand[0]
        cls = last_hidden[:, 0, :]                      # ← CLS，协议规定
        norm = np.linalg.norm(cls, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        vecs = (cls / norm).astype(np.float32)
        self.dim = int(vecs.shape[1])
        return [row.tolist() for row in vecs]

    # 与运行时 BaseEmbedder 接口对齐（search.hybrid_search 只要求有 embed）
    def signature_tuple(self) -> tuple[str, str, int]:
        return self.source, self.model, int(self.dim or 512)
