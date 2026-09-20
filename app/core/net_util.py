"""标准库 HTTP 工具 —— 零第三方依赖即可完成 LLM / Embedding / 抓取的网络交互。

统一负责：系统代理环境变量、certifi CA 包、UTF-8 编解码、超时与流式读取。
"""
from __future__ import annotations

import http.client
import json
import os
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


def _proxy_for(scheme: str, host: str) -> "tuple[str, int, str | None] | None":
    """按环境变量解析该目标主机应使用的代理；命中 no_proxy 或未配置则返回 None。"""
    p = proxies()
    no = p.get("no", "")
    if no:
        h = (host or "").lower()
        for item in no.split(","):
            it = item.strip().lower()
            if not it:
                continue
            if it == "*":
                return None
            it = it.lstrip(".")
            if h == it or h.endswith("." + it):
                return None
    raw = p.get(scheme) or p.get("http")
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    u = urllib.parse.urlparse(raw)
    if not u.hostname:
        return None
    auth = None
    if u.username:
        import base64

        cred = f"{u.username}:{u.password or ''}".encode("utf-8")
        auth = "Basic " + base64.b64encode(cred).decode("ascii")
    return u.hostname, int(u.port or 80), auth


def _set_sock_timeout(sock, timeout: float) -> None:
    """给真实 socket 设超时。

    ⚠ 调用方必须在 ``getresponse()`` **之前** 捕获 socket：当响应是 close-delimited
    （HTTP/1.0 / 无 Content-Length / 带 Connection: close）时，``getresponse()`` 会
    ``close()`` 掉连接并把 ``conn.sock`` 置 None —— 之后再想设超时就只能拿到 None，
    于是「首 token / 空闲超时」全部静默失效（这正是真实 Ollama 明明在吐字却被判
    「长时间无新内容」的根因之一）。捕获到的 socket 对象本身仍可用（fd 被响应缓冲
    持有），对它 setttimeout 有效。
    """
    if sock is None:
        return
    try:
        sock.settimeout(timeout)
    except OSError:
        pass


def http_post_stream(
    url: str,
    payload: dict,
    headers: dict[str, str] | None = None,
    connect_timeout: float = 10.0,
    first_token_timeout: float = 120.0,
    idle_timeout: float = 60.0,
    with_proxy: bool = False,
) -> Iterator[str]:
    """SSE / 换行分隔 JSON 流式 POST，逐行产出，并区分明确的失败原因（P0-2）。

    ⚠ 三档超时必须**真正彼此独立**：

    * ``connect_timeout``      —— 仅建立 TCP/TLS 连接（含代理 CONNECT）。
    * ``first_token_timeout``  —— 建连后到**首个响应头 / 首个数据帧**的等待。本地模型
      首次加载权重（CPU/GPU offload、冷启动）经常远超 10s，必须给足。

      旧实现用 ``opener.open(req, timeout=10)``：urllib 会把同一个 timeout 同时用于
      **建连和等待响应头**，于是「模型正在加载、还没回响应头」被误判成
      ``OLLAMA_CONNECTION_CLOSED / timed out`` —— 模型明明是好的。这里改用
      ``http.client`` 显式分阶段调 socket 超时，从根上把这三档拆开。

    * ``idle_timeout``         —— 两帧之间的空闲上限（流式吐字中才生效）。

    本地 Ollama（127.0.0.1 / localhost）走 ``with_proxy=False``，绝不经过系统代理。
    任何真实网络错误都以 :class:`StreamError` 抛出（携带 ``kind``），绝不静默吞掉。
    """
    hdrs = {
        "User-Agent": DEFAULT_UA,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Connection": "close",
    }
    hdrs.update(headers or {})
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    # ⚠ 用低层 putrequest/putheader/endheaders 时 **必须自己写 Content-Length**：
    #   HTTPConnection.request() 会自动补，但低层 API 不会 —— 少了它，Ollama 直接
    #   回 HTTP 400 Bad Request（实测所有模型全部 400）。
    if not any(k.lower() == "content-length" for k in hdrs):
        hdrs["Content-Length"] = str(len(data))

    u = urllib.parse.urlparse(url)
    scheme = (u.scheme or "http").lower()
    if scheme not in ("http", "https"):
        raise StreamError("OLLAMA_CONNECTION_CLOSED", f"不支持的协议：{scheme or '（空）'}")
    host = u.hostname or ""
    if not host:
        raise StreamError("OLLAMA_CONNECTION_CLOSED", "URL 缺少主机名")
    port = int(u.port or (443 if scheme == "https" else 80))
    path = u.path or "/"
    if u.query:
        path += "?" + u.query

    ctx = ssl_context() if scheme == "https" else None
    proxy = _proxy_for(scheme, host) if with_proxy else None

    conn = None
    sock = None
    try:
        if proxy:
            phost, pport, pauth = proxy
            if pauth:
                hdrs["Proxy-Authorization"] = pauth
            if scheme == "https":
                # 经代理访问 https：连代理 → CONNECT 隧道 → TLS 到目标
                conn = http.client.HTTPSConnection(phost, pport, timeout=connect_timeout,
                                                   context=ctx)
                conn.set_tunnel(host, port)
            else:
                conn = http.client.HTTPConnection(phost, pport, timeout=connect_timeout)
                path = url                       # 明文走代理要绝对 URL
        elif scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=connect_timeout, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=connect_timeout)
        conn.connect()                               # 只有这一步受 connect_timeout 约束
        # 关键：**在 getresponse() 之前**留下一份 socket 引用（见 _set_sock_timeout 注释）
        sock = conn.sock
    except (socket.timeout, OSError, http.client.HTTPException) as exc:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        raise StreamError("OLLAMA_CONNECTION_CLOSED", f"连接失败：{_safe_err(exc)}")

    first = True
    try:
        # —— 阶段二：发送请求 + 等待响应头（模型加载可能很久 → first_token_timeout）——
        try:
            _set_sock_timeout(sock, first_token_timeout)
            conn.putrequest("POST", path, skip_accept_encoding=True)
            for k, v in hdrs.items():
                conn.putheader(k, v)
            conn.endheaders(data)
            resp = conn.getresponse()
        except socket.timeout:
            raise StreamError("OLLAMA_FIRST_TOKEN_TIMEOUT", "等待响应头超时（模型可能正在加载权重）")
        except (OSError, http.client.HTTPException) as exc:
            raise StreamError("OLLAMA_CONNECTION_CLOSED", f"请求发送/等待响应失败：{_safe_err(exc)}")

        if resp.status >= 400:
            raise StreamError("OLLAMA_HTTP_ERROR", f"HTTP {resp.status}")

        # —— 阶段三：逐行读取（首帧用 first_token_timeout，其后用 idle_timeout）——
        while True:
            try:
                _set_sock_timeout(sock, first_token_timeout if first else idle_timeout)
                line = resp.readline()
            except socket.timeout:
                raise StreamError(
                    "OLLAMA_FIRST_TOKEN_TIMEOUT" if first else "OLLAMA_STREAM_TIMEOUT",
                    "本地模型首 token 超时" if first else "流式响应中断（长时间无新内容）",
                )
            except OSError as exc:
                raise StreamError("OLLAMA_CONNECTION_CLOSED", f"连接中断：{_safe_err(exc)}")
            if not line:
                return                               # EOF：正常结束
            # line 含结尾 \n。Ollama 用裸 \n 分隔 JSON，OpenAI 兼容用 `data:` 前缀，原样产出。
            yield line.decode("utf-8", "replace")
            first = False
    finally:
        try:
            if conn is not None:
                conn.close()
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
