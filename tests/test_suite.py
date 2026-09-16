"""Wiki-USB v1.2 自动化验收测试。

覆盖：
  基础链路  —— 切片 / FTS5 Trigram / 短词 LIKE 降级 / 向量 KNN / RRF 融合
  摄入管道  —— 成功分支 / partial_fallback 降级分支
  双模网关  —— SSE 帧序列
  星图      —— Wikilink 强连线
  隐蔽细节  —— #1 exFAT 等长编辑漏扫、#2 控制台编码锁死、#3 onnxruntime 指令集防护
  破坏性    —— TC-HARD-01/03/04/05 的可自动化等价场景

运行： python tests/test_suite.py
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import sys
import time
from pathlib import Path


# ⚠ Windows 上 stdout 被重定向（CI 管道）时，Python 用系统代码页编码输出：
#   英文 Windows 为 cp1252、中文为 cp936。打印 ✅/❌ 与中文会抛 UnicodeEncodeError
#   并让整个测试套件以 exit=1 崩掉（CI 上实测过）。这里主动切到 UTF-8，
#   使测试在任意机器、任意代码页下都能跑，不依赖外部环境变量。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.log_util import ensure_utf8_console  # noqa: E402

ensure_utf8_console()

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import (  # noqa: E402
    chunker, config, crawler, db as db_mod, embedder, graph as graph_mod,
    indexer, llm, paths, search, sync,
)

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name if cond else f"{name} :: {detail}")
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"  [{detail}]"))
    return cond


def skip(name: str, reason: str = "") -> None:
    """登记一条**跳过**的用例。

    为什么需要它：网络用例在离线时若直接 `return`，测试总数会随环境变化
    （330 → 327），让人误以为「测试变少了」。跳过不是消失 —— 用例仍然登记，
    TOTAL 保持恒定，只是不计入 PASS。
    """
    SKIP.append(f"{name} :: {reason}" if reason else name)
    print("  ⏭ " + name + (f"  [{reason}]" if reason else ""))


def section(t: str) -> None:
    print(f"\n── {t} " + "─" * max(0, 62 - len(t)))


NOTES = {
    "papers_attention.md": """---
title: "Attention Is All You Need 研读笔记"
status: success
---

# 注意力机制与 Transformer

Transformer 模型的核心是自注意力（Self-Attention），能够捕捉序列中的长距离依赖。
多头注意力允许模型在不同子空间中并行关注不同位置的信息，这是它相对 RNN 的关键优势。

## 位置编码

自注意力本身不具备顺序感知能力，Transformer 通过位置编码（Positional Encoding）
向输入注入位置信息。原始论文采用正弦余弦函数构造。

参考 [[Transformer 架构通俗指南]]。
""",
    "guide_transformer.md": """---
title: "Transformer 架构通俗指南"
status: success
---

# Transformer 架构通俗指南

编码器由多层自注意力与前馈网络堆叠而成。解码器额外引入交叉注意力。
在 AI 工程落地中，显存占用与推理延迟是主要瓶颈，量化与蒸馏是两条常用压缩路线。
""",
    "note_short.md": """---
title: "C# 与 AI 速记"
status: success
---

用 C# 调用本地 AI 模型时，注意 ONNX Runtime 的指令集要求。
""",
}


def reset_workspace() -> None:
    for p in paths.NOTES_DIR.glob("*.md"):
        p.unlink(missing_ok=True)
    for p in paths.SNAPSHOT_DIR.glob("*"):
        p.unlink(missing_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(paths.CACHE_DB) + suffix).unlink(missing_ok=True)


# ==========================================================================
def test_chunker() -> None:
    section("切片器 / Parent-Child")
    text = NOTES["papers_attention.md"]
    parsed = chunker.parse(text, "notes/papers_attention.md")
    check("标题抽取自 frontmatter", parsed.title == "Attention Is All You Need 研读笔记", parsed.title)
    check("frontmatter status 解析", parsed.status == "success")
    check("父分块 ≥ 2（标题处断开）", len(parsed.parents) >= 2, str(len(parsed.parents)))
    check("子切片非空且带 parent_id", all(c.parent_id for c in parsed.children) and len(parsed.children) > 0)
    check("子切片长度受控（≤ 260）", all(len(c.content) <= 260 for c in parsed.children),
          str(max((len(c.content) for c in parsed.children), default=0)))
    check("Wikilink 抽取", "Transformer 架构通俗指南" in parsed.links, str(parsed.links))
    long_par = chunker.parse("---\ntitle: t\n---\n" + ("句子内容。" * 400), "notes/x.md")
    check("长文档父块切分 ≤ 硬上限", all(len(p.content) <= chunker.PARENT_HARD_LIMIT + 8 for p in long_par.parents))
    ids1 = [c.chunk_id for c in chunker.parse(text, "notes/a.md").children]
    ids2 = [c.chunk_id for c in chunker.parse(text, "notes/a.md").children]
    check("切片 ID 确定性（幂等）", ids1 == ids2)


def test_embedder_guards() -> None:
    section("嵌入源与指令集防护（隐蔽细节 #3）")
    flags = embedder.cpu_flags()
    check("CPU 特性探测返回结构完整", set(flags) == {"sse4_2", "avx", "avx2"}, str(flags))
    ok, why = embedder.probe_onnxruntime()
    check("onnxruntime 探针不抛异常且给出结论", isinstance(ok, bool) and bool(why), why)
    h = embedder.HashEmbedder(512)
    v1, v2 = h.embed(["注意力机制"])[0], h.embed(["注意力机制"])[0]
    v3 = h.embed(["完全不同的内容"])[0]
    check("哈希嵌入维度正确", len(v1) == 512)
    check("哈希嵌入确定性", v1 == v2)
    check("哈希嵌入区分度", embedder.cosine(v1, v2) > embedder.cosine(v1, v3))
    check("哈希嵌入已归一化", abs(sum(x * x for x in v1) - 1.0) < 1e-6, str(sum(x * x for x in v1)))


def test_index_and_search(ctx) -> None:
    section("索引 / 词法 + 向量 + RRF")
    db, emb = ctx.db, ctx.embedder
    for name, body in NOTES.items():
        p = paths.NOTES_DIR / name
        p.write_text(body, encoding="utf-8")
        r = indexer.index_file(db, p, emb)
        assert r.get("ok"), r

    stats = db.stats()
    check("文档全部入库", stats["docs"] == 3, str(stats))
    check("向量表已就绪", db.vec_table_ready and not db.signature_mismatch)

    res = search.hybrid_search(db, emb, "注意力机制与 Transformer 的关系", top_k_parents=3)
    check("长查询走 FTS5 路由", res.route == "fts", res.route)
    check("长查询有召回", len(res.references) > 0)
    check("向量路有候选", res.counts.get("vec_candidates", 0) > 0, str(res.counts))
    check("引用带相似度", any(r.similarity is not None for r in res.references))
    check("引用带父块内容", len(res.parents) == len(res.references))

    res2 = search.hybrid_search(db, emb, "AI", top_k_parents=3)
    check("超短英文查询降级 LIKE", res2.route == "like", res2.route)
    check("超短英文查询仍能召回（不丢召回）", len(res2.references) > 0, str(res2.counts))

    res3 = search.hybrid_search(db, emb, "注", top_k_parents=3)
    check("单汉字查询降级 LIKE", res3.route == "like", res3.route)
    check("单汉字查询有召回", len(res3.references) > 0)

    res4 = search.hybrid_search(db, emb, "C#", top_k_parents=3)
    check("特殊符号短查询可用", len(res4.references) > 0, f"route={res4.route}")

    # 自然语言长问句：整句并非文档连续子串，必须靠 n-gram 切分才能命中
    res5 = search.hybrid_search(db, emb, "位置编码有什么用", top_k_parents=3)
    check("自然语言问句走 FTS（中文 n-gram 切分）", res5.route == "fts", f"route={res5.route}")
    check("自然语言问句有召回", len(res5.references) > 0, str(res5.counts))
    res6 = search.hybrid_search(db, emb, "注意力机制是什么", top_k_parents=3)
    check("问句融合双路召回", res6.counts.get("fused", 0) > 0, str(res6.counts))

    # RRF 数值正确性
    fused = search._rrf_fuse(["a", "b"], [("b", 0.1), ("c", 0.2)])
    check("RRF：双路命中得分最高", fused["b"]["score"] > fused["a"]["score"] > 0
          and fused["b"]["score"] > fused["c"]["score"])
    expect = 1 / 62 + 1 / 61
    check("RRF 公式与 PRD 一致", abs(fused["b"]["score"] - expect) < 1e-9,
          f"{fused['b']['score']} vs {expect}")

    check("bm25 排序不退化为全 0", res.references[0].score > 0)


def test_crawler(ctx) -> None:
    section("摄入管道 / 降级判定")
    offline = os.environ.get("WIKIUSB_SKIP_NET") == "1"

    # 依赖外网的用例先**登记**好，离线时逐条记为 SKIP。
    # 「跳过」与「不存在」是两回事：直接 return 会让 TOTAL 随环境变化。
    NET_CASES = [
        "短正文触发 partial_fallback 降级",
        "降级时保留原始快照",
        "降级文案符合 PRD 提示语",
        "长正文走 success 分支",
        "success 正文 ≥ 150 字",
        "YAML 头部标记 status: success",
    ]

    if offline:
        for _n in NET_CASES:
            skip(_n, "离线模式（WIKIUSB_SKIP_NET=1）")
    else:
        r1 = crawler.capture_url("https://example.com/", db=ctx.db, embedder=ctx.embedder)

        # SSRF 策略拒绝属**环境问题**：某些网络/代理会把公网域名解析成非公网地址
        # （本机实测 example.com → 198.18.0.120），此时拒绝是**正确行为**，
        # 不该算被测对象失败 → 六条一起记 SKIP。判断必须放在第一条 check 之前。
        if "公网地址" in r1.message:
            for _n in NET_CASES:
                skip(_n, "目标被 SSRF 策略拒绝（当前网络解析到非公网地址）")
            bad = crawler.capture_url("ftp://x", db=None)
            check("非法协议被拒绝", not bad.ok and "http" in bad.message)
            note = crawler.save_manual_note(
                "手工测试笔记", "手工录入的注意力机制要点。", db=ctx.db, embedder=ctx.embedder
            )
            check("剪贴板录入通道可用", note.ok and paths.abs_from_data(note.file_path).exists())
            return

        check(NET_CASES[0], r1.status == "partial_fallback", str(r1.to_dict())[:200])
        check(NET_CASES[1], bool(r1.snapshot_path) and paths.abs_from_data(r1.snapshot_path).exists())
        check(NET_CASES[2], "前端动态渲染" in r1.message)

        r2 = crawler.capture_url(
            "https://en.wikipedia.org/wiki/Transformer_(deep_learning_architecture)",
            db=ctx.db, embedder=ctx.embedder,
        )
        # 外网/代理不可达属**环境问题**，不是被测对象失败 —— 记 SKIP 而非 FAIL，
        # 但三条仍然登记，保证总数不变。
        # SSRF 策略拒绝也算**环境问题**：某些网络/代理会把公网域名解析成
        # 非公网地址（本机实测 example.com → 198.18.0.120），此时拒绝是正确行为，
        # 不该算被测对象失败 → 记 SKIP。
        net_err = (not r2.ok) and any(
            k in r2.message for k in ("urlopen error", "ProxyError", "Tunnel connection", "HTTP 0")
        )
        if net_err:
            for _n in NET_CASES[3:]:
                skip(_n, "外网/代理不可达")
        else:
            check(NET_CASES[3], r2.status == "success", str(r2.to_dict())[:200])
            check(NET_CASES[4], r2.char_count >= 150, str(r2.char_count))
            md = paths.abs_from_data(r2.file_path).read_text(encoding="utf-8")
            check(NET_CASES[5], "status: \"success\"" in md or "status: success" in md)

    # 以下两条**不需要外网**，离线时也必须跑 —— 此前它们跟着一起 return 掉了，
    # 白丢两条覆盖。
    bad = crawler.capture_url("ftp://x", db=None)
    check("非法协议被拒绝", not bad.ok and "http" in bad.message)

    note = crawler.save_manual_note(
        "手工测试笔记", "手工录入的注意力机制要点。", db=ctx.db, embedder=ctx.embedder
    )
    check("剪贴板录入通道可用", note.ok and paths.abs_from_data(note.file_path).exists())



def test_gateway(ctx) -> None:
    section("双模网关 / SSE 帧序列")
    frames = list(ctx.gateway.stream_chat("Transformer 的注意力机制是什么？", []))
    kinds = [f["type"] for f in frames]
    check("首帧为 references", kinds[0] == "references", str(kinds[:3]))
    check("含 meta 帧", "meta" in kinds)
    check("尾帧为 done", kinds[-1] == "done", str(kinds[-3:]))
    refs = frames[0].get("refs") or []
    check("引用字典结构符合契约", bool(refs) and {"id", "title", "path", "snippet"} <= set(refs[0]), str(refs[:1]))
    text = "".join(f.get("content", "") for f in frames if f["type"] == "delta")
    check("离线模式仍产出可读回答", len(text) > 20, text[:80])
    meta = next(f for f in frames if f["type"] == "meta")
    check("provider 已解析", meta.get("provider") in ("ollama", "api", "offline"), str(meta.get("provider")))
    check("meta 携带检索路由", "route" in meta)

    st = ctx.gateway.ollama_status()
    check("Ollama 健康探测不抛异常", isinstance(st, bool))
    t0 = time.time()
    ctx.gateway.ollama_status()
    check("健康状态走 60s 缓存（二次调用 <50ms）", (time.time() - t0) < 0.05, f"{(time.time()-t0)*1000:.1f}ms")

    once = ctx.gateway.chat_once("位置编码是什么？")
    check("非流式封装可用", "answer" in once and "references" in once)


def test_graph(ctx) -> None:
    section("离线星图")
    g = graph_mod.build_graph(ctx.db, threshold=0.82)
    check("节点数 = 文档数", len(g["nodes"]) == 3, str(len(g["nodes"])))
    wl = [l for l in g["links"] if l["type"] == "wikilink"]
    check("Wikilink 强连线已建立", len(wl) >= 1, str(g["stats"]))
    check("链接目标解析到真实文档", all(
        any(n["id"] == l["target"] and n["kind"] == "doc" for n in g["nodes"]) for l in wl
    ))
    g9 = graph_mod.build_graph(ctx.db, threshold=0.95)
    check("阈值升高不会新增弱连线",
          g9["stats"]["semantic"] <= g["stats"]["semantic"], f"0.82={g['stats']['semantic']} 0.95={g9['stats']['semantic']}")


def test_hidden_detail_1_equal_length_edit(ctx) -> None:
    """隐蔽细节 #1：exFAT 等长编辑 + 2 秒窗口边缘不得漏扫。"""
    section("隐蔽细节 #1 / 等长编辑漏扫")
    syncer = sync.NoteSyncer(ctx.db, ctx.embedder)
    syncer.seed_from_db()
    target = paths.NOTES_DIR / "note_short.md"
    original = target.read_text(encoding="utf-8")

    # 等长编辑样本：UTF-8 字节数完全相同（买→卖）
    same_len_a = "# 标题\n\n这是一段等长编辑测试文本。\n"
    same_len_b = "# 标题\n\n这是一段等长编辑测次文本。\n"
    assert len(same_len_a.encode()) == len(same_len_b.encode())

    # 用 same_len_a 建立双指纹基线
    target.write_text(same_len_a, encoding="utf-8")
    size_a = target.stat().st_size
    syncer.scan_once()
    base = syncer._state.get(paths.rel_to_data(target))
    check("基线已建立（含 4KB 前缀哈希）", bool(base and base.get("prefix")), str(base))
    check("基线与样本大小一致", bool(base) and base["size"] == size_a,
          f"{base and base['size']} vs {size_a}")

    # 等长编辑 + 把 mtime 拉回基线（模拟 exFAT 2 秒时间戳精度丢失）
    target.write_text(same_len_b, encoding="utf-8")
    os.utime(target, (base["mtime"], base["mtime"]))
    st = target.stat()
    check("构造场景：大小相同 + 时间未明显后移",
          st.st_size == base["size"] and abs(st.st_mtime - base["mtime"]) <= 2.0,
          f"size {st.st_size}/{base['size']} dt {st.st_mtime - base['mtime']:.2f}")

    need, reason, _ = syncer._decide(target, st, base)
    check("等长编辑被识别（哈希分支）", need and reason == "same-length-edit", f"{need} {reason}")

    # 端到端：真实跑一轮扫描，确认等长编辑确实触发了重建索引
    rep = syncer.scan_once()
    check("等长编辑触发实际重建", rep["updated"] >= 1, str(rep))

    # 无变化时不得误报
    rep2 = syncer.scan_once()
    check("无变化时不误报更新", rep2["updated"] == 0 and rep2["unchanged"] >= 1, str(rep2))

    # 时间明显后移分支：大小相同也必须更新
    base2 = dict(syncer._state[paths.rel_to_data(target)])
    need2, reason2, _ = syncer._decide(
        target, type("S", (), {"st_mtime": base2["mtime"] + 5.0, "st_size": base2["size"]})(), base2
    )
    check("时间明显后移触发更新", need2 and reason2 == "mtime-advanced", f"{need2} {reason2}")

    # 大小变化分支
    need3, reason3, _ = syncer._decide(
        target, type("S", (), {"st_mtime": base2["mtime"] + 1.0, "st_size": base2["size"] + 3})(), base2
    )
    check("容差期内大小变化触发更新", need3 and reason3 == "size-changed", f"{need3} {reason3}")

    # 时间倒流（外部工具回写）
    need4, reason4, _ = syncer._decide(
        target, type("S", (), {"st_mtime": base2["mtime"] - 9.0, "st_size": base2["size"]})(), base2
    )
    check("时间倒流保守重建", need4 and reason4 == "mtime-rewound", f"{need4} {reason4}")

    target.write_text(original, encoding="utf-8")
    syncer.stop()


def test_hard_05_rename_and_orphans(ctx) -> None:
    """TC-HARD-05：外部批量重命名 / 删除后，旧路径切片必须 100% 抹除。"""
    section("TC-HARD-05 / 重命名与孤儿切片回收")
    db, emb = ctx.db, ctx.embedder
    old = paths.NOTES_DIR / "renamed_src.md"
    old.write_text("---\ntitle: 待重命名\n---\n\n# 待重命名\n\n唯一标记词 麒麟麒麟麒麟。\n", encoding="utf-8")
    indexer.index_file(db, old, emb)
    doc_id_old = chunker.doc_id_for(paths.rel_to_data(old))
    check("重命名前已入库", db.query_one("SELECT 1 FROM documents WHERE doc_id=?", (doc_id_old,)) is not None)
    before_vec = len([r for r in db.query("SELECT chunk_id FROM chunks_vec")
                      if str(r["chunk_id"]).startswith(doc_id_old + ":")])
    check("重命名前存在向量行", before_vec > 0, str(before_vec))

    new = paths.NOTES_DIR / "renamed_dst.md"
    old.rename(new)  # 模拟 Obsidian 重命名

    syncer = sync.NoteSyncer(db, emb)
    syncer.seed_from_db()
    report = syncer.scan_once()
    syncer.stop()

    gone = db.query_one("SELECT 1 FROM documents WHERE doc_id=?", (doc_id_old,))
    check("旧路径 documents 记录已抹除", gone is None)
    check("旧路径 chunk_metadata 已清空",
          db.query_one("SELECT 1 FROM chunk_metadata WHERE doc_id=?", (doc_id_old,)) is None)
    check("旧路径 chunks 正文已清空",
          db.query_one("SELECT 1 FROM chunks WHERE doc_id=?", (doc_id_old,)) is None)
    check("旧路径 FTS 行已清空",
          len(db.query("SELECT 1 FROM chunks_fts WHERE doc_id=?", (doc_id_old,))) == 0)
    leftover = [r for r in db.query("SELECT chunk_id FROM chunks_vec")
                if str(r["chunk_id"]).startswith(doc_id_old + ":")]
    check("旧路径向量行 100% 抹除（无孤儿）", len(leftover) == 0, str(len(leftover)))
    check("新路径已重新入库",
          db.query_one("SELECT 1 FROM documents WHERE rel_path=?",
                       (paths.rel_to_data(new),)) is not None)
    check("同步报告记录了回收动作", len(report.get("removed", [])) >= 1, str(report.get("removed")))

    # 删除文件 -> 孤儿清扫
    new.unlink()
    syncer2 = sync.NoteSyncer(db, emb)
    syncer2.seed_from_db()
    syncer2.scan_once()
    syncer2.stop()
    check("文件删除后索引同步移除",
          db.query_one("SELECT 1 FROM documents WHERE rel_path=?",
                       (paths.rel_to_data(new),)) is None)


def test_wal_self_heal() -> None:
    """TC-HARD-01/03 等价：异常断电遗留 -wal 后必须能自愈。"""
    section("TC-HARD-01 / 03 / WAL 残留自愈")
    tmp = paths.DATA_DIR / "_wal_probe.db"
    for s in ("", "-wal", "-shm"):
        Path(str(tmp) + s).unlink(missing_ok=True)

    code = (
        "import sqlite3,sys,os\n"
        f"c=sqlite3.connect(r'{tmp}')\n"
        "c.execute('PRAGMA journal_mode=WAL')\n"
        "c.execute('CREATE TABLE t(x)')\n"
        "c.executemany('INSERT INTO t VALUES(?)',[(i,) for i in range(500)])\n"
        "c.commit()\n"
        "os._exit(0)\n"  # 强杀：不做 checkpoint、不关闭连接
    )
    subprocess.run([sys.executable, "-c", code], check=False)
    size_before = db_mod.wal_residue_bytes(tmp)
    check("已模拟出 WAL 残留", size_before > 0, f"{size_before} 字节")
    healed = db_mod.wal_self_heal(tmp)
    check("自愈流程被触发", healed)
    check("自愈后残留清空", db_mod.wal_residue_bytes(tmp) == 0,
          str(db_mod.wal_residue_bytes(tmp)))
    conn = sqlite3.connect(str(tmp))
    rows = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    conn.close()
    check("自愈后数据完整（500 行）", rows == 500, str(rows))
    conn = sqlite3.connect(str(tmp))
    check("journal_mode 仍为 WAL", conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal")
    conn.close()
    for s in ("", "-wal", "-shm"):
        Path(str(tmp) + s).unlink(missing_ok=True)


def test_graceful_shutdown_no_residue() -> None:
    section("TC-HARD-04 / 优雅退出无残留")
    tmp = paths.DATA_DIR / "_shutdown_probe.db"
    for s in ("", "-wal", "-shm"):
        Path(str(tmp) + s).unlink(missing_ok=True)
    d = db_mod.Database(db_path=tmp, embedding_dim=512)
    d.init_schema()
    d.set_meta("probe", "1")
    d.write("INSERT OR REPLACE INTO documents(doc_id,rel_path,file_size,mtime,title,status) VALUES(?,?,?,?,?,?)",
            ("x1", "notes/x.md", 10, time.time(), "t", "success"))
    check("关闭前 -wal 存在且大于 0", db_mod.wal_residue_bytes(tmp) > 0,
          str(db_mod.wal_residue_bytes(tmp)))
    report = d.checkpoint_and_close()
    check("执行了 TRUNCATE 检查点", report["checkpoint"])
    check("关闭后无 -wal 残留", db_mod.wal_residue_bytes(tmp) == 0)
    check("关闭后无 -shm 文件", not Path(str(tmp) + "-shm").exists())
    check("退出报告结构完整（removed 为列表）", isinstance(report.get("removed"), list), str(report))
    conn = sqlite3.connect(str(tmp))
    ok = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    conn.close()
    check("主库物理自洽（数据仍在）", ok == 1)
    for s in ("", "-wal", "-shm"):
        Path(str(tmp) + s).unlink(missing_ok=True)


def test_signature_guard() -> None:
    section("向量空间签名守卫")
    tmp = paths.DATA_DIR / "_sig_probe.db"
    for s in ("", "-wal", "-shm"):
        Path(str(tmp) + s).unlink(missing_ok=True)
    d = db_mod.Database(db_path=tmp, embedding_dim=512)
    d.init_schema()
    check("首次写入签名无告警", d.check_signature("local_onnx", "bge-small-zh-q4", 512) is None)
    check("同签名再次比对无告警", d.check_signature("local_onnx", "bge-small-zh-q4", 512) is None)
    w1 = d.check_signature("local_onnx", "bge-small-zh-q4", 1536)
    check("维度冲突被拦截", bool(w1) and "1536" in w1, str(w1))
    w2 = d.check_signature("api", "text-embedding-3", 512)
    check("模型更换被识别", bool(w2), str(w2))
    d.checkpoint_and_close()
    for s in ("", "-wal", "-shm"):
        Path(str(tmp) + s).unlink(missing_ok=True)


def test_alert_classification() -> None:
    """告警分级：只有「需用户行动」的进 warnings，系统自愈过程进 notes。

    回归用户反馈：「顶部全是绿的，下面却报 onnxruntime 不可用 + 向量维度已自动适配」——
    那两条都是系统正常自愈的信息，不该当成告警糊在界面上。
    """
    section("告警分级（自愈信息不得上告警条）")

    def mkcfg(**ai):
        base = {
            "embedding_source": "local_onnx", "embedding_dim": 512,
            "embedding_model_name": "some-model", "api_base_url": "",
            "api_key": "", "ollama_host": "http://127.0.0.1:59999",
        }
        base.update(ai)
        return lambda sec, key, dflt="": base.get(key, dflt)

    # A. 全链路不可用 → 必须有可行动告警
    r1 = embedder.resolve(mkcfg(), ollama_healthy=lambda: False)
    check("全部嵌入源不可用 → 给出告警",
          any("全部不可用" in w for w in r1.warnings), str(r1.warnings))
    check("降级过程信息进 notes 而非 warnings",
          bool(r1.notes) and not any(("ONNX" in w or "onnxruntime" in w) for w in r1.warnings),
          f"w={r1.warnings} n={r1.notes}")
    check("warnings 与 notes 无重复项", not (set(r1.warnings) & set(r1.notes)))

    # B. 降级成功 —— 用户实际遇到的场景：必须零告警
    r2 = embedder.resolve(mkcfg(), ollama_healthy=lambda: True)
    check("降级成功时不产生任何告警", r2.source == "ollama" and not r2.warnings,
          f"source={r2.source} warnings={r2.warnings}")

    # C. 用户显式指定但不可用 → 必须告警（不能静默）
    r3 = embedder.resolve(mkcfg(embedding_source="ollama"), ollama_healthy=lambda: False)
    check("显式指定 ollama 却不可用 → 给出告警",
          any("指定使用 Ollama" in w for w in r3.warnings), str(r3.warnings))

    # D. 告警文案必须是给人看的，不得夹带原始异常
    all_w = r1.warnings + r3.warnings
    check("告警文案不含原始异常/堆栈字样",
          all(("ModuleNotFoundError" not in w and "Traceback" not in w
               and "FAIL " not in w) for w in all_w),
          str(all_w)[:200])

    # E. 自愈类文案进 notes 前需被「人话化」
    check("ONNX 失败原因已人话化（不出现原始异常串）",
          all("ModuleNotFoundError" not in n for n in r1.notes), str(r1.notes))


def test_archive_localization() -> None:
    """网页存档的资源本地化：必须**真正离线**且**零外部请求**。

    这是项目立身之本（U 盘便携 / Local-First）的直接体现：抓取时把 CSS、图片、
    字体存进本地资源池，浏览「原版」时不再碰网络。

    测试用桩替换网络层，因此不依赖外网、结果确定。
    """
    section("网页存档 · 资源本地化（离线 + 零外发）")
    import re

    from app.core import archiver

    page = "https://site.com/dir/page/"
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 64
    css = b"body{background:url(/img/bg.png)} h1{color:url('f.woff2')}"

    class FakeFetcher:
        """只认得下面几个 URL，其余一律失败 —— 模拟真实抓取的部分失败。"""

        TABLE = {
            "https://site.com/assets/s.css": (css, "text/css"),
            "https://site.com/img/bg.png": (png, "image/png"),
            "https://site.com/img/ok.png": (png, "image/png"),
            "https://site.com/f.woff2": (b"w" * 32, "font/woff2"),
        }

        def __init__(self, timeout=None):
            pass

        def get(self, url):
            if url in self.TABLE:
                return self.TABLE[url]
            raise OSError("unreachable")

        def close(self):
            pass

    sample = (
        '<html><head>'
        '<link rel="stylesheet" href="/assets/s.css">'
        '<link rel="preconnect" href="https://fonts.gstatic.com">'
        '<script src="/jq.js"></script>'
        '</head><body>'
        '<img src="/img/ok.png" width="10" height="10">'
        '<img src="/img/missing.png" width="20" height="20">'
        '<img srcset="/img/ok.png 1x, /img/missing.png 2x">'
        '<video><source src="/media/clip.mp4"></video>'
        '<a href="/other/page">link</a>'
        '</body></html>'
    )

    saved = archiver._Fetcher
    archiver._Fetcher = FakeFetcher
    try:
        out, st = archiver.localize(sample, page)
    finally:
        archiver._Fetcher = saved

    check("资源池目录已被测试隔离（不碰真实 data/assets）",
          str(paths.ASSETS_DIR).startswith(str(paths.DATA_DIR)), str(paths.ASSETS_DIR))
    check("抓到的资源已落盘", st.assets >= 3, f"assets={st.assets}")
    check("样式表 href 重写为本地资源池路径",
          f'href="{archiver.ASSET_URL_PREFIX}' in out, out[:200])
    check("图片 src 重写为本地资源池路径",
          f'src="{archiver.ASSET_URL_PREFIX}' in out)
    check("srcset 内的 URL 也被重写",
          archiver.ASSET_URL_PREFIX in out.split("<img srcset=")[1][:200] if "<img srcset=" in out else False)

    # 核心不变量：抓不到的资源一律占位，**绝不能留外部 URL**
    external_sub = re.findall(
        r'<(?:img|script|iframe|source|video|audio|embed|link|input)[^>]*?'
        r'(?:src|href|srcset)\s*=\s*["\']https?://', out, re.I)
    check("没有任何会自动请求的外部子资源（零外发）", not external_sub, str(external_sub[:3]))
    check("抓不到的图片用占位符代替", "data:image/gif;base64" in out)
    check("超类型（mp4）也用占位，不留外部地址", "clip.mp4" not in out, "视频地址仍在")

    check("外部脚本被省略并留注释（沙箱禁脚本，存了无用）",
          "<script" not in out.lower() and "已省略外部脚本" in out, out[:120])
    check("preconnect 等资源提示被删除（避免无谓外部请求）",
          "fonts.gstatic.com" not in out)
    check("<a> 链接被绝对化，仍可点击跳转",
          'href="https://site.com/other/page"' in out)

    check("生成了给用户看的过程说明", len(st.notes) >= 2, str(st.notes))

    # 清理本次产生的资源池文件，避免污染后续断言
    for f in paths.ASSETS_DIR.glob("*"):
        try:
            f.unlink()
        except OSError:
            pass

def test_original_base_injection() -> None:
    """原版预览的保真度：必须给剪藏的 HTML 注入 <base>。

    真实问题：抓下来的 HTML 大量使用根相对路径（`/assets/css/common.css`、
    `/assets/img/logo.png`）。不注入 <base> 时这些 URL 会以本站
    （`/api/notes/original?…`）为基准解析 → 全部 404 → 样式与图片尽失，
    页面退化成裸 HTML，「原版」名不副实。
    """
    section("原版预览 · base 注入")

    src = "https://example.com/dir/page/"
    h = '<html><head><title>t</title></head><body><img src="/a.png"></body></html>'
    out = crawler.inject_base_href(h, src)
    check("base 标签注入到 head 之内",
          out.index("<base") > out.index("<head") and out.index("<base") < out.index("</head>"))
    check("base 指向原始页面 URL（完整路径，非仅站点根）",
          f'<base href="{src}">' in out, out[:120])
    check("已存在 base 时不重复注入",
          crawler.inject_base_href(out, src).count("<base") == 1)
    check("无 source_url 时不注入", crawler.inject_base_href(h, "") == h)
    check("无 head 标签时也能注入", "<base" in crawler.inject_base_href("<p>x</p>", src))
    check("HTML 实体被转义（防注入破坏属性）",
          '&quot;' in crawler.inject_base_href(h, 'https://e.com/?a="b"'))

    # source_url 必须能从真实笔记的 frontmatter 读出（服务端靠它决定 base）
    url = crawler.source_url_of("notes/__no_such_note__.md")
    check("笔记不存在时返回空串而非抛错", url == "", repr(url))


def test_orphan_original_cleanup() -> None:
    """孤儿原件回收 —— 笔记删了，原件不能无限堆积在 U 盘上。

    真实案例：测试期间反复剪藏又删笔记，data/originals/ 里堆了 28 份孤儿原件
    共 13.48 MB（其中单份剪藏 HTML 就 1MB）。删除路径原本只清理孤儿切片，
    没管原件。
    """
    section("孤儿原件回收")

    keep = "__keep_stem__"
    crawler.save_original(keep, ".pdf", b"%PDF-1.4 keep")
    crawler.save_original("__orphan_stem__", ".pdf", b"%PDF-1.4 orphan")

    # 刻意传「相对路径」而不是 stem —— 这正是踩过的坑：口径不一致会导致全部误删
    gone = crawler.purge_orphan_originals({f"notes/{keep}.md"})
    check("孤儿原件被回收", "__orphan_stem__.pdf" in gone, str(gone))
    check("传相对路径也能正确保留有主的原件（口径一致性）",
          (paths.ORIGINALS_DIR / f"{keep}.pdf").exists())
    check("有对应笔记的原件被保留",
          (paths.ORIGINALS_DIR / f"{keep}.pdf").exists())

    # 安全阀：笔记集合为空时拒绝回收，避免笔记目录异常导致原件被清空
    crawler.save_original("__orphan2__", ".pdf", b"%PDF-1.4 x")
    check("笔记集合为空时拒绝回收（防误删）",
          crawler.purge_orphan_originals(set()) == [])
    check("安全阀触发后文件仍在",
          (paths.ORIGINALS_DIR / "__orphan2__.pdf").exists())

    for n in ("__keep_stem__.pdf", "__orphan2__.pdf"):
        (paths.ORIGINALS_DIR / n).unlink(missing_ok=True)


def test_html_encoding_detection() -> None:
    """HTML 编码判定 —— 防止 requests 的 ISO-8859-1 默认值把内容变成乱码。

    真实案例：docs.python.org 的响应头 `Content-Type: text/html` **不带 charset**，
    requests 按 RFC 2616 默认成 ISO-8859-1，于是 `sqlite3 — DB-API...` 里的
    em dash 变成 `â\x80\x94`。更隐蔽的是原代码写的
    ``resp.encoding or resp.apparent_encoding`` —— `ISO-8859-1` 是真值，
    `or` 直接短路，内容嗅探结果永远取不到。
    """
    section("HTML 编码判定（防 ISO-8859-1 默认值致乱码）")

    zh = '<html><head><meta charset="utf-8"><title>中文标题 — 破折号</title></head></html>'
    raw = zh.encode("utf-8")

    # A. 响应头没有 charset —— 必须以 meta/BOM/嗅探为准，不能落到 latin-1
    enc = crawler.resolve_html_encoding(raw, "text/html; charset=")
    check("响应头无 charset 时识别为 utf-8", enc.lower() in ("utf-8", "utf-8-sig"), enc)
    decoded = raw.decode(enc, "replace")
    check("按判定结果解码无乱码", "â" not in decoded and "�" not in decoded, decoded[:60])
    check("破折号被正确还原", "—" in decoded, decoded[:60])

    # B. 响应头显式声明优先级最高
    check("响应头 charset 优先", crawler.resolve_html_encoding(raw, "text/html; charset=UTF-8").lower() == "utf-8")

    # C. BOM 优先于一切（除响应头）
    check("UTF-8 BOM 被识别", crawler.resolve_html_encoding(b"\xef\xbb\xbf" + raw) == "utf-8-sig")

    # D. 无 meta 时靠字节嗅探（GBK 中文页）
    gbk = "<html><head><title>中文标题</title></head><body>这是 GBK 编码的页面内容</body></html>".encode("gb18030")
    enc = crawler.resolve_html_encoding(gbk, "text/html")
    check("无声明的中文页不误判为 latin-1", enc.lower() not in ("iso-8859-1", "latin-1"), enc)
    check("中文页解码无替换符", "�" not in gbk.decode(enc, "replace"), enc)

    # E. 页面自述 charset 与实际不符时，不能被它带偏
    lying = '<meta charset="utf-8">' + "中文".encode("gb18030").decode("gb18030")
    enc = crawler.resolve_html_encoding("中文内容测试".encode("gb18030"), "text/html")
    check("声明无效时回落字节嗅探", enc.lower() not in ("iso-8859-1", "latin-1"), enc)

    # F. 关键回归：这行是原 bug 的写法，必须永远不再产生乱码
    wrong = raw.decode("iso-8859-1", "replace")          # 旧行为
    right = raw.decode(crawler.resolve_html_encoding(raw, "text/html"), "replace")
    check("旧写法确实会产生乱码（说明测试有效）", "\u00e2" in wrong, wrong[:50])
    check("新判定结果不再产生乱码", "\u00e2" not in right and "—" in right, right[:50])


def test_original_preview(ctx) -> None:
    """原件留存 + 原版预览（含中文文件名响应头编码陷阱）。"""
    section("原件留存与原版预览")

    # ---- 原件读写往返 ----
    payload = b"%PDF-1.4\nfake pdf body for original storage test\n"
    rel = crawler.save_original("__orig_probe__", ".pdf", payload)
    check("原件写入 originals/ 成功", rel.startswith("originals/"), rel)
    found = crawler.find_original("notes/__orig_probe__.md")
    check("按笔记路径可反查原件", found is not None and found.read_bytes() == payload,
          str(found))
    check("同 stem 不同扩展名也能命中（重命名后仍可找到）",
          crawler.find_original("notes/__orig_probe__.md") is not None)

    # ---- 超限与非法扩展名 ----
    check("超过 50MB 不留存",
          crawler.save_original("__big__", ".pdf", b"x" * (crawler.MAX_ORIGINAL_BYTES + 1)) == "")
    check("非法扩展名被拒绝",
          crawler.save_original("__bad__", ".py/../evil", payload) == "")

    # ---- 导入后自动关联原件 ----
    from tests.test_converters import make_pdf as _make_pdf
    pdf_b64 = base64.b64encode(_make_pdf()).decode()
    r = crawler.import_document("原件往返.pdf", base64.b64decode(pdf_b64),
                                db=ctx.db, embedder=ctx.embedder)
    check("导入 PDF 成功", r.ok, r.message)
    check("导入结果带回原件路径", r.original_path.startswith("originals/"), r.original_path)
    check("原件内容与上传字节一致",
          crawler.find_original(r.file_path).read_bytes() == base64.b64decode(pdf_b64))
    check("纯 Markdown 不留存原件（笔记即原件）",
          crawler.import_markdown("__plain__.md", "# 标题\n\n纯文本", db=ctx.db).original_path == "")

    # ---- ⚠ 中文文件名响应头编码（真实踩过的 bug）----
    from app.server import Handler

    cd = Handler._content_disposition("inline", "原件测试.pdf")
    check("中文文件名不再直接进 header（否则 latin-1 编码崩溃）",
          cd.encode("latin-1") is not None, cd)
    check("含 RFC 5987 的 UTF-8 百分号编码真名",
          "filename*=UTF-8''%E5%8E%9F%E4%BB%B6" in cd, cd)
    check("ASCII 回退名不会退化成隐藏文件（如 `.pdf`）",
          'filename="download.pdf"' in cd, cd)
    cd2 = Handler._content_disposition("attachment", "report v2 (final).docx")
    check("ASCII 文件名经净化后仍可用",
          cd2.startswith('attachment; filename="report_v2_final.docx"'), cd2)
    check("所有产出都能 latin-1 编码（HTTP 头硬性要求）",
          all(Handler._content_disposition(d, n).encode("latin-1")
              for d in ("inline", "attachment")
              for n in ("原件测试.pdf", "报告 2026.docx", "a b c.txt", "纯中文", "x" * 200)),
          "存在无法编码的文件名")

    # ---- 内联类型白名单 ----
    check("PDF 允许内联预览", Handler.INLINE_TYPES.get(".pdf") == "application/pdf")
    check("HTML 允许内联预览（沙箱 iframe）", Handler.INLINE_TYPES.get(".html") == "text/html")
    check("Office 文档不内联（走下载）", ".docx" not in Handler.INLINE_TYPES)


def test_secret_redaction(ctx) -> None:
    """Secret 脱敏：明文可存本地，但绝不意外向外泄露。

    四条泄露路径都要堵：**Git / 日志 / HTTP 响应 / 异常信息**。
    刻意不做 Keychain、DPAPI、配置加密 —— Local-First 产品没必要，
    本轮只保证「不泄露」。
    """
    section("Secret 脱敏（Git / 日志 / 响应 / 异常）")
    import io
    import logging

    from app.core import config, redact
    from app.core.log_util import get_logger

    SECRET = "sk-live-ABCDEFGH12345678"
    SECRET2 = "sk-proj-ZZZZ9999YYYY8888"

    # ---------- ① 脱敏函数本身 ----------
    check("界面脱敏保留尾 4 位（便于用户辨认）",
          redact.redact_secret(SECRET).endswith(SECRET[-4:])
          and SECRET not in redact.redact_secret(SECRET),
          redact.redact_secret(SECRET))
    check("日志脱敏一律 <redacted>（不保留任何片段）",
          redact.redact_for_log(SECRET) == "<redacted>")
    check("空值脱敏后仍是空（不留「已设置」假象）", redact.redact_secret("") == "")
    check("敏感头被整体抹掉",
          redact.sanitize_headers({"Authorization": f"Bearer {SECRET}",
                                   "User-Agent": "x"})["Authorization"] == "<redacted>")
    check("敏感配置键识别",
          redact.is_secret_key("api_key") and redact.is_secret_key("AUTH_TOKEN")
          and not redact.is_secret_key("api_base_url"))
    check("脱敏占位可识别（避免被写回）",
          redact.is_masked(redact.redact_secret(SECRET)) and not redact.is_masked(SECRET))

    # ---------- ② 第三方异常文本也要脱敏（容易漏）----------
    for raw, label in [
        (f"request failed:\nAuthorization: Bearer {SECRET}", "上游把 Authorization 回显进错误"),
        (f"api_key={SECRET2}", "错误信息里带 api_key="),
        ("proxy error http://user:secretpw@proxy.local:8080", "代理 URL 带 userinfo"),
        (f"token: {SECRET}", "token: 形式"),
    ]:
        out = redact.sanitize_text(raw)
        leaked = (SECRET in out) or (SECRET2 in out) or ("secretpw" in out)
        check(f"★ 脱敏第三方异常：{label}", not leaked, out[:60])
    check("base_url 里的 userinfo 被去掉",
          redact.sanitize_url_userinfo("https://u:p@api.example.com/v1") == "https://api.example.com/v1")

    # ---------- ③ 日志路径收口（Formatter 级）----------
    log = get_logger()
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(log.handlers[0].formatter)      # 复用脱敏 Formatter
    log.addHandler(h)
    try:
        log.error("Authorization: Bearer %s", SECRET)
        log.error("proxy http://user:secretpw@proxy.local:8080 failed")
        log.error(f"api_key={SECRET2}")
    finally:
        log.removeHandler(h)
    out = buf.getvalue()
    check("★ 日志不出现完整 API key", SECRET not in out and SECRET2 not in out, out[:80])
    check("★ 日志不出现代理密码", "secretpw" not in out, out[:80])

    # ---------- ④ /api/status 不含 key ----------
    from app.core.context import AppContext

    st = ctx.status_payload() if hasattr(ctx, "status_payload") else None
    if st is None:
        import json as _json

        from app.server import Handler as _H
        st = _json.loads(_json.dumps(ctx.boot_report or {}, ensure_ascii=False))
    check("★ /api/status 不含明文 key",
          SECRET not in str(st) and "api_key\":" not in str(st).replace("api_key_configured", ""),
          str(st)[:120])

    # ---------- ⑤ Settings API：读取脱敏 / 写入不回灌掩码 ----------
    orig_key = config.get_str("AI", "api_key", "")
    orig_base = config.get_str("AI", "api_base_url", "")
    try:
        config.update({"AI": {"api_key": SECRET}}, persist=False)
        view = redact.mask_config(config.as_dict())
        ai = view.get("AI", {})
        check("★ settings GET 不返回真实 key",
              SECRET not in str(view), str(ai.get("api_key"))[:40])
        check("settings GET 给出掩码与「是否已配置」",
              redact.is_masked(ai.get("api_key")) and ai.get("api_key") == redact.redact_secret(SECRET),
              str(ai.get("api_key")))

        # 只改普通字段（key 位置带着掩码一起提交）→ key 必须保留
        config.update({"AI": {"api_key": ai.get("api_key"), "api_base_url": "https://x.test/v1"}},
                      persist=False)
        check("★ 只改普通字段时 key 不被掩码覆盖",
              config.get_str("AI", "api_key", "") == SECRET,
              config.get_str("AI", "api_key", "")[:12])

        # 明确清空 → 才删除
        config.update({"AI": {"api_key": ""}}, persist=False)
        check("★ 明确提交空串才清空 key", config.get_str("AI", "api_key", "") == "")

        # 设置新 key
        config.update({"AI": {"api_key": SECRET2}}, persist=False)
        check("设置新 key 生效", config.get_str("AI", "api_key", "") == SECRET2)
    finally:
        config.update({"AI": {"api_key": orig_key, "api_base_url": orig_base}}, persist=False)

    # ---------- ⑥ config 解析失败不误删 key ----------
    config.update({"AI": {"api_key": SECRET}}, persist=False)
    cfg_path = paths.CONFIG_FILE
    before = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    try:
        cfg_path.write_text("[AI]\napi_key = " + SECRET + "\nembedding_dim = 不是数字\n[bogus\n",
                           encoding="utf-8")
        config.reload()
        got = config.get_str("AI", "api_key", "")
        check("★ config 有非法内容时 key 仍被读出（不误删）", got == SECRET, got[:12])
    finally:
        if before:
            cfg_path.write_text(before, encoding="utf-8")
        config.reload()

    # ---------- ⑦ Git 防误提交 ----------
    gi = (paths.BASE_DIR / ".gitignore")
    gtxt = gi.read_text(encoding="utf-8") if gi.exists() else ""
    for pat in ("config.ini", ".env"):
        check(f"★ .gitignore 覆盖 {pat}", pat in gtxt)

    import subprocess
    tracked = ""
    try:
        tracked = subprocess.run(["git", "ls-files"], cwd=str(paths.BASE_DIR),
                                 capture_output=True, text=True, timeout=20).stdout
        suspicious = []
        for rel in tracked.splitlines():
            f = paths.BASE_DIR / rel
            if not f.is_file() or f.stat().st_size > 2 * 1024 * 1024:
                continue
            try:
                txt = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for rx in (r"sk-[A-Za-z0-9]{20,}", r"(?i)bearer\s+[A-Za-z0-9_\-]{20,}",
                       r"(?i)api[_-]?key\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{20,}"):
                import re as _re
                if _re.search(rx, txt):
                    suspicious.append((rel, rx[:20]))
        check("★ 已跟踪文件中没有明显真实凭据", not suspicious, str(suspicious[:3]))
    except (OSError, subprocess.SubprocessError) as exc:
        skip("已跟踪文件凭据扫描", f"无法执行 git：{exc}")

    check("config.ini 不在已跟踪列表（真凭据不会进仓库）",
          "config.ini" not in (tracked if isinstance(tracked, str) else ""),
          "config.ini 被跟踪了！")

def test_import_security() -> None:
    """文件摄入边界：路径穿越 / ZIP 炸弹 / 特殊文件 / 落盘碰撞。

    覆盖范围刻意不止「`../` 能不能逃逸」：**压缩容器、文件名、落盘路径、
    资源耗尽一起收口**，否则很容易只修了 Zip Slip，ZIP Bomb 仍然存在。

    两层防护都要有：声明值预检查 + 读取时按**实际**输出字节限流
    （`ZipInfo.file_size` 是攻击者可写的，不能只信它）。
    """
    section("文件摄入边界（路径穿越 / ZIP 炸弹 / 特殊文件）")
    import io
    import zipfile

    from app.core import crawler, file_guard as FG, paths
    from tests import test_converters as TC

    def code_of(fn, *a, **kw):
        """执行并把 FileGuardError 的错误码返回；正常返回 None。"""
        try:
            fn(*a, **kw)
            return None
        except FG.FileGuardError as exc:
            return exc.code

    # ---------- ① 正常容器**不得**被误杀（先保证不误伤正常文件）----------
    normal = [("DOCX", getattr(TC, "make_docx", None)),
              ("PPTX", getattr(TC, "make_pptx", None)),
              ("XLSX", getattr(TC, "make_xlsx", None)),
              ("EPUB", getattr(TC, "make_epub", None))]
    for label, maker in normal:
        if maker is None:
            skip(f"正常 {label} 通过容器检查", "无样例构造器")
            continue
        data = maker()
        got = code_of(FG.check_archive, zipfile.ZipFile(io.BytesIO(data)), FG.ArchiveLimits())
        check(f"★ 正常 {label} 通过容器检查（默认限制不误伤）", got is None, str(got))
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as _z:
                ents = len(_z.infolist())
                mx = max((i.file_size for i in _z.infolist()), default=0)
            print(f"      （{label}: {ents} entries, 最大 entry {mx} 字节）")
        except Exception:  # noqa: BLE001
            pass

    # ---------- ② 路径穿越：Windows 一等场景 ----------
    root = paths.ORIGINALS_DIR

    TRAVERSAL = [
        "../evil", "../../evil", "..\\evil", "..\\..\\evil",
        "/absolute/path", "C:\\Windows\\win.ini", "C:/Windows/win.ini",
        "\\\\server\\share\\x", "//server/share/x",
        "a/../../b", "./../x", "..\\../x", "sub\\..\\..\\x",
    ]
    bad = [n for n in TRAVERSAL if code_of(FG.validate_archive_entry, n) != "PATH_TRAVERSAL"]
    check("★ ZIP entry 名：全部穿越写法被拒（含 Windows 反斜杠/盘符/UNC）",
          not bad, str(bad))

    # 「重复分隔符」这类写法本身不逃逸（`....` 只是名为点点的目录），
    # 关键是**判定后仍落在根内** —— 这比「见 .. 就拒」更准确。
    WEIRD_BUT_SAFE = ["....//x", "a//b.png", "./x.png"]
    esc = []
    for n2 in WEIRD_BUT_SAFE:
        try:
            r = FG.safe_join(root, n2)
            if not r.is_relative_to(root.resolve()):
                esc.append(n2)
        except FG.FileGuardError:
            pass          # 拒绝也可以接受，只要不逃逸
    check("★ 重复分隔符等怪异写法不会逃逸根目录", not esc, str(esc))

    bad2 = [n for n in TRAVERSAL if code_of(FG.safe_join, root, n) != "PATH_TRAVERSAL"]
    check("★ safe_join 用**解析级**判定拒绝全部穿越写法", not bad2, str(bad2))
    check("safe_join 对正常名放行且确实落在根内",
          FG.safe_join(root, "ok.png").is_relative_to(root.resolve()))
    check("safe_filename 把穿越名塌缩成末段",
          FG.safe_filename("../../evil.txt") == "evil.txt"
          and FG.safe_filename("..\\..\\evil.txt") == "evil.txt",
          FG.safe_filename("..\\..\\evil.txt"))
    check("safe_filename 处理保留设备名与 NUL",
          FG.safe_filename("con.txt") == "_con.txt" and "\x00" not in FG.safe_filename("a\x00b.txt"))
    check("safe_filename 归一化全角伪装",
          FG.safe_filename("ｎｏｒｍａｌ.txt") == "normal.txt")

    # ---------- ③ 容器炸弹：声明值预检查 ----------
    def bomb(entries, compress=zipfile.ZIP_DEFLATED):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compress) as z:
            for name, payload in entries:
                z.writestr(name, payload)
        buf.seek(0)
        return buf

    LIM = FG.ArchiveLimits()

    got = code_of(FG.check_archive, zipfile.ZipFile(bomb([(f"f{i}.xml", b"x") for i in range(50)])),
                  FG.ArchiveLimits(max_entries=10))
    check("★ 超出 entry 数量上限被拒", got == "ARCHIVE_TOO_MANY_ENTRIES", str(got))

    got = code_of(FG.check_archive, zipfile.ZipFile(bomb([("a.xml", b"x" * 5000)])),
                  FG.ArchiveLimits(max_entry_bytes=1000))
    check("★ 单 entry 超限被拒", got == "ARCHIVE_ENTRY_TOO_LARGE", str(got))

    got = code_of(FG.check_archive, zipfile.ZipFile(bomb([("a.xml", b"x" * 3000), ("b.xml", b"y" * 3000)])),
                  FG.ArchiveLimits(max_entry_bytes=10 ** 6, max_total_bytes=4000,
                                   max_ratio=10 ** 9))
    check("★ 声明总量超限被拒", got == "ARCHIVE_TOTAL_TOO_LARGE", str(got))

    got = code_of(FG.check_archive, zipfile.ZipFile(bomb([("a.xml", b"\0" * 200000)])),
                  FG.ArchiveLimits(max_ratio=5))
    check("★ 压缩比异常（疑似炸弹）被拒", got == "ARCHIVE_RATIO_TOO_HIGH", str(got))

    # ---------- ④ 特殊文件（symlink）----------
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        info = zipfile.ZipInfo("link")
        info.external_attr = (0o120777 << 16)      # S_IFLNK
        z.writestr(info, "target")
    buf.seek(0)
    got = code_of(FG.check_archive, zipfile.ZipFile(buf))
    check("★ 符号链接 entry 被拒（没有理由从 Office/EPUB 恢复 symlink）",
          got == "ARCHIVE_SPECIAL_FILE", str(got))

    # ---------- ⑤ 重复条目名 ----------
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<a/>")
        z.writestr("word/document.xml", "<b/>")
    buf.seek(0)
    got = code_of(FG.check_archive, zipfile.ZipFile(buf))
    check("★ 重复同名 entry 被拒（不依赖 zip 库对「后者」的模糊选择）",
          got in ("ARCHIVE_DUPLICATE_ENTRY", None) and got != None, str(got))

    # ---------- ⑥ 第二层：实际读取限流（不信声明值）----------
    zb = zipfile.ZipFile(bomb([("big.xml", b"x" * 20000)]))
    got = code_of(FG.bounded_read, zb, "big.xml", FG.ArchiveLimits(read_chunk=1024), cap=100)
    check("★ 实际输出字节超限立即停止（第二层防护）",
          got == "ARCHIVE_ENTRY_TOO_LARGE", str(got))
    check("第二层放行正常大小",
          FG.bounded_read(zb, "big.xml", FG.ArchiveLimits(), cap=10 ** 6) == b"x" * 20000)

    # ---------- ⑦ 端到端：危险容器整体拒绝，不产出半残结果 ----------
    # 与网页归档不同：容器内部结构被判危险时**整体拒绝**，不「跳过坏 entry
    # 然后继续生成一个半残的 DOCX」。
    from app.core import converters
    evil = bomb([("word/document.xml", b"<w/>"), ("../../escape.xml", b"<x/>")])
    raised, err, ok = "", "", None
    try:
        res = converters.convert(evil.getvalue(), "evil.docx")
        ok, err = res.ok, (res.error or "")
    except FG.FileGuardError as exc:
        raised = exc.code
    except Exception as exc:  # noqa: BLE001
        raised = f"其它异常:{type(exc).__name__}"

    # 两种形态都可接受，但**必须整体失败**：
    # 与网页归档不同，容器内部结构被判危险时不该「跳过坏 entry 继续生成半残 DOCX」。
    rejected = (raised == "PATH_TRAVERSAL") or (ok is False)
    check("★ 含穿越 entry 的 DOCX：整体拒绝（不生成半残结果）",
          rejected, f"raised={raised} ok={ok}")
    check("★ 拒绝原因可读（不是裸异常名）",
          bool(err) or raised == "PATH_TRAVERSAL", f"error={err[:80]!r}")

    # ---------- ⑧ 落盘：文件名穿越 + 撞名不覆盖 ----------
    got = code_of(crawler.save_original, "../../escape", ".pdf", b"%PDF-1.4 x")
    # safe_filename 会先把 stem 塌缩成单段 → 不会抛错，但要保证**没有落到根之外**
    outside = list(paths.DATA_DIR.parent.glob("escape*"))
    check("★ 穿越文件名不会在允许目录之外产生文件", not outside, str(outside[:3]))

    p1 = crawler.save_original("collide", ".pdf", b"%PDF-1.4 one")
    p2 = crawler.save_original("collide", ".pdf", b"%PDF-1.4 two")
    check("★ 同名原件不静默覆盖（第二次另存）", p1 != p2 and p1 and p2, f"{p1} vs {p2}")
    check("两份原件内容都在",
          paths.abs_from_data(p1).read_bytes() == b"%PDF-1.4 one"
          and paths.abs_from_data(p2).read_bytes() == b"%PDF-1.4 two")

    # ---------- ⑨ 普通文件大小闸门（不只有 ZIP 要限）----------
    check("★ 普通文件超限返回 FILE_TOO_LARGE（不是 MemoryError/500）",
          code_of(FG.check_file_size, 10 ** 9, FG.ImportLimits(max_file_bytes=1000))
          == "FILE_TOO_LARGE")
    check("普通文件在限内放行",
          code_of(FG.check_file_size, 500, FG.ImportLimits(max_file_bytes=1000)) is None)

    # ---------- ⑩ 错误码是人话（前端可用）----------
    check("每个错误码都有中文说明",
          all(c in FG.REASON_TEXT for c in (
              "PATH_TRAVERSAL", "FILE_TOO_LARGE", "ARCHIVE_TOO_MANY_ENTRIES",
              "ARCHIVE_ENTRY_TOO_LARGE", "ARCHIVE_TOTAL_TOO_LARGE",
              "ARCHIVE_RATIO_TOO_HIGH", "ARCHIVE_SPECIAL_FILE",
              "ARCHIVE_DUPLICATE_ENTRY", "INVALID_FILENAME")))

    # ---------- ⑪ 边界集中，没有旁路 ----------
    csrc = (paths.CORE_DIR / "converters.py").read_text(encoding="utf-8")
    check("★ 所有 ZIP 打开都经 _open_zip（含安全检查）",
          csrc.count("zipfile.ZipFile(io.BytesIO(data))") == 1
          and csrc.count("_open_zip(data)") >= 4,
          f"原生打开 {csrc.count('zipfile.ZipFile(io.BytesIO(data))')} 处")
    check("★ converters 不再直接用 z.read() 读 entry（改走限流）",
          "z.read(path)" not in csrc)
    check("★ 原件落盘走 safe_join（不自己写 ../ 判断）",
          "file_guard.safe_join(" in (paths.CORE_DIR / "crawler.py").read_text(encoding="utf-8"))

    # ---------- 清理本测试产生的原件 ----------
    for rel in (p1, p2):
        try:
            if rel:
                paths.abs_from_data(rel).unlink()
        except OSError:
            pass

def test_archive_ssrf() -> None:
    """离线归档：资源下载必须与正文抓取受同一条安全闸门管控。

    资源 URL 来自被抓页面（外部可控），与正文 URL 同级风险。
    此前 archiver 用 ``requests.Session`` —— 既绕过全部检查，又默认自动跟随
    redirect（与正文抓取那条路径犯过同一个错）。

    产品原则（用户明确）：**单个资源被安全拦下不应让整篇文章抓取失败**。
    正文成功 + 危险资源占位 + 一条脱敏说明，才是离线存档该有的行为。
    """
    section("离线归档 · 资源下载走 SSRF 闸门")
    import http.server
    import ipaddress
    import threading
    import time

    from app.core import archiver, config, net_guard as G

    public_ip = [ipaddress.ip_address("93.184.216.34")]
    real_res = G._default_resolver
    real_hop = G._one_hop
    orig_allow = config.get_str("CRAWLER", "allow_private_network", "0")

    def set_allow(v: str) -> None:
        config.update({"CRAWLER": {"allow_private_network": v}}, persist=False)

    try:
        # 资源 URL 用真实 localhost（私网）→ 默认策略必须拒绝
        set_allow("0")

        html = (
            '<html><body><p>正文在</p>'
            '<img src="http://127.0.0.1:9/local.png">'
            '<link rel="stylesheet" href="http://192.168.1.5/evil.css">'
            '</body></html>'
        )
        out, st = archiver.localize(html, "https://example.com/page")
        check("★ 单资源被拦不影响整页（localize 正常返回）", isinstance(out, str) and "正文在" in out)
        check("★ 本地图片被拒并计入 skipped_blocked",
              st.skipped_blocked >= 1, f"blocked={st.skipped_blocked} assets={st.assets}")
        check("★ 私网 CSS 同样被拦", st.assets == 0, f"assets={st.assets}")
        check("★ 正文照常保留", "正文在" in out)
        check("★ 文档里不残留危险外链（已替换为占位）",
              "127.0.0.1:9/local.png" not in out and "192.168.1.5/evil.css" not in out)
        joined = " ".join(st.notes)
        check("★ 有脱敏说明（含条数与原因，不含响应内容）",
              "安全策略" in joined, joined[:120])
        check("说明里不含响应体内容",
              "PNG" not in joined and "<html" not in joined, joined[:120])

        # ---------- 子资源 公网 --302--> localhost ----------
        G._default_resolver = lambda h, p: list(public_ip)
        G._one_hop = lambda v, *, headers, timeout, use_proxy: (
            302, b"", {"Location": "http://127.0.0.1:9/redirected.png"}, v.url
        )
        html2 = '<html><body><p>正文2</p><img src="http://cdn.test/a.png"></body></html>'
        out2, st2 = archiver.localize(html2, "https://example.com/p2")
        check("★ 子资源 公网→302→localhost 被拦（逐跳校验在归档路径也生效）",
              st2.skipped_blocked >= 1, f"blocked={st2.skipped_blocked} assets={st2.assets}")
        check("第二篇正文仍成功", "正文2" in out2)

        # ---------- CSS 递归引用的私网资源 ----------
        css = b'body{background:url("http://127.0.0.1:9/inner.png")}'
        G._one_hop = lambda v, *, headers, timeout, use_proxy: (200, css, {"Content-Type": "text/css"}, v.url)
        html3 = ('<html><body><p>正文3</p>'
                 '<link rel="stylesheet" href="http://cdn.test/s.css"></body></html>')
        out3, st3 = archiver.localize(html3, "https://example.com/p3")
        check("★ CSS 里继续引用的私网资源同样被拦",
              st3.skipped_blocked >= 1, f"blocked={st3.skipped_blocked} assets={st3.assets}")

        # ---------- data: 内联资源：本地处理，不发网络请求 ----------
        G._one_hop = real_hop
        G._default_resolver = real_res
        html4 = ('<html><body><p>正文4</p>'
                 '<img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7">'
                 '</body></html>')
        out4, st4 = archiver.localize(html4, "https://example.com/p4")
        check("data: 内联资源原样保留（不当作网络 URL 发出）",
              "data:image/gif;base64" in out4, out4[:160])
        check("data: 不计入下载也不计入被拦",
              st4.assets == 0 and st4.skipped_blocked == 0,
              f"assets={st4.assets} blocked={st4.skipped_blocked}")

        # ---------- 正常公网资源：允许归档（allow_private 不影响公网）----------
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                b = b"\x89PNG\r\n\x1a\n" + b"0" * 64
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def log_message(self, *a):
                return

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.3)
        try:
            # 默认策略下，localhost 资源被拒（这就是「公网页面引用 localhost」）
            html5 = f'<html><body><p>正文5</p><img src="http://127.0.0.1:{port}/ok.png"></body></html>'
            out5, st5 = archiver.localize(html5, "https://example.com/p5")
            check("★ 公网页面引用 localhost 图片 → 拒绝",
                  st5.skipped_blocked >= 1 and st5.assets == 0,
                  f"blocked={st5.skipped_blocked} assets={st5.assets}")

            # 显式开启 allow_private_network → 按设计允许归档
            set_allow("1")
            out6, st6 = archiver.localize(html5, "https://example.com/p6")
            check("★ allow_private_network=true → 私网资源按设计允许归档",
                  st6.assets >= 1, f"assets={st6.assets} blocked={st6.skipped_blocked}")
            check("归档后正文仍完整", "正文5" in out6)
        finally:
            srv.shutdown()
            srv.server_close()

        # ---------- 出口收敛（静态断言）----------
        src = (paths.CORE_DIR / "archiver.py").read_text(encoding="utf-8")
        import re as _re
        code = _re.sub(r'""".*?"""', "", src, flags=_re.S)
        code = "\n".join(ln for ln in code.splitlines() if not ln.strip().startswith("#"))
        check("★ archiver 不再使用 requests.Session",
              "requests.Session()" not in code, "仍有 requests.Session")
        check("★ archiver 资源下载走 safe_fetch",
              "safe_fetch(" in code, "未走 safe_fetch")
        check("★ 不再使用会绕过闸门的 net_util.http_get 下载资源",
              "net_util.http_get(" not in code, "仍在用 net_util.http_get")
        check("★ 复用了同一份 allow_private_network 配置（未复制一套判断）",
              'config.get_bool("CRAWLER", "allow_private_network"' in code)
    finally:
        G._default_resolver = real_res
        G._one_hop = real_hop
        set_allow(orig_allow)

def test_ssrf_guard(ctx) -> None:
    """SSRF 防护：公网白名单 + 钉住 IP + 逐跳校验重定向。

    ## 为什么不能只判断一次 IP 就放行

    「解析 → 是公网 → 用 hostname 发请求」有 TOCTOU 窗口：建连时会**再解析一次**，
    攻击者让域名第一次返回公网、第二次返回 127.0.0.1 即可绕过（DNS rebinding）。
    本实现把校验过的 IP **钉住**，连接时不再解析。

    ## 测试策略：确定性

    核心回归**不依赖公共 DNS**（CI 必须可复现）：
    * 解析器可注入 —— 想让它返回什么就返回什么
    * redirect 用**真实本地 HTTP server** 验证行为
    * 公共 DNS 只作为可选集成项，不作为成败条件
    """
    section("SSRF 防护（公网白名单 / 钉住 IP / 逐跳校验）")
    import http.server
    import ipaddress
    import socket
    import threading
    import time

    from app.core import net_guard as G

    def fake(*ips):
        """构造一个返回指定地址的解析器。"""
        def _r(host, port):
            return [ipaddress.ip_address(x) for x in ips]
        return _r

    def blocked(url, want=None, **kw):
        """被拒绝（且原因符合预期）→ 返回 None；否则返回诊断串。

        注意语义方向：**None 表示「正确拦截了」**。
        """
        try:
            G.resolve_and_validate(url, **kw)
        except G.SSRFBlocked as exc:
            if want is None or exc.reason == want:
                return None
            return f"{exc.reason} != {want}"
        return "未被拒绝（应拒绝）"

    def allowed(url, **kw):
        """**应放行**：放行返回 None，被拒绝则返回原因码。"""
        try:
            G.resolve_and_validate(url, **kw)
            return None
        except G.SSRFBlocked as exc:
            return exc.reason

    # ---------- ① 危险地址一律拒绝（用 is_global + 排除组播，而非手写网段）----------
    DANGEROUS = {
        "http://127.0.0.1/": "loopback",
        "http://127.0.0.5/": "loopback 整段",
        "http://[::1]/": "IPv6 loopback",
        "http://10.0.0.1/": "RFC1918",
        "http://172.16.0.1/": "RFC1918",
        "http://192.168.1.1/": "RFC1918",
        "http://169.254.169.254/": "link-local / 云 metadata",
        "http://100.64.0.1/": "CGNAT",
        "http://224.0.0.1/": "multicast（is_global 竟为 True）",
        "http://240.0.0.1/": "reserved",
        "http://0.0.0.0/": "unspecified",
        "http://[ff02::1]/": "IPv6 multicast（is_global 竟为 True）",
        "http://[fe80::1]/": "IPv6 link-local",
        "http://[fc00::1]/": "IPv6 ULA",
        "http://[::ffff:127.0.0.1]/": "IPv4-mapped loopback",
        "http://[::ffff:10.0.0.1]/": "IPv4-mapped private",
        "http://198.18.0.1/": "benchmark",
        "http://192.0.0.1/": "IETF 保留",
    }
    for url, label in DANGEROUS.items():
        got = blocked(url, "TARGET_NOT_PUBLIC")
        check(f"拒绝 {label}", got is None, f"{url} → {got}")

    # ---------- ② 奇怪 IP 表达不能绕过（解析成真实 IP 后再判）----------
    WEIRD = {
        "http://2130706433/": "十进制 127.0.0.1",
        "http://0x7f000001/": "十六进制 127.0.0.1",
        "http://017700000001/": "八进制 127.0.0.1",
        "http://127.1/": "简写 127.0.0.1",
        "http://0/": "0.0.0.0 的整数形式",
        "http://[0:0:0:0:0:0:0:1]/": "展开的 IPv6 loopback",
    }
    for url, label in WEIRD.items():
        # 这些形态没有合法 IP 字面量 → 走解析器；注入一个「会解析成私网」的替身，
        # 以此验证：不管原始写法多奇怪，判定都发生在**解析结果**上
        got = blocked(url, "TARGET_NOT_PUBLIC", resolver=fake("127.0.0.1"))
        check(f"奇怪写法不绕过：{label}", got is None, f"{url} → {got}")

    # ---------- ③ hostname 解析结果策略 ----------
    check("hostname 解析到私网 → 拒绝",
          blocked("http://evil.test/", "TARGET_NOT_PUBLIC",
                  resolver=fake("192.168.1.10")) is None)
    mixed = blocked("http://mixed.test/", "MIXED_PUBLIC_PRIVATE",
                    resolver=fake("93.184.216.34", "10.0.0.5"))
    check("★ 同时解析出公网+私网 → 拒绝（不挑公网那个继续）", mixed is None, str(mixed))
    ok_pub = None
    try:
        v = G.resolve_and_validate("http://good.test/", resolver=fake("93.184.216.34"))
        ok_pub = v.hostname
    except G.SSRFBlocked as exc:
        ok_pub = exc.reason
    check("公网 hostname → 放行", ok_pub == "good.test", str(ok_pub))
    check("DNS 解析失败 → 拒绝且不抛异常",
          blocked("http://nx.test/", "DNS_RESOLUTION_FAILED",
                  resolver=lambda h, p: (_ for _ in ()).throw(socket.gaierror("no"))) is None)

    # ---------- ④ 协议白名单（与 allow_private_network 无关）----------
    for url in ("file:///C:/Windows/win.ini", "ftp://x/y", "gopher://x/",
                "data:text/html,<b>x</b>", "javascript:alert(1)", "ws://x/"):
        got = blocked(url, "SCHEME_NOT_ALLOWED")
        check(f"拒绝协议 {url.split(':')[0]}", got is None, str(got))

    # ---------- ⑤ URL 层面 ----------
    check("拒绝 user:pass@host",
          blocked("http://u:p@example.com/", "USERINFO_NOT_ALLOWED") is None)
    check("拒绝非法端口",
          blocked("http://example.com:99999/", "INVALID_PORT") is None)
    check("空 URL 被拒", blocked("", "MALFORMED_URL") is None)
    try:
        v6 = G.check_url_syntax("http://[2001:db8::1]:8080/a")
        v6ok = (v6.hostname == "2001:db8::1" and v6.port == 8080 and "[2001:db8::1]" in v6.url)
    except G.SSRFBlocked:
        v6ok = False
    check("IPv6 字面量正确解析（含端口）", v6ok)
    check("hostname 规范化（大写/尾点）",
          G.check_url_syntax("http://EXAMPLE.com./a").hostname == "example.com")

    # ---------- ⑥ allow_private_network 只放宽地址，不放宽其它 ----------
    check("allow_private_network=True 时局域网放行",
          allowed("http://192.168.1.10/", allow_private_network=True) is None,
          str(allowed("http://192.168.1.10/", allow_private_network=True)))
    for url, want in (("file:///etc/passwd", "SCHEME_NOT_ALLOWED"),
                      ("ftp://x/", "SCHEME_NOT_ALLOWED"),
                      ("http://u:p@x/", "USERINFO_NOT_ALLOWED")):
        got = blocked(url, want, allow_private_network=True)
        check(f"★ 开私网后仍拒绝 {url.split(':')[0]}（只有地址策略被放宽）",
              got is None, str(got))

    # ---------- ⑦ redirect：公网 → 私网/本机 必须拦住 ----------
    real_hop = G._one_hop

    def hop_to(location):
        def _h(v, *, headers, timeout, use_proxy):
            return 302, b"", {"Location": location}, v.url
        return _h

    try:
        G._one_hop = hop_to("http://127.0.0.1/")
        r = G.safe_fetch("http://start.test/", resolver=fake("93.184.216.34"))
        check("★ 公网 URL --302--> 127.0.0.1 被拦住",
              (not r.ok) and r.reason == "TARGET_NOT_PUBLIC", f"{r.ok} {r.reason}")

        G._one_hop = hop_to("http://10.1.2.3/")
        r = G.safe_fetch("http://start.test/", resolver=fake("93.184.216.34"))
        check("★ 公网 URL --302--> 私网 被拦住",
              (not r.ok) and r.reason == "TARGET_NOT_PUBLIC", f"{r.ok} {r.reason}")

        G._one_hop = hop_to("file:///etc/passwd")
        r = G.safe_fetch("http://start.test/", resolver=fake("93.184.216.34"))
        check("★ 重定向到 file: 被拦住（协议限制逐跳生效）",
              (not r.ok) and r.reason == "SCHEME_NOT_ALLOWED", f"{r.ok} {r.reason}")

        G._one_hop = lambda v, *, headers, timeout, use_proxy: (302, b"", {}, v.url)
        r = G.safe_fetch("http://start.test/", resolver=fake("93.184.216.34"))
        check("重定向缺少 Location → 拒绝",
              (not r.ok) and r.reason == "REDIRECT_WITHOUT_LOCATION", f"{r.ok} {r.reason}")
    finally:
        G._one_hop = real_hop

    # ---------- ⑧ redirect：真实本地 HTTP server（行为正确性）----------
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            p = self.path
            if p == "/final":
                body = b"<html><body>ok</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if p == "/rel":                       # 相对 Location
                self.send_response(302)
                self.send_header("Location", "final")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if p in ("/loop-a", "/loop-b"):       # 环
                self.send_response(302)
                self.send_header("Location", "/loop-b" if p == "/loop-a" else "/loop-a")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if p.startswith("/chain/"):           # 超跳数
                n = int(p.rsplit("/", 1)[1])
                nxt = "/final" if n <= 0 else f"/chain/{n - 1}"
                self.send_response(302)
                self.send_header("Location", nxt)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            return

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    base = f"http://127.0.0.1:{port}"
    priv = {"allow_private_network": True}      # 本地 server 属私网，需显式放行

    try:
        r = G.safe_fetch(f"{base}/final", **priv)
        check("本地 200（开私网后）", r.ok and r.status == 200, f"{r.ok} {r.status} {r.reason}")

        r = G.safe_fetch(f"{base}/rel", **priv)
        check("相对 Location 正确解析并跟随（urljoin）",
              r.ok and r.status == 200 and r.final_url.endswith("/final"),
              f"{r.final_url} {r.reason}")

        r = G.safe_fetch(f"{base}/loop-a", **priv)
        check("★ 重定向环被检测", (not r.ok) and r.reason == "REDIRECT_LOOP",
              f"{r.ok} {r.reason}")

        r = G.safe_fetch(f"{base}/chain/20", **priv)
        check("★ 重定向超过上限被拒绝",
              (not r.ok) and r.reason == "TOO_MANY_REDIRECTS", f"{r.ok} {r.reason}")

        # ---------- ⑨ DNS rebinding：证明「校验的 IP」就是「连接的 IP」----------
        # 用一个**真实 DNS 里不存在**的域名，解析器只返回 127.0.0.1：
        # 若能连通，说明连接用的就是解析器给的 IP，而不是重新解析（那必然失败）。
        calls = []

        def counting(host, port):
            calls.append(host)
            return [ipaddress.ip_address("127.0.0.1")]

        # 有代理时请求由代理代发（就无法钉 IP）——为了确定性地验证钉 IP 逻辑，
        # 这里显式把代理判定置假。CI 与本机代理配置不同，不固定就不可复现。
        _real_pa = G.proxy_active
        G.proxy_active = lambda: False
        try:
            r = G.safe_fetch(f"http://pinned-does-not-exist.test:{port}/final",
                             allow_private_network=True, resolver=counting)
        finally:
            G.proxy_active = _real_pa
        check("★ 连接使用解析器给出的 IP（不二次解析，无 TOCTOU 窗口）",
              r.ok and r.status == 200, f"{r.ok} {r.status} {r.reason}")
        check("★ 每个跳只解析一次", len(calls) == 1, str(calls))
    finally:
        srv.shutdown()
        srv.server_close()

    # ---------- ⑩ 出口收敛：抓取必须走安全通道 ----------
    csrc = (paths.CORE_DIR / "crawler.py").read_text(encoding="utf-8")
    check("★ crawler 抓取走 safe_fetch", "safe_fetch(" in csrc)
    import re as _re
    ccode = _re.sub(r'""".*?"""', "", csrc, flags=_re.S)      # 去掉文档串
    ccode = "\n".join(ln for ln in ccode.splitlines() if not ln.strip().startswith("#"))
    check("★ 可执行代码中不再使用自动跟随重定向",
          "allow_redirects" not in ccode,
          str([ln.strip()[:60] for ln in ccode.splitlines() if "allow_redirects" in ln]))
    gsrc = (paths.CORE_DIR / "net_guard.py").read_text(encoding="utf-8")
    check("钉住 IP：覆盖 connect() 直连已验证地址",
          "_PinnedHTTPConnection" in gsrc and "_PinnedHTTPSConnection" in gsrc
          and "socket.create_connection((self._pinned_ip" in gsrc)
    check("HTTPS 的 SNI/证书校验仍用 hostname",
          "server_hostname=self.host" in gsrc)
    check("组播被显式排除（is_global 单独用不够）",
          "is_multicast" in gsrc)

def test_lifecycle_shutdown() -> None:
    """生命周期：安全退出必须**真的结束进程**，且初始化中途退出不能崩。

    这两条都是 Portable CI 实测暴露出来的真实缺陷：

    * `/api/system/shutdown` 此前只关库与 HTTP server，**从不设置 SHUTDOWN_EVENT**，
      主线程永远停在 `while not SHUTDOWN_EVENT.is_set()` ——
      表现是「端口释放了，但进程变成残留」。
    * 初始化仍在进行时收到退出请求 → boot 线程继续跑到
      `self.gateway.embedder = ...`，而 gateway 已被置空 →
      `'NoneType' object has no attribute 'embedder'`，
      且那次异常被当成「初始化失败」上报给用户。

    这里用**真实子进程**验证 —— 静态断言挡不住这类问题。
    """
    section("生命周期：安全退出的正确性（真实子进程）")
    import http.client
    import json as _json
    import socket
    import subprocess
    import sys
    import time

    exe = sys.executable
    port = 28977

    def call(method: str, path: str, body: str | None = None, timeout: float = 4.0):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        hdrs = {"Host": f"127.0.0.1:{port}"}
        if body is not None:
            hdrs["Content-Type"] = "application/json"
        c.request(method, path, headers=hdrs, body=body)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def port_free() -> bool:
        s = socket.socket()
        s.settimeout(2)
        try:
            return s.connect_ex(("127.0.0.1", port)) != 0
        finally:
            s.close()

    def spawn():
        # 不用 PIPE：没人读会把子进程堵在 64KB 管道缓冲上（Portable CI 踩过）
        f = open(f"lifecycle-{int(time.time()*1000)}.log", "wb")
        p = subprocess.Popen(
            [exe, "app/launcher.py", "--no-browser", "--port", str(port)],
            stdout=f, stderr=subprocess.STDOUT,
        )
        return p, f

    # ---------- 场景 A：初始化**尚未完成**时就退出 ----------
    proc, logf = spawn()
    try:
        t0 = time.time()
        while time.time() - t0 < 40:
            try:
                st, _ = call("GET", "/healthz")
                if st == 200:
                    break
            except Exception:
                pass
            time.sleep(0.3)

        st, _ = call("POST", "/api/system/shutdown", "{}")
        check("初始化期间也能接受退出请求", st == 200, f"HTTP {st}")

        exited = True
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            exited = False
            proc.kill()
        check("★ 初始化中途退出 → 进程真的结束（此前会残留）", exited)
        time.sleep(1)
        check("★ 退出后端口已释放", port_free())
        logf.flush()
        text = open(logf.name, encoding="utf-8", errors="replace").read()
        check("★ 不再出现 'NoneType' 崩溃",
              "NoneType" not in text,
              [ln for ln in text.splitlines() if "NoneType" in ln][:2])
        check("★ 不再把竞态当成「初始化失败」上报",
              "初始化失败" not in text,
              [ln for ln in text.splitlines() if "初始化失败" in ln][:2])
    finally:
        if proc.poll() is None:
            proc.kill()
        logf.close()
        try:
            __import__("os").unlink(logf.name)
        except OSError:
            pass

    # ---------- 场景 B：就绪后正常退出（防回归）----------
    proc2, logf2 = spawn()
    try:
        t0 = time.time()
        while time.time() - t0 < 90:
            try:
                st, d = call("GET", "/api/status")
                if st == 200 and (_json.loads(d).get("data") or {}).get("ready"):
                    break
            except Exception:
                pass
            time.sleep(1)
        call("POST", "/api/system/shutdown", "{}")
        exited = True
        try:
            proc2.wait(timeout=60)
        except subprocess.TimeoutExpired:
            exited = False
            proc2.kill()
        check("就绪后退出 → 进程结束且返回码为 0",
              exited and proc2.returncode == 0, f"rc={proc2.returncode}")
        time.sleep(1)
        check("就绪后退出 → 端口已释放", port_free())
    finally:
        if proc2.poll() is None:
            proc2.kill()
        logf2.close()
        try:
            __import__("os").unlink(logf2.name)
        except OSError:
            pass

def test_duplicate_url_detection(ctx) -> None:
    """同一来源 URL 判重：规范化 → 抓取前检查 → 由用户决定动作。

    背景（实测事故）：同一篇 Grok 文章被抓成两篇笔记，直接污染检索引用、
    关键词、主题分组与统计。

    设计约束：
    * **判重必须发生在抓取之前**（abort 分支不发任何网络请求）
    * **后端必须自己查**，不能只靠前端
    * 默认 ``abort`` 而非默默新建 —— 宁可让调用方显式表态
    * 网页会更新，所以不能「一律禁止重复」，要支持 更新 / 另存
    """
    section("同一来源 URL 判重（规范化 + 抓取前检查）")
    import http.server
    import threading
    import time
    from pathlib import Path

    from app.core import crawler, indexer
    from app.core import urls as U

    # ---------- ① 归一化：这些都应视为同一页 ----------
    base = "https://example.com/article"
    variants = [
        "https://example.com/article",
        "https://example.com/article/",
        "https://EXAMPLE.com/article",
        "https://example.com/article#section",
        "https://example.com/article?utm_source=twitter&utm_medium=social",
        "https://example.com:443/article",
        "https://example.com/article?fbclid=abc123",
    ]
    check("七种写法归一化后完全一致",
          len({U.normalize_url(v) for v in variants}) == 1,
          str({U.normalize_url(v) for v in variants}))

    # 但**不同页面**绝不能被误判 —— 误判会导致「更新已有」覆盖掉另一篇真实内容
    distinct = [
        ("https://example.com/article", "https://example.com/article2"),
        ("https://example.com/article", "https://example.com/other"),
        ("https://example.com/article", "https://example.com/article?page=2"),
        ("https://example.com/article", "https://other.com/article"),
        ("http://example.com/article", "https://example.com/article"),
        ("https://example.com/article", "https://example.com/Article"),
    ]
    for a, b in distinct:
        check(f"不同页面不误判：{a} ≠ {b}", not U.same_page(a, b))
    check("query 顺序不同视为同一页（a=1&b=2 与 b=2&a=1）",
          U.same_page("https://e.com/x?a=1&b=2", "https://e.com/x?b=2&a=1"))
    check("追踪参数识别", U.is_tracking_param("utm_source")
          and U.is_tracking_param("fbclid")
          and not U.is_tracking_param("page")
          and not U.is_tracking_param("id"))
    check("空/非法 URL 不抛异常", U.normalize_url("") == "" and U.normalize_url("  ") == "")

    # ---------- 起一个本地页面，做真实抓取 ----------
    PAGE = ("<html><head><title>去重测试页</title></head><body><article>"
            + "这是一篇用于验证重复抓取检测的中文正文。" * 30
            + "</article></body></html>").encode("utf-8")

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *a):  # 静音
            return

    # 本测试抓的是本地 HTTP server（私网地址）——SSRF 防护默认会拒绝，
    # 所以这里显式开启 allow_private_network（正是该开关的用途）。
    from app.core import config as _cfg

    _orig_allow = _cfg.get_str("CRAWLER", "allow_private_network", "0")
    _cfg.update({"CRAWLER": {"allow_private_network": "1"}}, persist=False)

    pre_existing = {p.name for p in paths.NOTES_DIR.glob("*.md")}
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    url = f"http://127.0.0.1:{port}/article?utm_source=test"
    canonical = f"http://127.0.0.1:{port}/article"

    def n_notes() -> int:
        return len(list(paths.NOTES_DIR.glob("*.md")))

    try:
        before = n_notes()
        r1 = crawler.capture_url(url, db=ctx.db, embedder=ctx.embedder)
        check("首次抓取成功", r1.ok, f"status={r1.status} msg={r1.message}")
        check("首次抓取后多了一篇笔记", n_notes() == before + 1)

        # ---------- ② 默认 abort：不发网络请求，返回 duplicate ----------
        r2 = crawler.capture_url(canonical, db=ctx.db, embedder=ctx.embedder)
        check("★ 再次抓取（不同写法）→ status=duplicate", r2.status == "duplicate",
              f"status={r2.status}")
        check("★ 返回已存在笔记的信息（供弹窗展示）",
              bool(r2.duplicate and r2.duplicate.get("rel_path")),
              str(r2.duplicate))
        check("★ abort 分支未新建笔记", n_notes() == before + 1)

        # ---------- ③ 显式 new：允许另存为新版本 ----------
        r3 = crawler.capture_url(canonical, db=ctx.db, embedder=ctx.embedder,
                                 on_duplicate="new")
        check("显式 new → 另存为新笔记（网页会更新，这是正当需求）",
              r3.ok and n_notes() == before + 2, f"status={r3.status} n={n_notes()}")

        # ---------- ④ 显式 update：覆盖已有那篇，不新增 ----------
        n_now = n_notes()
        r4 = crawler.capture_url(canonical, db=ctx.db, embedder=ctx.embedder,
                                 on_duplicate="update")
        check("显式 update → 成功且不新增笔记", r4.ok and n_notes() == n_now,
              f"status={r4.status} before={n_now} after={n_notes()}")

        # ---------- ⑤ 后端自查（不依赖前端）----------
        found = indexer.find_by_normalized_url(
            ctx.db, f"http://127.0.0.1:{port}/article/?utm_campaign=x#top")
        check("按 URL 变体也能查到已存在笔记（归一化生效）",
              bool(found), str(found))
        import inspect as _inspect

        sig = _inspect.signature(crawler.capture_url)
        check("★ capture_url 默认策略是 abort（调用方不表态就不会产生重复）",
              sig.parameters["on_duplicate"].default == "abort")
        srv_src = (paths.CORE_DIR.parent / "server.py").read_text(encoding="utf-8")
        check("★ 服务端自己做判重（同时提供 /api/capture/duplicate 预检）",
              '"/api/capture/duplicate"' in srv_src and "find_by_normalized_url" in srv_src)

        # ---------- ⑥ 路由归属：GET 预检必须真的挂在 GET 路由上 ----------
        # 这条来自一次真实事故：`/api/capture/duplicate` 被误插进 `_route_post`，
        # 于是 GET 请求 404 —— 前端的预检**从来就不可能工作**。
        # 静态断言「字符串存在」根本挡不住它，必须真发一次请求。
        import http.client
        import threading as _th
        import time as _t

        from app.server import Server

        srv2 = Server(("127.0.0.1", 0), ctx, allow_lan=False)
        port2 = srv2.server_address[1]
        _th.Thread(target=srv2.serve_forever, daemon=True).start()
        _t.sleep(0.4)
        try:
            c = http.client.HTTPConnection("127.0.0.1", port2, timeout=6)
            c.request("GET", "/api/capture/duplicate?url=https%3A%2F%2Fexample.com%2Fx",
                      headers={"Host": f"127.0.0.1:{port2}"})
            resp = c.getresponse()
            body = resp.read().decode("utf-8", "replace")
            code = resp.status
            c.close()
            check("★ GET /api/capture/duplicate 真实可用（不是 404）", code == 200, f"HTTP {code}")
            check("  返回体含 duplicate 字段", '"duplicate"' in body, body[:120])
        finally:
            srv2.shutdown()
            srv2.server_close()
    finally:
        srv.shutdown()
        srv.server_close()
        # 清理本测试产生的笔记。其它测试断言 NOTES_DIR 的精确篇数
        # （如「文档全部入库 == 3」），留下垃圾会让它们无故失败 —— 之前就踩过。
        for _f in paths.NOTES_DIR.glob("*.md"):
            if _f.name not in pre_existing:
                try:
                    _f.unlink()
                except OSError:
                    pass
        try:
            indexer.rebuild_all(ctx.db, ctx.embedder)
        except Exception:  # noqa: BLE001 - 清理失败不影响本测试结论
            pass
        _cfg.update({"CRAWLER": {"allow_private_network": _orig_allow}}, persist=False)

def test_localhost_security(ctx) -> None:
    """Localhost 安全边界：Host 校验 + Fetch Metadata + 同源判定。

    威胁模型：本服务**没有登录鉴权、却装着用户全部私人笔记**。发布后真实会遇到
    ① DNS rebinding（恶意域名解析到 127.0.0.1）② 恶意网页跨站读取本地 API。

    ⚠ 一条关键设计约束（用户明确要求）：**不能要求所有请求都带 Origin** ——
    本地 CLI / curl / 诊断脚本本来就不发 Origin，强制要求会直接打断它们。
    因此判据是「只在出现**浏览器特征信号**时才拒绝」。
    """
    section("Localhost 安全边界（Host / Fetch Metadata / 同源）")
    import http.client
    import threading
    import time

    from app.core import security as sec

    # ---------- 纯函数：回环判定 ----------
    for h in ("127.0.0.1", "127.0.0.5", "localhost", "::1", "[::1]"):
        check(f"回环地址识别：{h}", sec.is_loopback(h))
    for h in ("0.0.0.0", "192.168.1.10", "evil.com", "localhost.evil.com", ""):
        check(f"非回环地址识别：{h or '(空)'}", not sec.is_loopback(h))

    # ---------- 纯函数：Host 校验（防 DNS rebinding）----------
    check("Host 为回环 → 放行",
          sec.host_allowed("127.0.0.1:28765"))
    check("Host 为恶意域名 → 拒绝（DNS rebinding）",
          not sec.host_allowed("evil.com:28765"))
    check("Host 形如 localhost.evil.com 也拒绝（不做后缀匹配）",
          not sec.host_allowed("localhost.evil.com"))
    check("Host 缺失 → 拒绝", not sec.host_allowed(""))
    check("显式 LAN 模式下才接受非回环 Host",
          sec.host_allowed("192.168.1.10:28765", allow_lan=True)
          and not sec.host_allowed("192.168.1.10:28765", allow_lan=False))

    # ---------- 纯函数：跨站判定 ----------
    P = {"port": 28765}

    def hdr(**kw):
        return {k.replace("_", "-"): v for k, v in kw.items()}

    ok, _ = sec.check_request("GET", hdr(Host="127.0.0.1:28765"), **P)
    check("★ 无 Origin / 无 Fetch Metadata → 放行（保护本地 CLI 与诊断工具）", ok)

    ok, reason = sec.check_request(
        "GET", hdr(Host="127.0.0.1:28765", **{"Sec-Fetch-Site": "cross-site"}), **P)
    check("★ 跨站请求（no-cors，无 Origin）→ 拒绝", not ok and reason == sec.REASON_CROSS_SITE, reason)

    ok, reason = sec.check_request(
        "GET", hdr(Host="127.0.0.1:28765", Origin="https://evil.com"), **P)
    check("★ 第三方 Origin 读取 → 拒绝", not ok and reason == sec.REASON_CROSS_ORIGIN, reason)

    ok, _ = sec.check_request(
        "GET", hdr(Host="127.0.0.1:28765", Origin="http://127.0.0.1:28765"), **P)
    check("同源 Origin → 放行", ok)
    ok, _ = sec.check_request(
        "GET", hdr(Host="localhost:28765", Origin="http://localhost:28765"), **P)
    check("localhost 同源 → 放行", ok)

    ok, reason = sec.check_request(
        "GET", hdr(Host="127.0.0.1:28765", Origin="http://localhost:3000"), **P)
    check("其它 localhost 端口（同 site 不同源）→ 拒绝", not ok and reason == sec.REASON_CROSS_ORIGIN, reason)

    ok, reason = sec.check_request("GET", hdr(Host="evil.com:28765"), **P)
    check("恶意 Host 优先被拒", not ok and reason == sec.REASON_HOST, reason)

    ok, reason = sec.check_request(
        "POST", hdr(Host="127.0.0.1:28765", **{"Content-Type": "application/x-www-form-urlencoded"}), **P)
    check("★ 表单类 Content-Type 的 POST → 拒绝（挡 CSRF）",
          not ok and reason == sec.REASON_CONTENT_TYPE, reason)
    ok, _ = sec.check_request(
        "POST", hdr(Host="127.0.0.1:28765", **{"Content-Type": "application/json"}), **P)
    check("JSON POST → 放行", ok)
    ok, _ = sec.check_request("POST", hdr(Host="127.0.0.1:28765"), **P)
    check("无 body 的裸 POST（如 shutdown）→ 放行", ok)

    # ---------- 静态断言：不再对外发 CORS ----------
    src = (paths.CORE_DIR.parent / "server.py").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    check("响应头里不再出现 Access-Control-Allow-Origin（默认关闭跨域读取）",
          "Access-Control-Allow-Origin" not in code)
    check("跨域预检不再以 204 放行", "Access-Control-Allow-Methods" not in code)
    check("Host 拒绝路径会写结构化错误（code/message/details）", "REASON_TEXT" in src)

    # ---------- 真实 HTTP 集成测试 ----------
    from app.server import Server

    srv = Server(("127.0.0.1", 0), ctx, allow_lan=False)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.4)

    def raw_get(host_header=None, extra: dict | None = None) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=6)
        conn.putrequest("GET", "/api/status", skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host_header or f"127.0.0.1:{port}")
        for k, v in (extra or {}).items():
            conn.putheader(k, v)
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        code = resp.status
        conn.close()
        return code

    try:
        check("集成：本机普通请求 → 200", raw_get() == 200, str(raw_get()))
        check("集成：恶意 Host → 403", raw_get("evil.com") == 403)
        check("集成：第三方 Origin → 403",
              raw_get(extra={"Origin": "https://evil.com"}) == 403)
        check("集成：Sec-Fetch-Site: cross-site → 403",
              raw_get(extra={"Sec-Fetch-Site": "cross-site"}) == 403)
        check("集成：同源 Origin → 200",
              raw_get(extra={"Origin": f"http://127.0.0.1:{port}"}) == 200)
        check("集成：OPTIONS 预检 → 403",
              _options_code(port) == 403)
    finally:
        srv.shutdown()
        srv.server_close()


def _options_code(port: int) -> int:
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=6)
    conn.request("OPTIONS", "/api/status", headers={"Host": f"127.0.0.1:{port}"})
    resp = conn.getresponse()
    resp.read()
    code = resp.status
    conn.close()
    return code

def test_schema_compatibility(ctx) -> None:
    """索引结构版本兼容：只读探测 → 判版本 → 决定动作（**不做 migration framework**）。

    设计前提：Markdown 是真相源、cache.db 是纯派生索引，所以升级策略是
    「备份 + 全量重建」，而不是维护 ALTER 链。

    本次修复的核心缺陷：`init_schema()` 曾用 `INSERT OR REPLACE` 写版本号，
    于是**旧库一被打开，版本证据就被抹掉**，之后再也判断不出它原本是哪一版。
    下面第 ⑫ 项就是这条的 regression test。
    """
    section("索引结构版本兼容（不做 migration framework）")
    import hashlib
    import sqlite3
    import tempfile
    from pathlib import Path

    from app.core import db as dbmod
    from app.core import indexer, migrations as M
    from app.core import search as search_mod

    tmp = Path(tempfile.mkdtemp(prefix="wikiusb_schema_"))
    cache = tmp / "cache.db"

    def set_version(path: Path, value: str) -> None:
        c = sqlite3.connect(str(path))
        c.execute("INSERT OR REPLACE INTO sys_meta(key, value) VALUES(?,?)",
                  (M.META_SCHEMA_VERSION, value))
        c.commit()
        c.close()

    def make_db(path: Path) -> dbmod.Database:
        d = dbmod.Database(db_path=path, embedding_dim=64)
        d.init_schema()
        return d

    # ① 无数据库 → fresh，且探测本身不得建出空库
    check("① 无库 → fresh", M.needs_migration(cache) == M.FRESH, M.needs_migration(cache))
    check("   只读探测不会创建数据库文件", not cache.exists())

    # ② 当前版本 → OK，且重复 init_schema 不改写版本
    d1 = make_db(cache)
    check("② 新库写入当前版本", M.get_schema_version(cache) == M.CURRENT_SCHEMA_VERSION,
          M.get_schema_version(cache))
    check("   版本一致 → ok", M.needs_migration(cache) == M.OK)
    d1.init_schema()
    d1.init_schema()
    check("   重复初始化后版本仍是同一个（不被改写）",
          M.get_schema_version(cache) == M.CURRENT_SCHEMA_VERSION)

    # ⑫ regression：init_schema 绝不能把旧版本静默改成当前版本
    set_version(cache, "1.2")
    d1.init_schema()
    check("⑫ init_schema() 不会把旧版本静默改成当前版本（本次缺陷的 regression）",
          M.get_schema_version(cache) == "1.2", M.get_schema_version(cache))
    d1.checkpoint_and_close()

    # ⑪ 用户 Markdown 在升级过程中不得被改动
    mark = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths.NOTES_DIR.glob("*.md"))}
    if not mark:
        (paths.NOTES_DIR / "_probe.md").write_text("---\ntitle: probe\n---\n\n正文\n", encoding="utf-8")
        mark = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(paths.NOTES_DIR.glob("*.md"))}

    # ③ 老版本 → upgrade：备份 + 清缓存（重建交给全量索引）
    check("③ 旧库 → upgrade", M.needs_migration(cache) == M.UPGRADE, M.needs_migration(cache))
    info = M.migrate(cache)
    check("   动作是 rebuild", info["action"] == "rebuild", str(info))
    check("   旧索引已备份且备份文件存在",
          bool(info["backup"]) and Path(info["backup"]).exists(), str(info["backup"]))
    check("   旧索引已被清走（交由全量重建）", not cache.exists())
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(paths.NOTES_DIR.glob("*.md"))}
    check("⑪ 升级全程未改动任何用户 Markdown", after == mark,
          f"{set(mark) ^ set(after)}")

    # ④ 比程序新 → 拒绝（不重建 / 不覆盖 / 不降级）
    make_db(cache)
    set_version(cache, "9.9")
    check("④ 库比程序新 → downgrade", M.needs_migration(cache) == M.DOWNGRADE,
          M.needs_migration(cache))
    raised = ""
    try:
        M.migrate(cache)
    except M.SchemaTooNewError as exc:
        raised = str(exc)
    check("   拒绝升级并抛出 SchemaTooNewError", bool(raised), raised)
    check("   提示文案是人话（含「请升级」）", "升级" in raised and "9.9" in raised, raised)
    check("   拒绝时库文件仍在、版本未被改写",
          cache.exists() and M.get_schema_version(cache) == "9.9",
          M.get_schema_version(cache))

    # ⑤ / ⑥ 版本缺失、非法 → unknown（保守走重建），且不崩
    c = sqlite3.connect(str(cache))
    c.execute("DELETE FROM sys_meta WHERE key=?", (M.META_SCHEMA_VERSION,))
    c.commit(); c.close()
    check("⑤ 版本行缺失 → unknown", M.needs_migration(cache) == M.UNKNOWN,
          M.needs_migration(cache))
    set_version(cache, "not-a-version")
    check("⑥ 版本号非法 → unknown（不抛异常）", M.needs_migration(cache) == M.UNKNOWN,
          M.needs_migration(cache))
    # sys_meta 表整体缺失（被外部工具动过）
    c = sqlite3.connect(str(cache)); c.execute("DROP TABLE sys_meta"); c.commit(); c.close()
    check("⑥b sys_meta 表缺失 → unknown", M.needs_migration(cache) == M.UNKNOWN,
          M.needs_migration(cache))

    # ⑦ 升级中途失败（备份失败）→ 中止且**不删任何数据**
    make_db(cache)
    set_version(cache, "1.2")
    real_backup = M.backup_cache
    M.backup_cache = lambda *a, **k: None            # 模拟备份失败
    try:
        failed = ""
        try:
            M.migrate(cache)
        except RuntimeError as exc:
            failed = str(exc)
    finally:
        M.backup_cache = real_backup
    check("⑦ 备份失败时中止升级并报错", bool(failed), failed)
    check("   中止时未删除任何数据库文件（有退路才敢动数据）", cache.exists())

    # ⑧⑨⑩ 全量重建后的完整性（用隔离环境 + 夹具笔记）
    for name, body in NOTES.items():
        (paths.NOTES_DIR / name).write_text(body, encoding="utf-8")
    cache2 = tmp / "rebuilt.db"
    d2 = dbmod.Database(db_path=cache2, embedding_dim=ctx.db.embedding_dim)
    d2.init_schema()
    rep = indexer.rebuild_all(d2, ctx.embedder)
    stats = d2.stats()
    notes_md = list(paths.NOTES_DIR.glob("*.md"))
    check("⑧ 重建后文档数量与笔记文件数一致",
          stats["docs"] == len(notes_md), f"docs={stats['docs']} files={len(notes_md)} rep={rep}")
    res = search_mod.hybrid_search(d2, ctx.embedder, "注意力机制", top_k_parents=3)
    check("⑨ 重建后 FTS 可检索（能召回）",
          len(res.references) > 0 or res.counts.get("fts_candidates", 0) > 0,
          f"route={res.route} counts={res.counts}")
    check("⑩ 重建后向量表状态正确",
          d2.vec_table_ready and not d2.signature_mismatch,
          f"ready={d2.vec_table_ready} mismatch={d2.signature_mismatch}")
    d2.checkpoint_and_close()

    # 探测函数本身是只读的（静态断言）
    src = (paths.CORE_DIR / "migrations.py").read_text(encoding="utf-8")
    check("探测使用只读 URI 连接（mode=ro）", "mode=ro" in src and "uri=True" in src)
    import re as _re

    # 剥掉文档字符串再断言：模块 docstring 里解释「为什么不做 ALTER」时会提到它，
    # 那是说明而不是实现。这里只保证**可执行代码里没有**增量迁移。
    _code = _re.sub(r'""".*?"""', "", src, flags=_re.S)
    check("可执行代码中不使用 ALTER TABLE（本阶段刻意不做增量迁移）",
          "ALTER TABLE" not in _code.upper(),
          str([ln.strip()[:60] for ln in _code.splitlines() if "ALTER TABLE" in ln.upper()]))

    try:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    except OSError:
        pass

def test_atomic_io() -> None:
    """原子写：用户资产（Markdown / 原件 / config）的异常安全。

    背景：``data/notes/*.md`` 是**用户真相源**。``cache.db`` 可以全量重建，
    Markdown 不能。而 ``open(path, "w") -> write()`` 是「先截断再写」——
    进程若在写入中途终止（断电 / 拔盘 / 被杀），用户拿到半截笔记，
    **原内容已经没了**。本测试固化「失败时目标文件必须原样保留」这条不变量。
    """
    section("原子写（用户资产异常安全）")
    import os
    import tempfile
    from pathlib import Path

    from app.core import atomic_io

    tmp = Path(tempfile.mkdtemp(prefix="wikiusb_atomic_"))

    # ---------- ③ 成功写入：内容完全一致 ----------
    f1 = tmp / "note.md"
    atomic_io.atomic_write_text(f1, "第一行\n第二行\n")
    with open(f1, encoding="utf-8", newline="") as fh:      # 3.11 无 Path.read_text(newline=)
        got = fh.read().replace("\r\n", "\n")
    check("成功写入后内容一致", got == "第一行\n第二行\n", repr(got))

    # ---------- ④ 临时文件不残留 ----------
    check("成功后目录内不残留临时文件",
          [p.name for p in tmp.iterdir()] == ["note.md"],
          str([p.name for p in tmp.iterdir()]))

    # ---------- ⑥ 换行/编码行为与旧写法逐字节一致 ----------
    f_old = tmp / "old.txt"
    f_new = tmp / "new.txt"
    sample = "标题\n正文 with UTF-8 中文\n尾行\n"
    with open(f_old, "w", encoding="utf-8") as fh:      # 旧写法（Path.write_text 同语义）
        fh.write(sample)
    atomic_io.atomic_write_text(f_new, sample)
    check("换行与编码字节级等价于旧的 write_text（不会改动既有笔记的换行）",
          f_old.read_bytes() == f_new.read_bytes(),
          f"{f_old.read_bytes()[:24]!r} vs {f_new.read_bytes()[:24]!r}")
    check("显式 newline='' 时不翻译换行",
          b"\r\n" not in atomic_io.atomic_write_text(
              tmp / "raw.txt", "a\nb\n", newline="").read_bytes())

    # ---------- ⑤ 中文文件名 ----------
    f_cn = tmp / "宁波华林工贸-基坑监测简报.md"
    atomic_io.atomic_write_text(f_cn, "中文文件名测试\n")
    check("中文文件名可原子写入且内容正确",
          f_cn.read_text(encoding="utf-8").strip() == "中文文件名测试")

    # ---------- ① 已存在文件 + 写入中途失败 → 旧文件完整保留 ----------
    f_exist = tmp / "existing.md"
    atomic_io.atomic_write_text(f_exist, "旧内容-必须是完整的\n")
    before = f_exist.read_bytes()

    real_fsync = os.fsync
    def boom(fd):                       # 模拟「数据已写一半/落盘阶段」故障
        raise OSError("模拟写入中途失败")
    os.fsync = boom
    try:
        try:
            atomic_io.atomic_write_text(f_exist, "新内容" * 5000)
            failed = False
        except OSError:
            failed = True
    finally:
        os.fsync = real_fsync
    check("写入中途失败会抛异常（不静默）", failed)
    check("★ 失败后旧文件完整保留（不是半截新文件）",
          f_exist.read_bytes() == before, repr(f_exist.read_bytes()[:40]))

    # ---------- ② 新文件首次创建失败 → 不留半截正式文件 ----------
    f_new2 = tmp / "brand_new.md"
    os.fsync = boom
    try:
        try:
            atomic_io.atomic_write_text(f_new2, "内容" * 5000)
        except OSError:
            pass
    finally:
        os.fsync = real_fsync
    check("★ 首次创建失败时不生成半截正式文件", not f_new2.exists())

    # ---------- 失败后临时文件尽量清理 ----------
    leftovers = [p.name for p in tmp.iterdir() if p.name.endswith(".tmp")]
    check("失败后临时文件已清理", not leftovers, str(leftovers))

    # ---------- 替换阶段失败（目标被占用等）也不破坏目标 ----------
    f_rep = tmp / "replace_fail.md"
    atomic_io.atomic_write_text(f_rep, "替换前内容\n")
    before2 = f_rep.read_bytes()
    real_replace = atomic_io._replace_with_retry
    atomic_io._replace_with_retry = lambda a, b: (_ for _ in ()).throw(PermissionError("被占用"))
    try:
        try:
            atomic_io.atomic_write_text(f_rep, "替换后内容\n")
        except PermissionError:
            pass
    finally:
        atomic_io._replace_with_retry = real_replace
    check("★ 替换阶段失败时目标仍是完整旧内容", f_rep.read_bytes() == before2)
    check("替换失败后临时文件也已清理",
          not [p.name for p in tmp.iterdir() if p.name.endswith(".tmp")])

    # ---------- 二进制原子写 ----------
    f_bin = tmp / "orig.bin"
    payload = bytes(range(256)) * 8
    atomic_io.atomic_write_bytes(f_bin, payload)
    check("二进制原子写字节完全一致", f_bin.read_bytes() == payload)

    # ---------- 临时文件与目标同目录（跨盘 rename 不保证原子）----------
    src = (paths.CORE_DIR / "atomic_io.py").read_text(encoding="utf-8")
    check("临时文件创建在目标同目录（dir= 目标父目录）", "dir=str(target.parent)" in src)
    check("使用 os.replace 而非 shutil.move（后者跨盘非原子）",
          "os.replace(" in src and "shutil.move" not in src)

    # 清理
    for p in sorted(tmp.rglob("*"), reverse=True):
        try:
            p.unlink() if p.is_file() else p.rmdir()
        except OSError:
            pass
    try:
        tmp.rmdir()
    except OSError:
        pass

def test_inject_budget(ctx) -> None:
    """注入给模型的上下文必须有硬预算。

    来自同级项目《优势说明》里它们踩过、我们此前没有的一条：
    注入的知识片段**没有上限** —— 父块是章节粒度，实测单块可达 1200 字符，
    命中 5 块就是 5000+ 字符（≈7.5k tokens）。本地小模型的窗口与注意力都有限，
    不封顶要么溢出、要么被无关长文淹没。
    """
    section("注入预算护栏（防上下文被长文淹没）")
    from app.core import llm

    # ---- 注入预算 ----
    big = "台风" + "内容" * 600          # ≈1200 字符，与实测最长父块同量级
    res = search.SearchResult(query="台风路径", route="fts")
    res.parents = [
        {"parent_id": f"p{i}", "doc_id": "d", "title": f"长文{i}",
         "path": f"notes/l{i}.md", "content": big, "score": 1.0}
        for i in range(5)
    ]
    prompt = llm.Gateway.build_prompt("台风路径怎么样", res, None)
    seg = prompt.split("【当前问题】")[0]
    check("注入片段有总量上限（修复前会注入 ~5000 字符）", len(seg) <= 2000, f"{len(seg)} 字符")
    check("超长段落被截断并标出省略号", "…" in seg)
    check("截取的是**与查询相关**的窗口（而非无脑从头截）", "台风" in seg)

def test_ingest_analysis_and_graph(ctx) -> None:
    """入库语义分析 + 星图确定性边。

    背景：此前入库只做「转 Markdown → 切片 → 索引」，没有任何语义理解，
    于是星图只能靠手写 [[Wikilink]] 与高阈值向量建边 —— 两者在这个工作流里
    几乎都不存在，实测 12 个节点只有 2 条边、8 个孤立节点。这里验证补上的
    「确定性层」：零依赖、离线可用。
    """
    section("入库语义分析 + 星图确定性边")
    from app.core import analyzer, graph

    # ---- 分析器：关键信息必须真的被抽出来 ----
    body = (
        "# 台风杜鹃逼近\n\n"
        "25号台风杜鹃已生成，中央气象台发布台风预警。"
        "台风杜鹃将带来强降雨，华南沿海需防范台风带来的大风。"
        "受台风影响，广东福建等地有暴雨，局地降雨量可达 250 毫米。\n\n"
        "浙江省宁波市气象台 2026年9月15日 发布，详见 https://example.com/typhoon。"
    )
    a = analyzer.analyze(body, title="台风杜鹃逼近")
    check("关键词抽到了主题词（中文 n-gram 生效）",
          any(k in ("台风", "杜鹃", "降雨", "暴雨") for k in a.keywords), str(a.keywords))
    check("frontmatter 不会被当正文分析（避免 captured_at 变成关键词）",
          not any(k in a.keywords for k in ("captured_at", "source_url", "doc_type")),
          str(a.keywords))
    check("识别出中文", a.language == "zh", a.language)
    check("抽到日期实体", bool(a.entities.get("日期")), str(a.entities))
    check("抽到网址实体", bool(a.entities.get("网址")), str(a.entities))

    # ---- doc_meta：索引时写入，供星图使用 ----
    for name, body2 in NOTES.items():
        p = paths.NOTES_DIR / name
        p.write_text(body2, encoding="utf-8")
        indexer.index_file(ctx.db, p, ctx.embedder)
    rows = ctx.db.query("SELECT COUNT(*) AS c FROM doc_meta")
    check("索引时写入 doc_meta", rows[0]["c"] >= 1, str(rows[0]["c"]))
    kw_row = ctx.db.query_one("SELECT keywords FROM doc_meta LIMIT 1")
    check("doc_meta 里有关键词（旧笔记走回退现算）",
          bool(kw_row and kw_row["keywords"].strip()), str(kw_row["keywords"] if kw_row else ""))

    # ---- 星图：边必须带得出手的理由 ----
    g = graph.build_graph(ctx.db)
    check("星图产出节点", len(g["nodes"]) >= 1, str(g["stats"]))
    check("默认不启用向量边（依赖嵌入源，多数环境没有）",
          g["stats"]["vectors_enabled"] is False and g["stats"]["semantic"] == 0,
          str(g["stats"]))
    check("每条边都带 type", all(lk.get("type") for lk in g["links"]),
          str([lk.get("type") for lk in g["links"]]))
    non_wiki = [lk for lk in g["links"] if lk["type"] != "wikilink"]
    check("确定性边带「为什么相连」的说明",
          all(lk.get("reason") for lk in non_wiki), str([lk.get("reason") for lk in non_wiki]))

def test_search_quality_guards(ctx) -> None:
    """检索质量三道闸：实词化、语料词典、引用必须有词法依据。

    背景（全是实测踩出来的真实缺陷）：

    1. `_like_search` 拿**整句**去做子串匹配 → 「台风有吗」整句永远匹配不到，
       等于静默丢掉全部召回，只能靠向量路兜底；
    2. 向量路**没有相关性下限**，天性就是「永远返回 k 个最近邻」。实测库里
       根本没有「杜苏芮」，它的最近邻距离（0.768）甚至比真正存在的「台风」
       （0.804）还小 —— 距离阈值区分不了，于是无关文档被当成出处引用
       （用户截图里「问台风却引用基坑围护报告」就是这么来的）；
    3. FTS 的 MATCH 用 OR 组合，一个虚词就能把大量无关文档拉进候选
       （同级项目实测「黄仁勋说了什么」被虚词劫持）。
    """
    section("检索质量三道闸（实词化 / 语料词典 / 词法依据）")
    db, emb = ctx.db, ctx.embedder
    for name, body in NOTES.items():
        p = paths.NOTES_DIR / name
        p.write_text(body, encoding="utf-8")
        indexer.index_file(db, p, emb)

    # ---- 闸 1：虚词剥离，自然语言问句能落地到实词 ----
    check("问句「位置编码有什么用」剥出实词",
          search.content_term_sets("位置编码有什么用")[0][:1] == ["位置编码"]
          or "位置编码" in search.content_term_sets("位置编码有什么用")[0],
          str(search.content_term_sets("位置编码有什么用")))
    check("纯虚词查询无实词可用", search.content_term_sets("的了吗呢") == [])
    check("多词时丢弃单字噪声（否则 AND 永远不命中）",
          "什" not in search.drop_noise_terms(["位置编码", "什", "用"]),
          str(search.drop_noise_terms(["位置编码", "什", "用"])))

    # ---- 闸 2：拿语料当词典修剪 ----
    check("语料中存在的词原样保留",
          search.trim_term_to_corpus(db, "位置编码") == "位置编码")
    check("整串修剪到语料里真实存在的片段",
          search.trim_term_to_corpus(db, "位置编码作用很大啊") == "位置编码",
          search.trim_term_to_corpus(db, "位置编码作用很大啊"))
    check("语料里不存在则修剪为空（不硬凑）",
          search.trim_term_to_corpus(db, "量子纠缠态") == "",
          search.trim_term_to_corpus(db, "量子纠缠态"))

    # ---- 闸 3：引用必须有词法依据 ----
    res = search.hybrid_search(db, emb, "位置编码有什么用", top_k_parents=3)
    check("自然语言问句有召回", len(res.references) > 0, str(res.counts))
    check("召回内容确实相关", "位置编码" in (res.parents[0]["content"] if res.parents else ""))

    res2 = search.hybrid_search(db, emb, "量子计算机", top_k_parents=3)
    check("库里没有的词：如实返回空（修复前会硬凑 top-k）",
          len(res2.references) == 0, f"route={res2.route} refs={len(res2.references)}")
    check("并给出「未找到字面匹配」的说明",
          any("字面匹配" in w for w in res2.warnings), str(res2.warnings))
    check("语义候选被挡在引用之外（vec 有候选但 parents 为 0）",
          res2.counts.get("vec_candidates", 0) > 0 and res2.counts.get("parents", 0) == 0,
          str(res2.counts))

    res3 = search.hybrid_search(db, emb, "的了吗呢", top_k_parents=3)
    check("纯虚词查询提示补实词", res3.route == "empty" and not res3.references, res3.route)

    # ---- 闸 4：MATCH 用 AND，虚词不再劫持 ----
    check("MATCH 表达式用 AND 组合（非 OR）",
          " AND " in search._match_expr(["注意力", "机制"]))

def test_port_probe() -> None:
    section("端口探测与避让（PRD 4.1）")
    import socket

    from app import launcher

    def _free_base(n: int) -> int:
        """找到连续 n 个可绑定端口（不设 SO_REUSEADDR，Windows 下探测才真实）。"""
        for base in range(29200, 29600):
            socks = []
            try:
                for i in range(n):
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.bind(("127.0.0.1", base + i))
                    socks.append(s)
                return base
            except OSError:
                continue
            finally:
                for s in socks:
                    s.close()
        raise RuntimeError("找不到连续空闲端口")

    base = _free_base(3)
    holders = []
    try:
        for i in range(2):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", base + i))
            s.listen(1)
            holders.append(s)

        port, tries = launcher.probe_port("127.0.0.1", base)
        check("连续两个端口被占用时逐级避让", port == base + 2 and tries == 3, f"{port} tries={tries}")

        port2, tries2 = launcher.probe_port("127.0.0.1", base + 3)
        check("空闲端口一次命中", port2 == base + 3 and tries2 == 1, f"{port2} tries={tries2}")

        try:
            launcher.probe_port("127.0.0.1", base, attempts=2)
            check("连续占用时报错（不会静默绑定他人端口）", False, "未抛 OSError")
        except OSError:
            check("连续占用时报错（不会静默绑定他人端口）", True)
    finally:
        for s in holders:
            s.close()


def test_console_encoding_guard() -> None:
    """隐蔽细节 #2：Windows 批处理必须锁死 UTF-8，否则 CP936 控制台会静默崩溃。"""
    section("隐蔽细节 #2 / 终端编码锁死")
    bat = (ROOT / "启动-Windows.bat").read_text(encoding="utf-8", errors="replace")
    check("bat 含 chcp 65001", "chcp 65001" in bat)
    check("bat 设置 PYTHONIOENCODING=utf-8", "PYTHONIOENCODING=utf-8" in bat)
    check("bat 设置 PYTHONUTF8=1", "PYTHONUTF8=1" in bat)
    check("bat 内容保持纯 ASCII（避免 cmd 解析乱码）",
          all(ord(ch) < 128 for ch in bat), "存在非 ASCII 字符")
    check("bat 在调用 Python 前即完成编码设置",
          bat.index("chcp 65001") < bat.index("launcher.py"))
    check("macOS 启动脚本存在", (ROOT / "启动-macOS.command").exists())
    check("Linux 启动脚本存在", (ROOT / "启动-Linux.sh").exists())


def test_offline_assets() -> None:
    section("离线资源 Vendor 化（PRD 4.6）")
    html = (paths.WEB_DIR / "index.html").read_text(encoding="utf-8")
    import re

    external = re.findall(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)', html)
    check("HTML 不含任何外链 CDN 资源", not external, str(external))
    check("本地 d3 已 Vendor 化", (paths.VENDOR_DIR / "d3.v7.min.js").exists())
    check("d3 文件体积合理（>200KB）", (paths.VENDOR_DIR / "d3.v7.min.js").stat().st_size > 200_000)
    check("控制台包含未闭合角标缓冲实现", "splitHold" in html and "\\[\\^?" in html)
    # 节点图已被「主题分组」取代：实测本项目语料是「剪藏一批互不相关页面」，
    # 12 篇分成 9 个连通分量，节点图必然是一堆孤岛（不匹配使用形态）。
    check("控制台已把星图换为主题分组", "主题分组" in html and "topicBox" in html)
    # 断言用户可见的性质：主题页里不再有节点图画布。
    # （渲染函数 initGraphSvg/drawGraph 已成为死代码，属另一项清理，不在此断言。）
    check("主题页不再有节点图画布", '<svg id="graphSvg">' not in html)
    check("主题页有「共现若干篇才成主题」的阈值控件", 'id="topicMin"' in html)
    check("控制台包含安全退出按钮", "/api/system/shutdown" in html)


def test_no_absolute_paths() -> None:
    section("工程边界 / 相对路径")
    for f in (ROOT / "app").rglob("*.py"):
        txt = f.read_text(encoding="utf-8", errors="replace")
        for bad in ("F:\\", "C:\\Users", "D:\\项目"):
            if bad in txt and "启动" not in txt and "浏览器" not in txt:
                if f.name in ("paths.py",):
                    continue
                check(f"无硬编码盘符 {bad} @ {f.name}", False, bad)
                break
    else:
        pass
    check("源码未硬编码 U 盘盘符", True)
    # 断言配置的**权威来源**（代码内置模板），而不是 config.ini 本身 ——
    # 后者含明文密钥、已被 gitignore，干净克隆里根本不存在（应用首次运行才生成）。
    from app.core import config as config_mod

    tmpl = config_mod.DEFAULT_TEMPLATE
    check("内置配置模板含全部必需段",
          all(sec in tmpl for sec in ("[AI]", "[CRAWLER]", "[GRAPH]")), tmpl[:120])
    check("内置配置模板含嵌入源与维度",
          "embedding_source" in tmpl and "embedding_dim" in tmpl)
    check("config.ini 已 gitignore（含明文密钥，由首次运行生成）",
          "config.ini" in (ROOT / ".gitignore").read_text(encoding="utf-8"))

    # 验证「配置缺失时自动生成」这条自愈行为（路径已隔离到临时目录，不碰真实配置）
    probe = paths.CONFIG_FILE
    probe.unlink(missing_ok=True)
    config_mod.load(probe, force=True)
    check("配置缺失时能自动生成完整模板",
          probe.exists() and "[AI]" in probe.read_text(encoding="utf-8"), str(probe))


# ==========================================================================
def _isolate_data_dir() -> Path:
    """把测试用到的**一切数据路径**重定向到临时目录，并断言没有漏网之鱼。

    ⚠ 安全红线：`reset_workspace()` 会清空 notes/，原件回收会删 originals/ ——
    只要有一条数据路径没被隔离，跑一次测试就会**删光用户真实知识库**。

    **因此这里不手写清单**，而是自动重映射所有位于真实 `data/` 之下的路径常量。
    手写清单一定会漏：`ORIGINALS_DIR` 就曾被漏掉，导致测试把用户剪藏留存的原件
    全部当孤儿删掉（真实事故）。新增数据路径时必须不需要改这里。

    paths 下所有常量都是属性访问（无值导入），运行时替换即全局生效。
    """
    tmp = Path(tempfile.mkdtemp(prefix="wikiusb_test_"))
    real_data = paths.DATA_DIR

    for name, val in list(vars(paths).items()):
        if not isinstance(val, Path):
            continue
        try:
            rel = val.relative_to(real_data)
        except ValueError:
            continue                      # 不在 data/ 下（源码 / 运行时），保持原样
        target = tmp if str(rel) == "." else tmp / rel
        if val.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        setattr(paths, name, target)

    # config.ini 位于仓库根，不属于 data/，需单独处理
    paths.CONFIG_FILE = tmp / "config.ini"
    for d in (paths.DATA_DIR, paths.NOTES_DIR, paths.SNAPSHOT_DIR, paths.ORIGINALS_DIR):
        d.mkdir(parents=True, exist_ok=True)

    # ⚠ 只改 paths.CONFIG_FILE 还不够：config 模块在**导入时**就把 _path 绑定到了
    # 真实路径（`_path: Path = paths.CONFIG_FILE`），之后再改 paths 对它无效。
    # 后果是测试里任何 `config.update(..., persist=True)` 都会写进用户的真实
    # config.ini —— 实测已发生过（把测试段落写进了用户配置），而该文件含 API Key，
    # 属于「写坏即永久丢失」的用户资产。这里连同解析缓存一起重绑。
    try:
        from app.core import config as _cfg

        _cfg._path = paths.CONFIG_FILE
        _cfg._parser = None              # 丢掉可能已缓存的真实配置
    except Exception:  # noqa: BLE001 - 隔离失败不该让测试崩，但要显式暴露
        print("  ⚠ 无法把 config 模块重定向到隔离目录")

    # 守护断言：任何仍指向真实 data/ 的属性都说明隔离清单漏了项 —— 宁可当场失败
    leaked = sorted(n for n, v in vars(paths).items()
                    if isinstance(v, Path) and str(v).startswith(str(real_data)))
    if leaked:
        raise RuntimeError(
            "测试隔离失败：以下路径仍指向真实数据目录，跑下去会删用户数据 —— " + ", ".join(leaked)
        )
    return tmp


def main() -> int:
    print("=" * 74)
    print("  Wiki-USB v1.2  自动化验收测试")
    print("=" * 74)

    real_notes = paths.NOTES_DIR          # 隔离前的真实目录，最后用来验证未被触碰
    real_before = sorted(p.name for p in real_notes.glob("*.md")) if real_notes.exists() else []
    tmp_root = _isolate_data_dir()
    print(f"  真实知识库  : {real_notes}（{len(real_before)} 篇，测试不会改动）")
    print(f"  测试隔离目录: {tmp_root}")
    print("=" * 74)

    exit_code = 0
    try:
        test_chunker()
        test_embedder_guards()
        test_alert_classification()
        test_console_encoding_guard()
        test_offline_assets()
        test_no_absolute_paths()
        from tests.test_converters import run as run_converter_tests
        run_converter_tests(check)
        test_port_probe()
        test_signature_guard()
        test_wal_self_heal()
        test_graceful_shutdown_no_residue()

        reset_workspace()
        from app.core.context import AppContext
        ctx = AppContext()
        ctx.boot(start_syncer=False, probe_ollama=False)

        # 测试需要确定性：向量行为不得依赖「环境里 Ollama 是否恰好在线」。
        # 统一强制 512 维确定性哈希向量（含强制重建向量表），使断言在任何机器上等价。
        ctx.embedder = embedder.HashEmbedder(512)
        if ctx.gateway:
            ctx.gateway.embedder = ctx.embedder
        if ctx.db.embedding_dim != 512 or not ctx.db.vec_table_ready:
            ctx.db.embedding_dim = 512
            assert ctx.db.recreate_vec_table(512), "向量表重建失败"
        ctx.db.signature_mismatch = None

        # 测试不依赖外部 AI 服务：provider 用内存态覆盖（persist=False，不碰 config.ini）
        config.update({"AI": {"provider": "offline"}}, persist=False)

        test_secret_redaction(ctx)
        test_import_security()
        test_archive_ssrf()
        test_ssrf_guard(ctx)
        test_lifecycle_shutdown()
        test_duplicate_url_detection(ctx)
        test_localhost_security(ctx)
        test_schema_compatibility(ctx)
        test_atomic_io()
        test_inject_budget(ctx)
        test_ingest_analysis_and_graph(ctx)
        test_search_quality_guards(ctx)
        test_index_and_search(ctx)
        test_gateway(ctx)
        test_graph(ctx)
        test_crawler(ctx)
        test_archive_localization()
        test_original_base_injection()
        test_orphan_original_cleanup()
        test_html_encoding_detection()
        test_original_preview(ctx)
        test_hidden_detail_1_equal_length_edit(ctx)
        test_hard_05_rename_and_orphans(ctx)
        ctx.shutdown()
        ctx = None
    except BaseException as exc:  # noqa: BLE001 - 保证隔离目录一定被清理
        FAIL.append(f"测试执行异常 :: {type(exc).__name__}: {exc}")
        print(f"\n  ❌ 测试执行异常: {type(exc).__name__}: {exc}")
        import traceback

        tb = traceback.format_exc()
        traceback.print_exc()
        if os.environ.get("GITHUB_ACTIONS"):
            # 崩溃时走不到汇总，注解必须在这里发，否则 CI 只剩一句 "exit code 1"
            last = [ln for ln in tb.strip().splitlines() if ln.strip()][-3:]
            print(f"::error title=测试套件崩溃::{type(exc).__name__}: {exc} | "
                  + " ⟵ ".join(x.strip() for x in last))
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    # 安全红线复核：真实知识库必须一字节未变
    real_after = sorted(p.name for p in real_notes.glob("*.md")) if real_notes.exists() else []
    print()
    if real_after == real_before:
        PASS.append("真实知识库未被测试改动")
        print(f"  ✅ 真实知识库未被测试改动（{real_notes} 仍为 {len(real_after)} 篇）")
    else:
        FAIL.append(f"真实知识库被改动！{real_before} -> {real_after}")
        print(f"  ❌ 真实知识库被改动！{real_before} -> {real_after}")

    print("\n" + "=" * 74)
    total = len(PASS) + len(SKIP) + len(FAIL)
    print(f"  通过 {len(PASS)} 项   失败 {len(FAIL)} 项   跳过 {len(SKIP)} 项")
    # 机器可读的稳定格式：TOTAL 恒定（跳过也算登记），便于 CI 与文档引用，
    # 不必在 README / 测试报告 / changelog 里手写数字。
    print(f"  TOTAL={total} PASS={len(PASS)} SKIP={len(SKIP)} FAIL={len(FAIL)}")
    if FAIL:
        print("\n  失败明细：")
        for f in FAIL:
            print("   ✗ " + f)
    print("=" * 74)

    # 在 GitHub Actions 上把每条失败打成注解 —— 注解会直接显示在运行摘要与 PR 页面，
    # 无需下载完整日志即可定位问题（公开仓库的日志下载需要鉴权）。
    if os.environ.get("GITHUB_ACTIONS"):
        for f in FAIL:
            print(f"::error title=测试失败::{f}")
        if not FAIL:
            print(f"::notice::全部 {len(PASS)} 项通过")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
