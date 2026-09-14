"""标准库 HTTP 工具 —— 零第三方依赖即可完成 LLM / Embedding / 抓取的网络交互。

统一负责：系统代理环境变量、certifi CA 包、UTF-8 编解码、超时与流式读取。
"""
from __future__ import annotations

import json
import os
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


def http_post_stream(
    url: str,
    payload: dict,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
    idle_timeout: float = 30.0,
    with_proxy: bool = False,
) -> Iterator[str]:
    """SSE 风格流式 POST，逐行产出；空闲超过 idle_timeout 秒则中断（PRD 4.4）。

    注意：本地 Ollama 走 with_proxy=False，避免系统代理劫持 127.0.0.1。
    """
    hdrs = {
        "User-Agent": DEFAULT_UA,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    hdrs.update(headers or {})
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    opener = _build_opener(with_proxy)
    with opener.open(req, timeout=timeout) as resp:
        while True:
            try:
                line = resp.readline()
            except (TimeoutError, OSError):
                return
            if not line:
                return
            yield line.decode("utf-8", "replace")


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
