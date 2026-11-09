"""구매 건(케이스) 단위 품의 판정: 입력 PDF들 → 판정 JSON + Word 품의서.

    python tools/run_set.py 입력폴더/*.pdf [--fresh] [--no-form]

입력은 몇 개든, 파일 하나에 견적서가 몇 장이든 된다. 파일 이름은 보지 않고 내용을 읽어 구매 건을 나눈다.
1) 파일을 쪽마다 나눈다(set_pipeline.split_dir).
2) 쪽마다 Studio 품의 워크플로를 동시에 돌린다. 이미 돌린 쪽 결과(set_pipeline.result_dir)가 있으면 다시 쓴다(--fresh면 새로).
3) 견적서가 아닌 쪽(분류가 견적서가 아니거나 품목·금액이 없는 쪽: FAX 송부장 등)은 뺀다.
4) 읽어 낸 견적서를 같은 물품끼리 묶는다(tools/grouping.py). 한 묶음 = 구매 건 1건 = 케이스 1건.
5) 케이스마다 판정(tools/set_decision.py)을 내려 out/sets/case_<A..>.json에 쓰고,
   모두 모아 정답지 템플릿 모양(task, unit, output_keys, decision_values, cases)으로 out/sets/answer_pred.json을 쓴다.
6) 작성 가능이면 Word 품의서(set_pipeline.form)를 채워 out/sets/case_<A..>_품의서.docx와 .pdf로 쓴다.
7) 정답지(set_pipeline.answer_file)가 있으면 케이스마다 output_keys를 맞춰 보고 다른 곳을 출력한다.
"""
import argparse
import datetime as dt
import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import ROOT, fields, rules
from run_api import NotQuote, StudioError, run_file
from score import unwrap
from set_decision import cfg, decide


def split(pdf, dst):
    from pypdf import PdfReader, PdfWriter
    pages = []
    for n, page in enumerate(PdfReader(str(pdf)).pages, 1):
        out = dst / f"{pdf.stem}_p{n}.pdf"
        if not out.exists():
            w = PdfWriter()
            w.add_page(page)
            buf = io.BytesIO()
            w.write(buf)
            out.write_bytes(buf.getvalue())
        pages.append((pdf.name, n, out))
    return pages


def read_page(page, res_dir, fresh):
    name, n, path = page
    res = res_dir / f"{path.stem}.json"
    if fresh or not res.exists():
        try:
            run_file(path, res_dir)
        except NotQuote as e:
            return name, n, None, e.category
        except StudioError as e:  # 한 쪽이 실패해도 케이스는 계속 본다. 실패한 쪽은 not_quote_pages에 남는다
            print(f"  {name} p{n} Studio 오류({e.kind}): {e}", file=sys.stderr)
            return name, n, None, f"Studio 오류: {e.kind}"
    r = unwrap(json.loads(res.read_text(encoding="utf-8")))
    F = fields()
    if not (r.get(F["items"]) or r.get(F["total"])):
        return name, n, None, (r.get("studio") or {}).get("category") or "-"
    return name, n, r, None


def to_quote(name, n, r):
    F, keys = fields(), cfg()["item_keys"]
    return {"file": name, "page": n, "vendor_name": r.get(F["vendor_name"]),
            "vendor_representative": r.get(F["vendor_representative"]) or "", "quote_date": r.get("quote_date") or "",
            "items": [{k: it.get(k) for k in keys} for it in r.get(F["items"]) or []],
            "supply_amount": r.get("supply_amount"), "vat_amount": r.get("vat_amount"), "total_amount": r.get(F["total"]),
            "review_flags": list(r.get("review_flags") or [])}


def write_form(case, result, out, out_dir):
    from docx_pdf import to_pdf
    from fill_form import fill
    selection = {"quote_count": out["quote_count"], "vendor": out["selected_vendor"], "reason": out["reason"]}
    doc, report = fill(ROOT / cfg()["form"], result, {"case_id": case, "draft_date": dt.date.today().isoformat()}, selection)
    dst = doc.save(out_dir / f"{case}_품의서.docx")
    pdf, way = to_pdf(dst)
    return dst, pdf, way, report


def read_all(pdfs, fresh):
    """모든 입력 파일을 쪽마다 Studio로 읽는다: [(파일, 쪽, 결과 또는 None, 견적서가 아니면 분류)]."""
    c = cfg()
    split_dir, res_dir = ROOT / c["split_dir"], ROOT / c["result_dir"]
    for d in (split_dir, res_dir, ROOT / c["out_dir"]):
        d.mkdir(parents=True, exist_ok=True)
    pages = [p for pdf in pdfs for p in split(pdf, split_dir)]
    with ThreadPoolExecutor(max_workers=rules()["runtime"]["studio"].get("workers", 4)) as ex:
        return list(ex.map(lambda p: read_page(p, res_dir, fresh), pages))


def cases_of(reads):
    """파일 이름은 보지 않고 견적서 내용으로 구매 건을 나눈다(tools/grouping.py: Solar 품목 분류 + 품목 이름 유사도).
    견적서가 아닌 쪽(표지 등)은 같은 파일의 견적서가 든 구매 건에 붙이고, 견적서가 하나도 없는 파일은 따로 돌려준다."""
    from grouping import group
    quotes = {f"{name}#{n}": r for name, n, r, _ in reads if r is not None}
    of = group(quotes)[0] if quotes else {}
    groups, file_group, loose = {}, {}, []
    for name, n, r, cat in reads:
        if r is not None:
            groups.setdefault(of[f"{name}#{n}"], []).append((name, n, r, cat))
            file_group.setdefault(name, of[f"{name}#{n}"])
    for name, n, r, cat in reads:
        if r is None:
            (groups[file_group[name]] if name in file_group else loose).append((name, n, r, cat))
    ordered = sorted(groups.values(), key=lambda rs: min((x[0], x[1]) for x in rs))
    return [(f"case_{chr(ord('A') + i)}", rs) for i, rs in enumerate(ordered)], loose


def run_case(case, reads, form=True):
    """reads: 이 구매 건에 든 쪽들."""
    c = cfg()
    out_dir = ROOT / c["out_dir"]
    quotes = [to_quote(name, n, r) for name, n, r, _ in reads if r is not None]
    skipped = [{"file": name, "page": n, "category": cat} for name, n, r, cat in reads if r is None]
    files = sorted({name for name, *_ in reads})
    out = decide(files[0] if len(files) == 1 else files, quotes, skipped)
    out["documents"] = [("견적서" if r is not None else cat) for _, _, r, cat in sorted(reads, key=lambda x: (x[0], x[1]))]
    (out_dir / f"{case}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{case} (파일 {len(files)}개, 견적서 {len(quotes)}장): {out['decision']} | {out['selected_vendor'] or '-'} "
          f"{out['selected_total'] or ''} | {out['reason']}")
    print(f"  파일: {', '.join(files)}")
    if skipped:
        print(f"  견적서 아님: {skipped}")
    if form and out["decision"] == c["decisions"]["ok"]:  # 품의서는 작성 가능일 때만
        result = next(r for _, _, r, _ in reads if r is not None and r.get(fields()["vendor_name"]) == out["selected_vendor"])
        dst, pdf, way, report = write_form(case, result, out, out_dir)
        print(f"  품의서: {dst.relative_to(ROOT)}, {pdf.relative_to(ROOT)} (PDF: {way}, 빈칸 {len(report['missing'])}개)")
    return out


def as_answer(outs):
    """케이스 결과들을 정답지 템플릿 모양으로."""
    c = cfg()
    keys = c["output_keys"]
    return {"task": c["task"], "unit": c["unit"], "output_keys": keys, "decision_values": list(c["decisions"].values()),
            "cases": [{"case_id": chr(ord("A") + i), "file": o["file"], "documents": o["documents"],
                       "answer": {k: o.get(k) for k in keys}} for i, o in enumerate(outs)]}


def compare(pred, ans):
    by_file = {json.dumps(x["file"], ensure_ascii=False): x for x in ans["cases"]}
    for p in pred["cases"]:
        a = by_file.get(json.dumps(p["file"], ensure_ascii=False))
        if a is None:
            print(f"  정답지에 없음: {p['file']}")
            continue
        diffs = [f"{k}: 결과 {p['answer'].get(k)!r} / 정답 {a['answer'].get(k)!r}" for k in pred["output_keys"]
                 if p["answer"].get(k) != a["answer"].get(k)]
        print(f"  {p['file']}: {'정답과 일치' if not diffs else ' · '.join(diffs)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", nargs="+")
    ap.add_argument("--fresh", action="store_true", help="이미 돌린 쪽 결과가 있어도 Studio를 다시 돌린다")
    ap.add_argument("--no-form", action="store_true", help="Word 품의서를 만들지 않는다")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    reads = read_all(sorted(Path(x).resolve() for x in a.pdf), a.fresh)
    cases, loose = cases_of(reads)
    if loose:
        print(f"견적서가 없는 파일: {sorted({x[0] for x in loose})}")
    outs = [run_case(case, rs, not a.no_form) for case, rs in cases]
    pred = as_answer(outs)
    out_dir = ROOT / cfg()["out_dir"]
    (out_dir / "answer_pred.json").write_text(json.dumps(pred, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ans = ROOT / cfg()["answer_file"]
    if ans.exists():
        print("정답지 대조:")
        compare(pred, json.loads(ans.read_text(encoding="utf-8")))


if __name__ == "__main__":
    main()
