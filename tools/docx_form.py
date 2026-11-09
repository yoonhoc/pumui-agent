"""Word(.docx) 양식을 읽고 표 칸에 글자를 써 넣는다. tools/hwpx.py와 같은 모양(Document, Table, Cell)이라
fill_form.py가 HWPX 양식과 같은 방법으로 채운다.

DOCX는 zip이고 본문은 word/document.xml에 있다. 표는 w:tbl, 줄은 w:tr, 칸은 w:tc이며
가로 병합은 w:gridSpan, 세로 병합은 w:vMerge(restart로 시작, 값 없는 것이 이어지는 칸)다.
글자는 w:p > w:r > w:t 에 있다. 길이 단위는 twip(1/1440 inch)이다.

    python tools/docx_form.py 양식.docx        # 표·칸 구조를 출력 (양식 분석용)
"""
import copy
import itertools
import sys
import zipfile
from pathlib import Path

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
HWP_PER_TWIP = 5  # HWPUNIT(1/7200 inch) → twip(1/1440 inch). 양식 맞추기 설정값(form_roles.json)은 HWPUNIT로 적혀 있다
_REFS = {}  # 글자·문단 모양 참조: id → rPr/pPr 복사본 (hwpx의 charPrIDRef·paraPrIDRef 자리)
_IDS = itertools.count(1)


def q(tag):
    return f"{{{W}}}{tag}"


def _ref(el):
    if el is None:
        return None
    k = f"docx{next(_IDS)}"
    _REFS[k] = copy.deepcopy(el)
    return k


def _set_child(parent, el, before=()):
    """parent의 같은 태그 자식을 el로 바꾼다. 없으면 before 태그들 앞(스키마 순서)에 넣는다."""
    old = parent.find(el.tag)
    if old is not None:
        parent.replace(old, el)
        return
    for b in before:
        nxt = parent.find(q(b))
        if nxt is not None:
            nxt.addprevious(el)
            return
    parent.append(el)


class Cell:
    def __init__(self, table, el, row, col, colspan):
        self.table, self.el = table, el
        self.row, self.col, self.colspan, self.rowspan = row, col, colspan, 1

    @property
    def text(self):
        return "\n".join(self.para_text(p) for p in self.paragraphs()).strip()

    def inner_width(self):
        w = self.el.find("w:tcPr/w:tcW", NS)
        return int(w.get(q("w"), "0")) * HWP_PER_TWIP if w is not None and w.get(q("type")) == "dxa" else 0

    def set_side_margin(self, margin):
        """칸 안쪽 좌우 여백을 margin(HWPUNIT) 이하로 줄인다."""
        pr = self.el.find("w:tcPr", NS)
        if pr is None:
            pr = etree.SubElement(self.el, q("tcPr"))
            self.el.insert(0, pr)
        mar = pr.find("w:tcMar", NS)
        if mar is None:
            mar = etree.Element(q("tcMar"))
            _set_child(pr, mar, ("textDirection", "tcFitText", "vAlign", "hideMark"))
        for side in ("left", "right"):
            el = mar.find(q(side))
            if el is None:
                el = etree.SubElement(mar, q(side))
            el.set(q("w"), str(margin // HWP_PER_TWIP))
            el.set(q("type"), "dxa")

    def set_text(self, value, char_ref=None, para_ref=None):
        """칸 글자를 value로 바꾼다. 첫 문단만 남기고(안에 든 표는 둔다) 그 문단의 글자를 바꾼다."""
        paras = self.paragraphs()
        if not paras:
            paras = [etree.SubElement(self.el, q("p"))]
        first = paras[0]
        for p in paras[1:]:
            self.el.remove(p)
        if para_ref in _REFS:
            _set_child(first, copy.deepcopy(_REFS[para_ref]))
            first.insert(0, first.find("w:pPr", NS))
        self.set_para_text(first, "" if value is None else str(value), char_ref)

    def para_ref(self):
        p = self.el.find("w:p", NS)
        return _ref(p.find("w:pPr", NS)) if p is not None else None

    def char_ref(self):
        r = self.el.find("w:p/w:r", NS)
        return _ref(r.find("w:rPr", NS)) if r is not None else None

    def paragraphs(self):
        return self.el.findall("w:p", NS)

    def append_line(self, value, char_ref=None):
        paras = self.paragraphs()
        new = copy.deepcopy(paras[-1])
        paras[-1].addnext(new)
        self.set_para_text(new, value, char_ref)

    def insert_line_before(self, para, value):
        new = copy.deepcopy(para)
        para.addprevious(new)
        self.set_para_text(new, value)

    @staticmethod
    def para_text(p):
        out = []
        for el in p.iter(q("t"), q("tab"), q("br")):
            out.append(el.text or "" if el.tag == q("t") else ("\t" if el.tag == q("tab") else "\n"))
        return "".join(out)

    @staticmethod
    def set_para_text(p, value, char_ref=None):
        """문단 하나의 글자를 value로 바꾼다(첫 글자 모양 유지, char_ref를 주면 그 모양)."""
        runs = p.findall(".//w:r", NS)
        rpr = _REFS.get(char_ref) if char_ref else None
        if rpr is None and runs:
            rpr = runs[0].find("w:rPr", NS)
        rpr = copy.deepcopy(rpr) if rpr is not None else None
        for child in list(p):
            if child.tag != q("pPr"):
                p.remove(child)
        r = etree.SubElement(p, q("r"))
        if rpr is not None:
            r.append(rpr)
        for i, line in enumerate(str(value).split("\n")):
            if i:
                etree.SubElement(r, q("br"))
            t = etree.SubElement(r, q("t"))
            t.text = line
            t.set(XML_SPACE, "preserve")


class Table:
    def __init__(self, doc, el, index):
        self.doc, self.el, self.index = doc, el, index
        grid = el.findall("w:tblGrid/w:gridCol", NS)
        self.trs = el.findall("w:tr", NS)
        self.rows = len(self.trs)
        self.cells = []
        top = {}  # 열 → 위에서 세로 병합이 시작된 칸
        width = 0
        for r, tr in enumerate(self.trs):
            before = tr.find("w:trPr/w:gridBefore", NS)
            col = int(before.get(q("val"))) if before is not None else 0
            for tc in tr.findall("w:tc", NS):
                span_el = tc.find("w:tcPr/w:gridSpan", NS)
                span = int(span_el.get(q("val"))) if span_el is not None else 1
                vm = tc.find("w:tcPr/w:vMerge", NS)
                if vm is not None and vm.get(q("val"), "continue") != "restart" and col in top:
                    top[col].rowspan += 1
                else:
                    c = Cell(self, tc, r, col, span)
                    self.cells.append(c)
                    if vm is not None:
                        top[col] = c
                    else:
                        top.pop(col, None)
                col += span
            width = max(width, col)
        self.cols = max(len(grid), width)

    def at(self, row, col):
        for c in self.cells:
            if c.row <= row < c.row + c.rowspan and c.col <= col < c.col + c.colspan:
                return c
        return None

    def row_cells(self, row):
        return sorted({id(c): c for c in self.cells if c.row <= row < c.row + c.rowspan}.values(), key=lambda c: c.col)

    def insert_row_after(self, row):
        """row 줄을 복제해 바로 아래에 빈 줄을 넣는다. 세로 병합 칸은 이어지는 칸으로 넣는다."""
        tr = self.trs[row]
        clone = copy.deepcopy(tr)
        for tc in clone.findall("w:tc", NS):
            vm = tc.find("w:tcPr/w:vMerge", NS)
            if vm is not None:
                vm.attrib.pop(q("val"), None)
            c = Cell(self, tc, row + 1, 0, 1)
            c.set_text("")
        tr.addnext(clone)
        self.__init__(self.doc, self.el, self.index)

    def row_height(self, row):
        h = self.trs[row].find("w:trPr/w:trHeight", NS) if row < self.rows else None
        return int(h.get(q("val"), "0")) if h is not None else 0

    def set_row_height(self, row, height):
        h = self.trs[row].find("w:trPr/w:trHeight", NS)
        if h is not None and int(h.get(q("val"), "0")) > height:
            h.set(q("val"), str(height))


class Document:
    cell_type = Cell
    doc_xml = "word/document.xml"

    def __init__(self, path):
        self.path = Path(path)
        with zipfile.ZipFile(self.path) as z:
            self.files = {i.filename: z.read(i.filename) for i in z.infolist()}
        self.sections = {self.doc_xml: etree.fromstring(self.files[self.doc_xml])}
        root = self.sections[self.doc_xml]
        self.tables = [Table(self, el, i) for i, el in enumerate(root.iter(q("tbl")))]

    def char_styles(self):
        return {}  # 글자 모양을 바꿔 줄이는 일(shrink_to_fit)은 HWPX 전용. Word는 칸 폭에 맞춰 줄을 바꾼다

    def outside_paragraphs(self):
        """표 밖 본문 문단."""
        return [p for root in self.sections.values() for p in root.iter(q("p"))
                if not any(a.tag == q("tc") for a in p.iterancestors())]

    def body_paragraphs(self):
        return [t.strip() for t in (Cell.para_text(p) for p in self.outside_paragraphs()) if t.strip()]

    def fit_page(self, line=1.4, safety=1.06):
        """표가 한 쪽을 넘으면, 가장 여유가 큰 줄(양식의 큰 빈칸)의 최소 높이를 넘친 만큼 줄인다.
        글자 높이는 칸 폭에 맞춘 줄 수로 어림한다(한글 1자 ≈ 글자 크기, 영숫자 ≈ 0.55배). 품목 줄을 늘려도 1쪽에 맞추기 위함."""
        root = self.sections[self.doc_xml]
        sect = root.find(".//w:body/w:sectPr", NS)
        if sect is None or not self.tables:
            return
        sz, mg = sect.find("w:pgSz", NS), sect.find("w:pgMar", NS)
        room = int(sz.get(q("h"))) - int(mg.get(q("top"))) - int(mg.get(q("bottom")))
        t = max((t for t in self.tables if not any(a.tag == q("tbl") for a in t.el.iterancestors())), key=lambda t: t.rows)
        need, slack = [], []
        for r in range(t.rows):
            text_h = 0
            for c in (c for c in t.cells if c.row == r and c.rowspan == 1):
                w = c.inner_width() / 5 or 2000
                h = 0
                for p in c.paragraphs():
                    s = p.find(".//w:r/w:rPr/w:sz", NS)
                    pt = int(s.get(q("val"))) / 2 if s is not None else 10
                    text = Cell.para_text(p)
                    width = sum(pt * 20 * (1 if ord(ch) > 0x2E80 else 0.6) for ch in text)
                    h += max(1, -(-int(width) // max(1, int(w) - 200))) * pt * 20 * line
                text_h = max(text_h, h + 60)
            need.append(max(t.row_height(r), text_h))
            slack.append(t.row_height(r) - text_h)
        over = int(sum(need) * safety) - room  # 글꼴이 바뀌면 글자 폭이 달라지므로 여유를 둔다
        if over <= 0:
            return
        r = max(range(t.rows), key=lambda i: slack[i])
        if slack[r] > 0:
            t.set_row_height(r, int(t.row_height(r) - min(over, slack[r])))

    def save(self, path):
        for name, root in self.sections.items():
            self.files[name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        names = sorted(self.files, key=lambda n: n != "[Content_Types].xml")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for name in names:
                z.writestr(name, self.files[name])
        return Path(path)


def dump(path):
    doc = Document(path)
    print(f"# {Path(path).name}")
    for t in doc.tables:
        print(f"  [표 {t.index}] {t.rows}행 x {t.cols}열, 칸 {len(t.cells)}개")
        for r in range(t.rows):
            cells = [c for c in t.row_cells(r) if c.row == r]
            if cells:
                print(f"    r{r}: " + " | ".join(
                    f"c{c.col}{'' if c.colspan == 1 else f'+{c.colspan}'}{'' if c.rowspan == 1 else f'v{c.rowspan}'}"
                    f"「{c.text[:18].replace(chr(10), '/')}」" for c in cells))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for p in sys.argv[1:]:
        dump(p)
