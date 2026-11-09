"""품의·결의 agent 로컬 웹 화면 서버 (표준 라이브러리만 사용).

    python tools/server.py          # http://127.0.0.1:8766 (config/rules.json ui.host/port)
    python tools/server.py --open   # 켜면서 브라우저도 연다

화면(ui/)이 부르는 API로 기존 도구를 그대로 쓴다:
  품의  견적 검사·질문 = harness.review/resolve, 업체 추천 = select_vendor.recommend/finalize,
        양식 채우기 = fill_form.fill, 미리보기 = hwpx_preview.
  결의  증빙 묶음 → Studio 결의 워크플로(run_api kind=gyeolui, 백그라운드) → gyeolui.review/answer
        → 결의 정보 → fill_form.fill(roles_name="form_roles_gyeolui").
  품의서를 다 쓰면 그 값(handoff)을 결의 쪽 비교 기준으로 넘기고, 품의서 PDF를 증빙 묶음 앞에 붙일 수 있다.
상태(답변·선정)는 서버 메모리에 둔다. 다시 켜면 처음부터(Studio 결과 JSON은 파일로 남아 다시 읽는다).
"""
import base64
import datetime as dt
import html
import json
import mimetypes
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import gyeolui
from common import ROOT, config, fields, html_to_pdf, rules
from fill_form import fill
from grouping import group as group_quotes
from harness import ACK, HOLD, coerce, normalize, recheck, resolve, source_text
from hwpx_preview import preview as hwpx_preview


def preview(path, pdf=False):
    """양식 미리보기(HTML)와, pdf=True면 PDF. Word는 tools/docx_pdf.py(Word → LibreOffice → 미리보기 인쇄 순), HWPX는 미리보기 인쇄."""
    if Path(path).suffix.lower() != ".docx":
        return hwpx_preview(path, pdf)
    from docx_preview import preview as docx_html
    html_file = docx_html(path)
    if not pdf:
        return html_file
    from docx_pdf import to_pdf
    return to_pdf(path)[0]
from score import unwrap
from select_vendor import cross_checks, finalize, recommend

UI = ROOT / "ui"
CFG = rules()["ui"]
OUT = ROOT / CFG["output_dir"]
STATE = {"case": {}, "quotes": {}, "selection": None, "texts": {}, "g": {}, "handoff": None}


# ---------------------------------------------------------------- 견적

def add_quote(n, p, src_dir):
    src = next((s for s in (ROOT / src_dir).glob(f"{p.stem}.*") if s.suffix.lower() == ".pdf"), None)
    data = json.loads(p.read_text(encoding="utf-8"))
    qid = f"{n}:{p.stem}"
    STATE["quotes"][qid] = {
        "set": n, "name": p.stem, "file": str(p), "source": str(src) if src else None,
        "state": {"result": normalize(unwrap(data) or data), "questions": [], "resolution_log": [],
                  "acknowledged": [], "hold": False, "source": str(src) if src else None}}
    check(qid)
    return qid


def load_quotes():
    STATE["quotes"].clear()
    for n, (res_dir, src_dir, _label) in enumerate(CFG["quote_sets"]):
        for p in sorted((ROOT / res_dir).glob("*.json")):
            add_quote(n, p, src_dir)


def studio_missing(kind="pumui"):
    from run_api import settings
    return settings(kind)[1]


class NotQuoteError(ValueError):
    def __init__(self, message, category):
        super().__init__(message)
        self.category = category


PROGRESS = {}  # 올리는 중인 견적서: token → {"done": [...], "found": {...}, "finished": bool}


def split_pages(name, data_b64):
    """여러 쪽 PDF를 쪽마다 한 파일로 나눈다(한 파일에 업체 견적서 여러 장이 들어오는 경우). 한 쪽이면 그대로."""
    raw = base64.b64decode(data_b64)
    if not CFG.get("split_pages") or Path(name).suffix.lower() != ".pdf":
        return [{"name": name, "data": data_b64}]
    import io
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(io.BytesIO(raw))
    if len(reader.pages) < 2:
        return [{"name": name, "data": data_b64}]
    out = []
    for n, page in enumerate(reader.pages, 1):
        w = PdfWriter()
        w.add_page(page)
        buf = io.BytesIO()
        w.write(buf)
        out.append({"name": f"{Path(name).stem}_p{n}.pdf", "page": n, "data": base64.b64encode(buf.getvalue()).decode()})
    return out


def studio_quote(name, data_b64, token=None):
    """올린 견적서를 Studio 품의 Agent(API)로 읽어 견적 목록에 더한다."""
    from run_api import ROOT as _root, run_file
    if studio_missing():  # 키가 없으면 파일을 남기기 전에 멈춘다
        raise RuntimeError(config("ui")["quotes"]["studio_missing"].format(env=_root / ".env", keys=", ".join(studio_missing())))
    res_dir, src_dir, _ = CFG["quote_sets"][0]
    name = Path(name).name
    if Path(name).suffix.lower() not in CFG["quote_upload_ext"]:
        raise ValueError(config("ui")["quotes"]["upload_bad_ext"].format(ext=", ".join(CFG["quote_upload_ext"])))
    src = ROOT / src_dir / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(base64.b64decode(data_b64))
    from run_api import NotQuote, StudioError
    msg = config("ui")["quotes"]
    try:
        def progress(done, found, finished):
            if token:
                PROGRESS[token] = {"done": done, "found": found, "finished": finished}
        _, job_id = run_file(src, ROOT / res_dir, progress)
    except NotQuote as e:  # 견적서가 아니면 올린 파일을 남기지 않는다
        src.unlink(missing_ok=True)
        raise NotQuoteError(msg["not_quote"].format(category=e.category), e.category) from None
    except StudioError as e:  # 읽지 못했으면 올린 파일도 남기지 않고, 무엇을 확인할지 알려 준다
        src.unlink(missing_ok=True)
        print(f"Studio 오류: {e}", file=sys.stderr)
        raise RuntimeError(msg["studio_errors"][e.kind].format(code=e.code)) from None
    except Exception:
        src.unlink(missing_ok=True)
        raise
    res = ROOT / res_dir / f"{src.stem}.json"
    r = unwrap(json.loads(res.read_text(encoding="utf-8")))
    F = fields()
    if not (r.get(F["items"]) or r.get(F["total"])):  # 품목도 금액도 없으면 표지·안내문 같은 쪽이다
        res.unlink(missing_ok=True)
        src.unlink(missing_ok=True)
        raise NotQuoteError(msg["not_quote_empty"], msg["empty_category"])
    return add_quote(0, res, src_dir), job_id


def text_of(q):
    if q["source"] not in STATE["texts"]:
        STATE["texts"][q["source"]] = source_text(q["source"])
    return STATE["texts"][q["source"]]


def check(qid):
    q = STATE["quotes"][qid]
    recheck(q["state"], text_of(q), STATE["case"])


def groups():
    """지금 견적 목록을 같은 물품끼리 묶는다 → {id: {"group": 번호, "label": 이름}}."""
    if not STATE["quotes"]:
        return {}
    of, names = group_quotes({qid: q["state"]["result"] for qid, q in STATE["quotes"].items()})
    return {qid: {"group": n, "label": names[n]} for qid, n in of.items()}


def summary(qid):
    q = STATE["quotes"][qid]
    F, I, r = fields(), fields()["item"], q["state"]["result"]
    return {"id": qid, "set": q["set"], "name": q["name"], "has_source": bool(q["source"]),
            "vendor": r.get(F["vendor_name"]), "total": r.get(F["total"]), "supply": r.get(F["supply"]),
            "vat": r.get(F["vat"]), "quote_date": r.get(F["quote_date"]),
            "items": [{"category": it.get(I["category"]), "description": it.get(I["description"]),
                       "amount": it.get(I["amount"]), "basis": it.get(I["basis"])} for it in r.get(F["items"]) or []],
            "questions": q["state"]["questions"], "hold": q["state"]["hold"],
            "answered": q["state"]["resolution_log"], "model_flags": r.get("model_flags", []),
            "studio": r.get("studio")}


def answer(qid, key, value, who):
    st = STATE["quotes"][qid]["state"]
    qs = st["questions"]
    idx = next(i for i, x in enumerate(qs) if x["key"] == key)
    st["questions"] = [qs[idx]] + qs[:idx] + qs[idx + 1:]  # resolve()는 맨 앞 질문에 답한다
    resolve(st, coerce(qs[idx]["field"], value), who)
    check(qid)


def quote_for_select(qid):
    s = summary(qid)
    rep = STATE["quotes"][qid]["state"]["result"].get(fields()["vendor_representative"]) or ""
    return {"file": qid, "vendor": s["vendor"], "rep": rep, "total": s["total"] or 0, "quote_date": s["quote_date"] or "",
            "issues": [x["kind"] for x in s["questions"]], "issue_messages": [x["message"] for x in s["questions"]]}


# ---------------------------------------------------------------- 양식

def list_forms(kind="pumui"):
    """양식 목록. 결의는 forms/expense·업로드 양식과, 이름에 '결의'가 든 기관 양식만(rules.json ui.gyeolui_form_filter)."""
    out = []
    dirs = CFG["gyeolui_form_dirs"] if kind == "gyeolui" else CFG["form_dirs"]
    filters = CFG.get("gyeolui_form_filter", {}) if kind == "gyeolui" else {}
    for d in dirs:
        ext = CFG.get("gyeolui_form_ext" if kind == "gyeolui" else "form_ext", ".hwpx")
        for p in sorted((ROOT / d).glob(f"**/*{ext}")):
            if ".form." in p.name or (d in filters and not re.search(filters[d], p.stem)):
                continue
            out.append({"path": str(p.relative_to(ROOT)).replace("\\", "/"), "name": p.stem, "group": p.parent.name})
    return out


def out_url(path):
    return "/out/" + str(Path(path).resolve().relative_to(OUT.resolve())).replace("\\", "/")


def form_preview(rel):
    src = (ROOT / rel).resolve()
    dst = OUT / "forms" / (src.parent.name + "_" + src.name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        dst.write_bytes(src.read_bytes())
    return out_url(preview(dst))


def do_fill(form_rel, qid):
    q = STATE["quotes"][qid]
    st = q["state"]
    if st["questions"] or st["hold"]:
        return {"ok": False, "blocked": [x["message"] for x in st["questions"]] or [config("questions")["hold_flag"]]}
    doc, report = fill(ROOT / form_rel, st["result"], STATE["case"], STATE["selection"])
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = OUT / stamp
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{STATE['case'].get('case_id', 'case')}_{Path(form_rel).stem}"
    filled = doc.save(folder / f"{name}{Path(form_rel).suffix.lower()}")
    # PDF도 함께 저장한다(내려받기용, 결의 쪽 증빙 묶음 앞에 붙이는 용도)
    pdf = preview(filled, pdf=True)
    if not filled.with_suffix(".preview.html").exists():
        preview(filled)
    report["files"] = {"doc": out_url(filled), "pdf": out_url(pdf), "html": out_url(filled.with_suffix(".preview.html")),
                       "ext": filled.suffix}
    (folder / f"{name}.report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # 결의로 넘길 값: 품의 금액·업체·품목 + 이 품의서 PDF(증빙 묶음 앞에 붙일 수 있다)
    from fill_form import Roles, build_values  # 품의서에 실제로 들어간 제목(비었으면 자동 제목)
    title = (build_values(st["result"], STATE["case"], STATE["selection"], Roles()).get("title") or {}).get("value")
    STATE["handoff"] = gyeolui.handoff(st["result"], {**STATE["case"], "title": title or STATE["case"].get("title")},
                                       STATE["selection"], {"pdf": str(pdf), "doc": str(filled), "form": form_rel})
    (folder / f"{name}.handoff.json").write_text(json.dumps(STATE["handoff"], ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "report": report, "handoff": handoff_summary()}


def handoff_summary():
    h = STATE["handoff"]
    if not h:
        return None
    return {k: h.get(k) for k in ("case_id", "doc_no", "title", "vendor", "total", "draft_date", "at")} | \
        {"has_pdf": bool(h.get("files", {}).get("pdf")) and Path(h["files"]["pdf"]).exists()}


# ---------------------------------------------------------------- 결의

GCFG = CFG["gyeolui_sets"]


def g_text():
    return config("ui")["gyeolui"]


def g_entry(n, name, src_dir):
    gid = f"g{n}:{name}"
    src = next((s for s in (ROOT / src_dir).glob(f"{name}.*") if s.suffix.lower() in CFG["gyeolui_upload_ext"]), None)
    return gid, {"id": gid, "set": n, "name": name, "status": "running", "error": None, "job_id": None,
                 "started": time.time(), "finished": None, "source": str(src) if src else None,
                 "state": None, "link": None, "fill": None, "progress": {"done": [], "found": {}, "finished": False}}


def g_load(n, p, src_dir):
    """Studio 결의 결과 JSON 하나를 읽어 결의 건으로 둔다. 같은 이름의 .link.json이 있으면 품의 인계 값과 대조한다."""
    link_p = p.with_name(f"{p.stem}.link.json")
    link = json.loads(link_p.read_text(encoding="utf-8")) if link_p.exists() else None
    gid, e = g_entry(n, p.stem, src_dir)
    e.update(status="done", started=None, link=link,
             state=gyeolui.new_state(json.loads(p.read_text(encoding="utf-8")), link, e["source"]))
    STATE["g"][gid] = e
    return gid


def load_gyeolui():
    STATE["g"].clear()
    for n, (res_dir, src_dir, _label) in enumerate(GCFG):
        for p in sorted((ROOT / res_dir).glob("*.json")):
            if not p.name.endswith(".link.json"):
                g_load(n, p, src_dir)


def image_pdf(img, dst):
    """사진(영수증 촬영본 등)을 A4 PDF 한 쪽으로 만든다(Edge/Chrome 인쇄). 증빙 묶음에 붙이기 위함."""
    page = Path(tempfile.mkdtemp()) / "page.html"
    page.write_text(f'<!doctype html><meta charset="utf-8"><style>@page{{size:A4;margin:10mm}}body{{margin:0}}'
                    f'img{{max-width:100%;max-height:270mm;display:block;margin:auto}}</style>'
                    f'<img src="{html.escape(Path(img).resolve().as_uri())}">', encoding="utf-8")
    try:
        html_to_pdf(page, dst)
    except SystemExit:  # 브라우저가 없음
        raise ValueError(g_text()["need_pdf"]) from None
    finally:
        shutil.rmtree(page.parent, ignore_errors=True)
    return dst


def checked(bundle):
    """API 입력 제한(형식·500 MB·1,000쪽)에 걸리면 묶음을 지우고 화면에 이유를 보여 준다."""
    from run_api import check_input
    try:
        check_input(bundle)
    except ValueError:
        bundle.unlink(missing_ok=True)
        raise
    return bundle


def build_bundle(files, with_pumui, dst_dir):
    """올린 파일들(+ 작성한 품의서 PDF)을 증빙 묶음 파일 하나로 만든다. 결의 워크플로는 파일 하나를 받는다."""
    t = g_text()
    if not files:
        raise ValueError(t["no_files"])
    if len(files) > CFG["gyeolui_max_files"]:
        raise ValueError(t["too_many"].format(n=CFG["gyeolui_max_files"]))
    for f in files:
        if Path(f["name"]).suffix.lower() not in CFG["gyeolui_upload_ext"]:
            raise ValueError(t["bad_ext"].format(ext=", ".join(CFG["gyeolui_upload_ext"])))
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    first = re.sub(r"[^\w가-힣.-]+", "_", Path(files[0]["name"]).stem)[:40]
    name = f"{stamp}_{first}" + (f"_외{len(files) - 1}건" if len(files) > 1 else "")
    dst_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp())
    try:
        parts = []
        if with_pumui:
            h = STATE["handoff"] or {}
            pdf = (h.get("files") or {}).get("pdf")
            if not pdf or not Path(pdf).exists():
                raise ValueError(t["no_pumui_pdf"])
            parts.append(Path(pdf))
        for i, f in enumerate(files):
            p = work / f"{i:02d}{Path(f['name']).suffix.lower()}"
            p.write_bytes(base64.b64decode(f["data"]))
            parts.append(p)
        if len(parts) == 1:  # 파일 하나면 그대로 보낸다(사진도 Studio가 읽는다)
            dst = dst_dir / f"{name}{parts[0].suffix}"
            shutil.copyfile(parts[0], dst)
            return checked(dst)
        from pypdf import PdfWriter
        w = PdfWriter()
        for p in parts:
            w.append(str(p if p.suffix == ".pdf" else image_pdf(p, p.with_suffix(".pdf"))))
        dst = dst_dir / f"{name}.pdf"
        with open(dst, "wb") as fh:
            w.write(fh)
        return checked(dst)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def g_run(gid, bundle, res_dir, link):
    """Studio 결의 워크플로를 백그라운드로 돌린다. 화면은 /api/gyeolui/status로 진행을 본다."""
    from run_api import StudioError, run_file
    e, t = STATE["g"][gid], g_text()
    try:
        def progress(done, found, finished):  # Jobs API 조회마다 끝난 단계·읽어 낸 값을 남긴다(화면 진행 표시)
            e["progress"] = {"done": done, "found": found, "finished": finished}
        raw, job_id = run_file(bundle, ROOT / res_dir, progress, kind="gyeolui", on_job=lambda j: e.update(job_id=j))
        if link:
            (ROOT / res_dir / f"{bundle.stem}.link.json").write_text(json.dumps(link, ensure_ascii=False, indent=2), encoding="utf-8")
        e.update(status="done", job_id=job_id, finished=time.time(), state=gyeolui.new_state(raw, link, str(bundle)))
    except StudioError as err:
        print(f"Studio 오류: {err}", file=sys.stderr)
        e.update(status="error", finished=time.time(), error=config("ui")["quotes"]["studio_errors"][err.kind].format(code=err.code))
    except Exception as err:  # 한 건이 실패해도 서버는 계속
        traceback.print_exc()
        e.update(status="error", finished=time.time(), error=t["run_failed"].format(msg=err))


def g_upload(b):
    t = g_text()
    if studio_missing("gyeolui"):
        from run_api import ROOT as _root
        raise RuntimeError(config("ui")["quotes"]["studio_missing"].format(env=_root / ".env", keys=", ".join(studio_missing("gyeolui"))))
    res_dir, src_dir, _ = GCFG[0]
    with_pumui = bool(b.get("with_pumui"))
    bundle = build_bundle(b.get("files") or [], with_pumui, ROOT / src_dir)
    link = STATE["handoff"] if (b.get("link") or with_pumui) and STATE["handoff"] else None
    gid, e = g_entry(0, bundle.stem, src_dir)
    e.update(source=str(bundle), link=link, parts=[f["name"] for f in b["files"]] + ([t["pumui_part"]] if with_pumui else []))
    STATE["g"][gid] = e
    threading.Thread(target=g_run, args=(gid, bundle, res_dir, link), daemon=True).start()
    return gid


def g_summary(gid):
    e = STATE["g"][gid]
    out = {k: e.get(k) for k in ("id", "set", "name", "status", "error", "job_id", "parts", "progress")}
    out["has_source"] = bool(e["source"])
    out["source_kind"] = "pdf" if (e["source"] or "").lower().endswith(".pdf") else "img"
    out["elapsed"] = int((e["finished"] or time.time()) - e["started"]) if e["started"] else None
    out["linked"] = bool(e["link"])
    st = e["state"]
    if not st:
        return out
    r = st["result"]
    out.update({"amount": gyeolui.pay_amount(r), "payee": gyeolui.payee(r), "base": r["base"], "request": r["request"],
                "receipts": r["receipts"], "template": r["template"], "draft": r["draft"],
                "studio": {k: r["studio"].get(k) for k in ("categories", "reports", "decisions", "draft", "others", "job_id")},
                "questions": st["questions"], "hold": st["hold"], "answered": st["resolution_log"],
                "info": st["info"], "hints": st["hints"], "blocked": gyeolui.blocked(st), "fill": e["fill"]})
    return out


def g_fill(gid, form_rel):
    e = STATE["g"][gid]
    st = e["state"]
    if gyeolui.blocked(st):
        return {"ok": False, "blocked": gyeolui.blocked(st)}
    result, case = gyeolui.form_input(st)
    doc, report = fill(ROOT / form_rel, result, case, None, "form_roles_gyeolui")
    folder = OUT / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{e['name']}_{Path(form_rel).stem}"
    filled = doc.save(folder / f"{name}.hwpx")
    pdf = preview(filled, pdf=True)
    report["files"] = {"doc": out_url(filled), "pdf": out_url(pdf), "html": out_url(filled.with_suffix(".preview.html")),
                       "ext": filled.suffix}
    record = {"report": report, "state": st, "form_input": {"result": result, "case": case}}
    (folder / f"{name}.report.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    e["fill"] = report["files"]
    return {"ok": True, "report": report}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def file(self, base, rel):
        p = (base / unquote(rel)).resolve()
        if not str(p).startswith(str(base.resolve())) or not p.is_file():
            return self.send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if p.suffix == ".hwpx":
            ctype = "application/hwp+zip"
        self.send(200, p.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))

    def body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                return self.file(UI, "index.html")
            if u.path.startswith("/ui/"):
                return self.file(UI, u.path[4:])
            if u.path.startswith("/out/"):
                return self.file(OUT, u.path[5:])
            if u.path == "/healthz":  # Docker 상태 확인용
                return self.send(200, {"ok": True})
            if u.path == "/api/init":
                return self.send(200, {
                    "ui": config("ui"), "case": STATE["case"], "categories": config("categories")["categories"],
                    "funding_sources": rules()["form"]["funding_sources"],
                    "sets": [s[2] for s in CFG["quote_sets"]], "quotes": [summary(i) for i in STATE["quotes"]],
                    "forms": list_forms(), "selection": STATE["selection"], "special": {"keep": ACK, "hold": HOLD},
                    "studio_ready": not studio_missing(), "upload_workers": CFG.get("upload_workers", 1),
                    "groups": groups(),
                    "gyeolui": {"sets": [s[2] for s in GCFG], "items": [g_summary(i) for i in STATE["g"]],
                                "forms": list_forms("gyeolui"), "default_form": CFG["gyeolui_default_form"],
                                "ready": not studio_missing("gyeolui"), "handoff": handoff_summary(),
                                "accept": CFG["gyeolui_upload_ext"]}})
            if u.path == "/api/quotes/progress":
                return self.send(200, PROGRESS.get(parse_qs(u.query).get("token", [""])[0]) or {"done": [], "found": {}})
            if u.path == "/api/gyeolui/source":  # 증빙 묶음 미리보기(진행 화면 왼쪽)
                e = STATE["g"].get(parse_qs(u.query).get("id", [""])[0])
                if not e or not e["source"]:
                    return self.send(404, {"error": "not found"})
                src = Path(e["source"])
                return self.file(src.parent, src.name)
            if u.path == "/api/gyeolui/status":
                ids = parse_qs(u.query).get("id", [])
                return self.send(200, {"items": [g_summary(i) for i in ids if i in STATE["g"]]})
            if u.path == "/api/form_preview":
                return self.send(200, {"url": form_preview(parse_qs(u.query)["form"][0])})
            self.send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            self.send(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            b = self.body()
            if u.path == "/api/quotes/split":
                return self.send(200, {"pages": split_pages(b["name"], b["data"])})
            if u.path == "/api/quotes/upload":
                try:
                    qid, job_id = studio_quote(b["name"], b["data"], b.get("token"))
                except NotQuoteError as e:  # 견적서가 아닌 쪽(팩스 표지 등)은 실패가 아니라 건너뛴다
                    return self.send(200, {"skipped": str(e), "category": e.category})
                except (ValueError, RuntimeError) as e:
                    return self.send(400, {"error": str(e)})
                finally:
                    PROGRESS.pop(b.get("token"), None)
                return self.send(200, {"quote": summary(qid), "job_id": job_id, "groups": groups()})
            if u.path == "/api/answer":
                answer(b["id"], b["key"], b["value"], b.get("who") or STATE["case"].get("drafter", ""))
                return self.send(200, summary(b["id"]))
            if u.path == "/api/case":
                STATE["case"] = b["case"]
                for qid in STATE["quotes"]:
                    check(qid)
                return self.send(200, {"quotes": [summary(i) for i in STATE["quotes"]]})
            if u.path == "/api/compare":
                quotes = [quote_for_select(i) for i in b["ids"]]
                rec, auto, order, excluded = recommend(quotes)
                return self.send(200, {"quotes": quotes, "recommended": rec["file"] if rec else None, "auto_reason": auto,
                                       "excluded": [q["file"] for q in excluded], "warnings": cross_checks(quotes)})
            if u.path == "/api/select":
                quotes = [quote_for_select(i) for i in b["ids"]]
                rec, auto, _, _ = recommend(quotes)
                chosen = next(q for q in quotes if q["file"] == b["pick"])
                try:
                    STATE["selection"] = finalize(quotes, rec, auto, chosen, (b.get("reason") or "").strip(), True)
                except ValueError as e:
                    return self.send(400, {"error": str(e)})
                return self.send(200, STATE["selection"])
            if u.path == "/api/fill":
                return self.send(200, do_fill(b["form"], b["id"]))
            if u.path == "/api/gyeolui/upload":
                try:
                    gid = g_upload(b)
                except (ValueError, RuntimeError) as e:
                    return self.send(400, {"error": str(e)})
                return self.send(200, {"item": g_summary(gid)})
            if u.path == "/api/gyeolui/answer":
                st = STATE["g"][b["id"]]["state"]
                who = b.get("who") or st["info"].get("drafter") or STATE["case"].get("drafter", "")
                gyeolui.answer(st, b["key"], b["value"], who)
                return self.send(200, g_summary(b["id"]))
            if u.path == "/api/gyeolui/info":
                st = STATE["g"][b["id"]]["state"]
                st["info"] = {k: v for k, v in (b.get("info") or {}).items() if isinstance(v, str) and v.strip()}
                return self.send(200, g_summary(b["id"]))
            if u.path == "/api/gyeolui/fill":
                return self.send(200, g_fill(b["id"], b["form"]))
            if u.path == "/api/upload":
                name = Path(b["name"]).name
                if not name.lower().endswith(".hwpx"):
                    return self.send(400, {"error": config("ui")["upload"]["only_hwpx"]})
                dst = ROOT / CFG["upload_dir"] / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(base64.b64decode(b["data"]))
                return self.send(200, {"forms": list_forms(), "gyeolui_forms": list_forms("gyeolui"),
                                       "path": str(dst.relative_to(ROOT)).replace("\\", "/")})
            self.send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            self.send(500, {"error": str(e)})


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    STATE["case"] = json.loads((ROOT / CFG["default_case"]).read_text(encoding="utf-8"))
    load_quotes()
    load_gyeolui()
    # Docker에서는 APP_HOST=0.0.0.0으로 컨테이너 안 모든 인터페이스에 연다(호스트 공개 범위는 compose.yaml이 정한다)
    import os
    host, port = os.environ.get("APP_HOST") or CFG["host"], int(os.environ.get("APP_PORT") or CFG["port"])
    srv = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"품의·결의 agent 화면: {url}  (견적 {len(STATE['quotes'])}건, 결의 {len(STATE['g'])}건)  끄려면 Ctrl+C")
    if "--open" in sys.argv[1:]:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
