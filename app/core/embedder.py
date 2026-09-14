"""向量嵌入源解析（PRD 3.2 / 4.4）。

三个正式来源 + 一个兜底：
* ``local_onnx`` —— 内置微型 bge-small-zh（onnxruntime CPU）
* ``ollama``     —— 通过本地 Ollama 守护进程计算
* ``api``        —— 通过 OpenAI 兼容接口 ``/embeddings`` 计算
* ``local_hash`` —— 纯 Python 哈希向量兜底（零依赖，永不失败，召回质量有限）

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
import math
import platform
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field

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
    return (bool(data.get("ok")), str(data.get("detail") or ""))


def _write_probe_cache(result: tuple[bool, str]) -> None:
    import json
    import time as _t

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


class OnnxEmbedder(BaseEmbedder):
    """本地 ONNX 微型嵌入（bge-small-zh-q4）。仅在探针通过且模型存在时启用。"""

    source = "local_onnx"

    def __init__(self, model_path, dim: int = 512, model_name: str = "bge-small-zh-q4") -> None:
        super().__init__(dim)
        self.model_path = str(model_path)
        self.model = model_name
        self._session = None
        self._tokenizer = None

    def _lazy(self) -> None:
        if self._session is not None:
            return
        try:
            import numpy as np  # type: ignore
            import onnxruntime as ort  # type: ignore
        except BaseException as exc:  # noqa: BLE001 - 指令集非法必须在此兜住
            raise RuntimeError(f"onnxruntime 加载失败: {type(exc).__name__}: {exc}") from exc

        so = ort.SessionOptions()
        so.intra_op_num_threads = 2
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            self.model_path, sess_options=so, providers=["CPUExecutionProvider"]
        )
        self._np = np

    def embed(self, texts: list[str]) -> list[list[float]]:
        """无外置 tokenizer 时退回字符级哈希词表编码，保证接口可用。

        生产发行版会在 runtime/models/ 一并放置 tokenizer.json，届时替换为
        真实 WordPiece 编码即可，调用方无需改动。
        """
        self._lazy()
        np = self._np
        rows = []
        for t in texts:
            ids, mask = self._encode(t)
            rows.append((ids, mask))
        max_len = max(len(r[0]) for r in rows) if rows else 1
        input_ids = np.zeros((len(rows), max_len), dtype=np.int64)
        attn = np.zeros((len(rows), max_len), dtype=np.int64)
        for i, (ids, mask) in enumerate(rows):
            input_ids[i, :len(ids)] = ids
            attn[i, :len(mask)] = mask
        feed = {}
        names = {i.name for i in self._session.get_inputs()}
        if "input_ids" in names:
            feed["input_ids"] = input_ids
        if "attention_mask" in names:
            feed["attention_mask"] = attn
        if "token_type_ids" in names:
            feed["token_type_ids"] = np.zeros_like(input_ids)
        outputs = self._session.run(None, feed)
        last = outputs[0]
        # mean pooling
        m = attn[:, :, None].astype("float32")
        summed = (last * m).sum(axis=1)
        counts = np.clip(m.sum(axis=1), 1e-9, None)
        pooled = summed / counts
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        pooled = pooled / np.clip(norms, 1e-9, None)
        self.dim = pooled.shape[1]
        return pooled.astype("float32").tolist()

    def _encode(self, text: str, max_len: int = 256) -> tuple[list[int], list[int]]:
        """字符级 ID 派生（无 tokenizer 时的确定性近似编码）。"""
        t = (text or "")[:max_len]
        ids = [101] + [1 + (ord(ch) % 30000) for ch in t] + [102]
        return ids, [1] * len(ids)


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

    if requested in ("none", "offline", "disabled", "false", "0"):
        return EmbedderResolution(None, "none", notes=["已按配置关闭向量嵌入，仅使用 FTS5 词法检索"])

    # 1) local_onnx
    if requested == "local_onnx":
        ok, why = probe_onnxruntime()
        if ok and paths.ONNX_MODEL_FILE.exists():
            return EmbedderResolution(
                OnnxEmbedder(paths.ONNX_MODEL_FILE, dim, model_name), "local_onnx", notes=notes
            )
        if not ok:
            notes.append(f"本地 ONNX 嵌入引擎不可用（{_humanize_onnx_failure(why)}），已按降级链继续")
        else:
            notes.append(
                f"未找到本地模型文件 {paths.ONNX_MODEL_FILE.name}，已按降级链继续"
                "（可用 setup_runtime_windows.py --with-onnx 下载）"
            )
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
            return EmbedderResolution(emb, "ollama", warnings, notes)
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
        return EmbedderResolution(emb, "api", warnings, notes)
    if requested == "api" and not api_key:
        warnings.append("你指定使用云端 API 嵌入源，但未填写 api_key")
    if requested == "auto" and not api_key:
        notes.append("未配置 api_key，已跳过云端 API 嵌入源")

    # 4) 纯 Python 兜底
    if requested == "local_hash":
        return EmbedderResolution(HashEmbedder(dim), "local_hash", warnings, notes)

    warnings.append(
        "向量嵌入源全部不可用，已退化为纯 FTS5 词法检索（已有向量索引保留，恢复嵌入源后自动恢复）"
    )
    return EmbedderResolution(None, "none", warnings, notes)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
