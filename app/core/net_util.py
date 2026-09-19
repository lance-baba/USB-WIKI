"""标准库 HTTP 工具 —— 零第三方依赖即可完成 LLM / Embedding / 抓取的网络交互。

统一负责：系统代理环境变量、certifi CA 包、UTF-8 编解码、超时与流式读取。
"""
from __future__ import annotations

import json
import os
import select
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def ssl_context() -> ssl.SSLContext:
    """优先使用 certifi CA 包，缺失时回退系统证书库。"""
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        try:
            return ssl.create_default_context()
        except Exception:  # noqa: BLE001
            return ssl._create_unverified_context()  # noqa: SLF001


def proxies() -> dict[str, str]:
    """读取系统代理环境变量（PRD 4.2 要求自动读取）。"""
    out: dict[str, str] = {}
    for scheme in ("http", "https"):
        val = os.environ.get(f"{scheme.upper()}_PROXY") or os.environ.get(
            f"{scheme}_proxy"
        )
        if val:
            out[scheme] = val
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
    if no_proxy:
        out["no"] = no_proxy
    return out


def _build_opener(with_proxy: bool = True) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = [
        urllib.request.HTTPSHandler(context=ssl_context())
    ]
    if with_proxy:
        handlers.append(urllib.request.ProxyHandler(proxies() or {}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def http_get(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
    with_proxy: bool = True,
) -> tuple[int, bytes, dict[str, str]]:
    hdrs = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    opener = _build_opener(with_proxy)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read()
        except Exception:  # noqa: BLE001
            pass
        return exc.code, body, dict(exc.headers or {})
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        # 连接拒绝 / DNS 失败 / 超时：一律折算为 status=0，绝不向上抛异常
        return 0, str(exc).encode("utf-8", "replace"), {}


def http_post_json(
    url: str,
    payload: dict,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    with_proxy: bool = True,
) -> tuple[int, dict | str]:
    hdrs = {
        "User-Agent": DEFAULT_UA,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    hdrs.update(headers or {})
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    opener = _build_opener(with_proxy)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return 0, f"{type(exc).__name__}: {exc}"


class StreamError(Exception):
    """流式中止的明确原因（P0-B：禁止静默吞掉真实网络错误）。

    调用方应把 ``kind`` 透出到诊断信息，而不是笼统报「推流中断」。
    允许的取值（仅作建议，调用方可自行扩展）：

    * ``OLLAMA_HTTP_ERROR``        —— 服务端返回了非 2xx 状态码
    * ``OLLAMA_CONNECTION_CLOSED`` —— 连接建立失败或中途被对端关闭
    * ``OLLAMA_FIRST_TOKEN_TIMEOUT`` —— 建连后迟迟不出第一个 token（本地模型首载权重）
    * ``OLLAMA_STREAM_TIMEOUT``    —— 流式吐字过程中长时间无新内容
    * ``OLLAMA_INVALID_STREAM``    —— 收到的数据流无法解析
    """

    def __init__(self, kind: str, message: str):
        self.kind = kind
        self.message = message
        super().__init__(f"{kind}: {message}")


def _safe_err(exc) -> str:
    s = str(exc)
    return s[:200] if s else type(exc).__name__


def _socket_of(resp) -> "socket.socket | None":
    """从 urllib 的 HTTPResponse 中取出底层真实 socket。

    ``resp.fp`` 是 ``BufferedReader``，其 ``.raw`` 是 ``SocketIO``，而真正的
    ``socket.socket`` 挂在 ``SocketIO._sock`` 上。只有对真实 socket 才能调
    ``settimeout`` 实现「逐读超时」—— 直接对 ``SocketIO``/``BufferedReader``
    调 ``settimeout`` 会抛 ``AttributeError``（正是真实 Ollama 跑挂的根因）。
    取不到时返回 ``None``，调用方退化为「不控制逐读超时」的普通 readline。
    """
    fp = getattr(resp, "fp", None)
    if fp is None:
        return None
    raw = getattr(fp, "raw", None)
    if raw is not None:
        s = getattr(raw, "_sock", None)
        if s is not None:
            return s
    return getattr(fp, "_sock", None)


def http_post_stream(
    url: str,
    payload: dict,
    headers: dict[str, str] | None = None,
    connect_timeout: float = 10.0,
    first_token_timeout: float = 60.0,
    idle_timeout: float = 30.0,
    with_proxy: bool = False,
) -> Iterator[str]:
    """SSE / 换行分隔 JSON 流式 POST，逐行产出，并区分明确的失败原因（P0-B）。

    三档超时对应本地模型两类「慢」：
    * ``connect_timeout``        —— 建立 TCP/TLS 连接的最长等待（连接慢 ≠ 模型慢，故短）。
    * ``first_token_timeout``    —— 建连后到首个数据帧的最长等待。本地模型首次加载权重
                                   可能很慢，必须给足（默认 120s），不能 30s 就误判不可用。
    * ``idle_timeout``           —— 两帧之间的空闲上限，流式吐字中若长时间无新 token 才判超时。

    任何真实网络错误都会以 :class:`StreamError` 抛出（携带 ``kind``），**绝不再静默吞掉**。
    注意：本地 Ollama 走 ``with_proxy=False``，避免系统代理劫持 127.0.0.1。
    """
    hdrs = {
        "User-Agent": DEFAULT_UA,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Connection": "close",
    }
    hdrs.update(headers or {})
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    opener = _build_opener(with_proxy)

    try:
        resp = opener.open(req, timeout=connect_timeout)
    except urllib.error.HTTPError as exc:
        raise StreamError("OLLAMA_HTTP_ERROR", f"HTTP {exc.code}")
    except (urllib.error.URLError, OSError, TimeoutError, socket.timeout) as exc:
        raise StreamError("OLLAMA_CONNECTION_CLOSED", f"连接失败：{_safe_err(exc)}")

    # 拿到底层真实 socket 以便逐读设置超时；取不到就退化为普通 readline。
    sock = _socket_of(resp)
    try:
        first = True
        while True:
            try:
                timeout = first_token_timeout if first else idle_timeout
                if sock is not None:
                    sock.settimeout(timeout)
                line = resp.readline()
            except socket.timeout:
                raise StreamError(
                    "OLLAMA_FIRST_TOKEN_TIMEOUT" if first else "OLLAMA_STREAM_TIMEOUT",
                    "本地模型首 token 超时" if first else "流式响应中断（长时间无新内容）",
                )
            except OSError as exc:
                raise StreamError("OLLAMA_CONNECTION_CLOSED", f"连接中断：{_safe_err(exc)}")

            if not line:
                return  # EOF：正常结束
            # line 已含结尾 \n。Ollama 用裸 \n 分隔 JSON，OpenAI 兼容用 `data:` 前缀，原样产出。
            yield line.decode("utf-8", "replace")
            first = False
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def normalize_base(url: str) -> str:
    return (url or "").strip().rstrip("/")


def join_url(base: str, path: str) -> str:
    return normalize_base(base) + "/" + path.lstrip("/")


def is_local_host(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return False
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")
