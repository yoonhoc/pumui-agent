"""견적서 여러 장(한 세트)을 보고 품의 판정을 한 번 내린다. 정답지(answers_final/<입력파일명>.json)와 같은 키로 돌려준다.

판정(config/rules.json set_pipeline, 위에서부터 먼저 맞는 것 하나):
  작성 중지      견적서가 min_quotes건 미만
  담당자 확인 필요  합계를 읽지 못한 견적이 있음, 최저가 동률, 대표자 동일
  재견적 필요    최저가 견적서에 금액 계산 불일치(단가×수량, 품목 합계와 공급가액, 공급가액 10%와 부가세, 공급가액+부가세와 합계)
Studio 판정 워크플로(tools/make_set_workflow.py)의 '품의 판정' 노드와 같은 기준이다.
  작성 가능      위에 해당 없음 → 최저가 업체 선정
"""
from common import rules


def cfg():
    return rules()["set_pipeline"]


def won(n):
    return f"{n:,}"


def quote_flags(q):
    """견적서 한 장의 금액 계산을 원 단위로 다시 맞춘다."""
    m, flags = cfg()["messages"], []
    vat_rate = rules()["vat_rate"]
    for n, it in enumerate(q["items"], 1):
        qty, price, amt = it.get("quantity"), it.get("unit_price"), it.get("estimated_amount")
        if None not in (qty, price, amt) and qty * price != amt:
            flags.append(m["item_mismatch"].format(n=n, calc=won(qty * price), doc=won(amt)))
    s, v, t = q.get("supply_amount"), q.get("vat_amount"), q.get("total_amount")
    amounts = [it.get("estimated_amount") for it in q["items"]]
    if s is not None and amounts and None not in amounts and sum(amounts) != s:
        flags.append(m["supply_mismatch"].format(calc=won(sum(amounts)), doc=won(s)))
    if s is not None and v and round(s * vat_rate) != v:
        flags.append(m["vat_mismatch"].format(calc=won(round(s * vat_rate)), doc=won(v)))
    if None not in (s, v, t) and s + v != t:
        flags.append(m["total_mismatch"].format(calc=won(s + v), doc=won(t)))
    return flags


def decide(file_name, quotes, not_quote_pages=()):
    """quotes: [{page, vendor_name, vendor_representative, quote_date, items, supply_amount, vat_amount, total_amount, review_flags}]
    review_flags에는 Studio·사람이 남긴 확인 사항이 들어 있고, 여기서 계산 불일치를 더한다."""
    c, m, d = cfg(), cfg()["messages"], cfg()["decisions"]
    for q in quotes:
        q["review_flags"] = list(dict.fromkeys(list(q.get("review_flags") or []) + quote_flags(q)))
    known = [q for q in quotes if q.get("total_amount") is not None]
    unreadable = [q for q in quotes if q.get("total_amount") is None]
    low = min((q["total_amount"] for q in known), default=None)
    lows = [q for q in known if q["total_amount"] == low]
    set_flags = [m["unreadable_total"].format(vendor=q["vendor_name"]) for q in unreadable]
    if len(lows) > 1:
        set_flags.append(m["tie"].format(vendors=", ".join(q["vendor_name"] for q in lows), total=won(low)))
    reps = {}
    for q in quotes:
        if q.get("vendor_representative"):
            reps.setdefault(q["vendor_representative"], []).append(q["vendor_name"])
    set_flags += [m["same_rep"].format(vendors=", ".join(v), rep=r) for r, v in reps.items() if len(v) > 1]
    best = lows[0] if len(lows) == 1 and not unreadable else None
    codes = c["review_codes"]
    needs = [codes["unreadable"]] * bool(unreadable) + [codes["tie"]] * (len(lows) > 1) + [codes["same_rep"]] * any(len(v) > 1 for v in reps.values())
    blocking = [f for f in (best["review_flags"] if best else []) if any(w in f for w in c["blocking_flag_words"])]

    if len(quotes) < c["min_quotes"]:
        decision, reason = d["stop"], m["too_few"].format(n=len(quotes), min=c["min_quotes"])
    elif set_flags:
        decision, reason = d["check"], set_flags[0]
    elif blocking:
        decision, reason = d["requote"], m["requote"].format(vendor=best["vendor_name"], flag=blocking[0])
    else:
        second = sorted(q["total_amount"] for q in known)[1]
        decision = d["ok"]
        reason = m["ok"].format(n=len(quotes), vendor=best["vendor_name"], total=won(low), gap=won(second - low))
    out = {
        "file": file_name,
        "decision": decision,
        "selected_vendor": best["vendor_name"] if best else c["unknown_vendor"],
        "selected_total": best["total_amount"] if best else None,
        "quote_count": len(quotes),
        "needs_review": needs + [codes["calc"]] * bool(blocking),
        "reason": reason,
        "review_flags": set_flags + ([m["vendor_flag"].format(vendor=best["vendor_name"], flag=f) for f in best["review_flags"]] if best else []),
        "quotes": quotes,
    }
    if not_quote_pages:
        out["not_quote_pages"] = list(not_quote_pages)
    return out
