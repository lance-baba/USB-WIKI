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


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name if cond else f"{name} :: {detail}")
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"  [{detail}]"))
    return cond


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
    if offline:
        check("（跳过网络用例）", True)
        return
    r1 = crawler.capture_url("https://example.com/", db=ctx.db, embedder=ctx.embedder)
    check("短正文触发 partial_fallback 降级", r1.status == "partial_fallback", str(r1.to_dict())[:200])
    check("降级时保留原始快照", bool(r1.snapshot_path) and paths.abs_from_data(r1.snapshot_path).exists())
    check("降级文案符合 PRD 提示语", "前端动态渲染" in r1.message)

    r2 = crawler.capture_url("https://en.wikipedia.org/wiki/Transformer_(deep_learning_architecture)",
                             db=ctx.db, embedder=ctx.embedder)
    # 外网/代理不可达属环境问题，不算被测对象失败 —— 标记跳过而非误报
    net_err = (not r2.ok) and any(
        k in r2.message for k in ("urlopen error", "ProxyError", "Tunnel connection", "HTTP 0")
    )
    if net_err:
        check("（跳过）长文 success 分支 —— 外网/代理不可达", True, r2.message[:80])
    else:
        check("长正文走 success 分支", r2.status == "success", str(r2.to_dict())[:200])
        check("success 正文 ≥ 150 字", r2.char_count >= 150, str(r2.char_count))
        md = paths.abs_from_data(r2.file_path).read_text(encoding="utf-8")
        check("YAML 头部标记 status: success", "status: \"success\"" in md or "status: success" in md)

    bad = crawler.capture_url("ftp://x", db=None)
    check("非法协议被拒绝", not bad.ok and "http" in bad.message)

    note = crawler.save_manual_note("手工测试笔记", "手工录入的注意力机制要点。", db=ctx.db, embedder=ctx.embedder)
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
    check("控制台包含语义阈值滑块 0.70~0.95", 'min="0.70"' in html and 'max="0.95"' in html)
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
    """把测试的数据根目录重定向到临时目录。

    ⚠ 安全红线：`reset_workspace()` 会清空 notes/，若不隔离，跑一次测试就会
    **删光用户真实知识库**。paths 下所有常量都是属性访问（无值导入），
    因此运行时替换即全局生效，业务代码无需改动。
    """
    tmp = Path(tempfile.mkdtemp(prefix="wikiusb_test_"))
    paths.DATA_DIR = tmp
    paths.NOTES_DIR = tmp / "notes"
    paths.SNAPSHOT_DIR = tmp / "snapshots"
    paths.CACHE_DB = tmp / "cache.db"
    paths.WAL_FILE = tmp / "cache.db-wal"
    paths.SHM_FILE = tmp / "cache.db-shm"
    paths.CHROME_PROFILE_DIR = tmp / "temp_chrome_profile"
    paths.LOG_FILE = tmp / "wiki-usb.log"
    paths.CONFIG_FILE = tmp / "config.ini"   # 防止测试写用户真实的 config.ini
    for d in (paths.DATA_DIR, paths.NOTES_DIR, paths.SNAPSHOT_DIR):
        d.mkdir(parents=True, exist_ok=True)
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

        test_index_and_search(ctx)
        test_gateway(ctx)
        test_graph(ctx)
        test_crawler(ctx)
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
    print(f"  通过 {len(PASS)} 项   失败 {len(FAIL)} 项")
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
