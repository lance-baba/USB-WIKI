"""A4.1 —— AI Runtime Product Contract 验收（AI 能力分层 / Ollama 就绪状态 / 去隐藏默认模型）。

覆盖（按 A4.1 指令第十二节 A–H）：

  A  无 Ollama           → state=no_runtime，provider=offline，检索仍可用
  B  Ollama 在线 0 模型   → state=no_model，**不得** provider=ollama
  C  3 个模型但未选择     → state=selection_required，**不得**自动选 models[0]
  D  已选模型且存在       → state=ready，provider=ollama
  E  配置的模型被删       → state=model_missing，provider 不得继续 ollama
  F  Ollama 不 ready + 已配 API → provider=api
  G  无 Chat provider     → 检索链路照常被调用，provider=offline，不伪装成 LLM 回答
  H  全仓不再把某个具体模型当 V1 默认 / 推荐下载

设计：本模块自包含（自带 check/skip/section 与 PASS/FAIL/SKIP 列表），
由 tests/test_suite.py 的 main() 调用 run_a41_tests() 并合并结果。
**全程不联网、不装 Ollama、不下载模型**：HTTP 层被替换为可控替身。
config.ini 仅内存覆盖（persist=False）并在用例结束后原样还原，绝不落盘。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from app.core import config, llm, search as search_mod          # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []

#: 明显假名，绝不对应任何真实可下载模型（A4.1 指令明确要求）
TEST_CHAT = "test-chat-model:1b"
TEST_EMBED = "test-embed-model:1b"
TEST_VISION = "test-vision-model:1b"


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name if cond else f"{name} :: {detail}")
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"  [{detail}]"))
    return cond


def skip(name: str, reason: str = "") -> None:
    SKIP.append(f"{name} :: {reason}" if reason else name)
    print("  ⏭ " + name + (f"  [{reason}]" if reason else ""))


def section(t: str) -> None:
    print(f"\n── A4.1 {t} " + "─" * max(0, 52 - len(t)))


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------
class _fake_ollama:
    """把 llm.net_util 的 HTTP 出口换成可控替身（在线/离线/模型列表均可指定）。"""

    def __init__(self, models=None, *, available=True, post_map=None):
        self.models = list(models or [])
        self.available = available
        self.post_map = post_map or {}
        self.get_calls = 0

    def _tags_body(self) -> bytes:
        return json.dumps({
            "models": [
                {"name": n, "size": 1024, "modified_at": "2026-01-01T00:00:00Z",
                 "details": {"family": "test", "parameter_size": "1B",
                             "quantization_level": "Q4"}}
                for n in self.models
            ]
        }).encode("utf-8")

    def _get(self, url, headers=None, timeout=None, with_proxy=True):
        self.get_calls += 1
        if not self.available:
            return 0, b"", {}
        return 200, self._tags_body(), {}

    def _post(self, url, payload=None, headers=None, timeout=None, with_proxy=True):
        for key, resp in self.post_map.items():
            if key in url:
                return resp
        return 0, {"error": "test-fixture: 未声明的 POST"}

    def __enter__(self):
        nu = llm.net_util
        self._saved = (nu.http_get, nu.http_post_json, nu.http_post_stream)
        nu.http_get = self._get
        nu.http_post_json = self._post
        return self

    def __exit__(self, *exc):
        nu = llm.net_util
        nu.http_get, nu.http_post_json, nu.http_post_stream = self._saved
        return False


class _cfg_guard:
    """内存覆盖 AI 配置并在退出时原样还原（绝不 persist）。"""

    _KEYS = (("AI", "provider"), ("AI", "api_key"),
             ("AI", "api_base_url"), ("AI", "ollama_chat_model"))

    def __enter__(self):
        self.saved = {k: (config.get(*k) or "") for k in self._KEYS}
        return self

    def __exit__(self, *exc):
        values: dict[str, dict] = {}
        for (sec, key), val in self.saved.items():
            values.setdefault(sec, {})[key] = val
        config.update(values, persist=False)
        return False


def _set_ai(**kw) -> None:
    config.update({"AI": kw}, persist=False)


def _canned_result(query: str, n: int = 2) -> search_mod.SearchResult:
    refs, parents = [], []
    for i in range(1, n + 1):
        refs.append(search_mod.Reference(
            id=i, title=f"测试笔记 {i}", path=f"notes/t{i}.md",
            snippet="片段", parent_id=f"p{i}", doc_id=f"d{i}", score=1.0,
            similarity=None))
        parents.append({"parent_id": f"p{i}", "title": f"测试笔记 {i}",
                        "path": f"notes/t{i}.md", "content": f"正文内容 {i}", "similarity": None})
    return search_mod.SearchResult(query=query, route="fts5", counts={"parents": n},
                                   references=refs, parents=parents)


# ---------------------------------------------------------------------------
# A —— 无 Ollama
# ---------------------------------------------------------------------------
def _t_a_no_runtime() -> None:
    section("A 无 Ollama → no_runtime / provider=offline / 检索仍可用")
    fake = _fake_ollama([], available=False)
    with _cfg_guard(), fake:
        _set_ai(provider="auto", api_key="", ollama_chat_model="")
        gw = llm.Gateway(db=None, embedder=None)
        gw.ollama_status(force=True)
        r = gw.ai_readiness()

        check("A1 state = no_runtime", r["state"] == "no_runtime", str(r["state"]))
        check("A2 ollama_available = False", r["ollama_available"] is False)
        check("A3 chat_ready = False", r["chat_ready"] is False)
        check("A4 本机模型计数 0 / 未选择", r["installed_model_count"] == 0
              and r["selected_model"] is None, str(r))
        check("A5 state 在稳定枚举内", r["state"] in llm.AI_STATES)

        prov, warns = gw.resolve_provider()
        check("A6 provider = offline（不得 ollama）", prov == "offline", prov)
        check("A7 有可读的降级说明", bool(warns), str(warns))

        called = {"n": 0}

        def _retrieve(q):
            called["n"] += 1
            return _canned_result(q)

        gw.retrieve = _retrieve
        out = gw.chat_once("测试问题")
        check("A8 回答仍产出（检索链路照常被调用）", called["n"] == 1, str(called))
        check("A9 引用仍返回", len(out["references"]) == 2, str(len(out["references"])))
        check("A10 provider 报 offline（不伪装成模型生成）", out["provider"] == "offline",
              out["provider"])
        check("A11 明确说明「本地 AI 模型尚未配置」",
              any("本地 AI 模型尚未配置" in w for w in out["warnings"]), str(out["warnings"]))
        check("A12 正文标注未调用大模型", "未调用" in out["answer"])

        # A13/A14：/api/status 在冒烟测试里被高频轮询 → 默认读缓存**绝不发网络请求**；
        #          显式 probe=True 才许探测。这条不变量一破，首屏与冒烟都会被拖慢。
        before = fake.get_calls
        for _ in range(5):
            gw.ai_readiness()
        check("A13 ai_readiness 默认不触发任何网络探测",
              fake.get_calls == before, f"多出 {fake.get_calls - before} 次请求")
        gw.ai_readiness(probe=True)
        check("A14 probe=True 时才真的探测", fake.get_calls > before,
              f"请求次数仍为 {fake.get_calls}")


# ---------------------------------------------------------------------------
# B —— Ollama 在线但 0 模型
# ---------------------------------------------------------------------------
def _t_b_no_model() -> None:
    section("B Ollama 在线 0 模型 → no_model / 不得 provider=ollama")
    with _cfg_guard(), _fake_ollama([]):
        _set_ai(provider="auto", api_key="", ollama_chat_model=TEST_CHAT)
        gw = llm.Gateway(db=None, embedder=None)
        gw.ollama_status(force=True)
        r = gw.ai_readiness()

        check("B1 ollama_available = True（进程活着）", r["ollama_available"] is True)
        check("B2 state = no_model", r["state"] == "no_model", str(r["state"]))
        check("B3 chat_ready = False（活着 ≠ 可对话）", r["chat_ready"] is False)
        check("B4 配置了模型但本机没有 → selected_model_installed=False",
              r["selected_model_installed"] is False)

        for mode in ("auto", "ollama"):
            _set_ai(provider=mode)
            prov, _w = gw.resolve_provider()
            check(f"B5[{mode}] provider 不得是 ollama", prov != "ollama", prov)

        _set_ai(provider="auto")
        check("B6 提示语不含 ollama pull 具体模型推荐",
              not any("ollama pull" in w for w in gw.resolve_provider()[1]),
              str(gw.resolve_provider()[1]))


# ---------------------------------------------------------------------------
# C —— 有模型但未选择
# ---------------------------------------------------------------------------
def _t_c_selection_required() -> None:
    section("C 有模型未选择 → selection_required / 不得自动选 models[0]")
    with _cfg_guard(), _fake_ollama([TEST_EMBED, TEST_CHAT, TEST_VISION]):
        _set_ai(provider="auto", api_key="", ollama_chat_model="")
        gw = llm.Gateway(db=None, embedder=None)
        gw.ollama_status(force=True)
        r = gw.ai_readiness()

        check("C1 state = selection_required", r["state"] == "selection_required",
              str(r["state"]))
        check("C2 已发现 3 个本机模型", r["installed_model_count"] == 3,
              str(r["installed_model_count"]))
        check("C3 selected_model 为空（没替用户选）", r["selected_model"] is None,
              str(r["selected_model"]))
        check("C4 chat_ready = False", r["chat_ready"] is False)

        prov, _w = gw.resolve_provider()
        check("C5 provider 不得是 ollama", prov != "ollama", prov)
        # ⚠ 关键：跑完一整轮状态解析与 provider 解析后，配置里仍然没有模型
        check("C6 未把 models[0] 写进配置",
              config.get_str("AI", "ollama_chat_model", "") == "",
              config.get_str("AI", "ollama_chat_model", ""))
        check("C7 Gateway.ollama_model 仍为空", gw.ollama_model == "", repr(gw.ollama_model))
        # 端到端再确认一次：整条回答链路也不该偷偷选一个
        gw.retrieve = lambda q: _canned_result(q)
        gw.chat_once("随便问问")
        check("C8 回答链路跑完后仍未自动选择",
              config.get_str("AI", "ollama_chat_model", "") == "")


# ---------------------------------------------------------------------------
# D —— 已选择且存在
# ---------------------------------------------------------------------------
def _t_d_ready() -> None:
    section("D 已选模型且存在 → ready / provider=ollama")
    with _cfg_guard(), _fake_ollama([TEST_EMBED, TEST_CHAT]):
        _set_ai(provider="auto", api_key="", ollama_chat_model=TEST_CHAT)
        gw = llm.Gateway(db=None, embedder=None)
        gw.ollama_status(force=True)
        r = gw.ai_readiness()

        check("D1 state = ready", r["state"] == "ready", str(r["state"]))
        check("D2 chat_ready = True", r["chat_ready"] is True)
        check("D3 selected_model_installed = True", r["selected_model_installed"] is True)
        check("D4 selected_model 回显配置值", r["selected_model"] == TEST_CHAT)
        prov, warns = gw.resolve_provider()
        check("D5 provider = ollama", prov == "ollama", prov)
        check("D6 就绪时不产生降级告警", not warns, str(warns))

    # D7：省略 tag 时补 Ollama 默认 `:latest`（与 Ollama 自身解析一致）
    with _cfg_guard(), _fake_ollama(["foo:latest"]):
        _set_ai(provider="auto", api_key="", ollama_chat_model="foo")
        gw = llm.Gateway(db=None, embedder=None)
        gw.ollama_status(force=True)
        check("D7 省略 tag 命中 :latest → ready",
              gw.ai_readiness()["state"] == "ready",
              gw.ai_readiness()["state"])

    # D8：装的是 foo:1b 而配的是 foo → Ollama 实际会失败，故必须报 model_missing
    with _cfg_guard(), _fake_ollama(["foo:1b"]):
        _set_ai(provider="auto", api_key="", ollama_chat_model="foo")
        gw = llm.Gateway(db=None, embedder=None)
        gw.ollama_status(force=True)
        r8 = gw.ai_readiness()
        check("D8 前缀相同但 tag 不同 → model_missing（不假装 ready）",
              r8["state"] == "model_missing", str(r8["state"]))
        check("D9 且不得 provider=ollama", gw.resolve_provider()[0] != "ollama")


# ---------------------------------------------------------------------------
# E —— 配置的模型被删除
# ---------------------------------------------------------------------------
def _t_e_model_missing() -> None:
    section("E 配置模型被删 → model_missing / 不得继续 ollama")
    with _cfg_guard(), _fake_ollama([TEST_EMBED, TEST_VISION]):
        _set_ai(provider="auto", api_key="", ollama_chat_model=TEST_CHAT)
        gw = llm.Gateway(db=None, embedder=None)
        gw.ollama_status(force=True)
        r = gw.ai_readiness()

        check("E1 state = model_missing", r["state"] == "model_missing", str(r["state"]))
        check("E2 原因里点出是哪个模型", TEST_CHAT in (r["reason"] or ""), str(r["reason"]))
        check("E3 chat_ready = False", r["chat_ready"] is False)
        for mode in ("auto", "ollama"):
            _set_ai(provider=mode)
            prov, _w = gw.resolve_provider()
            check(f"E4[{mode}] provider 不得继续 ollama", prov != "ollama", prov)
        check("E5 未把配置静默替换成其它模型",
              config.get_str("AI", "ollama_chat_model", "") == TEST_CHAT,
              config.get_str("AI", "ollama_chat_model", ""))
        check("E6 旧值原样保留（升级用户不被改写）", gw.ollama_model == TEST_CHAT)


# ---------------------------------------------------------------------------
# F —— Ollama 不 ready + 已明确配置 API
# ---------------------------------------------------------------------------
def _t_f_api_fallback() -> None:
    section("F Ollama 不 ready + 已配 API → provider=api")
    with _cfg_guard(), _fake_ollama([], available=False):
        _set_ai(provider="auto", api_key="sk-test-fixture",
                api_base_url="https://api.test.invalid/v1", ollama_chat_model="")
        gw = llm.Gateway(db=None, embedder=None)
        gw.ollama_status(force=True)
        prov, warns = gw.resolve_provider()
        check("F1 provider = api", prov == "api", prov)
        check("F2 cloud_api_configured = True",
              gw.ai_readiness()["cloud_api_configured"] is True)

    # F3：显式 ollama 模式**不得**改走云端（用户明确要求不上云）
    with _cfg_guard(), _fake_ollama([], available=False):
        _set_ai(provider="ollama", api_key="sk-test-fixture",
                ollama_chat_model=TEST_CHAT)
        gw = llm.Gateway(db=None, embedder=None)
        gw.ollama_status(force=True)
        prov, warns = gw.resolve_provider()
        check("F3 provider=ollama 模式下不就绪 → offline（绝不改走 api）",
              prov == "offline", prov)
        check("F4 并给出原因", any("未就绪" in w for w in warns), str(warns))

    # F5：显式 offline 模式永远离线，且不需要额外解释
    with _cfg_guard(), _fake_ollama([TEST_CHAT]):
        _set_ai(provider="offline", api_key="sk-test-fixture",
                ollama_chat_model=TEST_CHAT)
        gw = llm.Gateway(db=None, embedder=None)
        gw.ollama_status(force=True)
        check("F5 provider=offline 模式即纯离线", gw.resolve_provider()[0] == "offline")
        check("F6 主动选纯离线时不再啰嗦提示", gw.offline_notice() == "")


# ---------------------------------------------------------------------------
# G —— 无 Chat provider 时，检索链路继续工作
# ---------------------------------------------------------------------------
def _t_g_retrieval_unaffected() -> None:
    section("G 无 Chat provider → 检索继续工作，且不伪装成 LLM 回答")
    with _cfg_guard(), _fake_ollama([], available=False):
        _set_ai(provider="auto", api_key="", ollama_chat_model="")
        gw = llm.Gateway(db=None, embedder=None)
        gw.ollama_status(force=True)

        calls = {"n": 0}

        def _retrieve(q):
            calls["n"] += 1
            return _canned_result(q, 3)

        gw.retrieve = _retrieve
        out = gw.chat_once("知识库里有什么")

        check("G1 检索被真实调用（不被 AI 就绪状态挡住）", calls["n"] == 1, str(calls))
        check("G2 引用 3 条", len(out["references"]) == 3, str(len(out["references"])))
        check("G3 provider = offline", out["provider"] == "offline", out["provider"])
        check("G4 回答包含检索到的段落（本地搜索结果展示原文）",
              "本地搜索结果" in out["answer"] and "正文内容" in out["answer"])
        check("G5 明确标注未调用大模型", "未调用" in out["answer"])
        check("G6 措辞为原文摘录，不冒充生成式回答",
              "未调用" in out["answer"] and "原文摘录" in out["answer"],
              out["answer"][-80:])

        # G7：连离线兜底都没有命中时也不报错（空手也要给建议，而不是崩）
        gw.retrieve = lambda q: search_mod.SearchResult(query=q, route="like")
        out2 = gw.chat_once("完全不相关的问题")
        check("G7 零命中时仍正常收尾（不抛错）",
              "没有找到相关内容" in out2["answer"] and out2["provider"] == "offline",
              out2["answer"][:60])

        # G8：整条链路上没有任何 error 帧
        frames = list(gw.stream_chat("再问一次"))
        check("G8 链路不产生 error 帧",
              not any(f.get("type") == "error" for f in frames),
              str([f.get("type") for f in frames]))
        check("G9 链路以 done 收尾", frames[-1].get("type") == "done",
              str(frames[-1]))


# ---------------------------------------------------------------------------
# H —— 不再把具体模型当默认 / 推荐
# ---------------------------------------------------------------------------
#: 被禁的出现物。刻意用拼接构造 —— 否则本文件自己会命中（它必须提到这个名字才能搜它），
#: 那种「测试把自己当违规」的假红会逼着后来人放宽断言。
_FORBIDDEN = "qwen" + "2.5:3b"

_SKIP_DIRS = ("runtime/", "data/", ".git/", ".workbuddy/")
_SKIP_SUFFIX = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".woff", ".woff2",
                ".db", ".pyc", ".zip", ".whl", ".pdf")


def _repo_text_files():
    for p in REPO.rglob("*"):
        rel = p.relative_to(REPO).as_posix()
        if not p.is_file() or any(rel.startswith(d) for d in _SKIP_DIRS):
            continue
        if "__pycache__" in rel or p.suffix.lower() in _SKIP_SUFFIX:
            continue
        try:
            yield rel, p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue


def _t_h_no_hidden_default() -> None:
    section("H 全仓不再把某个具体模型当 V1 默认 / 推荐下载")

    self_rel = Path(__file__).resolve().relative_to(REPO).as_posix()
    hits = [f"{rel}:{i}" for rel, txt in _repo_text_files()
            if rel != self_rel                      # 本文件用拼接构造，不参与扫描
            for i, line in enumerate(txt.splitlines(), 1)
            if _FORBIDDEN in line]
    check("H1 全仓（源码 / 文档 / 前端）无该模型名残留", not hits, str(hits[:6]))

    tmpl = config.DEFAULT_TEMPLATE
    value = ""
    for line in tmpl.splitlines():
        if line.strip().startswith("ollama_chat_model"):
            value = line.split("=", 1)[1].strip() if "=" in line else "<无等号>"
    check("H2 配置模板里 ollama_chat_model 留空（无预设模型）", value == "",
          f"值={value!r}")
    check("H3 内置模板不再推荐下载任何具体模型",
          "ollama pull" not in tmpl, "模板中出现 ollama pull")

    with _cfg_guard():
        # 模拟「全新安装」：配置里没有该键 → get_str 必须回落到空串
        config.update({"AI": {"ollama_chat_model": ""}}, persist=False)
        gw = llm.Gateway(db=None, embedder=None)
        check("H4 未配置时 Gateway.ollama_model 为空串", gw.ollama_model == "",
              repr(gw.ollama_model))

    # 前端：不得再出现「具体模型下载指引」或自动选第一个模型
    appjs = (REPO / "app" / "web" / "app.js").read_text(encoding="utf-8")
    check("H5 前端未出现 ollama pull 的对话模型推荐",
          "ollama pull" not in appjs, "app.js 仍含 ollama pull")
    check("H6 前端输入框不再以具体模型名作 placeholder",
          'placeholder = "qwen' not in appjs and 'placeholder="qwen' not in appjs)
    check("H7 前端不再自动选中 models[0]",
          "models[0].name" not in appjs, "app.js 仍会自动选 models[0]")
    check("H8 前端保留「未选择」占位选项",
          "未选择（请从下方本机模型里挑一个）" in appjs)

    # 后端：Core 里聊天模型默认值必须是空串，且不得有模型名字面量兜底
    llm_src = (REPO / "app" / "core" / "llm.py").read_text(encoding="utf-8")
    check("H9 llm.py 的 ollama_chat_model 默认值为空串",
          'config.get_str("AI", "ollama_chat_model", "")' in llm_src)
    literals = [m for m in ('"qwen', '"llama', '"gemma', '"mistral', '"phi',
                            '"deepseek-r1', '"yi-', '"glm-')
                if m in llm_src]
    check("H9b llm.py 不含任何具体聊天模型名字面量", not literals, str(literals))

    # 文档：README 的配置示例必须与新版契约一致
    import re as _re
    m = _re.search(r"ollama_chat_model\s*=\s*([^\n]*)", readme :=
                   (REPO / "README.md").read_text(encoding="utf-8"))
    raw = (m.group(1).split(";")[0].strip() if m else "<未找到>")
    check("H10 README 配置示例里聊天模型留空", raw == "", f"值={raw!r}")


def run_a41_tests() -> None:
    print("\n" + "=" * 66)
    print("  A4.1 —— AI Runtime Product Contract（分层 / 就绪状态 / 去隐藏默认）")
    print("=" * 66)
    cases = [
        ("A 无 Ollama", _t_a_no_runtime),
        ("B 在线 0 模型", _t_b_no_model),
        ("C 有模型未选择", _t_c_selection_required),
        ("D 已选可用", _t_d_ready),
        ("E 模型被删", _t_e_model_missing),
        ("F API 兜底", _t_f_api_fallback),
        ("G 检索不受影响", _t_g_retrieval_unaffected),
        ("H 无隐藏默认模型", _t_h_no_hidden_default),
    ]
    for label, fn in cases:
        try:
            fn()
        except Exception as exc:      # 单场景异常不得带崩整个套件
            check(f"A4.1 {label} 执行未抛异常", False, f"{type(exc).__name__}: {exc}")
    print(f"\n  A4.1 TOTAL={len(PASS) + len(FAIL) + len(SKIP)} "
          f"PASS={len(PASS)} SKIP={len(SKIP)} FAIL={len(FAIL)}")


if __name__ == "__main__":
    run_a41_tests()
    raise SystemExit(1 if FAIL else 0)
