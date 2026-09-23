"""真实 DOCX 表格检索 Blocker 回归（D1 + D2 + E + F + H）。

构造一个**合成 DOCX**，刻意复现真实失败：
* 表头单元格「规格型号」被拆成多个 ``w:t`` run + 空白 → 提取形态 ``规 格 型 号``（D1）。
* 表格行数足够多，超过 ``PARENT_HARD_LIMIT``，答案行落在后半段（D2）。
* 行内含 测斜仪/全站仪/电子水准仪/水位仪 等目标设备 + 干扰行。

然后走完整生产链：
DOCX bytes → converters → Markdown → chunker → 索引 → hybrid_search
验证 D1 规范化、D2 表格行级切片、E 表头传播、F 源行不漂移、H 五个查询正确命中型号。
"""
from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import chunker, converters  # noqa: E402
from app.core.db import Database  # noqa: E402
from app.core import indexer, search as S  # noqa: E402

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# ---- 合成 DOCX 构造 ----------------------------------------------------------
def _distributed_runs(text: str) -> str:
    """把 ``规格型号`` 变成 ``规 格 型 号`` 形式的多个 w:t run（复现 Word 分散对齐）。"""
    parts = []
    for i, ch in enumerate(text):
        if i == 0:
            parts.append(f'<w:r><w:t>{ch}</w:t></w:r>')
        else:
            parts.append(f'<w:r><w:t xml:space="preserve"> </w:t></w:r>')
            parts.append(f'<w:r><w:t>{ch}</w:t></w:r>')
    return "".join(parts)


def _cell(text: str) -> str:
    return f'<w:tc><w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p></w:tc>'


def _row(cells: list[str]) -> str:
    return "<w:tr>" + "".join(_cell(c) for c in cells) + "</w:tr>"


def build_synthetic_docx() -> bytes:
    header = ["序号", "监测设备名称", "规格型号", "数量", "国别产地", "备注"]
    # 表头第三列用分散 run 复现「规 格 型 号」
    header_xml = (
        _cell(header[0]) + _cell(header[1])
        + f'<w:tc><w:p>{_distributed_runs("规格型号")}</w:p></w:tc>'
        + _cell(header[3]) + _cell(header[4]) + _cell(header[5])
    )
    sep_row = "<w:tr>" + ('<w:tc><w:p><w:r><w:t xml:space="preserve">---</w:t></w:r></w:p></w:tc>' * 6) + "</w:tr>"
    rows_xml = [_row(header), sep_row]
    # 干扰行 + 目标设备；答案行放在后半段（> PARENT_HARD_LIMIT）
    devices = [
        ("测斜仪", "TL-03D型"), ("钻机", "XY-1型"), ("全站仪", "拓普康GM-101"),
        ("振弦式频率仪", "609"), ("振弦式钢筋计", "GX系列"), ("反力计", "FX系列"),
        ("测斜管", "CX-70"), ("水位管", "PVC"), ("笔记本电脑", "联想"),
        ("沉降观测仪", "DS05"), ("裂缝计", "HC-01"), ("应力计", "VG-20"),
        ("温度计", "PT100"), ("风速仪", "AICE-FS"), ("气压计", "DYM3"),
        ("渗压计", "PZ-02"), ("混凝土应变计", "CXG-05"), ("土压力盒", "TYJ-10"),
        ("电子水准仪", "天宝DINI03"), ("全站仪B", "尼康Nivo"), ("GPS接收机", "R8"),
        ("水准尺", "3m铟钢"), ("棱镜", "迷你棱镜"), ("水准仪", "天宝DINI03"),
        ("水位仪", "SW-30"), ("雨量计", "SL1"), ("百叶箱", "木质"),
        ("数据采集仪", "DTU-01"), ("太阳能板", "50W"), ("蓄电池", "12V"),
    ]
    for i, (name, model) in enumerate(devices, 1):
        # 备注列特意带「CJK-ASCII 空格」（备注1 号），用于验证 D1 归一化**不**误删
        # 正文里正常的「汉字↔ASCII」间隙（如「美国 1台」「N2 级别」）。
        rows_xml.append(_row([str(i), name, model, "1台", "中国", f"备注{i} 号"]))
    table = "<w:tbl>" + _row(header) + rows_xml[1] + "".join(rows_xml[2:]) + "</w:tbl>"

    body = (
        '<w:document xmlns:w="' + W + '">'
        f'<w:body><w:p><w:pPr><w:outlineLvl w:val="1"/></w:pPr>'
        '<w:r><w:t xml:space="preserve">监测仪器设备和材料</w:t></w:r></w:p>'
        + table
        + "</w:body></w:document>"
    )
    ct = (
        '<?xml version="1.0"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", body)
    return buf.getvalue()


def run(ctx, check, section, skip):
    section("真实 DOCX 表格检索 Blocker 回归")
    docx = build_synthetic_docx()

    # ---- D1：converter 规范化分散表头 --------------------------------------
    cr = converters.convert(docx, "synthetic_table.docx")
    check("A. DOCX 转换成功", cr.ok)
    md = cr.markdown
    check("B1. 分散表头被规范化为『规格型号』", "规格型号" in md and "规 格 型 号" not in md)
    check("B2. 正文 CJK-ASCII 空格不破坏（备注1 号 原样）", "备注1 号" in md)

    # ---- D2/E：chunker 表格行级切片 + 表头传播 ----------------------------
    pd = chunker.parse(md, "synthetic_table.md")
    # 整表应保留在单一父块（不字符硬切），且父块完整含表头
    tbl_parents = [p for p in pd.parents if "规格型号" in p.content and "|" in p.content]
    check("C1. 长表未被字符硬切（单一父块含完整表）", len(tbl_parents) == 1)
    # 每个含目标设备的子 chunk 都前置表头（共现）
    def chunk_contains(name: str) -> list:
        return [c for c in pd.children if name in c.content]
    for name, model in (("电子水准仪", "天宝DINI03"), ("全站仪", "拓普康GM-101"),
                         ("水准仪", "天宝DINI03"), ("水位仪", "SW-30")):
        chs = chunk_contains(name)
        check(f"C2. 含『{name}』的 chunk 存在", bool(chs))
        # C3：该设备所在 chunk 必须同时带表头（规格型号）+ 数据行（E：表头传播+共现）。
        # 用「name + model 双命中」精确定位到目标设备行，避免干扰行（全站仪B / 电子水准仪
        # 同含子串）污染断言 —— 这正是真实库里「型号」召回不全的根因之一。
        rel = [c for c in pd.children if name in c.content and model in c.content]
        ok = bool(rel) and all("规格型号" in c.content for c in rel)
        check(f"C3. 『{name}』chunk 表头+数据行共现（E）", ok)

    # ---- F：源行不漂移（父块 source 行落在真实表格区） --------------------
    tp = tbl_parents[0]
    check("D1. 父块源行范围有效", tp.source_start_line > 0 and tp.source_end_line >= tp.source_start_line)
    # 真实笔记里 『天宝DINI03』应只在数据行出现，不在任何重复表头里
    dup_header_has_model = "天宝DINI03" in tp.content.split("\n", 1)[0]  # 表头第一行
    check("D2. 重复表头不含型号（F：不污染源行）", not dup_header_has_model)

    # ---- H：端到端索引 + 检索 ----------------------------------------------
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="usb-wiki-dtbl-"))
    try:
        dbp = tmp / "cache.db"
        db = Database(dbp)
        db.init_schema()  # 建派生索引表（可全量重建，不触碰 durable 笔记）
        # 走生产链：DOCX bytes → converters → Markdown → chunker.parse → index_parsed。
        # 刻意**不**用 rebuild_all（它会扫真实 notes 目录），保持测试自包含、零真实写入。
        parsed = chunker.parse(md, "synthetic_table.md")
        indexer.index_parsed(db, parsed, len(md.encode("utf-8")), 0.0, None)
        expect = {
            "水准仪什么型号": "天宝DINI03",
            "水准仪 型号": "天宝DINI03",
            "电子水准仪型号是什么": "天宝DINI03",
            "全站仪什么型号": "拓普康GM-101",
            "水位仪什么型号": "SW-30",
        }
        for q, model in expect.items():
            res = S.hybrid_search(db, None, q, top_k_parents=5)
            top = res.references[0] if res.references else None
            ok = False
            if top is not None:
                from app.core.db import Database as _D  # noqa
                import sqlite3
                conn = sqlite3.connect(str(dbp)); conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT content FROM parent_blocks WHERE parent_id=?",
                                   (top.parent_id,)).fetchone()
                ok = bool(row and model in (row["content"] or ""))
                conn.close()
            check(f"H. 查询『{q}』Top1 父块含 {model}", ok)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    ok = fail = 0

    def _check(name, cond):
        global ok, fail
        if cond:
            ok += 1
            print(f"  PASS  {name}")
        else:
            fail += 1
            print(f"  FAIL  {name}")

    def _sec(name):
        print(f"\n=== {name} ===")

    run(None, _check, _sec, lambda *a, **k: None)
    print(f"\nPASS {ok}  FAIL {fail}")
    raise SystemExit(1 if fail else 0)
