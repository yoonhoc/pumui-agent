"""HWPX 기관 양식을 같은 모양의 Word(.docx) 양식으로 옮긴다. 표준 라이브러리와 lxml만 쓴다.

    python tools/hwpx_to_docx.py forms/web/godeok/물품구매품의서_양식.hwpx forms/word/물품구매품의서_양식.docx

옮기는 것: 쪽 크기·여백(pagePr), 표 칸 너비(칸 격자에서 푼 열 너비)·가로/세로 병합·줄 높이·칸 안 여백·세로 정렬,
문단 정렬, 글자 크기·굵기·글꼴(글자 모양별). 길이는 HWPUNIT(1/7200 inch)을 twip(1/1440 inch)으로 바꾼다(÷5).
선은 모두 가는 실선으로 그린다. 글자는 바꾸지 않으므로 config/form_roles.json의 라벨 사전으로 그대로 채워진다.
"""
import re
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from lxml import etree

from common import rules
from hwpx import NS, Cell, Document, q
from hwpx_preview import Styles, column_widths

TWIP = 5
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
JC = {"left": "left", "right": "right", "center": "center", "justify": "both"}
VALIGN = {"TOP": "top", "CENTER": "center", "BOTTOM": "bottom"}
BORDER = '<w:{s} w:val="single" w:sz="4" w:space="0" w:color="000000"/>'


class DocxStyles(Styles):
    """글자 모양 id → 크기·굵기·글꼴·자간·장평."""

    def __init__(self, doc):
        super().__init__(doc)
        self.fit, self.color, self.used = {}, {}, {}  # id → (자간 %, 장평 %), id → 글자 색, 글꼴 → 쓴 횟수
        hdr = doc.files.get("Contents/header.xml", b"").decode("utf-8", "replace")
        for m in re.finditer(r'<hh:charPr id="(\d+)"[^>]*>.*?</hh:charPr>', hdr, re.S):
            sp = re.search(r'<hh:spacing hangul="(-?\d+)"', m.group(0))
            ra = re.search(r'<hh:ratio hangul="(\d+)"', m.group(0))
            col = re.search(r'textColor="#([0-9A-Fa-f]{6})"', m.group(0).split(">", 1)[0])
            self.fit[m.group(1)] = (int(sp.group(1)) if sp else 0, int(ra.group(1)) if ra else 100)
            self.color[m.group(1)] = col.group(1).upper() if col else "000000"

    def run_props(self, char_id):
        size, bold = self.char.get(char_id, (10, False))
        face = self.face.get(char_id) or ""
        spacing, ratio = self.fit.get(char_id, (0, 100))
        fonts = (f'<w:rFonts w:ascii="{escape(face)}" w:hAnsi="{escape(face)}" w:eastAsia="{escape(face)}" w:cs="{escape(face)}"/>'
                 if face else "")
        if face:
            self.used[face] = self.used.get(face, 0) + 1
        half = int(round(size * 2))
        color = f'<w:color w:val="{self.color.get(char_id, "000000")}"/>'
        sp = f'<w:spacing w:val="{round(size * spacing / 100 * 20)}"/>' if spacing else ""  # 자간: 글자 크기의 % → 1/20pt
        w = f'<w:w w:val="{ratio}"/>' if ratio != 100 else ""  # 장평
        return f'<w:rPr>{fonts}{"<w:b/>" if bold else ""}{color}{sp}{w}<w:sz w:val="{half}"/><w:szCs w:val="{half}"/></w:rPr>'

    def font_table(self):
        """쓴 글꼴 목록. 글꼴이 없는 PC에서 Word가 대신 쓸 비슷한 글꼴(altName)을 rules.json preview.font_fallback에서 단다."""
        fb = rules().get("preview", {}).get("font_fallback", {})
        out = []
        for face in self.used:
            alt = next((v for k, v in fb.items() if k in face), None)
            serif = any(k in face for k in ("명조", "바탕", "궁서"))
            out.append(f'<w:font w:name="{escape(face)}">' + (f'<w:altName w:val="{escape(alt)}"/>' if alt else "")
                       + f'<w:charset w:val="81"/><w:family w:val="{"roman" if serif else "swiss"}"/></w:font>')
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:fonts xmlns:w="{W}">{"".join(out)}</w:fonts>')

    def styles(self):
        """기본 글꼴은 양식에서 가장 많이 쓴 글꼴, 기본 크기 10pt, 줄 간격 1줄."""
        face = escape(max(self.used, key=self.used.get)) if self.used else ""
        fonts = f'<w:rFonts w:ascii="{face}" w:hAnsi="{face}" w:eastAsia="{face}" w:cs="{face}"/>' if face else ""
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:styles xmlns:w="{W}"><w:docDefaults><w:rPrDefault><w:rPr>{fonts}'
                '<w:sz w:val="20"/><w:szCs w:val="20"/><w:lang w:val="ko-KR" w:eastAsia="ko-KR"/></w:rPr></w:rPrDefault>'
                '<w:pPrDefault><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
                '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style></w:styles>')


def para_xml(p, st):
    align = JC.get(st.para.get(p.get("paraPrIDRef"), "left"), "left")
    runs = []
    for run in p.findall("hp:run", NS):
        text = "".join(t.text or "" for t in run.findall("hp:t", NS))
        props = st.run_props(run.get("charPrIDRef"))
        runs.append(f'<w:r>{props}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>' if text else f"<w:r>{props}</w:r>")
    return f'<w:p><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/><w:jc w:val="{align}"/></w:pPr>{"".join(runs)}</w:p>'


def table_xml(tbl, st):
    cells = []
    for tr in tbl.findall("hp:tr", NS):
        for tc in tr.findall("hp:tc", NS):
            addr, span, sz = tc.find("hp:cellAddr", NS), tc.find("hp:cellSpan", NS), tc.find("hp:cellSz", NS)
            cells.append({"r": int(addr.get("rowAddr")), "c": int(addr.get("colAddr")),
                          "cs": int(span.get("colSpan")), "rs": int(span.get("rowSpan")),
                          "w": int(sz.get("width", 0)) if sz is not None else 0,
                          "h": int(sz.get("height", 0)) if sz is not None else 0, "tc": tc})
    ncol = max(c["c"] + c["cs"] for c in cells)
    nrow = max(c["r"] + c["rs"] for c in cells)
    widths = [round(w / TWIP) for w in column_widths(cells, ncol)]
    heights = {}
    for c in cells:
        if c["rs"] == 1 and c["h"]:
            heights[c["r"]] = min(heights.get(c["r"], c["h"]), c["h"])
    start = {(c["r"], c["c"]): c for c in cells}
    rows = []
    for r in range(nrow):
        tcs, col = [], 0
        while col < ncol:
            c = start.get((r, col))
            above = next((x for x in cells if x["c"] == col and x["r"] < r < x["r"] + x["rs"]), None)
            if c is None and above is None:  # 칸 격자에 빈 곳(양식 오류): 빈 칸으로 메운다
                tcs.append(f'<w:tc><w:tcPr><w:tcW w:w="{widths[col]}" w:type="dxa"/></w:tcPr><w:p/></w:tc>')
                col += 1
                continue
            x = c or above
            w = sum(widths[x["c"]:x["c"] + x["cs"]])
            pr = f'<w:tcW w:w="{w}" w:type="dxa"/>' + (f'<w:gridSpan w:val="{x["cs"]}"/>' if x["cs"] > 1 else "")
            if x["rs"] > 1:
                pr += '<w:vMerge w:val="restart"/>' if c else "<w:vMerge/>"
            m = x["tc"].find("hp:cellMargin", NS)
            if m is not None:
                pr += "<w:tcMar>" + "".join(f'<w:{s} w:w="{int(m.get(s, 0)) // TWIP}" w:type="dxa"/>'
                                            for s in ("top", "left", "bottom", "right")) + "</w:tcMar>"
            sub = x["tc"].find("hp:subList", NS)
            pr += f'<w:vAlign w:val="{VALIGN.get(sub.get("vertAlign") if sub is not None else "", "center")}"/>'
            body = "".join(para_xml(p, st) for p in x["tc"].findall("hp:subList/hp:p", NS)) if c else ""
            tcs.append(f"<w:tc><w:tcPr>{pr}</w:tcPr>{body or '<w:p/>'}</w:tc>")
            col += x["cs"]
        h = round(heights.get(r, 0) / TWIP)
        trpr = f'<w:trPr><w:trHeight w:val="{h}" w:hRule="atLeast"/></w:trPr>' if h else ""
        rows.append(f"<w:tr>{trpr}{''.join(tcs)}</w:tr>")
    borders = "".join(BORDER.format(s=s) for s in ("top", "left", "bottom", "right", "insideH", "insideV"))
    pr = (f'<w:tblPr><w:tblW w:w="{sum(widths)}" w:type="dxa"/><w:jc w:val="center"/><w:tblBorders>{borders}</w:tblBorders>'
          '<w:tblLayout w:type="fixed"/></w:tblPr>')
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    return f"<w:tbl>{pr}<w:tblGrid>{grid}</w:tblGrid>{''.join(rows)}</w:tbl>"


def convert(src, dst):
    doc = Document(src)
    st = DocxStyles(doc)
    body, sect = [], ""
    for root in doc.sections.values():
        pp = root.find(".//hp:pagePr", NS)
        mg = root.find(".//hp:pagePr/hp:margin", NS)
        if pp is not None and mg is not None and not sect:
            tw = lambda v: round(int(v) / TWIP)
            sect = (f'<w:sectPr><w:pgSz w:w="{tw(pp.get("width", 59528))}" w:h="{tw(pp.get("height", 84188))}"/>'
                    f'<w:pgMar w:top="{tw(mg.get("top", 0)) + tw(mg.get("header", 0))}" w:right="{tw(mg.get("right", 0))}" '
                    f'w:bottom="{tw(mg.get("bottom", 0)) + tw(mg.get("footer", 0))}" w:left="{tw(mg.get("left", 0))}" '
                    'w:header="0" w:footer="0" w:gutter="0"/></w:sectPr>')
        for p in root.findall("hp:p", NS):
            tbls = [t for t in p.iter(q("tbl")) if next(t.iterancestors(q("p"))) is p]
            if Cell.para_text(p).strip() or not tbls:
                body.append(para_xml(p, st))
            body += [table_xml(t, st) for t in tbls]
    if body and body[-1].startswith("<w:tbl"):
        body.append("<w:p/>")  # 본문은 문단으로 끝나야 한다
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           f'{"".join(body)}{sect}</w:body></w:document>')
    etree.fromstring(xml.encode("utf-8"))  # 잘 짜인 XML인지 확인
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/word/fontTable.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml"/>'
        '</Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>',
        "word/_rels/document.xml.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/fontTable" Target="fontTable.xml"/>'
        '</Relationships>',
        "word/document.xml": xml,
        "word/styles.xml": st.styles(),
        "word/fontTable.xml": st.font_table(),
    }
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return Path(dst)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(convert(sys.argv[1], sys.argv[2]))
