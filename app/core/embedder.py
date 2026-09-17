"""向量嵌入源解析（PRD 3.2 / 4.4）。

三个正式来源 + 一个兜底：
* ``local_onnx`` —— **随包** ONNX 嵌入（bge-small-zh-v1.5 INT8，onnxruntime CPU）。
  资源由 ``resources/embedding/artifact.json`` 描述（见 :class:`EmbeddingResource`），
  Core 不认识发布文件名，只认识 id / 路径 / 维度 / 精度 / hash。
* ``ollama``     —— 通过本地 Ollama 守护进程计算
* ``api``        —— 通过 OpenAI 兼容接口 ``/embeddings`` 计算
* ``local_hash`` —— 纯 Python 哈希向量兜底；**只在显式配置时启用**，绝不自动进入
  （自动进入会覆写主源签名 →「降级 → 重建」破坏性循环）

**AVX2 指令集防护**（PRD 编码阶段提示 #3）：
``import onnxruntime`` 在老 CPU 上会触发 SIGILL 直接杀进程，而 SIGILL 在 Python 层
不可捕获。因此采用双重前置防护：
1. 读 CPU 特性位（Windows ``IsProcessorFeaturePresent`` / Linux ``/proc/cpuinfo`` /
   macOS ``sysctl``）做快速否决；
2. 在**子进程**中真实执行 import 探针 —— 即便被 SIGILL 杀死，牺牲的也只是子进程。
探测通过后仍对顶层 import 加 try/except 兜底。
"""
from __future__ import annotations

import hashlib
import json
import math
import platform
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import net_util, paths
from .log_util import get_logger

log = get_logger()

PF_AVX2_INSTRUCTIONS_AVAILABLE = 40
PF_AVX_INSTRUCTIONS_AVAILABLE = 39
PF_SSE4_2_INSTRUCTIONS_AVAILABLE = 38

_onnx_probe_lock = threading.Lock()
_onnx_probe_result: tuple[bool, str] | None = None


# --------------------------------------------------------------------------
# CPU 能力探测
# --------------------------------------------------------------------------
def cpu_flags() -> dict[str, bool | None]:
    system = platform.system()
    if system == "Windows":
        try:
            import ctypes

            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            return {
                "sse4_2": bool(k32.IsProcessorFeaturePresent(PF_SSE4_2_INSTRUCTIONS_AVAILABLE)),
                "avx": bool(k32.IsProcessorFeaturePresent(PF_AVX_INSTRUCTIONS_AVAILABLE)),
                "avx2": bool(k32.IsProcessorFeaturePresent(PF_AVX2_INSTRUCTIONS_AVAILABLE)),
            }
        except Exception:  # noqa: BLE001
            return {"sse4_2": None, "avx": None, "avx2": None}
    if system == "Linux":
        try:
            info = open("/proc/cpuinfo", encoding="utf-8", errors="replace").read().lower()
            return {
                "sse4_2": "sse4_2" in info,
                "avx": bool(re.search(r"\bavx\b", info)),
                "avx2": "avx2" in info,
            }
        except OSError:
            return {"sse4_2": None, "avx": None, "avx2": None}
    if system == "Darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.features"],
                capture_output=True, text=True, timeout=5,
            ).stdout.lower()
            return {
                "sse4_2": "sse4_2" in out,
                "avx": "avx1.0" in out or "avx2" in out,
                "avx2": "avx2" in out,
            }
        except (OSError, subprocess.SubprocessError):
            return {"sse4_2": None, "avx": None, "avx2": None}
    return {"sse4_2": None, "avx": None, "avx2": None}


_PROBE_CODE = (
    "import sys\n"
    "try:\n"
    "    import onnxruntime as ort\n"
    "    print('OK', ort.__version__)\n"
    "except BaseException as e:\n"  # noqa: BLE001
    "    print('FAIL', type(e).__name__, e)\n"
)

_PROBE_CACHE_TTL = 7 * 24 * 3600.0


def _probe_cache_path():
    return paths.RUNTIME_DIR / ".onnx_probe.json"


def _read_probe_cache():
    """探针结果落盘缓存 —— 免去每次冷启动都付子进程开销（PRD 6.1 冷启动 ≤3.5s）。"""
    import json

    p = _probe_cache_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if __import__("time").time() - float(data.get("ts") or 0) > _PROBE_CACHE_TTL:
        return None
    if data.get("python") != f"{sys.version_info.major}.{sys.version_info.minor}":
        return None
    # ⚠ **负结果不参与磁盘缓存**：onnxruntime 现在是本机标准运行依赖，
    #   用户「先跑起来、后来才把引擎装上」是正常路径。若把 7 天前的
    #   「不可用」当真，装好之后仍会被静默禁用整整一周（且没有任何提示）。
    #   正结果照旧缓存（省掉每次冷启动的子进程开销）。
    if not data.get("ok"):
        return None
    return (True, str(data.get("detail") or ""))


def _write_probe_cache(result: tuple[bool, str]) -> None:
    import json
    import time as _t

    if not result[0]:
        # 负结果不落盘（见 _read_probe_cache 的说明）：让「装上引擎」立刻生效。
        return
    try:
        paths.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        _probe_cache_path().write_text(
            json.dumps(
                {
                    "ok": result[0],
                    "detail": result[1],
                    "ts": _t.time(),
                    "python": f"{sys.version_info.major}.{sys.version_info.minor}",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def probe_onnxruntime(force: bool = False) -> tuple[bool, str]:
    """在子进程中真实尝试 import onnxruntime，返回 (可用, 说明)。

    子进程隔离确保即便命中 Illegal Instruction 被杀，也只会得到负结果，
    而不是整个便携系统一起崩溃。结果带 7 天磁盘缓存以压低冷启动耗时。
    """
    global _onnx_probe_result
    with _onnx_probe_lock:
        if _onnx_probe_result is not None and not force:
            return _onnx_probe_result

        if not force:
            cached = _read_probe_cache()
            if cached is not None:
                _onnx_probe_result = cached
                return cached

        flags = cpu_flags()
        if flags.get("avx2") is False and flags.get("sse4_2") is False:
            _onnx_probe_result = (
                False,
                "本机 CPU 缺少 SSE4.2/AVX2 指令集，本地 onnx 引擎不可用",
            )
            _write_probe_cache(_onnx_probe_result)
            return _onnx_probe_result

        try:
            proc = subprocess.run(
                [sys.executable, "-c", _PROBE_CODE],
                capture_output=True, text=True, timeout=25,
                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _onnx_probe_result = (False, f"onnxruntime 探针执行失败: {exc}")
            return _onnx_probe_result

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode == 0 and out.startswith("OK"):
            version = out.split(" ", 1)[1] if " " in out else "?"
            _onnx_probe_result = (True, f"onnxruntime {version} 可用")
        elif proc.returncode in (-4, 4, 3221225477, -1073741819):  # SIGILL / 访问冲突
            _onnx_probe_result = (
                False,
                "检测到本机 CPU 指令集不兼容本地嵌入引擎（Illegal Instruction）",
            )
        else:
            _onnx_probe_result = (
                False,
                f"onnxruntime 不可用: {(err or out)[:160] or '未知原因'}",
            )
        log.info("onnxruntime 探针结果: %s", _onnx_probe_result[1])
        _write_probe_cache(_onnx_probe_result)
        return _onnx_probe_result


# --------------------------------------------------------------------------
# 嵌入器实现
# --------------------------------------------------------------------------
class BaseEmbedder:
    source = "none"
    model = "none"

    def __init__(self, dim: int = 512) -> None:
        self.dim = int(dim)

    @property
    def signature(self) -> str:
        return f"{self.source}:{self.model}:{self.dim}"

    def signature_extra(self) -> dict:
        """签名守卫的**额外字节级**字段（默认空）。

        bundled local_onnx 用它带上 artifact / tokenizer 的 SHA256 与精度 ——
        只记「模型名 + 维度」无法区分「同一个名字但字节换了」的 artifact（§12）。
        """
        return {}

    def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
        raise NotImplementedError

    def probe_dim(self) -> int | None:
        """探测真实向量维度。

        Ollama / API 的维度由所选模型决定，**不该让用户手填** —— 这里用一条极短
        文本实测一次拿到真实维度并自动覆盖配置值。探测失败返回 None（沿用配置）。
        """
        return None

    def health(self) -> tuple[bool, str]:
        return True, "OK"


class HashEmbedder(BaseEmbedder):
    """纯 Python 确定性哈希向量（字符 n-gram + 签名哈希技巧）。

    用于「无 onnxruntime、无 Ollama、无 API Key」时仍能保留向量召回通道，
    保证系统永不因嵌入缺失而崩塌。词法级相似度有效，语义级弱于真模型。
    """

    source = "local_hash"
    model = "char-ngram-hash-v1"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        dim = self.dim
        vec = [0.0] * dim
        t = re.sub(r"\s+", " ", (text or "").lower()).strip()
        if not t:
            return vec
        for n, weight in ((1, 1.0), (2, 0.9), (3, 0.5)):
            limit = len(t) - n + 1
            if limit <= 0:
                continue
            for i in range(limit):
                gram = t[i:i + n]
                if n > 1 and " " in gram:
                    continue
                digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
                idx = int.from_bytes(digest[:4], "little") % dim
                sign = 1.0 if digest[4] & 1 else -1.0
                vec[idx] += sign * weight
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class OllamaEmbedder(BaseEmbedder):
    source = "ollama"

    def __init__(self, host: str, model: str, dim: int = 512, timeout: float = 30.0) -> None:
        super().__init__(dim)
        self.host = net_util.normalize_base(host)
        self.model = model
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        status, data = net_util.http_post_json(
            net_util.join_url(self.host, "/api/embed"),
            {"model": self.model, "input": texts},
            timeout=self.timeout,
            with_proxy=False,
        )
        if status == 200 and isinstance(data, dict) and data.get("embeddings"):
            out = [list(map(float, e)) for e in data["embeddings"]]
            self.dim = len(out[0]) if out else self.dim
            return out
        # 兼容老版本 /api/embeddings（单条）
        results: list[list[float]] = []
        for t in texts:
            st, d = net_util.http_post_json(
                net_util.join_url(self.host, "/api/embeddings"),
                {"model": self.model, "prompt": t},
                timeout=self.timeout,
                with_proxy=False,
            )
            if st != 200 or not isinstance(d, dict) or not d.get("embedding"):
                raise RuntimeError(f"Ollama embedding 失败: {st} {str(d)[:120]}")
            results.append(list(map(float, d["embedding"])))
        if results:
            self.dim = len(results[0])
        return results

    def health(self) -> tuple[bool, str]:
        status, _ = net_util.http_post_json(
            net_util.join_url(self.host, "/api/embed"),
            {"model": self.model, "input": ["ping"]},
            timeout=5.0, with_proxy=False,
        )
        return (status == 200, f"HTTP {status}")

    def probe_dim(self) -> int | None:
        try:
            vecs = self.embed(["维度探测"])
            return len(vecs[0]) if vecs else None
        except Exception as exc:  # noqa: BLE001 - 探测失败沿用配置维度
            log.debug("Ollama 维度探测失败: %s", exc)
            return None


class ApiEmbedder(BaseEmbedder):
    source = "api"

    def __init__(self, base_url: str, api_key: str, model: str, dim: int = 512, timeout: float = 30.0) -> None:
        super().__init__(dim)
        self.base_url = net_util.normalize_base(base_url)
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        status, data = net_util.http_post_json(
            net_util.join_url(self.base_url, "/embeddings"),
            {"model": self.model, "input": texts, "encoding_format": "float"},
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError(f"API embedding 失败: {status} {str(data)[:160]}")
        items = data.get("data") or []
        out = [list(map(float, it.get("embedding") or [])) for it in items]
        if not out:
            raise RuntimeError("API embedding 返回空")
        self.dim = len(out[0])
        return out

    def probe_dim(self) -> int | None:
        try:
            vecs = self.embed(["dim"])
            return len(vecs[0]) if vecs else None
        except Exception as exc:  # noqa: BLE001 - 探测失败沿用配置维度
            log.debug("API 维度探测失败: %s", exc)
            return None


# --------------------------------------------------------------------------
# 随包嵌入资源（resources/embedding/artifact.json）
# --------------------------------------------------------------------------
#: 资源不可用的稳定判定码（供 diagnostics / 降级说明 / 测试使用）
RES_OK = "ok"
RES_MISSING_MANIFEST = "missing_manifest"       # 没有 artifact.json → 未随包
RES_INVALID_MANIFEST = "invalid_manifest"       # 清单存在但不可解析 / 字段不合法
RES_MISSING_MODEL = "missing_model"             # 清单在，模型文件不在
RES_SIZE_MISMATCH = "size_mismatch"             # 模型文件大小与清单不符（廉价完整性检查）
RES_MISSING_TOKENIZER = "missing_tokenizer"     # tokenizer.json 不在

_REQUIRED_KEYS = ("id", "artifact_source", "artifact_revision", "artifact_sha256",
                  "artifact_size", "precision", "dimension")


@dataclass
class EmbeddingResource:
    """随包嵌入资源的**解析结果** —— Core 唯一认识的形态。

    Core 不认识「发布文件名」，只认识这份清单描述的：id / 路径 / 维度 / 精度 / hash。
    换 artifact = 换资源目录里的字节与清单，**不需要改代码**。
    """

    root: Path
    id: str
    base_model: str
    base_model_revision: str
    artifact_source: str
    artifact_revision: str
    artifact_file: str
    artifact_sha256: str
    artifact_size: int
    precision: str
    dimension: int
    pooling: str
    normalize: bool
    max_length: int
    tokenizer_files: dict
    license: dict
    model_filename: str
    tokenizer_filename: str

    @property
    def model_path(self) -> Path:
        return self.root / self.model_filename

    @property
    def tokenizer_json(self) -> Path:
        return self.root / self.tokenizer_filename

    @property
    def declared_files(self) -> list[Path]:
        return [self.model_path, self.tokenizer_json]

    @property
    def tokenizer_sha256(self) -> str:
        rec = (self.tokenizer_files or {}).get(self.tokenizer_filename) or {}
        return str(rec.get("sha256") or "")

    def signature_extra(self) -> dict:
        """写进库内签名守卫的**字节级**字段（§12）。

        同 artifact 重装 → 不变；artifact 或 tokenizer 字节变化 → 必变。
        """
        return {
            "artifact_sha256": self.artifact_sha256,
            "tokenizer_sha256": self.tokenizer_sha256,
            "precision": self.precision,
        }

    def build_info(self) -> dict:
        """BUILD_INFO.embedding 块（不含绝对路径）。"""
        return {
            "id": self.id,
            "base_model": self.base_model,
            "artifact_source": self.artifact_source,
            "artifact_revision": self.artifact_revision,
            "precision": self.precision,
            "dimension": self.dimension,
            "artifact_sha256": self.artifact_sha256,
            "tokenizer_sha256": self.tokenizer_sha256,
        }


def load_embedding_resource(root: Path | None = None
                            ) -> tuple[EmbeddingResource | None, str, str]:
    """解析随包嵌入资源 → (resource | None, code, reason)。

    ⚠ 只做**廉价**检查（存在性 + 大小）。**不在这里重算 23MB 的 SHA256**：
    介质完整性由 Release 的 hash Gate + 安装前校验负责，启动时无条件重算会拖慢首屏。
    需要全量校验时用 :func:`verify_resource_files(deep=True)`（diagnostics / repair）。
    """
    root = Path(root) if root is not None else paths.EMBEDDING_DIR
    manifest = root / paths.EMBEDDING_ARTIFACT_NAME
    if not manifest.is_file():
        return None, RES_MISSING_MANIFEST, f"未找到随包嵌入资源清单（{paths.EMBEDDING_ARTIFACT_NAME}）"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, RES_INVALID_MANIFEST, f"资源清单无法解析：{exc}"
    if not isinstance(data, dict) or data.get("format_version") != 1:
        return None, RES_INVALID_MANIFEST, "资源清单 format_version 不受支持"
    missing = [k for k in _REQUIRED_KEYS if not data.get(k)]
    if missing:
        return None, RES_INVALID_MANIFEST, f"资源清单缺字段：{', '.join(missing)}"

    local = data.get("local_files") or {}
    model_filename = Path(str(local.get("model") or "model.onnx")).name
    tok_filename = Path(str(local.get("tokenizer") or "tokenizer.json")).name

    res = EmbeddingResource(
        root=root, id=str(data["id"]),
        base_model=str(data.get("base_model") or ""),
        base_model_revision=str(data.get("base_model_revision") or ""),
        artifact_source=str(data["artifact_source"]),
        artifact_revision=str(data["artifact_revision"]),
        artifact_file=str(data.get("artifact_file") or ""),
        artifact_sha256=str(data["artifact_sha256"]).lower(),
        artifact_size=int(data["artifact_size"]),
        precision=str(data["precision"]).lower(),
        dimension=int(data["dimension"]),
        pooling=str(data.get("pooling") or "cls").lower(),
        normalize=bool(data.get("normalize", True)),
        max_length=int(data.get("max_length") or 512),
        tokenizer_files=dict(data.get("tokenizer_files") or {}),
        license=dict(data.get("license") or {}),
        model_filename=model_filename, tokenizer_filename=tok_filename,
    )
    if not res.model_path.is_file():
        return None, RES_MISSING_MODEL, f"缺少模型文件 {model_filename}"
    if res.model_path.stat().st_size != res.artifact_size:
        return None, RES_SIZE_MISMATCH, (
            f"模型文件大小与清单不符（期望 {res.artifact_size}，"
            f"实际 {res.model_path.stat().st_size}）")
    if not res.tokenizer_json.is_file():
        return None, RES_MISSING_TOKENIZER, f"缺少 tokenizer 文件 {tok_filename}"
    return res, RES_OK, ""


def verify_resource_files(resource: EmbeddingResource, *, deep: bool = True) -> list[str]:
    """校验资源目录里的字节（**只读**）。deep=True 时逐文件重算 SHA256。

    刻意与启动路径分离：启动用 load_embedding_resource 的廉价检查，
    全量校验交给 diagnostics / repair（§16）。
    """
    problems: list[str] = []
    targets: list[tuple[Path, str]] = [(resource.model_path, resource.artifact_sha256)]
    for name, rec in (resource.tokenizer_files or {}).items():
        targets.append((resource.root / Path(name).name, str(rec.get("sha256") or "")))
    for path, want in targets:
        if not path.is_file():
            problems.append(f"{path.name}：缺失")
            continue
        if deep and want:
            actual = _sha256_file(path)
            if actual != want:
                problems.append(f"{path.name}：SHA256 不符（期望 {want[:16]}…，"
                                f"实际 {actual[:16]}…）")
    return problems


def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class OnnxEmbedder(BaseEmbedder):
    """随包本地 ONNX 嵌入（bge-small-zh-v1.5 INT8）。

    运行协议（与 A4.2a 选型时**完全一致**，不得逐处私改）：

        tokenizers.Tokenizer.from_file(本地 tokenizer.json)   ← 只读本地文件
            ↓ encode_batch（截断 max_length + padding）
        onnxruntime（CPUExecutionProvider）
            ↓ last_hidden_state
        CLS（token 位置 0）
            ↓ L2 归一化
        artifact 声明的维度

    ⚠ **没有字符级近似兜底。** 官方 tokenizer 加载失败即视为 `local_onnx`
    不可用并按 A4.1 契约降级 —— 偷偷切到字符级编码会产出一个「看起来能用」
    但语义错误的向量空间（旧实现正是如此，且当时用的是 mean pooling）。
    """

    source = "local_onnx"

    def __init__(self, resource: EmbeddingResource) -> None:
        super().__init__(resource.dimension)
        self.resource = resource
        self.model = resource.id
        self.precision = resource.precision
        self._session = None
        self._tok = None
        self._np = None
        self.io_names: dict = {}

    # ------------------------------------------------------------------
    def signature_extra(self) -> dict:
        return self.resource.signature_extra()

    def _lazy(self) -> None:
        if self._session is not None:
            return
        try:
            import numpy as np  # type: ignore
            import onnxruntime as ort  # type: ignore
        except BaseException as exc:  # noqa: BLE001 - 指令集非法必须在此兜住
            raise RuntimeError(f"onnxruntime 加载失败: {type(exc).__name__}: {exc}") from exc
        try:
            from tokenizers import Tokenizer  # type: ignore
        except BaseException as exc:  # noqa: BLE001
            raise RuntimeError(f"tokenizers 加载失败: {type(exc).__name__}: {exc}") from exc

        # ⚠ 只允许本地文件。绝不允许 from_pretrained / 访问 HuggingFace /
        #   调用 huggingface_hub —— 客户机必须零联网。
        tok = Tokenizer.from_file(str(self.resource.tokenizer_json))
        pad_id = tok.token_to_id("[PAD]")
        tok.enable_truncation(max_length=self.resource.max_length)
        tok.enable_padding(pad_id=pad_id if pad_id is not None else 0,
                           pad_token="[PAD]")

        so = ort.SessionOptions()
        so.intra_op_num_threads = 2
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess = ort.InferenceSession(str(self.resource.model_path), sess_options=so,
                                    providers=["CPUExecutionProvider"])

        outs = sess.get_outputs()
        self.io_names = {
            "inputs": [i.name for i in sess.get_inputs()],
            "outputs": [o.name for o in outs],
        }
        self._np, self._tok, self._session = np, tok, sess

    def probe(self) -> tuple[bool, str]:
        """真正把 session + tokenizer 加载起来验证一次（resolve 阶段用）。

        失败即 `local_onnx` 不可用 —— 不返回一个「一调用就炸」的嵌入器。
        """
        try:
            self._lazy()
            vecs = self.embed(["探针"])
            if not vecs or not vecs[0]:
                return False, "嵌入探针返回空向量"
            self.dim = len(vecs[0])
            return True, "OK"
        except BaseException as exc:  # noqa: BLE001 - 含 SIGILL 之外的加载期异常
            return False, f"{type(exc).__name__}: {exc}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._lazy()
        np = self._np
        encs = self._tok.encode_batch(list(texts))
        ids = np.array([e.ids for e in encs], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
        feed: dict = {}
        names = set(self.io_names["inputs"])
        if "input_ids" in names:
            feed["input_ids"] = ids
        if "attention_mask" in names:
            feed["attention_mask"] = mask
        if "token_type_ids" in names:
            feed["token_type_ids"] = np.zeros_like(ids)

        outputs = self._session.run(None, feed)
        last_hidden = None
        for name, arr in zip(self.io_names["outputs"], outputs):
            if "last_hidden_state" in name.lower():
                last_hidden = arr
                break
        if last_hidden is None:
            cand = [a for a in outputs if getattr(a, "ndim", 0) == 3]
            if not cand:
                raise RuntimeError("ONNX 输出里没有可取 CLS 的 3D 张量")
            last_hidden = cand[0]
        pooled = last_hidden[:, 0, :]                     # CLS，协议规定
        if self.resource.normalize:
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            pooled = pooled / np.clip(norms, 1e-9, None)
        self.dim = int(pooled.shape[1])
        return pooled.astype("float32").tolist()


# --------------------------------------------------------------------------
@dataclass
class EmbedderResolution:
    """嵌入源解析结果。

    ``warnings`` 只放**需要用户采取行动**的事（能力缺失、配置错误、模型未安装）；
    ``notes`` 放**系统已自行处理好的过程信息**（自动降级、维度自动适配、按配置关闭）。
    两者分开，避免把「一切正常」的自愈日志当成告警糊在界面上吓用户。
    """

    embedder: BaseEmbedder | None
    source: str
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: 随包资源（仅 local_onnx 时有值）—— 供 signature / BUILD_INFO / diagnostics 复用
    resource: "EmbeddingResource | None" = None
    #: 为什么没用到**配置指定的那个**来源（diagnostics 的 fallback_reason）
    fallback_reason: str = ""


def _humanize_onnx_failure(why: str) -> str:
    """把探针的原始异常转成人能看懂的一句话。"""
    w = why or ""
    if "ModuleNotFoundError" in w or "No module named" in w:
        return "未安装 onnxruntime"
    if "SIGILL" in w or "Illegal" in w or "指令集" in w:
        return "本机 CPU 指令集不兼容"
    if "模型" in w or "model" in w.lower():
        return "模型文件缺失"
    return w.strip() or "探测未通过"


def _ollama_installed(host: str) -> list[str]:
    """读取 Ollama 已安装模型名；不可用返回空列表。"""
    status, body, _ = net_util.http_get(
        net_util.join_url(host, "/api/tags"), timeout=4.0, with_proxy=False
    )
    if status != 200:
        return []
    try:
        import json as _json

        data = _json.loads(body.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for m in data.get("models") or []:
        name = str((m or {}).get("name") or "").strip()
        if name:
            out.append(name)
    return out


def resolve(cfg_get, ollama_healthy=None) -> EmbedderResolution:
    """按配置 + 可用性逐级解析嵌入源。

    ``cfg_get(section, key, default)`` 由 config 模块提供；
    ``ollama_healthy`` 可选回调查询 Ollama 是否在线。
    """
    requested = str(cfg_get("AI", "embedding_source", "local_onnx")).strip().lower()
    dim = int(cfg_get("AI", "embedding_dim", 512))
    model_name = str(cfg_get("AI", "embedding_model_name", "bge-small-zh-q4"))
    api_base = str(cfg_get("AI", "api_base_url", ""))
    api_key = str(cfg_get("AI", "api_key", ""))
    ollama_host = str(cfg_get("AI", "ollama_host", "http://127.0.0.1:11434"))
    warnings: list[str] = []      # 需用户行动
    notes: list[str] = []         # 系统已自愈的过程信息，不打扰用户
    fallback_reason = ""          # 没用到「配置指定的来源」时，这里给出人话原因

    if requested in ("none", "offline", "disabled", "false", "0"):
        return EmbedderResolution(None, "none", notes=["已按配置关闭向量嵌入，仅使用 FTS5 词法检索"])

    # 1) local_onnx —— 随包资源（resources/embedding/artifact.json）
    #    ⚠ 这里的每一项失败都必须**如实说明原因**并继续降级。绝不切换到
    #      字符级近似编码：那会产出一个「看起来能用」但语义错误的向量空间。
    if requested == "local_onnx":
        resource, code, why = load_embedding_resource()
        if resource is None:
            # 清单缺失 / 不可解析 / 模型缺失 / 尺寸不符 / tokenizer 缺失
            fallback_reason = f"随包嵌入资源不可用（{code}：{why}）"
            notes.append(fallback_reason + "，已按降级链继续")
        else:
            probe_ok, probe_why = probe_onnxruntime()
            if not probe_ok:
                fallback_reason = (
                    f"本地 ONNX 嵌入引擎不可用（{_humanize_onnx_failure(probe_why)}）")
                notes.append(fallback_reason + "，已按降级链继续")
            else:
                emb = OnnxEmbedder(resource)
                loaded, load_why = emb.probe()
                if loaded:
                    notes.append(
                        f"随包嵌入资源已启用：{resource.id}"
                        f"（{resource.precision}，{resource.dimension} 维，"
                        f"artifact {resource.artifact_sha256[:12]}…）"
                    )
                    return EmbedderResolution(emb, "local_onnx", warnings, notes,
                                              resource=resource)
                # 资源在、依赖在，但加载失败（tokenizer 损坏 / 模型不可读 / CPU 不兼容）
                fallback_reason = f"随包嵌入资源加载失败（{load_why}）"
                notes.append(fallback_reason + "，已按降级链继续")
        requested = "auto"

    # 2) ollama
    if requested in ("ollama", "auto"):
        probe = ollama_healthy
        if probe is None:
            def probe() -> bool:  # type: ignore[misc]
                st, _body, _h = net_util.http_get(
                    net_util.join_url(ollama_host, "/api/tags"),
                    timeout=2.0, with_proxy=False,
                )
                return st == 200

        healthy = False
        try:
            healthy = bool(probe())
        except Exception:  # noqa: BLE001
            healthy = False
        if healthy:
            emb = OllamaEmbedder(ollama_host, model_name, dim)
            installed = _ollama_installed(ollama_host)
            if installed and model_name not in installed:
                loose = {n.split(":")[0] for n in installed}
                if model_name.split(":")[0] not in loose:
                    # 这条要用户动手（pull 模型或改配置），保留为告警
                    warnings.append(
                        f"Ollama 在线，但未安装嵌入模型「{model_name}」—— "
                        f"可执行 ollama pull {model_name}，或改用已安装的："
                        f"{', '.join(sorted(installed)[:4])}"
                    )
            real = emb.probe_dim()  # 维度由模型决定，自动适配，不让用户手填
            if real:
                if real != dim:
                    notes.append(
                        f"向量维度已跟随模型自动适配：{dim} → {real}（{model_name}）"
                    )
                emb.dim = real
            return EmbedderResolution(emb, "ollama", warnings, notes,
                                      fallback_reason=fallback_reason)
        notes.append("Ollama 未在线，已跳过")
        if requested == "ollama":
            warnings.append("你指定使用 Ollama 嵌入源，但它当前不可用，已降级到其它来源")

    # 3) api
    if requested in ("api", "auto") and api_key:
        emb = ApiEmbedder(api_base, api_key, model_name, dim)
        real = emb.probe_dim()  # 同样自动适配，杜绝「维度对不上 → 向量被禁用」
        if real:
            if real != dim:
                notes.append(f"向量维度已跟随模型自动适配：{dim} → {real}（{model_name}）")
            emb.dim = real
        return EmbedderResolution(emb, "api", warnings, notes,
                                  fallback_reason=fallback_reason)
    if requested == "api" and not api_key:
        warnings.append("你指定使用云端 API 嵌入源，但未填写 api_key")
    if requested == "auto" and not api_key:
        notes.append("未配置 api_key，已跳过云端 API 嵌入源")

    # 4) 纯 Python 兜底 —— **只在用户显式配置 `embedding_source = local_hash` 时启用**。
    #    绝不自动进入：哈希向量会覆写主源签名，形成「降级 → 重建」的破坏性循环。
    if requested == "local_hash":
        return EmbedderResolution(HashEmbedder(dim), "local_hash", warnings, notes)

    warnings.append(
        "向量嵌入源全部不可用，已退化为纯 FTS5 词法检索（已有向量索引保留，恢复嵌入源后自动恢复）"
    )
    return EmbedderResolution(None, "none", warnings, notes,
                              fallback_reason=fallback_reason)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
