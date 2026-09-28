"""필지 하나의 연도별 이력, PC 판 (J5-043). `db parcels-history <PNU>` 가 속성 이력(J5-026·030)에 더해 이 필지에 닿는 실거래를 연도별로 보인다.
폰 필지 이력(web/app/view.js parcelHistory)과 같은 규칙이다.

- 거래는 정본 transactions 에서 시군구 코드(거래 lawd_cd = 필지 PNU 앞 5자리)와 법정동 이름이 같고 지번이 닿는 것: 같은 필지(exact) · 번지대(prefix, 지번 마스킹으로 필지 미확정).
  지번 대조는 연결 후보 CSV(`rt-candidates`, txlinks.jibun_matches)와 같다. `*` 처럼 앞자리가 없는 마스킹은 어느 필지에도 닿지 않는다.
- 이 필지에 지금 연결된 물건(asset_components)에 확정 연결(confirmed)된 거래는 지번·시군구가 닿지 않아도 넣는다(linked). 시군구가 다르면 그 줄에 적는다.
- 취소 확정 거래는 목록에 넣지 않고 수만 센다. 범위(core/comparison/outside/unclassified)와 무관하게 정본의 거래를 모두 본다(폰 파생본은 핵심·비교 범위만).
- 시군구가 다른 같은 이름 법정동의 거래는 넣지 않고 수만 센다(J5-044: 여러 지역을 한 정본에 넣으므로, 폰·후보 CSV 와 같은 규칙).
- 번지대 거래는 이 필지의 거래로 단정하지 않는다(AGENTS "마스킹된 거래를 추정으로 특정 PNU 에 확정하지 않는다"). 값은 제공자 표기 그대로이며 시세·가치 판단이 아니다.
정본에 쓰지 않는다.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal

from j5.db.parcels import active_links
from j5.db.store import Db
from j5.db.txlinks import jibun_matches, parse_jibun

MATCH_LABELS = {"exact": "같은 필지", "prefix": "번지대 (필지 미확정)", "linked": "연결 물건의 거래"}
MATCH_SHORT = {"exact": "같은 필지", "prefix": "번지대", "linked": "연결 물건"}
KIND_TEXT = {"general": "일반", "collective": "집합"}


def fmt_krw(v) -> str:
    """폰 fmtKrw(web/app/view.js) 와 같은 표기: 1억 이상은 억(100억 미만은 소수 한 자리), 1만 이상은 만, 그 밖은 원.
    억 자리 반올림은 JS toFixed 와 같게 double 의 정확한 값에서 반을 올린다 (Python 형식 지정자의 짝수 반올림을 쓰지 않는다, 리뷰 반영 PR #90)."""
    if v is None:
        return "금액 미확인"
    if v >= 1e8:
        x = v / 1e8
        if x == int(x):
            return f"{int(x)}억"
        digits = 0 if x >= 100 else 1
        q = Decimal(x).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
        return f"{q:f}".removesuffix(".0") + "억"
    if v >= 1e4:
        return f"{math.floor(v / 1e4 + 0.5):,}만"
    return f"{int(v):,}원"


def parcel_transactions(db: Db, pnu: str, *, on_date: str | None = None) -> dict:
    """PNU 하나에 닿는 거래 {transactions: [...], cancelled: n, other_sgg: n}. 거래 표가 없으면 빈 목록.
    other_sgg 는 법정동 이름·지번은 닿지만 시군구 코드가 달라 넣지 않은 거래 수(취소 확정 제외)다."""
    return parcels_transactions(db, [pnu], on_date=on_date)[pnu]


_TX_SELECT = ("SELECT t.*, l.asset_id AS linked_asset_id, l.status AS link_state FROM transactions t"
              " LEFT JOIN transaction_links l ON l.transaction_id = t.transaction_id AND l.status <> 'withdrawn'")


def _tx_item(t, match: str, linked_here: bool, sgg: str, has_zone: bool) -> dict:
    confirmed = t["link_state"] == "confirmed"
    return {"transaction_id": t["transaction_id"], "match": match, "linked_here": linked_here, "deal_ymd": t["deal_ymd"], "deal_date": t["deal_date"],
            "year": t["deal_ymd"][:4], "lawd_cd": t["lawd_cd"], "sgg_differs": t["lawd_cd"] != sgg,   # 시군구가 다른 것은 연결 물건의 거래(수동 확정)에서만 생긴다
            "emd_name": t["emd_name"], "jibun": t["jibun_raw"],
            "jibun_masked": bool(t["jibun_masked"]), "zone": t["zone"] if has_zone else None, "amount_krw": t["amount_krw"], "building_kind": t["building_kind"],
            "building_use": t["building_use_raw"], "land_use": t["land_use_raw"], "building_area_m2": t["building_area_m2"], "plottage_area_m2": t["plottage_area_m2"],
            "build_year": t["build_year"], "share_deal": bool(t["share_deal"]), "link_status": t["link_status"], "asset_id": t["linked_asset_id"] if confirmed else None,
            "missing_from_provider": t["missing_since_run_id"] is not None}


def parcels_transactions(db: Db, pnus: list[str], *, on_date: str | None = None) -> dict[str, dict]:
    """여러 필지에 닿는 거래를 한 번에 모은다 {pnu: {transactions, cancelled, other_sgg}} (J5-046). 규칙은 parcel_transactions 와 같다.
    법정동마다 거래를 한 번 읽고 지번을 한 번 해석해 (산, 본번, 부번)·(산, 본번 자릿수) 색인으로 필지마다 후보만 대조한다.
    정본에 없는 PNU 는 빈 결과다."""
    out = {p: {"transactions": [], "cancelled": 0, "other_sgg": 0} for p in pnus}
    if not pnus or not db._has_table("transactions"):
        return out
    parcels: dict[str, object] = {}
    uniq = list(dict.fromkeys(pnus))
    for i in range(0, len(uniq), 500):
        chunk = uniq[i:i + 500]
        for r in db.conn.execute(f"SELECT pnu, emd_code, emd_name, mountain, bon, bu FROM parcels WHERE pnu IN ({','.join('?' * len(chunk))})", chunk):
            parcels[r["pnu"]] = r
    on_date = on_date or db.now()[:10]
    here: dict[str, set] = {}
    for l in active_links(db, on_date):
        if l["pnu"] in parcels:
            here.setdefault(l["pnu"], set()).add(l["asset_id"])
    has_zone = "zone" in {r[1] for r in db.conn.execute("PRAGMA table_info(transactions)")}
    # 필지마다 거래 후보 {transaction_id: [행, 지번 대조 결과, 다른 시군구]}
    found: dict[str, dict] = {p: {} for p in parcels}
    by_name: dict[str, list] = {}
    for p in parcels.values():
        if p["emd_name"] is not None:
            by_name.setdefault(p["emd_name"], []).append(p)
    for name, group in by_name.items():
        exact_idx: dict[tuple, list] = {}
        masked_idx: dict[tuple, list] = {}
        for t in db.conn.execute(_TX_SELECT + " WHERE t.emd_name = ?", (name,)):
            j = parse_jibun(t["jibun_raw"])
            if j is None:
                continue
            if not j["masked"]:
                exact_idx.setdefault((j["mountain"], j["bon"], j["bu"]), []).append(t)
            elif j["bon_prefix"]:
                masked_idx.setdefault((j["mountain"], j["bon_digits"]), []).append((t, j))
        for p in group:
            sgg = p["emd_code"][:5]
            hits = [(t, "exact") for t in exact_idx.get((bool(p["mountain"]), p["bon"], p["bu"]), [])]
            hits += [(t, "prefix") for t, j in masked_idx.get((bool(p["mountain"]), len(str(p["bon"]))), []) if jibun_matches(j, p) == "jibun_prefix"]
            for t, m in hits:
                other = t["lawd_cd"] != sgg   # 이름이 같은 다른 시군구의 법정동 (J5-044)
                found[p["pnu"]][t["transaction_id"]] = [t, None if other else m, other]
    assets = sorted({a for s_ in here.values() for a in s_})
    for i in range(0, len(assets), 500):
        chunk = assets[i:i + 500]
        for t in db.conn.execute(_TX_SELECT + f" WHERE l.status = 'confirmed' AND l.asset_id IN ({','.join('?' * len(chunk))})", chunk):
            for pnu, s_ in here.items():
                if t["linked_asset_id"] in s_:
                    found[pnu].setdefault(t["transaction_id"], [t, None, False])
    for pnu, cands in found.items():
        sgg = parcels[pnu]["emd_code"][:5]
        o = out[pnu]
        items = []
        for t, match, other in cands.values():
            linked_here = t["link_state"] == "confirmed" and t["linked_asset_id"] in here.get(pnu, ())
            if match is None and linked_here:
                match = "linked"
            if match is None:
                o["other_sgg"] += int(other and t["cancel_status"] != "cancelled")
                continue
            if t["cancel_status"] == "cancelled":
                o["cancelled"] += 1
                continue
            items.append(((t["deal_ymd"], t["deal_date"] or "", t["jibun_raw"] or "", t["ordinal"]), _tx_item(t, match, linked_here, sgg, has_zone)))
        o["transactions"] = [it for _, it in sorted(items, key=lambda x: x[0])]
    return out


def by_year(changes: list[dict], transactions: list[dict]) -> list[dict]:
    """[{year, attr_changes, transactions, counts}] 최근 연도 먼저. 속성 변화는 기준일 연도, 거래는 계약연도. 각 연도 안은 날짜 오름차순."""
    years: dict[str, dict] = {}
    for c in changes:
        years.setdefault(c["as_of"][:4], {"attr_changes": [], "transactions": []})["attr_changes"].append(c)
    for t in transactions:
        years.setdefault(t["year"], {"attr_changes": [], "transactions": []})["transactions"].append(t)
    out = []
    for y in sorted(years, reverse=True):
        txs = years[y]["transactions"]
        out.append({"year": y, "attr_changes": years[y]["attr_changes"], "transactions": txs,
                    "counts": {m: sum(1 for t in txs if t["match"] == m) for m in MATCH_LABELS}})
    return out


def with_transactions(db: Db, h: dict, *, on_date: str | None = None) -> dict:
    """attribute_history 결과에 거래·연도별 묶음을 더한다."""
    tx = parcel_transactions(db, h["pnu"], on_date=on_date)
    h = dict(h)
    h["transactions"] = tx["transactions"]
    h["transactions_cancelled"] = tx["cancelled"]
    h["transactions_other_sgg"] = tx["other_sgg"]
    h["years"] = by_year(h["changes"], tx["transactions"])
    return h


def _tx_line(t: dict) -> str:
    date = t["deal_date"] or f"{t['deal_ymd'][:4]}-{t['deal_ymd'][4:]}"
    parts = [fmt_krw(t["amount_krw"])]
    if t["building_kind"] in KIND_TEXT:
        parts.append(KIND_TEXT[t["building_kind"]])
    if t["building_use"]:
        parts.append(t["building_use"])
    if t["building_area_m2"] is not None:
        parts.append(f"건물 {t['building_area_m2']}㎡")
    if t["plottage_area_m2"] is not None:
        parts.append(f"대지 {t['plottage_area_m2']}㎡")
    if t["share_deal"]:
        parts.append("지분 거래")
    tail = []
    if t["asset_id"]:
        tail.append("확정 연결")
    if t["missing_from_provider"]:
        tail.append("제공자 목록에서 사라짐")
    if t["sgg_differs"]:
        tail.append(f"시군구 코드 다름(거래 {t['lawd_cd']})")
    return (f"    {date} 실거래 {MATCH_LABELS[t['match']]}: " + " · ".join(parts) + f" ({t['emd_name'] or '동 미상'} {t['jibun'] or '지번 미공개'})"
            + (" · " + " · ".join(tail) if tail else ""))


def years_text(h: dict) -> str:
    """실거래를 계약연도별로 (속성 변화는 history_text 가 위에 보인다). 연도 줄에 그해의 속성 변화 수를 함께 적는다."""
    c = {m: sum(1 for t in h["transactions"] if t["match"] == m) for m in MATCH_LABELS}
    lines = [f"실거래 {len(h['transactions'])}건: 같은 필지 {c['exact']} · 번지대 {c['prefix']} · 연결 물건의 거래 {c['linked']}"
             + (f" · 취소 확정 {h['transactions_cancelled']}건 제외" if h["transactions_cancelled"] else "")
             + (f" · 다른 시군구의 같은 이름 법정동 {h['transactions_other_sgg']}건 제외" if h.get("transactions_other_sgg") else "")]
    if h["transactions"]:
        lines.append("연도별 (최근 먼저):")
        for y in h["years"]:
            if not y["transactions"]:
                continue
            head = [f"{MATCH_SHORT[m]} {k}" for m, k in y["counts"].items() if k]
            if y["attr_changes"]:
                head.append(f"속성 변화 {len(y['attr_changes'])}")
            lines.append(f"  {y['year']}: " + " · ".join(head))
            lines += [_tx_line(t) for t in y["transactions"]]
    if c["prefix"]:
        lines.append("번지대 거래는 지번이 마스킹되어 이 필지의 거래로 확정하지 않는다 (같은 번지대의 다른 필지일 수 있다).")
    return "\n".join(lines) + "\n"
