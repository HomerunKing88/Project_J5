"""필지 하나의 주변 사례와 같은 동 연도별 시세, PC 판 (J5-072). 폰 필지 카드의 주변 사례(J5-064·066·071, web/app/view.js 의
`nearbyComps`·`compsByYear`)와 같은 규칙으로 정본에서 계산한다. `db parcels-comps` 가 글·JSON 으로 낸다. 정본에 쓰지 않는다.

- 사례: 이 필지와 시군구(PNU 앞 5자리)·법정동 이름이 같은 거래(취소 확정 제외), 기간(최근 n년, 월만 아는 거래는 그달 1일) 안, 고른 종류.
  폰은 파생본의 핵심·비교 범위 거래만 보므로, 범위 밖 동의 필지는 PC 에만 사례가 있다.
- 단가: 대지·연면적 평당(지분 거래·면적 없음은 null). 공시지가 배율: 지번이 온전히 닿는 필지(같은 시군구·법정동·산·본번·부번)의 그 거래 연도 이하
  가장 늦은 기준연도 공시지가로 "대지 ㎡당 거래가 ÷ 공시지가". 배율 사례가 COMP_MIN_RATIOS 건 이상이고 이 필지 공시지가가 있을 때만 참고 추정.
- 연도별 시세: 기간이 시작하는 해의 1월부터 오늘의 해까지 달력 연도마다 거래 수·대지 평당 중간값·수집 개월. 받지 않은 해의 거래 수는 0 이 아니다 (미수집).
값은 실거래 신고 자료에서 계산한 단가·배율이며 감정평가·시세 판단이 아니다 (ADR-25). 번지대 거래를 어느 필지로도 확정하지 않는다.
"""

from __future__ import annotations

from j5.collect.rt import KINDS as RT_KINDS, kind_of_provider
from j5.db.parcel_timeline import fmt_krw
from j5.db.parcel_years import KIND_SHORT, PNU_RE, kinds_note
from j5.db.parcels import attribute_history
from j5.db.store import Db, DbError
from j5.db.transactions import coverage_by_year
from j5.db.txlinks import parse_jibun
from j5.db.validate import parse_date

PYEONG_M2 = 3.305785          # 폰 PYEONG_M2 와 같다
COMP_MIN_RATIOS = 3           # 참고 추정에 필요한 배율 사례 수 (폰 COMP_MIN_RATIOS)
TREND_MIN_CASES = COMP_MIN_RATIOS   # 연도별 변동에 필요한 그해 대지 평당 사례 수 (폰 TREND_MIN_CASES)
COMP_YEARS = (1, 3, 5, 0)     # 폰 DEAL_YEARS, 0 = 전체
KIND_CHOICES = ("all", *RT_KINDS)


def median(values: list[float]) -> float | None:
    v = sorted(values)
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2


def _deal_date(t) -> str:
    return t["deal_date"] or f"{t['deal_ymd'][:4]}-{t['deal_ymd'][4:6]}"


def _since(years: int, on_date: str) -> str | None:
    return f"{int(on_date[:4]) - years}{on_date[4:]}" if years else None


def _price_for_year(series: list[dict], year: int) -> dict | None:
    """기준연도가 year 이하인 공시지가 가운데 가장 늦은 것 {price, year}. 더 늦은 해의 값으로 거슬러 쓰지 않는다 (폰 priceForYear)."""
    best = None
    for pt in series:
        y = pt.get("base_year")
        if not isinstance(y, int) or y > year:
            continue
        if best is None or (y, pt.get("base_month") or 0) >= (best["base_year"], best.get("base_month") or 0):
            best = pt
    return {"price": best["price_krw_m2"], "year": best["base_year"]} if best else None


def _stats(values: list[float]) -> dict:
    return {"n": len(values), "median": median(values), "min": min(values) if values else None, "max": max(values) if values else None}


def parcel_comps(db: Db, pnu: str, *, years: int = 5, kind: str = "all", on_date: str | None = None, cache: dict | None = None) -> dict:
    """cache 를 주면 같은 동의 거래·필지 색인과 필지의 공시지가 추이를 여러 필지 사이에 다시 쓴다 (comps_table, J5-074). 한 번의 표 안에서만 쓴다."""
    if not PNU_RE.match(pnu or ""):
        raise DbError("bad_pnu", f"PNU 는 19자리 숫자: {pnu}")
    if years not in COMP_YEARS:
        raise DbError("bad_years", f"기간은 {', '.join(map(str, COMP_YEARS))} 가운데 하나 (0 = 전체): {years}")
    if kind not in KIND_CHOICES:
        raise DbError("bad_kind", f"종류는 {', '.join(KIND_CHOICES)} 가운데 하나: {kind}")
    on_date = on_date or db.now()[:10]
    if parse_date(on_date) is None:   # 달력에 없는 날(2026-02-31)도 거절한다 (리뷰 반영 PR #119)
        raise DbError("bad_date", f"기준일은 달력에 있는 YYYY-MM-DD: {on_date}")
    cache = {} if cache is None else cache
    series: dict[str, list] = cache.setdefault("series", {})
    h = attribute_history(db, pnu)   # 정본에 없으면 parcel_missing
    series.setdefault(pnu, h["price_series"])
    me = db.conn.execute("SELECT emd_code, emd_name FROM parcels WHERE pnu = ?", (pnu,)).fetchone()
    sgg, emd = me["emd_code"][:5], me["emd_name"]
    since = _since(years, on_date)
    from_year = int(since[:4]) if since else None
    dong = cache.setdefault("dong", {})
    if (sgg, emd, kind) not in dong:
        rows = []
        if db._has_table("transactions") and emd is not None:
            rows = [t for t in db.conn.execute("SELECT * FROM transactions WHERE emd_name = ? AND cancel_status <> 'cancelled'", (emd,))
                    if (t["lawd_cd"] is None or t["lawd_cd"] == sgg) and (kind == "all" or _kind(t) == kind)]
        # 지번이 온전한 거래가 닿는 필지 (같은 시군구·법정동·산·본번·부번). 공시지가 이력은 필지마다 한 번 읽는다
        index = {}
        for p in db.conn.execute("SELECT pnu, mountain, bon, bu FROM parcels WHERE emd_name = ? AND substr(emd_code, 1, 5) = ?", (emd, sgg)):
            index.setdefault((bool(p["mountain"]), p["bon"], p["bu"] or 0), p["pnu"])
        dong[(sgg, emd, kind)] = (rows, index)
    rows, parcels = dong[(sgg, emd, kind)]

    def comp(t) -> dict:
        j = parse_jibun(t["jibun_raw"])
        exact = parcels.get((j["mountain"], j["bon"], j["bu"])) if j and not j["masked"] else None
        land = unit(t, t["plottage_area_m2"])
        pr = None
        if exact is not None:
            if exact not in series:
                series[exact] = attribute_history(db, exact)["price_series"]
            pr = _price_for_year(series[exact], int(t["deal_ymd"][:4]))
        ok = land is not None and pr is not None and pr["price"] > 0
        return {"transaction_id": t["transaction_id"], "deal_date": _deal_date(t), "deal_kind": _kind(t), "jibun": t["jibun_raw"], "jibun_masked": bool(t["jibun_masked"]),
                "amount_krw": t["amount_krw"], "share_deal": bool(t["share_deal"]), "building_use": t["building_use_raw"],
                "land_unit_krw_py": land, "floor_unit_krw_py": unit(t, t["building_area_m2"]),
                "ratio": (t["amount_krw"] / t["plottage_area_m2"]) / pr["price"] if ok else None, "price_year": pr["year"] if ok else None, "mine": exact == pnu}

    def in_period(t, start: str | None) -> bool:
        return not start or f"{_deal_date(t)}-01"[:10] >= start

    order = lambda c: (c["deal_date"], c["transaction_id"])   # noqa: E731 (폰 byDealDesc 의 역순 기준)
    comps = sorted((comp(t) for t in rows if in_period(t, since)), key=order, reverse=True)
    lands = [c["land_unit_krw_py"] for c in comps if c["land_unit_krw_py"] is not None]
    floors = [c["floor_unit_krw_py"] for c in comps if c["floor_unit_krw_py"] is not None]
    ratios = [c["ratio"] for c in comps if c["ratio"] is not None]
    r = median(ratios)
    own = _price_for_year(h["price_series"], 9999)
    if len(ratios) < COMP_MIN_RATIOS:
        reference = {"missing": f"공시지가와 견줄 수 있는 사례가 {len(ratios)}건이라 참고값을 내지 않는다 ({COMP_MIN_RATIOS}건부터)"}
    elif own is None:
        reference = {"missing": "이 필지의 공시지가 자료가 없어 참고값을 내지 않는다"}
    else:
        reference = {"per_m2": own["price"] * r, "unit_krw_py": own["price"] * r * PYEONG_M2, "price_krw_m2": own["price"], "price_year": own["year"], "ratio": r, "n": len(ratios)}
    # 연도별 시세 (J5-071): 기간이 시작하는 해의 1월부터
    ycomps = [comp(t) for t in rows if in_period(t, f"{from_year}-01-01" if from_year else None)]
    by_year = _by_year(db, ycomps, sgg, kind, from_year, int(on_date[:4]))
    return {"pnu": pnu, "parcel": h["label"], "sgg_code": sgg, "emd_name": emd, "on_date": on_date, "years": years, "since": since, "kind": kind,
            "count": len(comps), "land": _stats(lands), "floor": _stats(floors), "ratio": {"n": len(ratios), "median": r}, "reference": reference,
            "comps": comps, "by_year": by_year}


def _kind(t) -> str:
    k = kind_of_provider(t["provider"])
    return k.name if k else "nrg"   # 폰 dealKindOf: 모르는 값은 상업·업무용


def unit(t, area) -> float | None:
    """평당 원 (지분 거래·면적 없음은 null, 폰 unitPrice)."""
    if t["amount_krw"] is None or t["share_deal"] or area is None or area <= 0:
        return None
    return t["amount_krw"] / (area / PYEONG_M2)


def _by_year(db: Db, comps: list[dict], sgg: str, kind: str, from_year: int | None, this_year: int) -> dict | None:
    """폰 compsByYear 와 같은 규칙. 거래가 없으면 None."""
    if not comps:
        return None
    ys = [int(c["deal_date"][:4]) for c in comps]
    y0 = min([from_year, *ys]) if from_year is not None else min(ys)
    y1 = max([*ys, this_year])
    cov_all = coverage_by_year(db, sgg)
    cov_kind = {k: coverage_by_year(db, sgg, k) for k in RT_KINDS}
    out = []
    for y in range(y0, y1 + 1):
        inyear = [c for c in comps if int(c["deal_date"][:4]) == y]
        lands = [c["land_unit_krw_py"] for c in inyear if c["land_unit_krw_py"] is not None]
        kind_months = {k: c[str(y)]["complete"] for k, c in cov_kind.items() if c.get(str(y), {}).get("complete")}
        complete = cov_all.get(str(y), {}).get("complete", 0)
        # 정본은 자기 수집 기록을 안다: 기록이 없는 해는 받지 않은 해다 (폰은 수집 현황이 없는 이전 파일에서만 모름).
        # 시도했으나 실패·부분만인 해는 incomplete 로 가른다 (parcels-years 의 collection_incomplete 와 같다, 리뷰 반영 PR #119)
        months = complete if kind == "all" else kind_months.get(kind, 0)
        months_any = (cov_all if kind == "all" else cov_kind[kind]).get(str(y), {}).get("any", 0)
        counted = "counted" if inyear or months > 0 else "incomplete" if months_any > 0 else "not_collected"
        out.append({"year": y, "count": len(inyear), "counted": counted, **_stats(lands), "months": months, "months_any": months_any,
                    "kinds": kinds_note(kind_months, complete) if kind == "all" else "", "partial": y == this_year})
    return {"years": out, "change": trend_change(out)}


def trend_change(years: list[dict]) -> dict:
    """대지 평당 사례가 TREND_MIN_CASES 건 이상인 가장 이른 해와 가장 늦은 해의 중간값 변동 {from, to, pct} 또는 {missing} (폰 compsByYear 와 같다).
    시작 해의 중간값이 0 이하이면(금액 0 신고) 변동률을 내지 않는다 (리뷰 반영 PR #119)."""
    q = [r for r in years if r["n"] >= TREND_MIN_CASES]
    if len(q) < 2:
        return {"missing": f"대지 평당 사례가 {TREND_MIN_CASES}건 이상인 해가 {len(q)}개라 시작·최근 변동을 적지 않는다 (2개 해부터)"}
    if not q[0]["median"] > 0:
        return {"missing": f"시작 해({q[0]['year']}년)의 대지 평당 중간값이 0 이하라 변동을 적지 않는다"}
    return {"from": q[0]["year"], "to": q[-1]["year"], "pct": (q[-1]["median"] - q[0]["median"]) / q[0]["median"] * 100}


def _py(v) -> str:
    return f"{fmt_krw(round(v))}/평" if v is not None else "모름"


def comps_text(r: dict) -> str:
    scope = f"최근 {r['years']}년 ({r['since']}부터)" if r["years"] else "전체 기간"
    lines = [f"{r['parcel']} (PNU {r['pnu']}) 주변 사례: {r['emd_name']}, {scope}" + ("" if r["kind"] == "all" else f", {KIND_SHORT[r['kind']]}") + f", 기준일 {r['on_date']}",
             f"  거래 {r['count']}건"]
    if r["count"]:
        for name, s in (("대지 평당", r["land"]), ("연면적 평당", r["floor"])):
            lines.append(f"  {name}: " + (f"중간 {_py(s['median'])} ({_py(s['min'])}~{_py(s['max'])}, {s['n']}건)" if s["n"] else "면적이 있는 거래 없음"))
        lines.append("  공시지가 대비: " + (f"중간 {r['ratio']['median']:.2f}배 ({r['ratio']['n']}건, 그 필지의 거래 연도 이하 가장 최근 공시지가 기준)" if r["ratio"]["n"] else "견줄 수 있는 사례 없음"))
        ref = r["reference"]
        lines.append("  " + (ref["missing"] if "missing" in ref else
                             f"참고 추정: 이 필지 공시지가 {int(ref['price_krw_m2']):,}원/㎡ ({ref['price_year']}년) × {ref['ratio']:.2f}배 ≈ {_py(ref['unit_krw_py'])} (사례 {ref['n']}건, 추정값이며 시세 판단이 아니다)"))
        lines.append("  사례 (최근 먼저):")
        for c in r["comps"]:
            where = (c["jibun"] or "지번 미공개") + (" 번지대" if c["jibun_masked"] else "") + (" (이 필지)" if c["mine"] else "")
            extra = ", 지분 거래" if c["share_deal"] else ""
            ratio = f", {c['price_year']}년 공시지가의 {c['ratio']:.2f}배" if c["ratio"] is not None else ""
            lines.append(f"    {c['deal_date']} {KIND_SHORT[c['deal_kind']]} {fmt_krw(c['amount_krw'])} · {where} · 대지 {_py(c['land_unit_krw_py'])}{extra}{ratio}")
    by = r["by_year"]
    if by:
        ch = by["change"]
        lines.append("  연도별 시세 (기간이 시작하는 해의 1월부터, 최근 먼저): " + (ch["missing"] if "missing" in ch else f"대지 평당 중간값 {ch['from']}→{ch['to']}년 {ch['pct']:+.1f}%"))
        for y in reversed(by["years"]):
            if y["counted"] != "counted":
                what = "거래 미수집" if y["counted"] == "not_collected" else f"거래 수집 실패·부분만 ({y['months_any']}개월 시도)"
                lines.append(f"    {y['year']}{' (오늘까지)' if y['partial'] else ''}: {what} (0 건이 아니다)")
                continue
            cnt = f"{y['count']}건"
            col = (f"수집 실패·부분만 ({y['months_any']}개월 시도)" if y["months_any"] else "미수집") if y["months"] == 0 else f"수집 {y['months']}/12개월" + (f", {y['kinds']}" if y["kinds"] else "")
            lines.append(f"    {y['year']}{' (오늘까지)' if y['partial'] else ''}: 거래 {cnt} · 대지 평당 " + (f"중간 {_py(y['median'])} ({y['n']}건)" if y["n"] else "사례 없음") + f" · {col}")
    lines.append("실거래 신고 자료에서 계산한 단가와 배율이며 감정평가가 아니다. 지분 거래는 단가에서 뺀다. 번지대 거래는 어느 필지로도 확정하지 않는다.")
    return "\n".join(lines) + "\n"


# ---- 여러 필지의 주변 사례 요약표 (J5-074): 관찰목록·찾은 필지 목록을 한 표로. 필지마다 parcel_comps 와 같은 값 ----

TABLE_FIELDS = ("pnu", "parcel", "sgg_code", "emd_name", "years", "since", "kind", "count",
                "land_n", "land_median_krw_py", "land_min_krw_py", "land_max_krw_py", "floor_n", "floor_median_krw_py",
                "ratio_n", "ratio_median", "reference_unit_krw_py", "reference_price_krw_m2", "reference_price_year", "reference_missing",
                "own_count", "own_last_date", "own_last_amount_krw", "own_last_land_unit_krw_py",
                "trend_from", "trend_to", "trend_pct", "trend_missing")


def comps_table(db: Db, pnus: list[str], *, years: int = 5, kind: str = "all", on_date: str | None = None) -> dict:
    """필지마다 한 줄 {rows, parcels, years, kind, on_date}. 값은 필지마다 parcel_comps 와 같다(같은 동의 거래·공시지가 추이는 한 번만 읽는다).
    own_* 는 이 필지에 지번이 온전히 닿는 기간 안 거래(취소 확정 제외)의 수와 가장 최근 거래. 순서는 입력 순서, 같은 PNU 는 한 번."""
    from j5.db.parcel_years import MAX_PARCELS
    pnus = list(dict.fromkeys(pnus))
    bad = [p for p in pnus if not PNU_RE.match(p)]
    if bad:
        raise DbError("bad_pnu", f"PNU 는 19자리 숫자: {', '.join(bad[:5])}")
    if not pnus:
        raise DbError("no_pnu", "대상 필지가 없다 (PNU, --pnu, --csv 또는 --watchlist)")
    if len(pnus) > MAX_PARCELS:
        raise DbError("too_many", f"한 번에 {MAX_PARCELS}필지까지 ({len(pnus)}필지). 대상을 좁히거나 나눠 만든다")
    missing = [p for p in pnus if db.conn.execute("SELECT 1 FROM parcels WHERE pnu = ?", (p,)).fetchone() is None]
    if missing:
        raise DbError("parcel_missing", f"정본에 없는 필지: {', '.join(missing[:5])}" + (f" 외 {len(missing) - 5}개" if len(missing) > 5 else ""))
    # 기준일은 표 전체에 한 번 정한다: 줄마다 오늘을 읽으면 실행 중 날짜가 바뀔 때 줄마다 기간이 달라진다 (리뷰 반영 PR #121)
    on = on_date or db.now()[:10]
    cache: dict = {}
    out = []
    for pnu in pnus:
        r = parcel_comps(db, pnu, years=years, kind=kind, on_date=on, cache=cache)
        ref, ch = r["reference"], (r["by_year"] or {}).get("change") or {"missing": "거래가 없다"}
        own = [c for c in r["comps"] if c["mine"]]
        last = own[0] if own else None   # 사례는 최근 먼저
        out.append({"pnu": pnu, "parcel": r["parcel"], "sgg_code": r["sgg_code"], "emd_name": r["emd_name"], "years": years, "since": r["since"], "kind": kind,
                    "count": r["count"], "land_n": r["land"]["n"], "land_median_krw_py": r["land"]["median"], "land_min_krw_py": r["land"]["min"], "land_max_krw_py": r["land"]["max"],
                    "floor_n": r["floor"]["n"], "floor_median_krw_py": r["floor"]["median"], "ratio_n": r["ratio"]["n"], "ratio_median": r["ratio"]["median"],
                    "reference_unit_krw_py": ref.get("unit_krw_py"), "reference_price_krw_m2": ref.get("price_krw_m2"), "reference_price_year": ref.get("price_year"),
                    "reference_missing": ref.get("missing"), "own_count": len(own), "own_last_date": last["deal_date"] if last else None,
                    "own_last_amount_krw": last["amount_krw"] if last else None, "own_last_land_unit_krw_py": last["land_unit_krw_py"] if last else None,
                    "trend_from": ch.get("from"), "trend_to": ch.get("to"), "trend_pct": ch.get("pct"), "trend_missing": ch.get("missing")})
    return {"parcels": len(pnus), "years": years, "kind": kind, "on_date": on, "rows": out}


def _num(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.6g}" if abs(v) < 1000 else str(round(v))
    return str(v)


def table_csv(t: dict) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(TABLE_FIELDS)
    for row in t["rows"]:
        w.writerow([_num(row[k]) for k in TABLE_FIELDS])
    return buf.getvalue()


def table_text(t: dict) -> str:
    scope = f"최근 {t['years']}년" if t["years"] else "전체 기간"
    lines = [f"필지 {t['parcels']}개 주변 사례 요약 ({scope}" + ("" if t["kind"] == "all" else f", {KIND_SHORT[t['kind']]}") + f", 기준일 {t['on_date']}). 필지마다 같은 동의 거래로 계산한다."]
    for r in t["rows"]:
        parts = [f"거래 {r['count']}건"]
        if r["count"]:
            parts.append(f"대지 평당 중간 {_py(r['land_median_krw_py'])} ({r['land_n']}건)" if r["land_n"] else "대지 평당 사례 없음")
            parts.append(f"배율 중간 {r['ratio_median']:.2f}배 ({r['ratio_n']}건)" if r["ratio_n"] else "배율 사례 없음")
            parts.append(f"참고 추정 {_py(r['reference_unit_krw_py'])} ({r['reference_price_year']}년 공시지가 기준)" if r["reference_unit_krw_py"] is not None else "참고 추정 없음")
            parts.append(f"변동 {r['trend_from']}→{r['trend_to']}년 {r['trend_pct']:+.1f}%" if r["trend_pct"] is not None else "변동 없음")
        if r["own_count"]:
            parts.append(f"이 필지 거래 {r['own_count']}건 (최근 {r['own_last_date']} {fmt_krw(r['own_last_amount_krw'])}, 대지 {_py(r['own_last_land_unit_krw_py'])})")
        lines.append(f"  {r['parcel']} (PNU {r['pnu']}): " + " · ".join(parts))
    lines.append("값이 없는 칸의 사유는 CSV·JSON 의 *_missing 열에 있다. 실거래 신고 자료에서 계산한 단가와 배율이며 감정평가가 아니다.")
    return "\n".join(lines) + "\n"
