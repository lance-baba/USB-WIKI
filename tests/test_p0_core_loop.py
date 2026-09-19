"""P0 Core-Loop 回归：导入 → 搜索 → 问答 → 引用 真正可用（P0-A ~ P0-F）。

全部使用**脱敏合成 fixture**（沉降观测方案），不提交任何真实用户文档 / 姓名 / 电话 /
正文进 Git。覆盖：

* P0-A  选择本地 Ollama 模型即持久化、立即 ready（不调用 /api/ai/test）
* P0-B  Ollama 真实流式：thinking 帧不误判、错误明确透出、provider=ollama 失败绝不转云端
* P0-C  DOCX styleId→标题 映射（用户真实 docx 里 pStyle=44 → 标题1）
* P0-D  DOCX 表格保留结构、与所属章节同 Parent、可被检索召回
* P0-E  中文短词 LIKE 确定性 lexical 重排（人员 / 观测人员有哪些 / 人员素质有什么要求）
* P0-F  离线搜索结果展示（本地搜索结果、表格可读、只说一次「未调用生成式 AI」）

由 tests/test_suite.py 调用（复用其 section/check/skip）。
"""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from app.core import config, converters, indexer, llm, paths, search as search_mod
from app.core.embedder import HashEmbedder
from app.core.llm import (
    STATE_READY,
    STATE_SELECTION_REQUIRED,
    Gateway,
)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DOCX_NAME = "沉降观测方案.docx"

# 用户真实 docx 暴露：styleId=44 → 标题1；段落用 w:pStyle/@w:val="44" 指向它。
# 这里用合成结构复现，绝不含真实姓名/电话（真实值只在本地人工验证时用）。
_STYLE_44 = "标题 1"


def _make_docx_bytes() -> bytes:
    """构造最小合法 docx（zip）。styles.xml 里 44→标题1；document.xml 含
    标题(八、观测人员配备) + 紧随其后的职责表 + 其它章节。"""
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:styles xmlns:w="{W}">'
        f'<w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
        f'<w:style w:type="paragraph" w:styleId="44"><w:name w:val="{_STYLE_44}"/>'
        f'<w:basedOn w:val="Normal"/></w:style>'
        f'</w:styles>'
    )
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W}">'
        '<w:body>'
        # —— 八、观测人员配备（标题1，styleId=44）+ 紧随其后的职责表（P0-C/D）——
        f'<w:p><w:pPr><w:pStyle w:val="44"/></w:pPr>'
        '<w:r><w:t>八、观测人员配备</w:t></w:r></w:p>'
        '<w:tbl>'
        '<w:tr>'
        '<w:tc><w:p><w:r><w:t>职责</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>姓名</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>职称</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>联系电话</w:t></w:r></w:p></w:tc>'
        '</w:tr>'
        '<w:tr>'
        '<w:tc><w:p><w:r><w:t>现场观测负责人</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>张三</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>工程师</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>13800000001</w:t></w:r></w:p></w:tc>'
        '</w:tr>'
        '<w:tr>'
        '<w:tc><w:p><w:r><w:t>内业资料整理</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>张三</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>助理工程师</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>13800000002</w:t></w:r></w:p></w:tc>'
        '</w:tr>'
        '<w:tr>'
        '<w:tc><w:p><w:r><w:t>观测辅助人员</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>李四</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>技术员</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>13800000003</w:t></w:r></w:p></w:tc>'
        '</w:tr>'
        '</w:tbl>'
        # —— 仪器设备、人员素质的要求（标题1）——
        f'<w:p><w:pPr><w:pStyle w:val="44"/></w:pPr>'
        '<w:r><w:t>仪器设备、人员素质的要求</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>沉降观测仪器设备应满足精度要求。观测人员素质要求：'
        '须持有相应资格证书，熟悉观测方案与操作规程。</w:t></w:r></w:p>'
        # —— 五定原则（标题1）：只泛化提到「观测人员要稳定」，不含「配备/素质」——
        f'<w:p><w:pPr><w:pStyle w:val="44"/></w:pPr>'
        '<w:r><w:t>五定原则</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>沉降观测实行五定原则：定人员、定仪器、定时间、定地点、定方法。'
        '其中观测人员要稳定，不宜频繁更换。</w:t></w:r></w:p>'
        '</w:body></w:document>'
    )
    ct = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", styles)
    return buf.getvalue()


def _index_synthetic(md: str, dim: int = 512):
    """把合成 markdown 写进隔离临时库并索引，返回 (db, tmp_root)。"""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="p0_e_"))
    db = None
    try:
        from app.core import db as db_mod

        db = db_mod.get_db(db_path=tmp / "cache.db", embedding_dim=dim)
        db.init_schema()
        notes = tmp / "notes"
        notes.mkdir(parents=True, exist_ok=True)
        f = notes / "沉降观测方案.md"
        f.write_text(md, encoding="utf-8")
        emb = HashEmbedder(dim)
        indexer.index_file(db, f, emb)
    except Exception:
        if tmp.exists():
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)
        raise
    return db, tmp


def run(ctx, check, section, skip) -> None:
    section("P0 Core-Loop 回归（导入→搜索→问答→引用）")

    # ---------------------------------------------------------------- P0-C / P0-D
    section("P0-C/D：DOCX 标题结构 + 表格可检索")
    docx = _make_docx_bytes()
    res = converters.convert(docx, DOCX_NAME)
    check("DOCX 转换成功", res.ok, res.error or "")
    md = res.markdown

    # 标题：八、观测人员配备 必须成为真正的 Markdown 标题（而非纯文本）
    lines = md.splitlines()
    heading_line = next((ln for ln in lines if "八、观测人员配备" in ln), "")
    check("styleId=44 被还原为 Markdown 标题（八、观测人员配备）",
          heading_line.startswith("#"), f"实际行：{heading_line!r}")

    # 表格：保留 Markdown table 且含职责/姓名
    check("DOCX 表格被转为 Markdown table（保留 | 分隔）",
          "|" in md and "职责" in md and "姓名" in md,
          "表格结构丢失" if "|" not in md else "")
    check("表格含具体职责-姓名对应（现场观测负责人 / 张三）",
          "现场观测负责人" in md and "张三" in md, "")

    # P0-D：标题与紧随其后的表格应在同一逻辑块（中间无空行），随章节语境入同一 Parent
    idx_head = lines.index(heading_line) if heading_line in lines else -1
    block_after = "\n".join(lines[idx_head: idx_head + 12]) if idx_head >= 0 else ""
    merged = bool(block_after) and "八、观测人员配备" in block_after and "|" in block_after
    # 标题行与首个表格行之间不应出现「空行分隔符」把二者拆开
    seg = block_after.split("八、观测人员配备", 1)[-1]
    no_blank_split = "\n\n" not in seg.split("|", 1)[0]
    check("标题与紧随表格并入同一块（不被空行拆成无关 Parent）",
          merged and no_blank_split, block_after[:80])

    # ---------------------------------------------------------------- P0-E
    section("P0-E：中文短词 LIKE 确定性 lexical 重排")
    db_e, tmp_e = _index_synthetic(md)
    try:
        emb = HashEmbedder(512)

        def top_parents(q: str, use_emb=None):
            r = search_mod.hybrid_search(db_e, use_emb, q, top_k_parents=5)
            return [p["content"] for p in r.parents]

        # 用例 1：人员 —— 人员应「人员配备」章节排到泛化描述（五定原则只泛提 人员）前列
        ps = top_parents("人员", None)
        check("P0-E「人员」有召回", len(ps) > 0, f"召回 {len(ps)}")
        if ps:
            peibei_idx = next((i for i, c in enumerate(ps) if "人员配备" in c), None)
            generic_idx = next((i for i, c in enumerate(ps)
                                if "人员" in c and "配备" not in c and "素质" not in c), None)
            if peibei_idx is None or generic_idx is None:
                check("P0-E「人员」人员配备 章节排在泛化描述前",
                      False, f"peibei={peibei_idx} generic={generic_idx}")
            else:
                check("P0-E「人员」人员配备 章节排在泛化描述前",
                      peibei_idx < generic_idx,
                      f"peibei@{peibei_idx} generic@{generic_idx}")

        # 用例 2：观测人员有哪些 —— Top1 必须是 人员配备 章节
        ps2 = top_parents("观测人员有哪些", None)
        check("P0-E「观测人员有哪些」Top1 = 人员配备章节",
              bool(ps2) and "人员配备" in ps2[0],
              (ps2[0][:40] if ps2 else "无召回"))

        # 用例 3：人员素质有什么要求 —— Top1 必须是 人员素质 章节
        ps3 = top_parents("人员素质有什么要求", None)
        check("P0-E「人员素质有什么要求」Top1 = 人员素质章节",
              bool(ps3) and "人员素质" in ps3[0],
              (ps3[0][:40] if ps3 else "无召回"))

        # 不破坏 hybrid（带嵌入）：仍应召回且 人员配备/人员素质 进入前列
        ps_h = top_parents("观测人员有哪些", emb)
        check("P0-E hybrid 模式下「观测人员有哪些」仍召回人员配备",
              bool(ps_h) and any("人员配备" in c for c in ps_h[:2]),
              (ps_h[0][:40] if ps_h else "无召回"))

        # 回归：allow_semantic_only 保持 0（不靠纯语义掩盖词法排序）
        check("P0-E 保持 allow_semantic_only=0（无语义-only 兜底）",
              not config.get_bool("SEARCH", "allow_semantic_only", False), "")
    finally:
        import shutil

        shutil.rmtree(tmp_e, ignore_errors=True)

    # ---------------------------------------------------------------- P0-A
    section("P0-A：选择本地模型即持久化并立即 ready（不调用 /api/ai/test）")
    gw = Gateway(ctx.db)
    # 用 patch 模拟 Ollama 在线且已安装该模型（不发起真实网络）
    gw.ollama_status = lambda force=False: True  # type: ignore[assignment]
    gw.state.ollama_checked_at = time.time()
    gw.state.ollama_healthy = True
    gw.state.ollama_models = ["qwen2.5:7b"]
    called_test = {"n": 0}

    def _fake_test(*a, **k):
        called_test["n"] += 1
        return {"ok": True}

    gw.test_ollama = _fake_test  # type: ignore[assignment]
    old_model = config.get_str("AI", "ollama_chat_model", "")
    old_provider_a = config.get_str("AI", "provider", "auto")
    try:
        # P0-A 验证的是「选模型即即时生效」，与用户选定的 provider 模式无关；
        # 显式用 auto 隔离该维度 —— test_suite 全局会把 provider 设为 offline，
        # 若不在此处覆盖，resolve_provider() 会直接返回 offline 而误判本用例失败。
        config.update({"AI": {"provider": "auto", "ollama_chat_model": ""}}, persist=False)
        before = gw.ai_readiness(probe=False)
        check("P0-A 未选模型时 state=selection_required",
              before["state"] == STATE_SELECTION_REQUIRED, before["state"])

        # 模拟前端 onSelectChatModel：只对 /api/config 发 POST，不点测试
        config.update({"AI": {"ollama_chat_model": "qwen2.5:7b"}}, persist=False)
        check("P0-A 选择后 ollama_model 立即生效（读取实时配置）",
              gw.ollama_model == "qwen2.5:7b", gw.ollama_model)
        after = gw.ai_readiness(probe=False)
        check("P0-A 选择后 state=ready / chat_ready=True（无需点测试/重启/刷新）",
              after["state"] == STATE_READY and after["chat_ready"] is True,
              f"{after['state']} ready={after['chat_ready']}")
        prov, _w = gw.resolve_provider()
        check("P0-A resolve_provider() == ollama", prov == "ollama", prov)
        check("P0-A 仅保存选择不触发 /api/ai/test",
              called_test["n"] == 0, f"test 被调用 {called_test['n']} 次")
    finally:
        config.update({"AI": {"provider": old_provider_a,
                              "ollama_chat_model": old_model}}, persist=False)

    # ---------------------------------------------------------------- P0-F
    section("P0-F：离线搜索结果展示（不伪装 AI 回答）")
    gw2 = Gateway(ctx.db)
    table_md = (
        "| 职责 | 姓名 | 职称 |\n| --- | --- | --- |\n"
        "| 现场观测负责人 | 张三 | 工程师 |\n| 内业资料整理 | 张三 | 助理工程师 |"
    )
    result = search_mod.SearchResult(query="观测人员有哪些", route="like")
    result.parents = [
        {"parent_id": "p1", "doc_id": "d1", "title": "八、观测人员配备",
         "path": "沉降观测方案.md", "content": "八、观测人员配备\n\n" + table_md,
         "score": 1.0, "similarity": None},
        {"parent_id": "p2", "doc_id": "d1", "title": "仪器设备、人员素质的要求",
         "path": "沉降观测方案.md", "content": "仪器设备、人员素质的要求\n\n观测人员素质要求。",
         "score": 0.5, "similarity": None},
    ]
    frames = list(gw2._offline_answer("观测人员有哪些", result))
    text = "".join(f.get("content", "") for f in frames if f.get("type") == "delta")
    check("P0-F 明确标注「本地搜索结果」", "本地搜索结果" in text, text[:60])
    check("P0-F 只说一次「未调用生成式 AI」",
          text.count("未调用生成式 AI") == 1, f"出现 {text.count('未调用生成式 AI')} 次")
    check("P0-F 不再使用旧伪装文案（已在本地知识库中检索到 N 段）",
          "已在本地知识库中检索到" not in text, "")
    check("P0-F 表格保留可读结构（多行 | 分隔，未被压成一行）",
          text.count("|") >= 6 and table_md.splitlines()[1] in text, "表格被压平")

    # ---------------------------------------------------------------- P0-B
    section("P0-B：Ollama 流式问答（thinking / 错误透出 / provider 锁定）")

    # B1: thinking 帧不得误判为「无输出」；应收到 ollama_progress + delta
    def _fake_stream_ok(url, payload, **kw):
        frames = [
            '{"message":{"thinking":"让我先看看人员配备表"},"done":false}',
            '{"message":{"content":"观测人员配备如下：现场观测负责人为张三。"},"done":false}',
            '{"done":true}',
        ]
        for fr in frames:
            yield fr

    gw3 = Gateway(ctx.db)
    # 模拟 Ollama 在线且已安装该模型（不发起真实网络）
    gw3.ollama_status = lambda force=False: True  # type: ignore[assignment]
    gw3.state.ollama_checked_at = time.time()
    gw3.state.ollama_healthy = True
    gw3.state.ollama_models = ["qwen2.5:7b"]
    # retrieve 返回一个含父块的结果：成功流走 Ollama，失败流走离线兜底（含本地搜索结果）
    _off_result = search_mod.SearchResult(query="观测人员有哪些", route="like")
    _off_result.parents = [{
        "parent_id": "p1", "doc_id": "d1", "title": "八、观测人员配备",
        "path": "沉降观测方案.md",
        "content": "八、观测人员配备\n\n| 职责 | 姓名 |\n| --- | --- |\n| 现场观测负责人 | 张三 |",
        "score": 1.0, "similarity": None,
    }]
    gw3.retrieve = lambda q, history=None: _off_result  # type: ignore[assignment]
    # 锁定 provider=ollama
    old_provider = config.get_str("AI", "provider", "auto")
    old_model_b = config.get_str("AI", "ollama_chat_model", "")
    try:
        config.update({"AI": {"provider": "ollama", "ollama_chat_model": "qwen2.5:7b"}},
                      persist=False)
        with patch.object(llm.net_util, "http_post_stream", _fake_stream_ok):
            frames_b1 = list(gw3.stream_chat("观测人员有哪些", []))
        types_b1 = [f.get("type") for f in frames_b1]
        has_delta = "delta" in types_b1
        has_done = "done" in types_b1
        # thinking 帧是内部「流活着」信号：不得误判为断流 → 仍应产出 delta + done，且不报错
        check("P0-B thinking 帧不误判为断流（仍产出 delta+done 且无 error）",
              has_delta and has_done and "error" not in types_b1, str(types_b1))

        # B2: 真实网络错误必须明确透出（不得静默 return）
        def _fake_stream_err(url, payload, **kw):
            raise llm.net_util.StreamError("OLLAMA_CONNECTION_CLOSED", "连接被拒")

        with patch.object(llm.net_util, "http_post_stream", _fake_stream_err):
            frames_b2 = list(gw3.stream_chat("观测人员有哪些", []))
        err_msg = " ".join(f.get("message", "") for f in frames_b2 if f.get("type") == "error")
        check("P0-B 网络错误明确透出（含 OLLAMA_CONNECTION_CLOSED）",
              "OLLAMA_CONNECTION_CLOSED" in err_msg, err_msg[:80])

        # B3: provider=ollama 失败 → 降级 offline，绝不转 api
        meta_providers = [f.get("provider") for f in frames_b2 if f.get("type") == "meta"]
        notice_msg = " ".join(f.get("message", "") for f in frames_b2 if f.get("type") == "notice")
        check("P0-B provider=ollama 失败时 meta 仍为 ollama（未偷偷改 api）",
              meta_providers and meta_providers[0] == "ollama", str(meta_providers))
        check("P0-B provider=ollama 失败降级文案明确「未转云端」",
              "未转云端" in notice_msg, notice_msg[:80])
        # 离线兜底内容应出现（本地搜索结果）
        off_text = "".join(f.get("content", "") for f in frames_b2 if f.get("type") == "delta")
        check("P0-B provider=ollama 失败后给出离线检索结果（非空手）",
              "本地搜索结果" in off_text, off_text[:40])

        # B4: 降级去向逻辑单元验证（不依赖网络）
        # ollama 模式 → offline
        check("P0-B _degrade_target(ollama) == offline",
              gw3._degrade_target() == "offline", gw3._degrade_target())
        # auto + 有 api_key → api（允许）
        config.update({"AI": {"provider": "auto", "api_key": "sk-x"}}, persist=False)
        check("P0-B _degrade_target(auto+key) == api",
              gw3._degrade_target() == "api", gw3._degrade_target())
    finally:
        config.update({"AI": {"provider": old_provider,
                              "ollama_chat_model": old_model_b,
                              "api_key": ""}}, persist=False)

    print("      P0 Core-Loop 回归：A/B/C/D/E/F 全部执行完毕")
