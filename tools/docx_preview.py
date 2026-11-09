"""Word(.docx) 양식을 Word 없이 HTML로 그려 미리보기·PDF를 만든다(tools/hwpx_preview.py의 Word판).

    python tools/docx_preview.py 문서.docx [--pdf]

쪽 크기·여백(sectPr), 표 칸 너비(tblGrid)·병합(gridSpan, vMerge)·줄 높이(trHeight)·세로 정렬(vAlign),
문단 정렬(jc), 글자 크기·굵기·글꼴(rPr, 없으면 styles.xml 기본값)을 따른다. 빈 문단도 줄로 그린다.
글꼴이 이 PC에 없으면 config/rules.json preview.font_fallback의 비슷한 글꼴로 그린다.
제출본은 .docx 원본이고, 이 미리보기는 확인·인쇄용이다.
"""
import argparse
import html
import sys
import zipfile
from pathlib import Path

from lxml import etree

from common import html_to_pdf
from docx_form import NS, Cell, Table, q
from hwpx_preview import NUMBER, font_family

PX = 15  # twip → CSS px (1440 twip = 96 px)
ALIGN = {"left": "left", "start": "left", "right": "right", "end": "right", "center": "center", "both": "justify",
         "distribute": "justify"}
VALIGN = {"top": "top", "center": "middle", "bottom": "bottom"}
CSS = """
table { border-collapse: collapse; table-layout: fixed; }
td { border: 1px solid #222; vertical-align: top; word-break: keep-all; overflow-wrap: anywhere; }
p { margin: 0; white-space: pre-wrap; }
body { color: #111; }
@media screen { body { width: 720px; margin: 16px auto; } }
"""


class Styles:
    """styles.xml 기본 글꼴·크기·줄 간격."""

    def __init__(self, files):
        self.face, self.size, self.line = "", 10.0, 1.15
        raw = files.get("word/styles.xml")
        if not raw:
            return
        root = etree.fromstring(raw)
        f = root.find(".//w:docDefaults//w:rFonts", NS)
        s = root.find(".//w:docDefaults//w:sz", NS)
        sp = root.find(".//w:docDefaults//w:spacing", NS)
        if f is not None:
            self.face = f.get(q("eastAsia")) or f.get(q("ascii")) or ""
        if s is not None:
            self.size = int(s.get(q("val"))) / 2
        if sp is not None and sp.get(q("line")) and sp.get(q("lineRule"), "auto") == "auto":
            self.line = int(sp.get(q("line"))) / 240


def run_style(p, st):
    r = next((r for r in p.iter(q("r")) if "".join(t.text or "" for t in r.iter(q("t"))).strip()), None)
    r = r if r is not None else p.find(".//w:r", NS)
    pr = r.find("w:rPr", NS) if r is not None else None
    face, size, bold, spacing, color = st.face, st.size, False, 0, ""
    if pr is not None:
        f, s, sp, col = pr.find("w:rFonts", NS), pr.find("w:sz", NS), pr.find("w:spacing", NS), pr.find("w:color", NS)
        color = col.get(q("val"), "") if col is not None and col.get(q("val"), "auto") != "auto" else ""
        face = (f.get(q("eastAsia")) or f.get(q("ascii")) or face) if f is not None else face
        size = int(s.get(q("val"))) / 2 if s is not None else size
        bold = pr.find("w:b", NS) is not None and pr.find("w:b", NS).get(q("val"), "1") not in ("0", "false")
        spacing = int(sp.get(q("val"))) / 20 if sp is not None else 0  # 자간(pt)
    return face, size, bold, spacing, color


def render_para(p, st):
    text = Cell.para_text(p)
    jc = p.find("w:pPr/w:jc", NS)
    face, size, bold, spacing, color = run_style(p, st)
    css = [f"text-align:{ALIGN.get(jc.get(q('val')) if jc is not None else 'left', 'left')}",
           f"font-size:{size:g}pt", f"line-height:{st.line:.2f}"]
    fam = font_family(face)
    if fam:
        css.append(f"font-family:{fam}")
    if bold:
        css.append("font-weight:700")
    if spacing:
        css.append(f"letter-spacing:{spacing:g}pt")
    if color:
        css.append(f"color:#{color}")
    if text.strip() and NUMBER.fullmatch(text.strip()):
        css.append("white-space:nowrap")
    return f'<p style="{";".join(css)}">{html.escape(text) if text else "&nbsp;"}</p>'


def render_block(el, st):
    return render_para(el, st) if el.tag == q("p") else render_table(el, st) if el.tag == q("tbl") else ""


def render_table(tbl, st):
    t = Table(None, tbl, 0)
    widths = [int(g.get(q("w"), "0")) for g in tbl.findall("w:tblGrid/w:gridCol", NS)]
    jc = tbl.find("w:tblPr/w:jc", NS)
    margin = "margin:0 auto;" if jc is not None and jc.get(q("val")) == "center" else ""
    out = [f'<table style="{margin}width:{sum(widths) / PX:.1f}px"><colgroup>']
    out += [f'<col style="width:{w / PX:.1f}px">' for w in widths]
    out.append("</colgroup>")
    for r in range(t.rows):
        h = t.row_height(r)
        out.append(f'<tr style="height:{h / PX:.1f}px">' if h else "<tr>")
        for c in sorted((c for c in t.cells if c.row == r), key=lambda c: c.col):
            va = c.el.find("w:tcPr/w:vAlign", NS)
            style = f"vertical-align:{VALIGN.get(va.get(q('val')), 'top')};" if va is not None else ""
            body = "".join(render_block(ch, st) for ch in c.el if ch.tag in (q("p"), q("tbl")))
            mar = c.el.find("w:tcPr/w:tcMar", NS)  # 칸 안 여백: 양식 값, 없으면 Word 기본(좌우 108 twip)
            pad = [int(mar.find(q(s)).get(q("w"), "0")) / PX if mar is not None and mar.find(q(s)) is not None else d
                   for s, d in (("top", 0), ("right", 7.2), ("bottom", 0), ("left", 7.2))]
            style += "padding:" + " ".join(f"{v:.1f}px" for v in pad)
            out.append(f'<td colspan="{c.colspan}" rowspan="{c.rowspan}" style="{style}">{body}</td>')
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def page_css(root):
    sect = root.find(".//w:body/w:sectPr", NS)
    mm = lambda v: int(v) / 1440 * 25.4
    if sect is None:
        return "@page { size: A4; margin: 20mm; }"
    sz, mg = sect.find("w:pgSz", NS), sect.find("w:pgMar", NS)
    w, h = mm(sz.get(q("w"), 11906)), mm(sz.get(q("h"), 16838))
    return (f"@page {{ size: {w:.1f}mm {h:.1f}mm; margin: {mm(mg.get(q('top'), 1440)):.1f}mm {mm(mg.get(q('right'), 1440)):.1f}mm "
            f"{mm(mg.get(q('bottom'), 1440)):.1f}mm {mm(mg.get(q('left'), 1440)):.1f}mm; }}")


def to_html(path):
    with zipfile.ZipFile(path) as z:
        files = {n: z.read(n) for n in ("word/document.xml", "word/styles.xml") if n in z.namelist()}
    root = etree.fromstring(files["word/document.xml"])
    st = Styles(files)
    body = "".join(render_block(el, st) for el in root.find("w:body", NS))
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><style>{page_css(root)}{CSS}</style>'
            '<script>if (location.search.indexOf("print") >= 0) addEventListener("load", () => setTimeout(print, 300));</script>'
            f"</head><body>{body}</body></html>")


def preview(path, pdf=False):
    path = Path(path)
    out = path.with_suffix(".preview.html")
    out.write_text(to_html(path), encoding="utf-8")
    if pdf:
        html_to_pdf(out, path.with_suffix(".pdf"))
        return path.with_suffix(".pdf")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("docx")
    ap.add_argument("--pdf", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    print(preview(a.docx, a.pdf))
