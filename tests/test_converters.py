"""多格式转换器验收测试。

**全部测试素材由代码现场合成**（含合法 OOXML / EPUB / PDF），不依赖任何外部样本文件，
因此在任何机器上结果一致 —— 这是"能不能解析真实 Office 文件"最硬的证明。
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from app.core import converters, crawler


def _ok_import(mod: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False

# ---------------------------------------------------------------- 素材合成
DOCX_DOCUMENT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:body>
  <w:p><w:pPr><w:pStyle w:val="Heading1"/><w:outlineLvl w:val="0"/></w:pPr>
       <w:r><w:t>注意力机制导论</w:t></w:r></w:p>
  <w:p><w:r><w:t>Transformer 的核心是自注意力结构，能够捕捉长距离依赖。</w:t></w:r></w:p>
  <w:p><w:pPr><w:pStyle w:val="Heading2"/><w:outlineLvl w:val="1"/></w:pPr>
       <w:r><w:t>位置编码</w:t></w:r></w:p>
  <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>
       <w:r><w:t>正弦余弦构造位置编码</w:t></w:r></w:p>
  <w:tbl>
   <w:tr><w:tc><w:p><w:r><w:t>模型</w:t></w:r></w:p></w:tc>
         <w:tc><w:p><w:r><w:t>维度</w:t></w:r></w:p></w:tc></w:tr>
   <w:tr><w:tc><w:p><w:r><w:t>Base</w:t></w:r></w:p></w:tc>
         <w:tc><w:p><w:r><w:t>512</w:t></w:r></w:p></w:tc></w:tr>
  </w:tbl>
  <w:sectPr/>
 </w:body>
</w:document>"""

DOCX_CORE = """<?xml version="1.0" encoding="UTF-8"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <dc:title>注意力机制导论</dc:title>
</cp:coreProperties>"""


XLSX_CORE = """<?xml version="1.0" encoding="UTF-8"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <dc:title>销售统计表</dc:title>
</cp:coreProperties>"""


def make_docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("word/document.xml", DOCX_DOCUMENT)
        z.writestr("docProps/core.xml", DOCX_CORE)
    return buf.getvalue()


def make_pptx() -> bytes:
    """刻意**不带** docProps/core.xml —— 用于覆盖「无元数据时回退文件名」这条路径。"""

    def slide(rows):
        body = ""
        for txt in rows:
            body += f'<a:p><a:r><a:t>{escape(txt)}</a:t></a:r></a:p>'
        return ('<?xml version="1.0" encoding="UTF-8"?>'
                '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
                ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                f'<p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:sld>')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("ppt/slides/slide1.xml", slide(["项目汇报", "本季度完成三件事", "第一件：上线知识库"]))
        z.writestr("ppt/slides/slide2.xml", slide(["下一步计划", "打通多格式导入", "补齐 PDF 解析"]))
        z.writestr("ppt/notesSlides/notesSlide1.xml",
                   '<p:notes xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
                   ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                   '<a:p><a:r><a:t>备注：重点讲知识库</a:t></a:r></a:p></p:notes>')
    return buf.getvalue()


def make_xlsx() -> bytes:
    shared = ('<?xml version="1.0" encoding="UTF-8"?>'
              '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<si><t>产品</t></si><si><t>销量</t></si>'
              '<si><t>加壳工具</t></si><si><t>知识库</t></si></sst>')
    sheet = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetData>'
             '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
             '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>128</v></c></row>'
             '<row r="3"><c r="A3" t="s"><v>3</v></c><c r="B3"><v>256</v></c></row>'
             '</sheetData></worksheet>')
    wb = ('<?xml version="1.0" encoding="UTF-8"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
          ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          '<sheets><sheet name="销售统计" sheetId="1" r:id="rId1"/></sheets></workbook>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"'
            ' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
            '</Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("xl/sharedStrings.xml", shared)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        z.writestr("docProps/core.xml", XLSX_CORE)
    return buf.getvalue()


def make_epub() -> bytes:
    container = ('<?xml version="1.0"?>'
                 '<container version="1.0"'
                 ' xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf"'
                 ' media-type="application/oebps-package+xml"/></rootfiles></container>')
    opf = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           '<dc:title>便携知识库手册</dc:title></metadata>'
           '<manifest>'
           '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
           '<item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>'
           '</manifest><spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>')
    ch = ('<html><head><title>{t}</title></head><body><h1>{t}</h1>'
          '<p>{p}</p></body></html>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/ch1.xhtml", ch.format(
            t="第一章 便携哲学",
            p="真相源应当是普通文件，数据库只承担可随时重建的索引职责，这样介质损坏也不丢数据。" * 2))
        z.writestr("OEBPS/ch2.xhtml", ch.format(
            t="第二章 混合检索",
            p="词法检索负责精确命中，向量检索负责语义近邻，两者用 RRF 融合排序。" * 2))
    return buf.getvalue()


def _pdf_from_lines(lines: list[tuple[int, int, int, str]]) -> bytes:
    """lines: [(字号, x, y, 文本)] → 合法 PDF（真实文本流 + 正确 xref 偏移）。"""
    parts = []
    for size, x, y, text in lines:
        esc = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        parts.append(f"BT /F1 {size} Tf {x} {y} Td ({esc}) Tj ET")
    content = "\n".join(parts).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n"
            f"{xref_at}\n%%EOF\n").encode()
    return bytes(out)


def make_pdf() -> bytes:
    """单页单行 PDF（含真实文本流与正确的 xref 偏移）。"""
    return _pdf_from_lines([(14, 72, 720, "Wiki USB PDF extraction works fine.")])


PDF_PARAGRAPHS = [
    "Portable knowledge bases keep plain Markdown files as the single source of truth.",
    "The SQLite database is only a rebuildable derived index so corruption never costs data.",
    "Hybrid retrieval fuses lexical and vector results with Reciprocal Rank Fusion.",
    "Always bind to loopback and probe for a free port instead of hardcoding one.",
]


def make_pdf_with_paragraphs() -> bytes:
    """多段落 PDF：段间留出垂直间距，模拟真实排版。"""
    lines: list[tuple[int, int, int, str]] = []
    y = 740
    for para in PDF_PARAGRAPHS:
        for i in range(0, len(para), 70):
            lines.append((11, 72, y, para[i:i + 70]))
            y -= 15
        y -= 13            # 段间距
    return _pdf_from_lines(lines)


# ==========================================================================
def run(check) -> None:
    print("\n── 多格式转换器 " + "─" * 56)

    # ---------- 纯文本族 ----------
    r = converters.convert("# 标题\n\n正文内容".encode(), "note.md")
    check("Markdown 直通", r.ok and "# 标题" in r.markdown and r.kind == "markdown")

    r = converters.convert("中文 GBK 内容测试".encode("gb18030"), "gbk.txt")
    check("GBK 编码文本自动识别", r.ok and "中文 GBK 内容测试" in r.markdown,
          str(r.warnings))

    r = converters.convert(b"", "empty.txt")
    check("空文件被拒绝", not r.ok and "空文件" in r.error, r.error)

    r = converters.convert(Path("app/core/converters.py").read_bytes(), "converters.py")
    check("代码文件包成对应语言围栏", r.ok and "```python" in r.markdown and r.kind == "code")

    # ---------- 数据格式 ----------
    r = converters.convert("产品,销量\n加壳工具,128\n知识库,256\n".encode(), "sales.csv")
    check("CSV 转 Markdown 表格",
          r.ok and "| 产品 | 销量 |" in r.markdown and "| --- | --- |" in r.markdown
          and "加壳工具" in r.markdown, r.markdown[:120])

    r = converters.convert('{"a":[1,2],"b":"中文"}'.encode(), "d.json")
    check("JSON 美化并围栏", r.ok and '"中文"' in r.markdown and "```json" in r.markdown)

    r = converters.convert(b"server:\n  port: 28765\n", "c.yaml")
    check("YAML 围栏保留", r.ok and "port: 28765" in r.markdown and "```yaml" in r.markdown)

    r = converters.convert(b"<root><item id='1'>hello</item></root>", "d.xml")
    check("XML 转层级大纲", r.ok and "item" in r.markdown and "hello" in r.markdown, r.markdown[:120])

    # ---------- 字幕 / RTF ----------
    srt = "1\n00:00:01,000 --> 00:00:03,000\n第一句台词\n\n2\n00:00:03,500 --> 00:00:05,000\n第二句台词\n"
    r = converters.convert(srt.encode(), "a.srt")
    check("SRT 去掉时间轴只留台词",
          r.ok and "第一句台词" in r.markdown and "00:00" not in r.markdown, r.markdown[:100])

    rtf = (r"{\rtf1\ansi\deff0 {\fonttbl{\f0 Arial;}}\f0\fs24 "
           + "加壳工具测试" + r"\par 第二行\par}").encode("utf-8")
    r = converters.convert(rtf, "a.rtf")
    check("RTF 有损提取可见文字", r.ok and "加壳工具测试" in r.markdown, r.markdown[:100])

    # ---------- HTML / 邮件 ----------
    html = ("<html><head><title>测试页</title><meta name='author' content='张三'></head>"
            "<body><article><h1>正文标题</h1><p>" + "这是一段足够长的正文用于通过抽取阈值。" * 8 +
            "</p></article></body></html>")
    r = converters.convert(html.encode(), "p.html")
    check("HTML 抽取正文并保留标题", r.ok and "正文标题" in r.markdown and r.kind == "html",
          r.markdown[:120])

    eml = ("From: a@b.com\r\nTo: c@d.com\r\nSubject: 测试邮件\r\n"
           "Content-Type: text/plain; charset=utf-8\r\nMIME-Version: 1.0\r\n\r\n"
           "这是邮件正文内容。").encode()
    r = converters.convert(eml, "m.eml")
    check("EML 提取头部与正文",
          r.ok and "测试邮件" in r.markdown and "这是邮件正文内容" in r.markdown, r.markdown[:140])

    # ---------- OOXML ----------
    r = converters.convert(make_docx(), "doc.docx")
    ok = (r.ok and "注意力机制导论" in r.markdown
          and "## 位置编码" in r.markdown        # outlineLvl 1 -> ##
          and "- 正弦余弦构造位置编码" in r.markdown
          and "| 模型 | 维度 |" in r.markdown)
    check("DOCX：标题层级 + 列表 + 表格", ok, r.markdown[:260] or r.error)
    check("DOCX：标题取自 core.xml 而非文件名",
          r.ok and r.title == "注意力机制导论", r.title)

    r = converters.convert(make_pptx(), "季度汇报.pptx")
    ok = (r.ok and "## 第 1 页" in r.markdown and "## 第 2 页" in r.markdown
          and "项目汇报" in r.markdown and "补齐 PDF 解析" in r.markdown)
    check("PPTX：按页拆分并提取文本框", ok, r.markdown[:220] or r.error)
    check("PPTX：无 core.xml 时标题回退为文件名",
          r.ok and r.title == "季度汇报", r.title)
    check("PPTX：备注页一并提取",
          r.ok and "重点讲知识库" in r.markdown, r.markdown[-160:])

    r = converters.convert(make_xlsx(), "book.xlsx")
    ok = (r.ok and "## 销售统计" in r.markdown
          and "| 产品 | 销量 |" in r.markdown and "| 加壳工具 | 128 |" in r.markdown)
    check("XLSX：工作表名 + sharedStrings + 表格", ok, r.markdown[:260] or r.error)
    check("XLSX：首行作为表头（非「列N」）",
          r.ok and "列1" not in r.markdown, r.markdown[:120])
    check("XLSX：标题取自 core.xml", r.ok and r.title == "销售统计表", r.title)

    r = converters.convert(make_epub(), "book.epub")
    ok = (r.ok and "便携知识库手册" in r.markdown
          and "第一章" in r.markdown and "第二章" in r.markdown
          and "RRF 融合排序" in r.markdown)
    check("EPUB：按 spine 顺序抽取全部章节", ok, r.markdown[:220] or r.error)

    # ---------- PDF ----------
    check("PDF 后端自动选择 pdfminer.six（若已安装）",
          converters.pdf_backend() in ("pdfminer.six", "pypdf", "PyPDF2"),
          converters.pdf_backend())
    if converters.pdf_backend():
        r = converters.convert(make_pdf(), "p.pdf")
        check(f"PDF 提取正文（后端 {converters.pdf_backend()}）",
              r.ok and "extraction works fine" in r.markdown, (r.error or r.markdown)[:160])

        # 段落结构：pdfminer.six 保留段落空行 → 切片能按段断开而不是句子中间硬切
        real = converters.pdf_backend
        outs = {}
        for backend in ("pdfminer.six", "pypdf"):
            if backend == "pypdf" and not any(
                _ok_import(m) for m in ("pypdf", "PyPDF2")
            ):
                continue
            converters.pdf_backend = lambda b=backend: b
            outs[backend] = converters.convert(make_pdf_with_paragraphs(), "p.pdf")
        converters.pdf_backend = real

        # 段首词组都不足 70 字符，不会被折行，适合作为断言锚点
        anchors = ["Portable knowledge bases", "The SQLite database",
                   "Hybrid retrieval", "Always bind to loopback"]
        check("PDF：两个后端都能提取多段落正文",
              all(x.ok and all(a in x.markdown for a in anchors) for x in outs.values()),
              str({k: (v.ok, [a for a in anchors if a not in v.markdown]) for k, v in outs.items()}))
        if "pdfminer.six" in outs and outs["pdfminer.six"].ok:
            body = outs["pdfminer.six"].markdown.split("\n\n", 2)[-1]
            check("PDF：pdfminer.six 保留段落空行（利于切片）",
                  "\n\n" in body.strip(), body[:120])
        if "pypdf" in outs and outs["pypdf"].ok:
            body = outs["pypdf"].markdown.split("\n\n", 2)[-1]
            check("PDF：pypdf 不保留段落结构（已知局限，故降级为后备）",
                  "\n\n" not in body.strip(), body[:120])
    else:
        r = converters.convert(make_pdf(), "p.pdf")
        check("PDF 后端缺失时给出可操作提示",
              (not r.ok) and "pypdf" in r.error, r.error)

    # ---------- 明确的边界拒绝 ----------
    r = converters.convert(b"\xd0\xcf\x11\xe0legacy", "old.doc")
    check("旧版 .doc 给出「另存为」指引",
          (not r.ok) and "另存为" in r.error, r.error)

    r = converters.convert(b"\x89PNG\r\n\x1a\n", "pic.png")
    check("图片给出「需 OCR」明确说明",
          (not r.ok) and "OCR" in r.error, r.error)

    r = converters.convert(b"PK\x03\x04xx", "archive.zip")
    check("未支持格式给出格式名", (not r.ok) and ".zip" in r.error, r.error)

    r = converters.convert(b"not a zip", "fake.docx")
    check("伪造后缀的坏文件不崩溃且提示清晰",
          (not r.ok) and ("不是有效" in r.error or "找不到" in r.error), r.error)

    # ---------- 能力清单 ----------
    caps = converters.supported_extensions()
    for ext in (".docx", ".pptx", ".xlsx", ".epub", ".pdf", ".html", ".csv", ".py", ".srt"):
        check(f"能力清单包含 {ext}", ext in caps["exts"])
    check("能力清单明确列出不支持的类型",
          ".doc" in caps["unsupported"]["legacy_office"]
          and ".png" in caps["unsupported"]["image"], json.dumps(caps["unsupported"], ensure_ascii=False)[:120])
