"""Secret 脱敏：集中一处，避免各模块自己 `key[:4] + "****"`。

## 目标（刻意不做的事也说清楚）

本项目是 Local-First 产品，**允许 API Key 以明文保存在本地 config.ini**——
用户自己管理自己的机器，硬上 Keychain / DPAPI / master password 属于过度设计。

要防的只有一件事：**明文可以存本地，但绝不意外向外泄露。** 具体是四条路径：

    Secret ──┬─► Git（误提交）
             ├─► 日志（含第三方异常回显）
             ├─► HTTP 响应（/api/config、/api/status）
             └─► 异常 / 诊断信息

## 为什么日志与界面的脱敏策略不同

* **界面**需要让用户认出「我填的是哪把 key」→ 保留尾 4 位：`sk-****abcd`
* **日志**没有这个需求 → 一律 `<redacted>`，**不保留任何片段**

日志会被贴到 issue、发给别人排障，多留一个字符就多一分风险。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

# 判定「这个配置键是不是敏感字段」
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|credential|authorization|bearer|private[_-]?key)",
    re.IGNORECASE,
)

# 敏感 HTTP 头（大小写无关地整头抹掉）
SECRET_HEADERS = frozenset({
    "authorization", "proxy-authorization", "x-api-key", "api-key",
    "x-auth-token", "cookie", "set-cookie",
})

MASK = "<redacted>"
_UI_TAIL = 4

# 常见 key 形态：sk-xxx / Bearer xxx / api_key=xxx / token: xxx
_KEY_PATTERNS = (
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{6,})"),
    re.compile(r"\b(sk-proj-[A-Za-z0-9_\-]{6,})"),
    re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9_\-.=]{8,})"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*([^\s,;'\"]{6,})"),
    # URL 里的 userinfo：https://user:pass@host
    re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/@\s]{1,64})@"),
)


def is_secret_key(name: str) -> bool:
    """配置键名是否敏感（用于遍历配置时决定是否脱敏）。"""
    return bool(_SECRET_KEY_RE.search(name or ""))


def redact_secret(value: str, *, keep_tail: int = _UI_TAIL) -> str:
    """给**界面**用的脱敏：保留尾几位，便于用户辨认。

    空值原样返回空串（否则界面会显示一个「已设置」的假象）。
    """
    v = str(value or "")
    if not v:
        return ""
    if len(v) <= keep_tail:
        return MASK
    return f"{v[:3]}****{v[-keep_tail:]}" if len(v) > keep_tail + 3 else f"****{v[-keep_tail:]}"


def redact_for_log(value: str) -> str:
    """给**日志**用的脱敏：一律 ``<redacted>``，不保留任何片段。

    日志会被贴到 issue / 发给别人排障 —— 多留一个字符就多一分风险。
    """
    return MASK if str(value or "") else ""


def sanitize_headers(headers) -> dict:
    """把 headers 里的敏感头整体替换为 ``<redacted>``。"""
    out: dict = {}
    for k, v in dict(headers or {}).items():
        out[k] = MASK if str(k).lower() in SECRET_HEADERS else v
    return out


def sanitize_text(text: str) -> str:
    """清洗**任意文本**中的密钥痕迹。

    用于「第三方异常回显」这类容易漏的路径：上游服务可能把
    ``Authorization: Bearer sk-xxx`` 原样写进错误信息，哪怕这段字符串不是
    我们拼的，也不能直接落日志或回给前端。
    """
    s = str(text or "")
    if not s:
        return s
    for i, rx in enumerate(_KEY_PATTERNS):
        if i == 2:                        # bearer <token>
            s = rx.sub(lambda m: f"{m.group(1)} {MASK}", s)
        elif i == 4:                      # scheme://user:pass@host
            s = rx.sub(lambda m: f"{m.group(1)}{MASK}@", s)
        else:
            s = rx.sub(MASK, s)
    return s


def sanitize_url_userinfo(url: str) -> str:
    """去掉 URL 里的 ``user:pass@``（base_url 可能被写成带凭据的形式）。"""
    raw = str(url or "")
    if "@" not in raw:
        return raw
    try:
        u = urlsplit(raw)
    except ValueError:
        return sanitize_text(raw)
    if not u.username and not u.password:
        return raw
    host = u.hostname or ""
    if u.port:
        host = f"{host}:{u.port}"
    return urlunsplit((u.scheme, host, u.path, u.query, u.fragment))


def mask_config(config_map: dict) -> dict:
    """把整份配置里的敏感字段换成脱敏值。

    ⚠ 用于**写回**时也要小心：脱敏值不能被当成新值存回配置，
    见 :func:`is_masked`。
    """
    out: dict = {}
    for section, items in dict(config_map or {}).items():
        if isinstance(items, dict):
            out[section] = {
                k: (redact_secret(v) if is_secret_key(k) else v)
                for k, v in items.items()
            }
        else:
            out[section] = items
    return out


def is_masked(value: str) -> bool:
    """判断一个值是否是**脱敏占位**。

    设置保存时必须跳过这类值 —— 否则「用户只改了别的字段、前端把
    ``sk-****abcd`` 一起提交上来」就会把真 key 覆盖成那串星号。
    """
    v = str(value or "")
    if not v:
        return False
    return MASK in v or ("****" in v and len(v) < 40)
