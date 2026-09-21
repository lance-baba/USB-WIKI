"""双模 AI 网关与流式闭环（PRD 4.4 / A4.1 产品契约）。

## AI 能力三层（V1 冻结）

    Level 1  基础知识库      启动 / 导入 / 查看 / FTS 检索 / 引用 —— **永不依赖 LLM**
    Level 2  本地语义检索    embedding 来源链（见 embedder.resolve）
    Level 3  生成式对话      本模块。**聊天模型由用户选择，Core 不预设任何具体模型**

## 路由拓扑

    auto    ──► Ollama「就绪」（在线 + 已选模型 + 模型确实在本机）──► 本地 Ollama
                        │否
                        └──► 配置了 api_key ──► 云端 OpenAI 兼容 API
                                            └──► 纯离线检索回答（provider=offline）
    ollama  ──► 只走本地；不就绪则离线（**用户明确要求不上云，故绝不改走 api**）
    api     ──► 直连云端；未配 key 报错
    offline ──► 纯离线

⚠ 铁律：**Ollama 进程活着 ≠ 可以对话**。必须有用户选择的、且本机存在的模型，
才允许 provider=ollama。也**不允许**在用户没选时自动挑 `models[0]` ——
那等于替用户拍板（可能是 embedding 模型 / vision 模型 / 资源超标的模型）。
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator

from . import config, net_util, search as search_mod
from .db import Database
from .log_util import get_logger

log = get_logger()

HEALTH_TTL = 60.0          # Ollama 健康状态缓存秒数（PRD 4.4）
PROBE_TIMEOUT = 3.0        # 健康探测超时
MAX_TOKENS = 2048          # 强制配置（PRD 4.4）
IDLE_TIMEOUT = 30.0        # 连续 30 秒无有效 Token 则中断

# ---------------------------------------------------------------------------
# Level 3 就绪状态（稳定枚举：API / 诊断 / 前端 / 测试共用，勿随意改名）
# ---------------------------------------------------------------------------
STATE_NO_RUNTIME = "no_runtime"                  # 本机没有可用的 Ollama 运行时
STATE_NO_MODEL = "no_model"                      # Ollama 在线，但一个模型都没装
STATE_SELECTION_REQUIRED = "selection_required"  # 有模型，但用户还没选对话模型
STATE_MODEL_MISSING = "model_missing"            # 选过的模型在本机已不存在
STATE_READY = "ready"                            # 已选择且本机可用

AI_STATES = (STATE_NO_RUNTIME, STATE_NO_MODEL, STATE_SELECTION_REQUIRED,
             STATE_MODEL_MISSING, STATE_READY)

_STATE_REASON = {
    STATE_NO_RUNTIME: "未检测到本地 Ollama 运行时",
    STATE_NO_MODEL: "Ollama 已启动，但本机尚未安装任何模型",
    STATE_SELECTION_REQUIRED: "本机已有模型，但尚未选择用哪个模型对话",
    STATE_MODEL_MISSING: "原来选择的模型在本机已不存在",
    STATE_READY: "已选择模型且本机可用",
}

SYSTEM_PROMPT = (
    "你是 Wiki-USB 随身知识库助手。严格依据下面提供的【资料片段】回答问题。\n"
    "\n"
    "【回答铁律】\n"
    "1. 每个事实都必须由资料片段**直接支持**。不得因为两件事出现在同一份资料里，"
    "就推断它们有因果关系、归属关系或影响关系。\n"
    "2. 如果用户问的是 A 的影响 / 原因 / 对象，只有当资料原文明确把 B 与 A 建立了关系，"
    "才能说「A 影响 B / B 属于 A」。同一资料里并列出现的其它事件（如不同的天气过程、"
    "不同的项目、不同的地区），即使与 A 同页，也**不是** A 的答案。\n"
    "3. 用户问 A 的影响 / 原因 / 对象，而资料没有明确建立这些关系时，回答必须先说明「现有资料没有明确说明」，"
    "并只陈述资料能确认的部分（如趋势、路径、时间）。**不要**用同文出现的其它内容补齐答案，"
    "也不要靠常识填补。\n"
    "4. 直接回答，不要出现「根据知识片段」「根据提供的知识片段」「从知识片段来看」这类措辞。\n"
    "5. 引用标注格式：在句末写 [1]、[2]（与片段编号对应）。不要写 [^1]、**1** 或 [1,2] 这类其它变体。\n"
    "\n"
    "回答使用简体中文，条理清晰，篇幅适中。"
)


@dataclass
class GatewayState:
    ollama_healthy: bool = False
    ollama_checked_at: float = 0.0
    ollama_detail: str = "未探测"
    #: 上次探测到的已安装模型名（与健康状态同一次请求取得，故同一 TTL）
    ollama_models: list[str] = field(default_factory=list)
    last_provider: str = "offline"
    last_error: str = ""
    warnings: list[str] = field(default_factory=list)


def _fmt_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _parse_tags(body) -> tuple[list[dict], bool]:
    """解析 `/api/tags` 响应 → (models, ok)。ok=False 表示返回内容无法解析。"""
    try:
        data = json.loads(bytes(body).decode("utf-8", "replace"))
    except (ValueError, AttributeError, TypeError):
        return [], False
    models: list[dict] = []
    for m in data.get("models") or []:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or m.get("model") or "").strip()
        if not name:
            continue
        details = m.get("details") or {}
        models.append(
            {
                "name": name,
                "size": m.get("size"),
                "size_text": _fmt_size(m.get("size")),
                "modified": str(m.get("modified_at") or "")[:19].replace("T", " "),
                "family": str(details.get("family") or ""),
                "params": str(details.get("parameter_size") or ""),
                "quant": str(details.get("quantization_level") or ""),
            }
        )
    models.sort(key=lambda x: x["name"])
    return models, True


def _model_present(selected: str, installed: list[str]) -> bool:
    """本地是否真的有这个模型。

    只做一种宽松匹配：省略 tag 时补 Ollama 的默认 tag `:latest`
    （写 `foo` 命中 `foo:latest`，与 Ollama 自身的解析一致）。

    ⚠ **不做「前缀相同就算存在」**：装的是 `foo:1b` 而配的是 `foo` 时，
    Ollama 实际会去找 `foo:latest` 并失败。按前缀算存在会让状态假装 ready，
    然后调用时撞墙 —— 宁可老实报 `model_missing`，让用户重新选一次。
    """
    if not selected or not installed:
        return False
    if selected in installed:
        return True
    if ":" not in selected:
        return f"{selected}:latest" in installed
    return False


def _query_window(text: str, query: str, limit: int) -> str:
    """在 limit 字符内**优先给出与查询最相关的片段**，而不是无脑从头截。

    章节开头常是过渡句，真正相关的往往在中间。这里用查询实词定位首次出现处，
    取其前后窗口；一个词都没命中才退回开头。
    """
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    terms = []
    try:
        terms = search_mod.content_terms(query)
    except Exception:  # noqa: BLE001
        terms = [w for w in (query or "").split() if len(w) >= 2]
    pos = -1
    for term in sorted(terms, key=len, reverse=True):
        pos = t.find(term)
        if pos >= 0:
            break
    if pos < 0:
        return t[:limit] + "…"
    start = max(0, pos - limit // 3)
    out = t[start:start + limit]
    return ("…" if start > 0 else "") + out + ("…" if start + limit < len(t) else "")


#: 高亮哨兵：前端在 **HTML 转义之后** 把这两个控制字符替换成 <mark>/</mark>。
#: 为什么不直接塞 <mark>：前端会对整段文本做 HTML 转义（防 XSS），直接塞标签会被
#: 转义成字面量。哨兵是控制字符，转义不动它，前端再替换 —— 既高亮又不破坏转义。
_HL_OPEN = "\u0001"
_HL_CLOSE = "\u0002"


def _highlight(text: str, terms: list[str]) -> str:
    """把命中关键词包上高亮哨兵（长词优先 + 单次扫描，避免短词把长词切碎/嵌套）。"""
    if not text or not terms:
        return text
    uniq = sorted({t for t in terms if t}, key=len, reverse=True)
    if not uniq:
        return text
    pattern = "|".join(re.escape(t) for t in uniq)
    try:
        return re.sub(pattern, lambda m: _HL_OPEN + m.group(0) + _HL_CLOSE,
                      text, flags=re.IGNORECASE)
    except re.error:  # noqa: BLE001 - 极端词形导致编译失败时不高亮，不影响展示
        return text


_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _table_excerpt(content: str, terms: list[str], max_rows: int = 6) -> str:
    """若查询命中 Markdown 表格 → 返回「表头 + 命中行 + 相邻 ≤1 行」。

    刻意**不按 300 字截断** —— 职责/姓名这类映射被砍掉一半比长一点更糟。
    未命中任何表格行时返回空串，调用方回退到普通窗口摘录。
    """
    if "|" not in content or not terms:
        return ""
    lines = content.splitlines()
    n = len(lines)
    i = 0
    while i < n - 1:
        head, sep = lines[i], lines[i + 1]
        if "|" in head and _TABLE_SEP_RE.match(sep or ""):
            j = i + 2
            rows: list[str] = []
            while j < n and "|" in lines[j]:
                rows.append(lines[j])
                j += 1
            hit = [k for k, r in enumerate(rows) if any(t in r for t in terms)]
            if hit:
                keep: set[int] = set()
                for k in hit:
                    for d in (-1, 0, 1):
                        kk = k + d
                        if 0 <= kk < len(rows):
                            keep.add(kk)
                body = [rows[k] for k in sorted(keep)]
                if len(body) > max_rows:
                    body = body[:max_rows]
                return "\n".join([head, sep, *body])
            i = j
        else:
            i += 1
    return ""


class Gateway:
    def __init__(self, db: Database | None = None, embedder=None) -> None:
        self.db = db
        self.embedder = embedder
        self.state = GatewayState()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    @property
    def ollama_host(self) -> str:
        return net_util.normalize_base(config.get_str("AI", "ollama_host", "http://127.0.0.1:11434"))

    @property
    def ollama_model(self) -> str:
        """用户选定的本地对话模型。

        **没有默认值**：产品尚未拍板推荐任何模型，Core 不得替用户预设一个 ——
        新安装时它必须是空串，由用户在设置页从本机已装模型里选。
        """
        return config.get_str("AI", "ollama_chat_model", "")

    @property
    def api_base(self) -> str:
        return net_util.normalize_base(config.get_str("AI", "api_base_url", "https://api.deepseek.com/v1"))

    @property
    def api_key(self) -> str:
        return config.get_str("AI", "api_key", "")

    @property
    def api_model(self) -> str:
        return config.get_str("AI", "api_chat_model", "deepseek-chat")

    # ------------------------------------------------------------------
    def _probe_tags(self, timeout: float, host: str | None = None,
                    update_cache: bool = True) -> tuple[bool, list[dict], str]:
        """GET `/api/tags` → (available, models, detail)，并刷新健康 + 模型缓存。

        健康与模型列表来自**同一次请求** —— 分开探测会出现「在线但列表过期」的窗口。
        """
        base = net_util.normalize_base(host or self.ollama_host)
        status, body, _headers = net_util.http_get(
            net_util.join_url(base, "/api/tags"), timeout=timeout, with_proxy=False
        )
        if status != 200:
            detail = (
                f"连接失败（HTTP {status}）" if status
                else "连不上 Ollama，请确认它已启动（命令行执行 ollama serve）"
            )
            available, models = False, []
        else:
            models, parsed = _parse_tags(body)
            available = bool(parsed)
            detail = (f"在线，发现 {len(models)} 个模型" if parsed
                      else "返回内容无法解析")
        if update_cache:
            with self._lock:
                self.state.ollama_healthy = available
                self.state.ollama_checked_at = time.time()
                self.state.ollama_detail = detail
                self.state.ollama_models = [m["name"] for m in models]
        return available, models, detail

    def ollama_status(self, force: bool = False) -> bool:
        """带 60 秒缓存的 Ollama 健康探测（PRD 4.4 健康状态缓存机）。

        ⚠ 「健康」仅表示 `/api/tags` 可访问，**不代表可以对话** ——
        是否可对话由 :meth:`ai_readiness` 判断。
        """
        now = time.time()
        with self._lock:
            fresh = (now - self.state.ollama_checked_at) < HEALTH_TTL
            if fresh and not force:
                return self.state.ollama_healthy
        available, _models, _detail = self._probe_tags(PROBE_TIMEOUT)
        log.debug("Ollama 探测: %s", self.state.ollama_detail)
        return available

    # ------------------------------------------------------------------
    def list_ollama_models(self, host: str | None = None) -> dict:
        """读取本机 Ollama 已安装的模型列表（供前端下拉选择）。

        一次成功的调用同时被视作健康探测，会刷新 60s 健康缓存。
        传入自定义 *host* 时只探测该地址，不污染默认主机的缓存。
        """
        base = net_util.normalize_base(host or self.ollama_host)
        available, models, detail = self._probe_tags(
            6.0, host=base, update_cache=(host is None)
        )
        if not available:
            return {"available": False, "host": base, "models": [], "detail": detail}
        return {
            "available": True,
            "host": base,
            "models": models,
            "detail": f"已发现 {len(models)} 个本地模型" if models
                      else "Ollama 在线，但本机尚未安装任何模型",
        }

    # ------------------------------------------------------------------
    def ai_readiness(self, *, probe: bool = False) -> dict:
        """统一的 AI 就绪状态 —— **不是** `ollama_healthy` 的别名。

        `probe=False`（默认，供 /api/status 与诊断）**只读缓存、不发网络请求**：
        status 在冒烟测试里被高频轮询，探测不能拖慢首屏。
        `probe=True`（设置页 / 显式自测）现场刷新一次。
        """
        if probe:
            self.ollama_status(force=True)
        with self._lock:
            probed = self.state.ollama_checked_at > 0
            available = bool(probed and self.state.ollama_healthy)
            names = list(self.state.ollama_models) if available else []
            detail = self.state.ollama_detail

        selected = self.ollama_model
        installed = _model_present(selected, names)

        if not available:
            state = STATE_NO_RUNTIME
        elif not names:
            state = STATE_NO_MODEL
        elif not selected:
            state = STATE_SELECTION_REQUIRED
        elif not installed:
            state = STATE_MODEL_MISSING
        else:
            state = STATE_READY

        reason = _STATE_REASON[state]
        if state == STATE_MODEL_MISSING:
            reason = f"{reason}：{selected}"
        elif state == STATE_SELECTION_REQUIRED:
            reason = f"{reason}（本机发现 {len(names)} 个）"
        elif state == STATE_NO_RUNTIME and not probed:
            reason = "尚未探测到本地 Ollama（后台探测中）"

        api_key = self.api_key
        api_base = self.api_base
        return {
            "state": state,
            "reason": reason,
            "chat_ready": state == STATE_READY,
            "ollama_available": available,
            "ollama_probed": probed,
            "ollama_detail": detail,
            "installed_models": names,
            "installed_model_count": len(names),
            "selected_model": selected or None,
            "selected_model_installed": bool(installed),
            "provider_mode": config.get_str("AI", "provider", "auto").lower(),
            "api_key_configured": bool(api_key),
            "cloud_api_configured": bool(api_key and api_base),
        }

    def test_ollama(self, model: str | None = None) -> dict:
        """真实跑一次对话探针，验证模型真的可用（而不只是进程活着）。"""
        name = (model or self.ollama_model).strip()
        if not name:
            return {
                "ok": False,
                "target": "ollama",
                "detail": "尚未选择本地对话模型 —— 请先在「设置 → AI → 本地对话模型」"
                          "里从本机已安装的模型中选择一个",
            }
        info = self.list_ollama_models()
        if not info["available"]:
            return {"ok": False, "target": "ollama", "detail": info["detail"]}

        installed = {m["name"] for m in info["models"]}
        if installed and not _model_present(name, sorted(installed)):
            return {
                "ok": False,
                "target": "ollama",
                "detail": f"Ollama 在线，但本机没有模型「{name}」。"
                          f"本机已装：{', '.join(sorted(installed)[:6]) or '（无）'}",
                "models": sorted(installed),
            }

        status, data = net_util.http_post_json(
            net_util.join_url(self.ollama_host, "/api/chat"),
            {
                "model": name,
                "messages": [{"role": "user", "content": "回复两个字：可用"}],
                "stream": False,
                "options": {"num_predict": 8},
            },
            timeout=45.0,
            with_proxy=False,
        )
        if status != 200:
            msg = data.get("error") if isinstance(data, dict) else data
            return {"ok": False, "target": "ollama", "detail": f"调用失败：{str(msg)[:160]}"}
        reply = ""
        if isinstance(data, dict):
            reply = ((data.get("message") or {}).get("content") or data.get("response") or "").strip()
        return {
            "ok": True,
            "target": "ollama",
            "detail": f"模型「{name}」可用" + (f"，返回：{reply[:30]}" if reply else ""),
        }

    def test_api(self) -> dict:
        """验证云端 OpenAI 兼容接口 + API Key 是否真的能调通。"""
        if not self.api_key:
            return {"ok": False, "target": "api", "detail": "未填写 api_key"}
        headers = {"Authorization": f"Bearer {self.api_key}"}

        # 先试 /models（不消耗 token），部分服务商不支持则退化为 1 token 对话
        status, _body, _h = net_util.http_get(
            net_util.join_url(self.api_base, "/models"), headers=headers, timeout=15.0
        )
        if status == 200:
            return {"ok": True, "target": "api", "detail": "接口与 API Key 校验通过（/models 可用）"}
        if status in (401, 403):
            return {"ok": False, "target": "api", "detail": f"API Key 被拒绝（HTTP {status}），请检查是否复制完整"}

        status2, data2 = net_util.http_post_json(
            net_util.join_url(self.api_base, "/chat/completions"),
            {
                "model": self.api_model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "stream": False,
            },
            headers=headers,
            timeout=30.0,
        )
        if status2 == 200:
            return {"ok": True, "target": "api", "detail": f"连通成功，模型「{self.api_model}」可调用"}
        msg = data2
        if isinstance(data2, dict) and isinstance(data2.get("error"), dict):
            msg = data2["error"].get("message")
        return {
            "ok": False,
            "target": "api",
            "detail": f"调用失败（HTTP {status2 or '连接错误'}）：{str(msg)[:180]}",
        }

    def resolve_provider(self) -> tuple[str, list[str]]:
        """按配置与**就绪状态**解析本次请求的提供方 → (provider, warnings)。

        provider=ollama 的前提是「在线 + 用户已选模型 + 模型确实在本机」三者同时成立；
        否则**不得**解析为 ollama（否则就是拿着不存在的模型名去撞墙，
        或替用户默认一个模型）。

        `provider_mode = ollama` 表示用户明确要求**只走本地**（不把内容送到云端），
        因此不就绪时降级为 offline，**绝不改走 api**。
        """
        mode = config.get_str("AI", "provider", "auto").lower()
        if mode in ("offline", "none", "local"):
            return "offline", []

        ready = self.ai_readiness()
        state, reason = ready["state"], ready["reason"]

        if mode == "ollama":
            if ready["chat_ready"]:
                return "ollama", []
            return "offline", [f"本地 Ollama 未就绪（{reason}），本次为纯离线检索回答"]

        if mode == "api":
            if self.api_key:
                return "api", []
            return "error", ["api 模式未配置 api_key"]

        # auto：本地就绪优先 → 已配置的云端 API → 纯离线
        if ready["chat_ready"]:
            return "ollama", []
        warns: list[str] = []
        if state == STATE_NO_RUNTIME:
            warns.append("Ollama 未在线，自动降级")
        else:
            warns.append(f"本地 Ollama 未就绪（{reason}）")
        if self.api_key:
            warns.append("已改用云端 API")
            return "api", warns
        if state == STATE_NO_RUNTIME:
            warns.append("未配置 API Key，本次降级为纯离线 FTS5 检索回答")
        else:
            warns.append("未配置可用的对话模型与 API Key，本次为纯离线检索回答")
        return "offline", warns

    # ------------------------------------------------------------------
    def _degrade_target(self) -> str:
        """Ollama 真实调用失败后的降级去向（P0-B #5）。

        ``provider=ollama`` 是用户明确「只走本地」，失败**绝不转云端** → offline；
        只有 ``auto`` 才允许在配置了 api_key 时回落云端 API。
        """
        if config.get_str("AI", "provider", "auto").lower() == "ollama":
            return "offline"
        return "api" if self.api_key else "offline"

    def _ollama_fail_notice(self) -> str:
        mode = config.get_str("AI", "provider", "auto").lower()
        if mode == "ollama":
            return "本地 Ollama 调用失败，本次为纯离线检索回答（未转云端）"
        return "本地 Ollama 调用失败，已自动降级"

    # ------------------------------------------------------------------
    def retrieve(self, query: str) -> search_mod.SearchResult:
        top_k = config.get_int("AI", "top_k_parents", 5)
        cands = config.get_int("AI", "recall_candidates", 20)
        return search_mod.hybrid_search(
            self.db, self.embedder, query, top_k_parents=top_k, candidates=cands
        )

    @staticmethod
    def build_prompt(query: str, result: search_mod.SearchResult, history: list[dict] | None) -> str:
        """构造提示词。**注入的知识片段必须有硬预算**。

        为什么必须封顶：父块是「章节」粒度，实测单块可达 1200 字符；命中 5 块就是
        5000+ 字符（≈7.5k tokens）。本地小模型的上下文窗口与注意力都有限，
        不封顶要么溢出、要么被无关长文淹没 —— 而这恰恰是同级项目踩过并写进
        说明的一条经验（单段封顶 + 总量封顶）。这里做同样的事，且比它更贴题：
        每段**优先截取与查询词最接近的窗口**，而不是简单从头截。
        """
        per_limit = max(80, config.get_int("AI", "inject_per_parent_chars", 400))
        total_budget = max(200, config.get_int("AI", "inject_total_chars", 1800))
        # B3：概览/覆盖型问句（「X 是怎么样的」）要的是**整节**内容，按默认预算会把
        # 章节后半段（如「出现下列情况应加强监测」的整份条件列表）截掉，模型只能答
        # 「资料未明确列出」。这里对覆盖型做**有界放大**，仍然是硬封顶，不是无上限读全文。
        if getattr(result, "coverage", False):
            per_limit = max(per_limit, config.get_int("AI", "inject_per_parent_chars_coverage", 900))
            total_budget = max(total_budget,
                               config.get_int("AI", "inject_total_chars_coverage", 4000))

        parts: list[str] = ["【资料片段】"]
        if result.parents:
            used = 0
            for i, p in enumerate(result.parents, start=1):
                # A4：片段标号用 [i] —— 与回答要求的引用格式一致，模型会照着写 [1]；
                # 旧版写 [^1] 会被模型原样模仿，前端就得兼容 ^ 变体。
                head = f"[{i}] 来源：{p['title']}（{p['path']}）\n"
                room = min(per_limit, total_budget - used - len(head))
                if room < 80:
                    break                     # 预算用尽：宁可少给，不要塞爆
                body = _query_window(p.get("content") or "", query, room)
                parts.append(head + body)
                used += len(head) + len(body)
        else:
            parts.append("（检索无命中，知识库中暂无与该问题相关的切片）")
        parts.append("")

        if history:
            parts.append("【对话历史】")
            for turn in history[-6:]:
                role = "用户" if turn.get("role") == "user" else "助手"
                parts.append(f"{role}：{str(turn.get('content', ''))[:500]}")
            parts.append("")

        parts.append("【当前问题】")
        parts.append(query)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    def offline_notice(self) -> str:
        """离线回答时把「为什么没有 AI」讲清楚 —— 用户主动选纯离线时不废话。

        §9 硬要求：没有可用聊天模型时**必须明确说明**（而不是含糊的「调用了检索」），
        同时不能把检索结果伪装成大模型生成的回答。
        """
        if config.get_str("AI", "provider", "auto").lower() in ("offline", "none", "local"):
            return ""                      # 用户主动选择纯离线，不需要解释
        if self.api_key:
            return ""                      # 云端可用却没走通：上游已有专门 notice
        try:
            ready = self.ai_readiness()
        except Exception:  # noqa: BLE001 - 仅影响提示文案
            return ""
        if ready["chat_ready"]:
            return ""
        return (f"本地 AI 模型尚未配置（{ready['reason']}）：本次为纯离线检索回答，"
                "知识库检索与引用均不受影响。")

    def stream_chat(self, query: str, history: list[dict] | None = None) -> Iterator[dict]:
        """产出 SSE 数据帧字典：references → delta* → done / error。"""
        query = (query or "").strip()
        if not query:
            yield {"type": "error", "message": "问题不能为空"}
            return

        provider, warns = self.resolve_provider()
        self.state.last_provider = provider

        try:
            result = self.retrieve(query)
        except Exception as exc:  # noqa: BLE001
            log.error("检索失败: %s", exc)
            result = search_mod.SearchResult(query=query, route="like")

        refs = [
            {
                "id": r.id, "title": r.title, "path": r.path,
                # UX-1：界面「来源」用 display_source（导入文件→源文件名，剪藏/笔记→标题）
                "display_source": getattr(r, "display_source", "") or r.title,
                "snippet": r.snippet, "parent_id": r.parent_id,
                "score": r.score, "similarity": r.similarity,
                # A: 引用 stable anchor（源行范围）—— 前端据此定位，不再搜相似文字
                "source_start_line": getattr(r, "source_start_line", 0),
                "source_end_line": getattr(r, "source_end_line", 0),
            }
            for r in result.references
        ]
        # 首帧注入引用溯源字典（PRD 5.3）
        yield {"type": "references", "refs": refs}
        yield {
            "type": "meta", "provider": provider, "route": result.route,
            "counts": result.counts, "warnings": warns + result.warnings,
        }

        if provider == "error":
            yield {"type": "error", "message": warns[-1] if warns else "模型服务不可用"}
            yield {"type": "done"}
            return

        if provider == "ollama":
            # 显式流式状态机（P0-1）。旧实现有两个致命缺陷：
            #   ① delta 只记 got_content，**不计入「有效帧」**（got_frame 只由
            #      thinking/done 置位）；
            #   ② done=true 在 _stream_ollama 内部被直接 return，上层永远收不到。
            # 二者叠加 → 模型**已完整输出答案**后 got_frame 仍为 False，被误判成
            # 「未返回任何数据帧」→ 再追加一整套离线搜索结果。必须彻底拆开状态：
            #   stream_started 收到过任何有效帧（content / thinking / done）
            #   got_content    收到过正文
            #   got_done       收到 done=true
            #   stream_error   流式异常（连接中断 / 超时 / 服务端报错）
            stream_started = False
            got_content = False
            got_done = False
            stream_error = ""
            for frame in self._stream_ollama(query, history, result):
                ft = frame.get("type")
                if ft == "delta":
                    stream_started = True
                    got_content = True
                    yield frame
                elif ft == "ollama_progress":   # thinking 帧：流活着，但不算正文
                    stream_started = True
                elif ft == "ollama_done":
                    stream_started = True
                    got_done = True
                elif ft == "error":
                    stream_started = True
                    stream_error = frame.get("message", "") or "本地 Ollama 流式中止"
                    self.state.last_error = stream_error
                    break
                else:
                    yield frame

            # A. 有正文且无异常（含正常 EOF / 收到 done）→ 成功结束，绝不降级
            if got_content and not stream_error:
                yield {"type": "done"}
                return
            # B. 已有正文但连接异常结束（没等到 done）→ 保留已生成的答案 + 提示可能不完整，
            #    **绝不再追加离线全文**（否则「AI 答案 + 一整份离线答案」混成一坨）
            if got_content:
                log.warning("Ollama 流式在正文后异常结束：%s", stream_error)
                # 用 delta 追加**留在对话里**的提示（notice 只是 3.6s 的 toast，会被错过）
                yield {"type": "delta",
                       "content": "\n\n> ⚠ 本地模型连接提前结束，以上回答可能不完整。\n"}
                yield {"type": "done"}
                return
            # C. 完全没有正文 → 才允许降级（provider=ollama→offline；auto→api→offline）
            self.ollama_status(force=True)
            if stream_error:
                yield {"type": "error", "message": stream_error}
            elif not stream_started:
                yield {"type": "error", "message": "本地 Ollama 未返回任何数据帧"}
            else:
                yield {
                    "type": "error",
                    "message": "本地 Ollama 已完成请求，但未返回回答正文"
                    "（可能该模型不支持对话，或被 think/thinking 设置拦截）",
                }
            yield {"type": "notice", "message": self._ollama_fail_notice()}
            provider = self._degrade_target()

        if provider == "api":
            yielded = False
            for frame in self._stream_api(query, history, result):
                yielded = yielded or frame.get("type") == "delta"
                yield frame
                if frame.get("type") == "error":
                    self.state.last_error = frame.get("message", "")
                    break
            if yielded:
                yield {"type": "done"}
                return
            yield {"type": "notice", "message": "云端 API 调用失败，已降级为纯离线检索回答"}

        # 纯离线：先说清「为什么没有 AI」，再给检索结果（绝不伪装成模型生成）
        notice = self.offline_notice()
        if notice:
            yield {"type": "notice", "message": notice}
        for frame in self._offline_answer(query, result):
            yield frame
        yield {"type": "done"}

    # ------------------------------------------------------------------
    def _stream_ollama(self, query: str, history, result) -> Iterator[dict]:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for turn in (history or [])[-6:]:
            role = turn.get("role") if turn.get("role") in ("user", "assistant") else "user"
            messages.append({"role": role, "content": str(turn.get("content", ""))[:2000]})
        messages.append({"role": "user", "content": self.build_prompt(query, result, None)})

        payload = {
            "model": self.ollama_model,
            "messages": messages,
            "stream": True,
            # 知识库问答默认关闭 thinking：更快首 token、避免小模型无意义长思考、
            # 也避免 message.thinking 被前端误判为「无输出」。thinking 内容不展示给用户。
            "think": False,
            "options": {"num_predict": MAX_TOKENS, "temperature": 0.3},
        }
        url = net_util.join_url(self.ollama_host, "/api/chat")
        try:
            for line in net_util.http_post_stream(
                url, payload,
                connect_timeout=10.0,
                first_token_timeout=120.0,   # 本地模型首载权重可很慢，给足时间
                idle_timeout=60.0,           # 流式吐字中两帧间空闲上限
                with_proxy=False,
            ):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("error"):
                    yield {"type": "error", "message": f"Ollama 报错：{obj['error']}"}
                    return
                msg = obj.get("message") or {}
                piece = msg.get("content") or obj.get("response") or ""
                if piece:
                    yield {"type": "delta", "content": piece}
                elif msg.get("thinking") is not None:
                    # thinking 模型：思考内容不是正文，但证明流是活的 → 记一帧进度，
                    # 不让上层把它误判成「推流中断」。
                    yield {"type": "ollama_progress"}
                if obj.get("done"):
                    # 必须把「正常 done」明确传回上层（P0-1）：旧实现直接 return，
                    # 上层收不到 done，会把已完成的回答误判成失败。
                    yield {"type": "ollama_done"}
                    return
        except net_util.StreamError as exc:
            yield {
                "type": "error",
                "message": f"本地 Ollama 流式中止（{exc.kind}）：{exc.message}",
            }
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "message": f"Ollama 流式连接失败：{exc}"}

    def _stream_api(self, query: str, history, result) -> Iterator[dict]:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for turn in (history or [])[-6:]:
            role = turn.get("role") if turn.get("role") in ("user", "assistant") else "user"
            messages.append({"role": role, "content": str(turn.get("content", ""))[:2000]})
        messages.append({"role": "user", "content": self.build_prompt(query, result, None)})

        payload = {
            "model": self.api_model,
            "messages": messages,
            "stream": True,
            "max_tokens": MAX_TOKENS,
            "temperature": 0.3,
        }
        url = net_util.join_url(self.api_base, "/chat/completions")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            for line in net_util.http_post_stream(
                url, payload, headers=headers,
                connect_timeout=15.0,
                first_token_timeout=60.0,
                idle_timeout=45.0,
                with_proxy=True,
            ):
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if obj.get("error"):
                    msg = obj["error"].get("message") if isinstance(obj["error"], dict) else obj["error"]
                    yield {"type": "error", "message": f"云端 API 报错：{msg}"}
                    return
                for choice in obj.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content") or ""
                    if piece:
                        yield {"type": "delta", "content": piece}
                    if choice.get("finish_reason"):
                        return
        except net_util.StreamError as exc:
            yield {
                "type": "error",
                "message": f"云端 API 流式中止（{exc.kind}）：{exc.message}",
            }
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "message": f"云端 API 流式连接失败：{exc}"}

    def _offline_answer(self, query: str, result: search_mod.SearchResult) -> Iterator[dict]:
        """纯离线兜底：把检索结果组织成**本地搜索结果**展示，永不空手而归。

        ⚠ 这不是「模型生成的回答」。按 P0-F 明确改为「本地搜索结果」：
        展示章节/标题 + **查询词附近的原文**（表格保持可读结构，不再压成一行）
        + 来源与引用入口；只在开头说一次「未调用生成式 AI」，不重复三遍。
        """
        if not result.parents:
            text = (
                "本地搜索没有找到相关内容。\n\n"
                "建议：\n1. 换用更具体的关键词重新提问；\n"
                "2. 在「剪藏」页录入相关网页或笔记后再试。"
            )
            yield {"type": "delta", "content": text}
            return

        yield {
            "type": "delta",
            "content": "**本地搜索结果**（未调用生成式 AI，以下均为知识库原文摘录）：\n",
        }
        # UX-2：只对**实际参与词法检索**的词高亮（拿不到就退回查询实词）
        terms = list(getattr(result, "lex_terms", None) or []) or search_mod.content_terms(query)
        # UX-3：默认最多展示 Top 3（需要更多请进资料正文查看，不新增搜索 UI）
        for idx, p in enumerate(result.parents[:3], start=1):
            body = self._offline_excerpt(p.get("content") or "", query, terms, limit=300)
            body = _highlight(body, terms)
            # UX-1：来源用 display_source（导入文件→源文件名；剪藏/笔记→标题）
            src = p.get("display_source") or p.get("title") or ""
            sim = f"，相似度 {p['similarity']:.3f}" if p.get("similarity") else ""
            yield {
                "type": "delta",
                "content": f"\n### [^{idx}] {src}{sim}\n\n{body}\n",
            }
        yield {
            "type": "delta",
            "content": "\n— 以上为本地检索结果，均来自知识库原文摘录。",
        }

    # ------------------------------------------------------------------
    def _offline_excerpt(self, content: str, query: str, terms: list[str],
                         limit: int = 300) -> str:
        """离线展示用的原文摘录（UX-3）：默认 ~250–350 字、以查询词为中心。

        表格命中时**保表格结构**（表头 + 命中行 + 相邻 ≤1 行）—— 绝不为了 300 字符
        把「职责 ↔ 姓名」这类映射截断。保留换行（不 flatten），所以表格仍可读。
        """
        c = (content or "").strip()
        if len(c) <= limit:
            return c
        tbl = _table_excerpt(c, terms)
        if tbl:
            return tbl
        return _query_window(content, query, limit)

    # ------------------------------------------------------------------
    def complete(self, prompt: str, max_tokens: int = 200) -> str:
        """直接补全（**不做检索**、不带知识库上下文）。

        供入库摘要这类轻量增强使用。走与问答相同的降级链：本地 Ollama 优先、
        云端 API 次之，都不可用就返回空串 —— 调用方必须把空串当作
        「本次没做增强」而不是错误（入库绝不能因为模型不可用而失败）。
        """
        if not prompt:
            return ""
        try:
            provider, _notes = self.resolve_provider()
        except Exception:  # noqa: BLE001
            return ""
        # ⚠ 只有 ollama / api 才真的能补全。offline / error 必须提前返回 ——
        # 否则会带着空 api_key 去撞云端接口（白等一次网络超时）。
        if provider not in ("ollama", "api"):
            return ""

        messages = [{"role": "user", "content": prompt}]
        try:
            if provider == "ollama":
                payload = {
                    "model": self.ollama_model,
                    "messages": messages,
                    "stream": False,
                    "options": {"num_predict": max_tokens, "temperature": 0.2},
                }
                status, body = net_util.http_post_json(
                    net_util.join_url(self.ollama_host, "/api/chat"), payload,
                    timeout=60.0, with_proxy=False,
                )
                if status != 200 or not isinstance(body, dict):
                    return ""
                return ((body.get("message") or {}).get("content") or "").strip()

            payload = {
                "model": self.api_model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.2,
            }
            status, body = net_util.http_post_json(
                net_util.join_url(self.api_base, "/chat/completions"), payload,
                headers={"Authorization": f"Bearer {self.api_key}"}, timeout=60.0,
            )
            if status != 200 or not isinstance(body, dict):
                return ""
            choice = (body.get("choices") or [{}])[0]
            return ((choice.get("message") or {}).get("content") or "").strip()
        except Exception:  # noqa: BLE001 - 补全失败一律静默降级
            return ""

    def chat_once(self, query: str, history: list[dict] | None = None) -> dict:
        """非流式封装（供测试与 CLI 使用）。"""
        text_parts: list[str] = []
        refs: list[dict] = []
        provider = "offline"
        warnings: list[str] = []
        for frame in self.stream_chat(query, history):
            t = frame.get("type")
            if t == "delta":
                text_parts.append(frame.get("content", ""))
            elif t == "references":
                refs = frame.get("refs", [])
            elif t == "meta":
                provider = frame.get("provider", "")
                warnings.extend(frame.get("warnings", []))
            elif t in ("error", "notice"):
                warnings.append(frame.get("message", ""))
        return {
            "answer": "".join(text_parts),
            "references": refs,
            "provider": provider,
            "warnings": warnings,
        }


_gateway: Gateway | None = None
_gw_lock = threading.Lock()


def get_gateway(db: Database | None = None, embedder=None) -> Gateway:
    global _gateway
    with _gw_lock:
        if _gateway is None or (db is not None and _gateway.db is not db):
            _gateway = Gateway(db=db, embedder=embedder)
        elif embedder is not None:
            _gateway.embedder = embedder
        return _gateway
