"""어떤 품의서 양식(HWPX·DOCX)이 와도 칸을 찾아 품의 agent 결과로 채운다.

    python tools/fill_form.py 양식.hwp <결과.json 또는 _resolved.json> <구매건.json> [--selection 선정.json] [--out out]

순서
  1) 한글로 양식을 HWPX로 바꾼다(tools/hwp_com.py).
  2) 파일 안에 여러 양식이 있으면 품의서 하나를 고른다(config/form_roles.json form_title).
  3) 품목 표를 찾는다: 머리글 줄의 칸 이름이 item_table.columns 동의어와 맞으면 품목 표.
     품목 수(+부가세 줄)만큼 줄을 채우고, 모자라면 줄을 늘리고, 남는 줄과 예시 글씨는 지운다.
  4) 나머지 칸에서 라벨(fields 동의어)을 찾아 오른쪽 값 칸이나 같은 칸의 '라벨 : 값' 자리에 쓴다.
  5) 값이 없는 라벨, 사전에 없는 라벨, 놓을 곳이 없는 값은 고치지 않고 보고서에 담아 담당자에게 묻는다.
  6) 한글로 HWP·PDF와 첫 쪽 그림을 만든다.

라벨·형식·자리표시 규칙은 전부 config/form_roles.json에 있다. 코드에는 칸을 찾고 쓰는 방법만 둔다.
HWPX는 tools/hwpx.py, Word(.docx)는 tools/docx_form.py가 같은 모양(Document·Table·Cell)으로 읽고 쓴다.
지출결의서는 같은 방법에 사전만 바꿔 쓴다(config/form_roles_gyeolui.json, fill(..., roles_name="form_roles_gyeolui")).
"""
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

from common import ROOT, config, fields, korean_amount, rules, to_int, won
import docx_form
import hwpx
from hwpx import q


def open_form(path):
    """양식 파일 확장자로 읽는 모듈을 고른다(.docx → docx_form, 그 밖 → hwpx)."""
    return (docx_form if Path(path).suffix.lower() == ".docx" else hwpx).Document(path)

KEY_STRIP = re.compile(r"[\s:：·.\-()\[\]※*〔〕「」]")


def key(s):
    return KEY_STRIP.sub("", s or "").lower()


class SafeDict(dict):
    """양식 문구 틀에 없는 이름이 있어도 멈추지 않고 빈칸으로 둔다."""

    def __missing__(self, k):
        return ""


class Roles:
    def __init__(self, name="form_roles"):
        self.name = name
        self.cfg = config(name)
        self.field_labels = sorted(((key(l), role) for role, f in self.cfg["fields"].items() for l in f["labels"]),
                                   key=lambda x: -len(x[0]))
        self.column_labels = {key(l): role for role, ls in self.cfg["item_table"]["columns"].items() for l in ls}
        self.total_row = {key(l) for l in self.cfg["item_table"]["total_row"]}
        self.stop_row = [key(l) for l in self.cfg["item_table"]["stop_row"]]
        self.placeholders = [re.compile(p) for p in self.cfg["placeholders"]["patterns"]]
        self.keep = [re.compile(p) for p in self.cfg["placeholders"].get("keep_if_matches", [])]

    def field_of(self, text):
        k = key(text)
        return next((role for lab, role in self.field_labels if k == lab), None)

    def field_prefix(self, text):
        """'라벨 : 값' 또는 '라벨:' 꼴의 줄이면 (role, 라벨이 끝나는 위치). 안내문 줄(skip_line_prefixes)은 제외."""
        if any(text.strip().startswith(p) for p in self.cfg["skip_line_prefixes"]):
            return None
        num = re.match(self.cfg["line_number_prefix"], text)
        skip = num.end() if num else 0
        k = key(text[skip:])
        for lab, role in self.field_labels:
            if k.startswith(lab):
                n, i = 0, skip
                while i < len(text) and n < len(lab):
                    if key(text[i]):
                        n += 1
                    i += 1
                return role, i
        return None

    def is_placeholder(self, text):
        return any(p.search(text.strip()) for p in self.placeholders)


# ---------------------------------------------------------------- 값 만들기

def fmt_date(s, cfg):
    m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", str(s or ""))
    return cfg["formats"]["date"].format(y=m[1], m=int(m[2]), d=int(m[3])) if m else (s or "")


def build_values(result, case, selection, roles):
    cfg, F, I = roles.cfg, fields(), fields()["item"]
    items = result.get(F["items"]) or []
    total = to_int(result.get(F["total"])) or 0
    src = {"result": {"vendor_name": result.get(F["vendor_name"]), "total": total},
           "case": case, "selection": selection or {}}
    first_item = item_name(items[0].get(I["description"]) if items else "", cfg["item_table"])
    more = cfg["templates"]["more"].format(n=len(items) - 1) if len(items) > 1 else ""
    form_ctx = SafeDict({**{k: v for k, v in case.items() if isinstance(v, (str, int))},
                         "project_name": case.get("project_name", ""), "first_item": title_item(first_item, cfg["templates"]),
                         "more": more, "quote_count": (selection or {}).get("quote_count", 1)})
    src["form"] = {k: v.format_map(form_ctx) for k, v in cfg["templates"].items() if not k.startswith("_") and k != "more"}
    out = {}
    for role, f in cfg["fields"].items():
        v = None
        for path in [f["source"]] + ([f["fallback"]] if f.get("fallback") else []):
            where, name = path.split(".", 1)
            v = src.get(where, {}).get(name)
            if v not in (None, ""):
                break
        if v in (None, ""):
            continue
        kind, raw = f.get("format"), v
        if kind == "date":
            v = fmt_date(v, cfg)
        elif kind == "amount_korean":
            v = cfg["formats"]["amount_korean"].format(korean=korean_amount(int(v)), won=won(int(v)))
        elif kind == "amount":
            v = cfg["formats"]["amount"].format(won=won(int(v)))
        out[role] = {"value": str(v), "format": kind, "raw": raw}
    return out


def item_name(text, it_cfg):
    """적요 칸에 쓸 품명: item_table.item_name.strip 정규식에 맞는 부분을 지운다."""
    name = str(text or "").strip()
    for pat in it_cfg.get("item_name", {}).get("strip", []):
        name = re.sub(pat, "", name).strip()
    return name or str(text or "").strip()


def build_rows(result, roles):
    F, I, it_cfg = fields(), fields()["item"], roles.cfg["item_table"]
    rows = []
    for n, it in enumerate(result.get(F["items"]) or [], 1):
        basis = str(it.get(I["basis"]) or "")
        unit = re.sub(r"^.*?\*\s*[\d,]+", "", basis)
        qty, price = to_int(it.get(I["quantity"])), to_int(it.get(I["unit_price"]))
        rows.append({"no": str(n), "category": it.get(I["category"]) or "", "description": item_name(it.get(I["description"]), it_cfg),
                     "spec": "", "unit": unit, "quantity": won(qty) if qty is not None else "",
                     "unit_price": won(price) if price is not None else "",
                     "amount": won(to_int(it.get(I["amount"])) or 0), "basis": basis, "note": it.get(I["note"]) or ""})
    vat = to_int(result.get(F["vat"])) or 0
    if it_cfg["vat_row"]["enabled"] and vat:
        rate = f"{rules()['vat_rate'] * 100:g}"
        rows.append({"no": "", "category": it_cfg["vat_row"]["category"],
                     "description": it_cfg["vat_row"]["description"].format(rate=rate),
                     "spec": "", "unit": "", "quantity": "", "unit_price": "", "amount": won(vat), "basis": "", "note": ""})
    return rows


# ---------------------------------------------------------------- 양식 고르기

def pick_tables(doc, roles):
    """한 파일에 여러 양식이 있으면 찾는 양식(품의서 또는 결의서)으로 보이는 첫 묶음의 표만 고른다.
    form_title.include_first가 true면 include를 먼저 본다(제목에 '품의'와 '결의'가 같이 있는 양식용)."""
    ft = roles.cfg["form_title"]
    inc = [re.compile(p) for p in ft["include"]]
    exc = [re.compile(p) for p in ft["exclude"]]
    blocks, current, seen_title = [], None, False
    for t in doc.tables:
        head = " ".join(c.text for c in t.cells if c.row == 0)[:ft.get("head_chars", 200)]
        kind = None
        if ft.get("include_first") and any(p.search(head) for p in inc):
            kind = "pumui"
        elif any(p.search(head) for p in exc):
            kind = "other"
        elif any(p.search(head) for p in inc):
            kind = "pumui"
        if kind:
            seen_title = True
            current = {"kind": kind, "tables": []}
            blocks.append(current)
        if current is None:
            current = {"kind": None, "tables": []}
            blocks.append(current)
        current["tables"].append(t)
    pumui = [b for b in blocks if b["kind"] == "pumui"]
    if pumui:
        return pumui[0]["tables"], len(pumui), [b["kind"] for b in blocks]
    if not seen_title:
        return doc.tables, 0, []
    return [], 0, [b["kind"] for b in blocks]


# ---------------------------------------------------------------- 품목 표

def find_item_table(table, roles):
    """품목 표면 {header_end, columns{role: cell}, data_rows, total_rows}를 돌려준다."""
    for r in range(table.rows):
        cols = {}
        for c in table.row_cells(r):
            role = roles.column_labels.get(key(c.text))
            if role and role not in cols:
                cols[role] = c
        if len(cols) >= 2 and ({"description", "amount"} & set(cols)):
            header_end = max(c.row + c.rowspan - 1 for c in cols.values())
            data, totals = [], []
            for rr in range(header_end + 1, table.rows):
                cells = [c for c in table.row_cells(rr) if c.row == rr]
                keys = [key(c.text) for c in table.row_cells(rr)]
                if any(k and any(k.startswith(s) for s in roles.stop_row) for k in keys[:1]) or \
                        (len(table.row_cells(rr)) == 1 and table.rows > 1):
                    break
                if any(k in roles.total_row for k in keys):
                    totals.append(rr)
                elif cells:
                    data.append(rr)
            return {"header_end": header_end, "columns": cols, "data_rows": data, "total_rows": totals}
    return None


def title_item(name, tpl):
    """제목에 넣을 첫 품목 이름: 괄호 앞까지, 그리고 규격 덩어리(i5-14500, 16GB, 2.4GHz, '/')가 나오기 전까지."""
    name = name.split("(")[0].strip()
    stop = re.compile(tpl.get("title_item_stop") or "$^")
    words = []
    for w in name.split():
        if words and stop.fullmatch(w):
            break
        words.append(w)
    return " ".join(words) or name


def text_width(text, height, ratio, widths):
    """글자 폭 어림(HWPUNIT). height는 1/100pt, 글자별 폭은 글자 높이에 대한 비율(form_roles item_table.fit.char_width)."""
    em = height / 100 * 100  # 1pt = 100 HWPUNIT
    return sum(em * widths.get(ch, widths["default"]) for ch in text) * ratio / 100


def shrink_to_fit(cell, doc, fit):
    """숫자가 칸 안쪽 폭보다 넓으면, 양식에 이미 있는 같은 글꼴·굵기의 더 작은 글자 모양으로 바꾼다."""
    text, ref = cell.text, cell.char_ref()
    styles = doc.char_styles() if hasattr(doc, "char_styles") else {}
    cur = styles.get(ref)
    room = cell.inner_width()
    if not text or not cur or not room or text_width(text, cur["height"], cur["ratio"], fit["char_width"]) <= room:
        return
    same = [(i, s) for i, s in styles.items() if s["font"] == cur["font"] and s["bold"] == cur["bold"]
            and fit["min_char_height"] <= s["height"] < cur["height"]]
    for i, s in sorted(same, key=lambda x: -x[1]["height"]):
        if text_width(text, s["height"], s["ratio"], fit["char_width"]) <= room:
            for run in cell.el.iter(q("run")):
                run.set("charPrIDRef", i)
            return


def fill_item_table(table, info, rows, values, roles, report, written):
    msg = roles.cfg["report"]
    need = len(rows) - len(info["data_rows"])
    if need > 0 and info["data_rows"]:
        for _ in range(need):
            table.insert_row_after(info["data_rows"][-1])
            info = find_item_table(table, roles)  # 줄 주소가 바뀌었으니 다시 읽는다
        report["notes"].append(msg["rows_added"].format(n=need))
    elif need > 0:
        report["unplaced"].append({"what": msg["item_name"], "why": msg["no_item_rows"]})
        return info
    cols, data = info["columns"], info["data_rows"]
    label_ref = next(iter(cols.values())).char_ref()
    # 양식은 고정: 열의 정렬은 양식의 첫 데이터 줄을 따르고, 채우는 모든 줄(부가세 줄 포함)에 같게 쓴다
    align = {}
    for role, head in cols.items():
        first = table.at(data[0], head.col) if data else None
        align[role] = first.para_ref() if first is not None and first.row == data[0] else None
    for i, rr in enumerate(data):
        row = rows[i] if i < len(rows) else None
        for role, head in cols.items():
            cell = table.at(rr, head.col)
            if cell is None or cell.row != rr:
                continue
            cell.set_text(row.get(role, "") if row else "", label_ref, align[role])
            written.add(row.get(role, "") if row else "")
    fit = roles.cfg["item_table"].get("fit") or {}
    # 양식의 큰 빈칸(자유 기재용) 한 줄을 품목마다 복사하면 줄이 지나치게 높아진다 → 머리줄 높이에 맞춘다
    head_h = table.row_height(info["header_end"])
    if head_h and fit.get("max_row_ratio"):
        for rr in data[:len(rows)]:
            if table.row_height(rr) > head_h * fit["max_row_ratio"]:
                table.set_row_height(rr, head_h)
    # 숫자 칸(수량·단가·금액)은 좌우 여백을 줄여 좁은 칸에서도 한 줄에 들어가게 한다
    if fit.get("number_margin") is not None:
        for role in fit.get("number_roles", []):
            if role in cols:
                for rr in data + info["total_rows"]:
                    cell = table.at(rr, cols[role].col)
                    if cell is not None and cell.row == rr:
                        cell.set_side_margin(fit["number_margin"])
    total = values.get(roles.cfg["item_table"].get("total_role", "total"))  # 합계 줄에 넣을 금액 역할
    for tr in info["total_rows"]:
        cell = table.at(tr, cols["amount"].col) if "amount" in cols else None
        if cell is not None and key(cell.text) not in roles.total_row:
            cell.set_text(won(int(total["raw"])) if total else "", label_ref)
            written.add(won(int(total["raw"])) if total else "")
    if fit.get("shrink_numbers"):  # 합계 줄까지 다 쓴 뒤에 맞춘다
        for role in fit.get("number_roles", []):
            if role in cols:
                for rr in data + info["total_rows"]:
                    cell = table.at(rr, cols[role].col)
                    if cell is not None and cell.row == rr:
                        shrink_to_fit(cell, table.doc, fit)
    return info


# ---------------------------------------------------------------- 라벨 칸

def mark_checkbox(text, chosen, cfg):
    """'□ 보조금 □ 자부담'에서 고른 항목만 ■로. 항목 이름은 rules.json form.funding_sources에서 읽는다.
    (양식에 기호가 특수 글꼴이라 글자로 안 읽히는 경우도 있어 기호 유무와 상관없이 다시 쓴다)"""
    on, off = cfg["formats"]["checkbox_on"], cfg["formats"]["checkbox_off"]
    for opt in rules()["form"]["funding_sources"]:
        text = re.sub(rf"[{on}{off}-]?\s*{re.escape(opt)}", f"{on if opt == chosen else off} {opt}", text, count=1)
    return text


def fill_fields(tables, skip_rows, values, roles, report, done=()):
    """done: 이미 다른 자리(본문 자리표시 등)에 쓴 역할."""
    cfg = roles.cfg
    msg = cfg["report"]
    used, seen_labels = set(done), []
    for t in tables:
        for c in t.cells:
            if any(c.row in rng for rng in skip_rows.get(t.index, [])):
                continue
            role = roles.field_of(c.text)
            if role:
                seen_labels.append((role, c.text))
                nb = t.at(c.row, c.col + c.colspan)
                v = values.get(role)
                if nb is None and v is not None and role not in used:  # 오른쪽 칸이 없는 라벨: 라벨 아래 줄에 쓴다
                    c.append_line(v["value"])
                    used.add(role)
                    continue
                if nb is None or roles.field_of(nb.text) or nb is c:
                    continue
                if v is None:
                    report["missing"].append({"role": role, "label": c.text})
                    continue
                existing = nb.text
                if v["format"] == "checkbox" and (cfg["formats"]["checkbox_off"] in existing or cfg["formats"]["checkbox_on"] in existing):
                    nb.set_text(mark_checkbox(existing, v["value"], cfg), c.char_ref())
                elif roles.is_placeholder(existing) or not existing:
                    nb.set_text(v["value"], c.char_ref())
                elif any(k.search(existing) for k in roles.keep):
                    nb.set_text(f"{v['value']}  {existing}", c.char_ref())
                else:
                    nb.set_text(v["value"], c.char_ref())
                used.add(role)
                continue
            guide = False  # 안내문 줄('*', '※'로 시작) 뒤로 같은 칸의 줄은 모두 안내문(들여쓴 '사업기간 : …' 같은 줄 포함)
            for p in c.paragraphs():
                line = c.para_text(p)
                guide = guide or any(line.strip().startswith(x) for x in cfg["skip_line_prefixes"])
                hit = None if guide else roles.field_prefix(line)
                if not hit:
                    continue
                role, end = hit
                seen_labels.append((role, line[:end]))
                v = values.get(role)
                if v is None:
                    report["missing"].append({"role": role, "label": line[:end].strip()})
                    continue
                rest = line[end:]
                if v["format"] == "checkbox":
                    new = line[:end] + mark_checkbox(rest, v["value"], cfg)
                elif roles.is_placeholder(re.sub(r"^[\s:：]*", "", rest)) or not key(rest):
                    new = line[:end].rstrip() + cfg["formats"]["inline_separator"] + v["value"]
                else:  # 자리표시가 아닌 글자(예시 문구 등)가 있었다: 바꾸되 보고서에 남긴다
                    new = line[:end].rstrip() + cfg["formats"]["inline_separator"] + v["value"]
                    report["notes"].append(msg["placeholder_replaced"].format(label=line[:end].strip(), text=rest.strip()[:20]))
                c.set_para_text(p, new)
                used.add(role)
    left = {role: v for role, v in values.items() if role not in used}
    placed = place_free_text(tables, skip_rows, left, roles)
    for role, v in left.items():
        if role in placed:
            report["notes"].append(msg["free_text_placed"].format(name=cfg["free_text"]["names"].get(role, role)))
        elif cfg["fields"].get(role, {}).get("optional"):  # 양식에 없으면 그만인 값(품의번호 등)
            continue
        else:
            report["unplaced"].append({"what": cfg["free_text"]["names"].get(role, role), "value": v["value"], "why": msg["no_slot"]})
    return seen_labels


def fill_body_slots(doc, values, roles, report):
    """표 밖 본문의 {{역할}} 자리표시(서명·날짜 줄)를 먼저 채운다. 채운 역할 목록을 돌려준다."""
    cfg = roles.cfg.get("inline_blanks") or {}
    if not (cfg.get("slot_pattern") and cfg.get("body")):
        return set()
    slot, done = re.compile(cfg["slot_pattern"]), set()
    Cell = doc.cell_type
    for p in doc.outside_paragraphs():
        line = Cell.para_text(p)
        hits = [m[1] for m in slot.finditer(line) if m[1] in values]
        if hits:
            Cell.set_para_text(p, slot.sub(lambda m: values[m[1]]["value"] if m[1] in values else m[0], line))
            done.update(hits)
            report.setdefault("_written", []).extend(values[h]["value"] for h in hits)
    return done


def fill_inline_blanks(tables, result, case, roles, report, doc=None, values=None):
    """문장 안 빈자리(빈 괄호, 빈 날짜 줄, {{역할}} 자리표시)를 채우고 예시 표시를 지운다(form_roles.json inline_blanks).
    body가 true면 표 밖 본문 문단(서명·날짜 줄)도 채운다."""
    cfg = roles.cfg.get("inline_blanks")
    if not cfg:
        return
    msg = roles.cfg["report"]
    items = result.get(fields()["items"]) or []
    first = title_item(item_name(items[0].get(fields()["item"]["description"]), roles.cfg["item_table"]), roles.cfg["templates"]) if items else ""
    more = roles.cfg["templates"]["more"].format(n=len(items) - 1) if len(items) > 1 else ""
    d = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", str(case.get(cfg.get("date_source", "draft_date")) or ""))
    ctx = {"item_summary": cfg["item_summary"].format(first_item=first, more=more) if first else "",
           "draft_dot": cfg["draft_dot"].format(y=d[1], m=int(d[2]), d=int(d[3])) if d else "",
           "project_name": case.get("project_name", "")}
    rules_ = [(re.compile(r["pattern"]), r["context"], r["value"]) for r in cfg["rules"]]
    slot = re.compile(cfg["slot_pattern"]) if cfg.get("slot_pattern") else None
    spots = [(c.para_text, c.set_para_text, p) for tbl in tables for c in tbl.cells for p in c.paragraphs()]
    if cfg.get("body") and doc is not None:
        spots += [(doc.cell_type.para_text, doc.cell_type.set_para_text, p) for p in doc.outside_paragraphs()]
    names = roles.cfg["free_text"]["names"]

    def fill_slot(m):  # 양식에 심어 둔 {{역할}} 자리표시: 값이 있으면 넣고, 없으면 비우고 보고서에 남긴다
        v = (values or {}).get(m[1])
        if v is None:
            report["missing"].append({"role": m[1], "label": names.get(m[1], m[1])})
            return ""
        report.setdefault("_written", []).append(v["value"])
        return v["value"]

    for get, put, p in spots:
        line = get(p)
        new = slot.sub(fill_slot, line) if slot else line
        for marker in cfg.get("strip_markers", []):
            if marker in new:
                new = new.replace(marker, "").strip()
                report["notes"].append(msg["marker_removed"].format(marker=marker))
        for pat, context, value in rules_:
            if not pat.search(new) or (context and not any(w in new for w in context)):
                continue
            if any(not ctx.get(k) for k in re.findall(r"\{(\w+)\}", value)):
                continue  # 채울 값이 없으면 빈자리 그대로
            new = pat.sub(value.format(**ctx), new, count=1)
            report.setdefault("_written", []).append(new.strip())
            report["notes"].append(msg["blank_filled"].format(value=new.strip()))
        if new != line:
            put(p, new)


def label_text(text):
    """칸 이름 표시용: 첫 줄만, '영 수'처럼 한 글자씩 띄운 이름은 붙이고 나머지는 공백 하나로."""
    line = (text or "").strip().split("\n")[0].strip()
    parts = line.split()
    return "".join(parts) if parts and all(len(x) == 1 for x in parts) else " ".join(parts)


def where_of(cell, line, cfg):
    """예시 값이 있는 칸의 이름: 같은 줄 '이름 :' → 왼쪽 칸 → 위쪽 머리글 → 본문."""
    hangul = re.compile(r"[가-힣]")
    m = re.match(r"\s*([^:：]{1,%d})[:：]" % cfg["label_max_chars"], line)
    if m and hangul.search(m.group(1)):
        return label_text(m.group(1))
    if cell is None:
        return cfg["body"]
    tbl = cell.table

    def ok(n):
        return n is not None and n is not cell and hangul.search(n.text) and len(key(n.text)) <= cfg["label_max_chars"]

    for col in range(cell.col - 1, -1, -1):
        n = tbl.at(cell.row, col)
        if n is not None and n is not cell and n.text.strip():
            if ok(n):
                return label_text(n.text)
            break
    for row in range(cell.row - 1, -1, -1):
        n = tbl.at(row, cell.col)
        if ok(n):
            return label_text(n.text)
    return cfg["body"]


def leftover_values(doc, written, roles, tables=None):
    """양식에 남은 예시 값(날짜, 금액 등): [{where, kind, values, action}] (form_roles.json leftover_check).
    우리가 쓴 값에 없는데 양식 글에 있는 것만. tables를 주면(한 파일에 양식이 여럿) 우리가 채운 표 안만 본다."""
    cfg = roles.cfg.get("leftover_check")
    if not cfg:
        return []
    kinds = [(k["name"], re.compile(k["pattern"]), k["action"]) for k in cfg["kinds"]]
    mine = " ".join(w for w in written if w)
    spots = [(c, p) for tbl in (tables if tables is not None else doc.tables) for c in tbl.cells for p in c.paragraphs()]
    if tables is None:  # 표 밖 본문
        spots += [(None, p) for p in doc.outside_paragraphs()]
    groups = {}
    for cell, p in spots:
        text = doc.cell_type.para_text(p).strip()
        for name, pat, action in kinds:
            for m in pat.finditer(text):
                v = m.group(0).strip()
                if v and v not in mine:
                    g = groups.setdefault((where_of(cell, text, cfg), name), {"action": action, "values": []})
                    if v not in g["values"]:
                        g["values"].append(v)
    return [{"where": w, "kind": k, "values": g["values"], "action": g["action"]} for (w, k), g in groups.items()]


def place_free_text(tables, skip_rows, left, roles):
    """전용 칸이 없는 값을 양식의 넓은 빈 칸에 '이름: 값' 줄로 적는다. 적은 역할 목록을 돌려준다."""
    ft = roles.cfg["free_text"]
    order = [r for r in ft["order"] if r in left]
    if not ft["enabled"] or not order:
        return []
    for t in tables:
        wide = [c for c in t.cells if not c.text and c.colspan >= t.cols * ft["min_colspan_ratio"]
                and not any(c.row in rng for rng in skip_rows.get(t.index, []))]
        if wide:
            cell = wide[0]
            lines = [ft["line"].format(name=ft["names"].get(r, r), value=left[r]["value"]) for r in order]
            cell.set_text(lines[0])
            for line in lines[1:]:
                cell.append_line(line)
            return order
    anchors, ends = [key(a) for a in ft["anchors"]], [key(e) for e in ft["end_markers"]]
    for t in tables:
        for cell in t.cells:
            paras = cell.paragraphs()
            texts = [key(cell.para_text(p)) for p in paras]
            hit = next((i for i, k in enumerate(texts) if any(a in k for a in anchors)), None)
            if hit is None:
                continue
            end = next((i for i, k in enumerate(texts) if k in ends), None)
            before = paras[end if end is not None else hit]
            for r in order:
                cell.insert_line_before(before, ft["line"].format(name=ft["names"].get(r, r), value=left[r]["value"]))
            return order
    return []


def unknown_labels(tables, skip_rows, roles):
    """사전에 없는 라벨처럼 보이는 칸(짧은 글자 + 오른쪽 칸이 비었거나 자리표시)."""
    out = []
    for t in tables:
        for c in t.cells:
            if any(c.row in rng for rng in skip_rows.get(t.index, [])):
                continue
            k = key(c.text)
            if not (2 <= len(k) <= 8) or not re.fullmatch(r"[가-힣]+", k) or roles.field_of(c.text):
                continue
            nb = t.at(c.row, c.col + c.colspan)
            if nb is not None and nb is not c and (not nb.text or roles.is_placeholder(nb.text)) and not roles.field_of(nb.text):
                out.append(c.text.strip())
    return out


# ---------------------------------------------------------------- 실행

def fill(form_hwpx, result, case, selection=None, roles_name="form_roles"):
    roles = Roles(roles_name)
    msg = roles.cfg["report"]
    doc = open_form(form_hwpx)
    report = {"form": str(form_hwpx), "missing": [], "unplaced": [], "unknown_labels": [], "notes": []}
    tables, n_pumui, kinds = pick_tables(doc, roles)
    if not tables:
        report["unplaced"].append({"what": msg["form_name"], "why": msg["no_form"].format(kinds=kinds)})
        return doc, report
    if n_pumui > 1 or (kinds and len(kinds) > 1):
        report["notes"].append(msg["first_of_many"].format(n=len(kinds)))
    values = build_values(result, case, selection, roles)
    rows = build_rows(result, roles)
    for col, role in roles.cfg["item_table"].get("row_fill", {}).items():  # 결의서 품목 표의 지급처·지급방법 열
        if not col.startswith("_") and role in values:
            for row in rows:
                if row["no"]:
                    row.setdefault(col, values[role]["value"])
    written = {v["value"] for v in values.values()}
    skip = {}
    item_done = False
    for t in tables:
        info = find_item_table(t, roles)
        if info and not item_done:
            info = fill_item_table(t, info, rows, values, roles, report, written) or info
            last = max(info["data_rows"] + info["total_rows"] + [info["header_end"]])
            first = min(c.row for c in info["columns"].values())
            skip.setdefault(t.index, []).append(range(first, last + 1))
            item_done = True
    if not item_done and roles.cfg["item_table"].get("optional"):  # 결의서는 지출내역 표가 없는 양식도 있다
        report["notes"].append(msg["no_item_table"])
    elif not item_done:
        report["unplaced"].append({"what": msg["item_name"], "why": msg["no_item_table"]})
    slotted = fill_body_slots(doc, values, roles, report)
    fill_fields(tables, skip, values, roles, report, slotted)
    fill_inline_blanks(tables, result, case, roles, report, doc, values)
    report["missing"] = list({m["role"]: m for m in report["missing"]}.values())  # 같은 칸이 표와 본문에 다 있으면 한 번만
    report["unknown_labels"] = unknown_labels(tables, skip, roles)
    multi = n_pumui > 1 or bool(kinds and len(kinds) > 1)
    report["leftover"] = leftover_values(doc, written | set(report.pop("_written", [])), roles, tables if multi else None)
    if hasattr(doc, "fit_page"):  # Word 양식: 품목 줄이 늘어도 한 쪽에 맞춘다(한글은 HWPX에서 알아서 맞춘다)
        doc.fit_page()
    return doc, report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("form")
    ap.add_argument("result")
    ap.add_argument("case")
    ap.add_argument("--selection")
    ap.add_argument("--out", default=str(ROOT / "out"))
    ap.add_argument("--use-hwp", action="store_true",
                    help="한글 자동화로 .hwp 양식 변환·HWP/PDF 출력 (한글 보안 확인창이 뜰 수 있음). 기본은 한글 없이 동작")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    from harness import normalize
    from score import unwrap
    data = json.loads(Path(a.result).read_text(encoding="utf-8"))
    result = data["result"] if "resolution_log" in data else normalize(unwrap(data) or data)
    case = json.loads(Path(a.case).read_text(encoding="utf-8"))
    selection = json.loads(Path(a.selection).read_text(encoding="utf-8")) if a.selection else None

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    form = Path(a.form)
    stem = f"{case.get('case_id', 'case')}_{form.stem}"
    if form.suffix.lower() != ".hwpx":
        if not a.use_hwp:
            sys.exit(f"{form.name}: HWPX 양식만 한글 없이 처리할 수 있어요. 한글에서 '다른 이름으로 저장 → HWPX'로 저장해 주세요"
                     " (또는 --use-hwp로 한글 자동화 사용).")
        from hwp_com import convert
        form = convert(form, out / f"{form.stem}.form.hwpx")
    doc, report = fill(form, result, case, selection)
    filled = doc.save(out / f"{stem}.hwpx")
    from hwpx_preview import preview
    outputs = {"hwpx": str(filled), "preview_pdf": str(preview(filled, pdf=True))}
    if a.use_hwp:
        from hwp_com import Hwp
        with Hwp() as h:
            outputs["hwp"] = str(h.convert(filled, out / f"{stem}.hwp"))
    report["outputs"] = outputs
    (out / f"{stem}.report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
