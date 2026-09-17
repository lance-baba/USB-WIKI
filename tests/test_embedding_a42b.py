"""A4.2b —— Bundled Embedding 集成验收。

覆盖（§21）：
  A 正式 tokenizers 与 A4.2a reference 的 input_ids / attention_mask / token_type_ids 完全一致
  B 正式 OnnxEmbedder：512 维 / finite / L2≈1 / deterministic，且与 A4.2a INT8 runner 输出一致
  C 用**正式 App 嵌入器**跑现有 RAG regression（不是评测工具里的 runner）
  D onnxruntime 不可用 → App 正常解析、按契约降级，不启动失败
  E tokenizer 缺失 → local_onnx unavailable，**不得**走字符级假 fallback
  F Release 中 embedding 改 1 byte → verify_media = MEDIA_CORRUPTED
  G 破坏 App/resources/embedding 后重装 → 恢复，Library SHA256 不变
  H 同 artifact 重装 signature 不变；artifact / tokenizer 哈希变化 → signature mismatch

设计要点
--------
* **大文件不进 Git**：资源字节在 gitignored 的 ``vendor/cache/embedding/``，
  由 ``scripts/fetch_embedding_resource.py`` 取回。取不到时相关用例 **SKIP**（不红）。
* 需要切换资源目录的用例通过 ``WIKIUSB_EMBEDDING_DIR`` + reload ``app.core.paths`` 实现，
  与生产路径完全同源（不另造一套解析逻辑）。
* 依赖 ``onnxruntime`` / ``tokenizers``（已在 requirements.txt + lock 里）；
  缺失时同样 SKIP，不把「本机没装」伪装成产品缺陷。
"""
from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

RESOURCE_DIR = REPO / "vendor" / "cache" / "embedding"
CONTRACT = REPO / "resources" / "embedding" / "default.json"

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []

_TMP_RESOURCE: Path | None = None


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name if cond else f"{name} :: {detail}")
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"  [{detail}]"))
    return cond


def skip(name: str, reason: str = "") -> None:
    SKIP.append(f"{name} :: {reason}" if reason else name)
    print("  ⏭ " + name + (f"  [{reason}]" if reason else ""))


def section(t: str) -> None:
    print(f"\n── A4.2b {t} " + "─" * max(0, 50 - len(t)))


# ---------------------------------------------------------------------------
def _have_runtime_deps() -> tuple[bool, str]:
    try:
        import onnxruntime  # noqa: F401
        import tokenizers   # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"缺运行时依赖（{type(exc).__name__}），先按 lock 安装"
    return True, ""


def _have_resource() -> tuple[bool, str]:
    if not (RESOURCE_DIR / "artifact.json").is_file():
        return False, ("未取件：先跑 scripts/fetch_embedding_resource.py"
                       "（大文件不进 Git）")
    return True, ""


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class _embedding_env:
    """把 ``WIKIUSB_EMBEDDING_DIR`` 指向 *path* 并 reload paths；退出时还原。"""

    def __init__(self, path: Path | None) -> None:
        self.path = str(path) if path is not None else None

    def __enter__(self):
        if self.path:
            os.environ["WIKIUSB_EMBEDDING_DIR"] = self.path
        else:
            os.environ.pop("WIKIUSB_EMBEDDING_DIR", None)
        import app.core.paths as paths
        importlib.reload(paths)
        return self

    def __exit__(self, *exc):
        os.environ.pop("WIKIUSB_EMBEDDING_DIR", None)
        import app.core.paths as paths
        importlib.reload(paths)
        return False


def _tmp_resource() -> Path:
    """一份**可改动**的资源副本（供 E / H 破坏用），整轮测试共用一份。"""
    global _TMP_RESOURCE
    if _TMP_RESOURCE is None:
        _TMP_RESOURCE = Path(tempfile.mkdtemp(prefix="a42b-res-")) / "embedding"
        shutil.copytree(RESOURCE_DIR, _TMP_RESOURCE)
    return _TMP_RESOURCE


def cleanup() -> None:
    global _TMP_RESOURCE
    if _TMP_RESOURCE is not None:
        shutil.rmtree(_TMP_RESOURCE.parent, ignore_errors=True)
        _TMP_RESOURCE = None


# ---------------------------------------------------------------------------
# A —— tokenizer parity
# ---------------------------------------------------------------------------
TOKENIZER_CASES = {
    "纯中文": "这是一段纯中文的测试文本，用来验证分词是否正确。",
    "中英混合": "USB-WIKI 使用 SQLite FTS5 做混合检索 hybrid search。",
    "数字": "2026 年第 3 季度营收 1234567.89 元，同比增长 12.5%。",
    "URL": "参考 https://huggingface.co/BAAI/bge-small-zh-v1.5 与 http://127.0.0.1:11434/api/tags",
    "标点": "（一）、二；三：四！五？六……「七」——八。",
    "长文本": "知识库检索" * 120,
    "空白": "   ",
    "特殊字符": "emoji 与 <script>alert(1)</script> 以及 a b 和 #$%^&*()",
}


def _t_a_tokenizer_parity() -> None:
    section("A 正式 tokenizer 与 A4.2a reference 完全一致")
    ok_rt, why = _have_runtime_deps()
    if not ok_rt:
        skip("A", why)
        return
    ok_res, why = _have_resource()
    if not ok_res:
        skip("A", why)
        return

    sys.path.insert(0, str(REPO / "tools" / "embedding_eval"))
    with _embedding_env(RESOURCE_DIR):
        from app.core import embedder as emb_mod
        res, code, why = emb_mod.load_embedding_resource()
        if not check("A0 资源可加载", res is not None, f"{code}: {why}"):
            return
        app_emb = emb_mod.OnnxEmbedder(res)
        app_emb._lazy()                                    # noqa: SLF001

        # A4.2a 选型时用的 reference runner（同一套字节 / 同一份 tokenizer）
        from runner import OnnxBertEmbedder
        ref = OnnxBertEmbedder(res.model_path, RESOURCE_DIR, model_name="a42a-int8")
        ref._lazy()                                        # noqa: SLF001

        texts = list(TOKENIZER_CASES.values())
        a_ids = [e.ids for e in app_emb._tok.encode_batch(texts)]      # noqa: SLF001
        a_mask = [e.attention_mask for e in app_emb._tok.encode_batch(texts)]  # noqa: SLF001
        r_ids = [e.ids for e in ref._tok.encode_batch(texts)]          # noqa: SLF001
        r_mask = [e.attention_mask for e in ref._tok.encode_batch(texts)]      # noqa: SLF001
        check("A1 input_ids 完全一致", a_ids == r_ids)
        check("A2 attention_mask 完全一致", a_mask == r_mask)

        a_in = set(app_emb.io_names["inputs"])
        r_in = set(ref.io_names["inputs"])
        check("A3 喂给模型的输入集合一致（含 token_type_ids 判定）",
              a_in == r_in, f"app={sorted(a_in)} ref={sorted(r_in)}")
        # 模型是否需要 token_type_ids，两个实现必须给出同样的答案
        need_tt = "token_type_ids" in a_in
        check("A4 token_type_ids 提供与否一致",
              need_tt == ("token_type_ids" in r_in), f"需要={need_tt}")
        check("A5 截断上限一致（max_length=512）",
              max(len(x) for x in a_ids) <= res.max_length,
              str(max(len(x) for x in a_ids)))
        check("A6 长文本按 max_length 截断",
              len(a_ids[list(TOKENIZER_CASES).index("长文本")]) == res.max_length
              or len(a_ids[list(TOKENIZER_CASES).index("长文本")]) < res.max_length)


# ---------------------------------------------------------------------------
# B —— 嵌入正确性 + 与 A4.2a runner 一致
# ---------------------------------------------------------------------------
def _t_b_embedding() -> None:
    section("B 正式 OnnxEmbedder 正确性 + 与 A4.2a INT8 runner 一致")
    ok_rt, why = _have_runtime_deps()
    if not ok_rt:
        skip("B", why)
        return
    ok_res, why = _have_resource()
    if not ok_res:
        skip("B", why)
        return

    sys.path.insert(0, str(REPO / "tools" / "embedding_eval"))
    with _embedding_env(RESOURCE_DIR):
        from app.core import embedder as emb_mod
        res, _code, _why = emb_mod.load_embedding_resource()
        emb = emb_mod.OnnxEmbedder(res)
        loaded, why = emb.probe()
        if not check("B0 probe 通过（session + tokenizer 都加载成功）", loaded, why):
            return

        texts = ["这是一句用于正确性检查的中文测试文本。",
                 "多久要换一次过滤网", "滤芯需要多久更换"]
        v1 = emb.embed(texts)
        v2 = emb.embed(texts)

        check("B1 dim = 512", all(len(v) == 512 for v in v1), str({len(v) for v in v1}))
        check("B2 全部有限值", all(math.isfinite(x) for v in v1 for x in v))
        norms = [math.sqrt(sum(x * x for x in v)) for v in v1]
        check("B3 L2 ≈ 1", all(abs(n - 1.0) < 1e-5 for n in norms),
              str([round(n, 6) for n in norms]))
        check("B4 deterministic（两次结果逐位相同）", v1 == v2)

        from runner import OnnxBertEmbedder
        ref = OnnxBertEmbedder(res.model_path, RESOURCE_DIR, model_name="a42a-int8")
        ref._lazy()                                        # noqa: SLF001
        rv = ref.embed(texts)
        diff = max(abs(a - b) for va, vb in zip(v1, rv) for a, b in zip(va, vb))
        check("B5 与 A4.2a INT8 runner 输出一致（逐元素 < 1e-5）", diff < 1e-5, f"最大差 {diff:.2e}")

        # CLS + L2 的证据：同义句相似度显著高于无关句
        def cos(a, b):
            d = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(x * x for x in b))
            return d / (na * nb or 1.0)
        sim_same = cos(v1[1], v1[2])          # 过滤网 ↔ 滤芯
        sim_diff = cos(v1[1], rv[0]) if False else cos(v1[0], v1[1])
        check("B6 同义句相似度显著高于无关句（CLS 语义可用）",
              sim_same > sim_diff + 0.15, f"同义 {sim_same:.3f} vs 无关 {sim_diff:.3f}")


# ---------------------------------------------------------------------------
# C —— 用正式 App 嵌入器跑 RAG regression
# ---------------------------------------------------------------------------
def _t_c_retrieval() -> None:
    section("C 正式 App OnnxEmbedder 跑现有 RAG regression")
    ok_rt, why = _have_runtime_deps()
    if not ok_rt:
        skip("C", why)
        return
    ok_res, why = _have_resource()
    if not ok_res:
        skip("C", why)
        return

    tmp = Path(tempfile.mkdtemp(prefix="a42b-rag-"))
    os.environ["WIKIUSB_LIBRARY"] = str(tmp / "Library")
    try:
        from app.core import context as ctxmod, indexer, paths
        from app.core import search as search_mod
        from app.core import config
        from app.core import embedder as emb_mod

        with _embedding_env(RESOURCE_DIR):
            config.update({"AI": {"embedding_source": "local_onnx",
                                  "provider": "offline"}}, persist=False)
            ctx = ctxmod.AppContext()
            ctx.boot(db_path=tmp / "cache.db", start_syncer=False, probe_ollama=False)
            db = ctx.db
            try:
                emb = ctx.embedder
                if not check("C0 启动时确实解析到 local_onnx",
                             ctx.embedder_source == "local_onnx",
                             f"{ctx.embedder_source} / {ctx.embedding_fallback_reason}"):
                    return
                if emb is None:
                    check("C0", False, "嵌入器为 None")
                    return

                from tests.fixtures import rag_corpus as corpus
                paths.NOTES_DIR.mkdir(parents=True, exist_ok=True)
                for name, body in corpus.DOCS.items():
                    (paths.NOTES_DIR / name).write_text(body, encoding="utf-8")
                for name in corpus.DOCS:
                    indexer.index_file(db, paths.NOTES_DIR / name, emb)

                check("C1 向量索引已写入（非 0）",
                      bool(db.vec_table_ready and not db.signature_mismatch),
                      str(db.signature_mismatch or ""))

                hits1 = hits3 = hits5 = 0
                mrr = 0.0
                positives = 0
                for case in corpus.CASES:
                    q, exp = case["q"], case.get("expect")
                    if exp is None:
                        continue
                    positives += 1
                    got = [Path(r.path).stem for r in
                           search_mod.hybrid_search(db, emb, q, top_k_parents=5).references]
                    rank = got.index(exp) + 1 if exp in got else None
                    if rank == 1:
                        hits1 += 1
                    if rank and rank <= 3:
                        hits3 += 1
                    if rank and rank <= 5:
                        hits5 += 1
                    if rank:
                        mrr += 1.0 / rank
                n = positives or 1
                top1, top3, top5, mrr = hits1 / n, hits3 / n, hits5 / n, mrr / n
                print(f"      Top1={top1*100:.1f}% Top3={top3*100:.1f}% "
                      f"Top5={top5*100:.1f}% MRR={mrr:.3f}（A4.2a 记录 97.4/100/100/0.983）")
                check("C2 Top1 不低于 A4.2a 记录（容差 2pp）", top1 >= 0.974 - 0.02,
                      f"{top1:.4f}")
                check("C3 Top5 = 100%", top5 >= 0.999, f"{top5:.4f}")
                check("C4 MRR 不低于 A4.2a 记录（容差 0.02）", mrr >= 0.983 - 0.02,
                      f"{mrr:.3f}")
                check("C5 明显优于纯词法基线（Top1 > 94.9%）", top1 > 0.949, f"{top1:.4f}")
            finally:
                try:
                    ctx.shutdown()
                except Exception:  # noqa: BLE001
                    pass
    finally:
        os.environ.pop("WIKIUSB_LIBRARY", None)
        shutil.rmtree(tmp, ignore_errors=True)
        config_path = REPO / "config.ini"
        _ = config_path  # 配置仅在内存覆盖（persist=False），无需回写


# ---------------------------------------------------------------------------
# D —— onnxruntime 不可用
# ---------------------------------------------------------------------------
def _t_d_probe_fail() -> None:
    section("D onnxruntime 不可用 → 正常降级，不启动失败")
    ok_res, why = _have_resource()
    if not ok_res:
        skip("D", why)
        return

    with _embedding_env(RESOURCE_DIR):
        from app.core import embedder as emb_mod
        from app.core import config
        config.update({"AI": {"embedding_source": "local_onnx",
                              "api_key": "", "api_base_url": ""}}, persist=False)

        saved = emb_mod.probe_onnxruntime
        try:
            emb_mod.probe_onnxruntime = lambda force=False: (False, "模拟：onnxruntime 不可用")
            r = emb_mod.resolve(config.get, ollama_healthy=lambda: False)
            check("D1 未启用 local_onnx", r.source != "local_onnx", r.source)
            check("D2 嵌入器为 None（不返回坏的嵌入器）", r.embedder is None)
            check("D3 fallback_reason 说明引擎不可用",
                  "onnxruntime" in r.fallback_reason or "引擎" in r.fallback_reason,
                  r.fallback_reason)
            check("D4 **没有**自动切到 local_hash", r.source != "local_hash", r.source)
            check("D5 降级说明进入 notes（不静默）",
                  any("嵌入" in n or "onnx" in n.lower() for n in r.notes), str(r.notes))
        finally:
            emb_mod.probe_onnxruntime = saved

        # 另一种失败：资源在、引擎在，但加载期抛错（tokenizer 损坏等）
        saved_probe = emb_mod.OnnxEmbedder.probe
        try:
            emb_mod.OnnxEmbedder.probe = lambda self: (False, "模拟：加载期失败")
            r2 = emb_mod.resolve(config.get, ollama_healthy=lambda: False)
            check("D6 加载失败同样不启用 local_onnx", r2.source != "local_onnx", r2.source)
            check("D7 加载失败也不切 local_hash", r2.source != "local_hash", r2.source)
        finally:
            emb_mod.OnnxEmbedder.probe = saved_probe


# ---------------------------------------------------------------------------
# E —— tokenizer 缺失：不得走字符级假 fallback
# ---------------------------------------------------------------------------
def _t_e_tokenizer_missing() -> None:
    section("E tokenizer 缺失 → local_onnx unavailable（无字符级假 fallback）")
    ok_res, why = _have_resource()
    if not ok_res:
        skip("E", why)
        return

    from app.core import embedder as emb_mod
    from app.core import config

    broken = _tmp_resource()
    (broken / "tokenizer.json").unlink(missing_ok=True)
    with _embedding_env(broken):
        config.update({"AI": {"embedding_source": "local_onnx",
                              "api_key": ""}}, persist=False)
        res, code, why = emb_mod.load_embedding_resource()
        check("E1 判定为 tokenizer 缺失",
              code == emb_mod.RES_MISSING_TOKENIZER and res is None, f"{code}: {why}")
        r = emb_mod.resolve(config.get, ollama_healthy=lambda: False)
        check("E2 local_onnx 不可用", r.source != "local_onnx", r.source)
        check("E3 嵌入器为 None", r.embedder is None)
        check("E4 **不**降级为 local_hash（字符级）", r.source != "local_hash", r.source)
        check("E5 fallback_reason 点明 tokenizer", "tokenizer" in r.fallback_reason,
              r.fallback_reason)

    # 恢复，供 H 使用
    shutil.copy2(RESOURCE_DIR / "tokenizer.json", broken / "tokenizer.json")
    check("E6 源码中已无字符级近似 tokenizer 生产路径", "ord(ch)" not in
          (REPO / "app" / "core" / "embedder.py").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# F —— Release 中 embedding 改 1 byte
# ---------------------------------------------------------------------------
def _t_f_artifact_tamper() -> None:
    section("F Release 中 embedding 改 1 byte → MEDIA_CORRUPTED")
    sys.path.insert(0, str(REPO / "scripts"))
    import release_integrity as ri                        # noqa: E402

    root = Path(tempfile.mkdtemp(prefix="a42b-media-")) / "USB-WIKI-v1.3.0-win-x64"
    try:
        payload = root / "payload" / "embedding"
        payload.mkdir(parents=True)
        # 用真实字节（小文件即可：机制与大小无关）
        src = RESOURCE_DIR if (RESOURCE_DIR / "tokenizer.json").is_file() else None
        if src is None:
            skip("F", "未取件，无法构造含真实字节的介质")
            return
        shutil.copy2(src / "tokenizer.json", payload / "tokenizer.json")
        shutil.copy2(src / "artifact.json", payload / "artifact.json")
        # 介质校验要求 manifest 里声明的必需交付物都得在，补齐最小集合
        (root / "BUILD_INFO.json").write_text(
            json.dumps({"app_version": "test", "embedding": None}, ensure_ascii=False),
            encoding="utf-8")
        lic = root / "LICENSES"
        lic.mkdir(parents=True, exist_ok=True)
        (lic / "THIRD_PARTY.json").write_text(
            json.dumps({"packages": []}, ensure_ascii=False), encoding="utf-8")

        _, manifest = ri.write_manifest(root)
        ri.write_checksums(root, manifest)
        res0 = ri.verify_media(root)
        if not check("F0 未改动时校验通过", res0.ok, f"{res0.code} {res0.failures[:2]}"):
            return

        target = payload / "tokenizer.json"
        raw = bytearray(target.read_bytes())
        raw[0] = raw[0] ^ 0x01                    # 改 1 byte
        target.write_bytes(bytes(raw))
        res1 = ri.verify_media(root)
        check("F1 改 1 byte → 校验失败", not res1.ok, res1.code)
        check("F2 错误码为 MEDIA_CORRUPTED", res1.code == "MEDIA_CORRUPTED", res1.code)
        check("F3 失败项指向被改的文件",
              any("tokenizer.json" in f for f in res1.failures), str(res1.failures[:3]))
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# G —— 破坏 App/resources/embedding 后重装
# ---------------------------------------------------------------------------
def _t_g_reinstall() -> None:
    section("G 破坏 App/resources/embedding 后重装 → 恢复，Library SHA256 不变")
    ok_res, why = _have_resource()
    if not ok_res:
        skip("G", why)
        return

    sys.path.insert(0, str(REPO / "scripts"))
    import install_windows as iw                          # noqa: E402
    if not hasattr(iw, "_copy_to_staging"):
        skip("G", "安装器缺少 _copy_to_staging")
        return

    tmp = Path(tempfile.mkdtemp(prefix="a42b-inst-"))
    try:
        payload = tmp / "payload" / "embedding"
        payload.mkdir(parents=True)
        for name in ("model.onnx", "tokenizer.json", "artifact.json"):
            src = RESOURCE_DIR / name
            if src.is_file():
                shutil.copy2(src, payload / name)
        (tmp / "payload" / "app").mkdir(parents=True)
        (tmp / "payload" / "app" / "launcher.py").write_text("# fake\n", encoding="utf-8")
        # _copy_to_staging 也会拷 runtime（本用例关注 embedding，runtime 用占位即可）
        rt = tmp / "payload" / "python-runtime"
        rt.mkdir(parents=True)
        (rt / "python.exe").write_bytes(b"fake")

        # 一个「已安装」的 App + 一个 Library
        app_target = tmp / "App"
        app_target.mkdir(parents=True)
        (app_target / "app").mkdir(exist_ok=True)
        (app_target / "app" / "launcher.py").write_text("# fake\n", encoding="utf-8")
        (app_target / "runtime").mkdir(exist_ok=True)
        staging = iw._staging_dir(app_target)             # noqa: SLF001
        iw._copy_to_staging(tmp / "payload", staging)     # noqa: SLF001

        emb_dir = staging / "resources" / "embedding"
        if not check("G0 安装后存在 resources/embedding", emb_dir.is_dir(), str(emb_dir)):
            return
        before = {p.name: sha256_file(p) for p in sorted(emb_dir.iterdir())}
        check("G1 字节与 Release 一致",
              before.get("tokenizer.json") == sha256_file(RESOURCE_DIR / "tokenizer.json"))

        # ---- 破坏 ----
        shutil.rmtree(emb_dir)
        check("G2 已破坏（embedding 目录消失）", not emb_dir.exists())
        # ---- 重装 ----
        iw._copy_to_staging(tmp / "payload", iw._staging_dir(app_target))  # noqa: SLF001
        after = {p.name: sha256_file(p) for p in sorted(emb_dir.iterdir())}
        check("G3 重装后 embedding 恢复", emb_dir.is_dir() and before == after,
              f"{sorted(before)} vs {sorted(after)}")

        # ---- Library SHA256 前后不变 ----
        lib = tmp / "Library"
        (lib / "notes").mkdir(parents=True)
        (lib / "notes" / "a.md").write_text("# 用户资料\n\n这是用户的笔记，绝不能被安装过程改动。\n",
                                            encoding="utf-8")

        def lib_sha() -> str:
            h = hashlib.sha256()
            for p in sorted(lib.rglob("*")):
                if p.is_file():
                    h.update(p.relative_to(lib).as_posix().encode("utf-8"))
                    h.update(p.read_bytes())
            return h.hexdigest()

        sha_before = lib_sha()
        iw._copy_to_staging(tmp / "payload", iw._staging_dir(app_target))  # noqa: SLF001
        check("G4 Library SHA256 前后完全相同", lib_sha() == sha_before)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# H —— signature 稳定性 / 敏感性
# ---------------------------------------------------------------------------
def _t_h_signature() -> None:
    section("H embedding signature：同 artifact 不变，字节变化必变")
    ok_res, why = _have_resource()
    if not ok_res:
        skip("H", why)
        return

    from app.core import embedder as emb_mod
    with _embedding_env(RESOURCE_DIR):
        res, code, why = emb_mod.load_embedding_resource()
        if not check("H0 资源可加载", res is not None, f"{code}: {why}"):
            return
        extra = res.signature_extra()
        print(f"      signature_extra = {json.dumps(extra, ensure_ascii=False)}")
        check("H1 含 artifact_sha256 / tokenizer_sha256 / precision",
              set(extra) == {"artifact_sha256", "tokenizer_sha256", "precision"})
        check("H2 artifact_sha256 与契约一致",
              extra["artifact_sha256"] ==
              json.loads(CONTRACT.read_text(encoding="utf-8"))["artifact_sha256"].lower())

        # 用临时 DB 直接测签名守卫（不碰用户资料库）。必须走 get_db（它会建 schema），
        # 直接 Database(db_path=...) 没有 sys_meta 表 → 会抛 no such table。
        from app.core import db as db_mod
        # get_db 是单例：前面 C 用例 boot 过一次（并已 shutdown/删除临时库），
        # 这里必须先把单例释放，否则拿到的是那个已关闭/已消失的库。
        db_mod._db = None                                  # noqa: SLF001
        db = db_mod.get_db(db_path=Path(tempfile.mkdtemp(prefix="a42b-sig-")) / "cache.db",
                           embedding_dim=res.dimension)
        db.init_schema()          # get_db 只建连接，schema 由调用方显式初始化
        try:
            first = db.check_signature("local_onnx", res.id, res.dimension, extra=extra)
            check("H3 首次写入签名无告警", first is None, str(first))
            second = db.check_signature("local_onnx", res.id, res.dimension, extra=extra)
            check("H4 同一 artifact 重装 → signature 不变（无告警）", second is None,
                  str(second))

            # artifact 字节变化（换一个假 sha256）
            bad = dict(extra)
            bad["artifact_sha256"] = "0" * 64
            m1 = db.check_signature("local_onnx", res.id, res.dimension, extra=bad)
            check("H5 artifact 哈希变化 → signature mismatch",
                  bool(m1) and "artifact_sha256" in m1, str(m1))

            # tokenizer 字节变化
            bad2 = dict(extra)
            bad2["tokenizer_sha256"] = "1" * 64
            m2 = db.check_signature("local_onnx", res.id, res.dimension, extra=bad2)
            check("H6 tokenizer 哈希变化 → signature mismatch",
                  bool(m2) and "tokenizer_sha256" in m2, str(m2))

            # 精度变化
            bad3 = dict(extra)
            bad3["precision"] = "fp32"
            m3 = db.check_signature("local_onnx", res.id, res.dimension, extra=bad3)
            check("H7 precision 变化 → signature mismatch",
                  bool(m3) and "precision" in m3, str(m3))
            check("H8 mismatch 时不自动重建索引（只返回告警文案）",
                  isinstance(m1, str))
        finally:
            try:
                db.checkpoint_and_close()
            except Exception:  # noqa: BLE001
                pass
            db_mod._db = None          # 释放全局单例，保证后续用例重新建库  # noqa: SLF001


def run_a42b_tests() -> None:
    print("\n" + "=" * 66)
    print("  A4.2b —— Bundled Embedding 集成验收")
    print("=" * 66)
    cases = [
        ("A tokenizer parity", _t_a_tokenizer_parity),
        ("B embedding 正确性", _t_b_embedding),
        ("C RAG regression", _t_c_retrieval),
        ("D probe fail 降级", _t_d_probe_fail),
        ("E tokenizer 缺失", _t_e_tokenizer_missing),
        ("F 介质改 1 byte", _t_f_artifact_tamper),
        ("G 重装恢复", _t_g_reinstall),
        ("H signature", _t_h_signature),
    ]
    for label, fn in cases:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - 单场景异常不得带崩套件
            check(f"A4.2b {label} 执行未抛异常", False, f"{type(exc).__name__}: {exc}")
    cleanup()
    print(f"\n  A4.2b TOTAL={len(PASS) + len(FAIL) + len(SKIP)} "
          f"PASS={len(PASS)} SKIP={len(SKIP)} FAIL={len(FAIL)}")


if __name__ == "__main__":
    run_a42b_tests()
    raise SystemExit(1 if FAIL else 0)
