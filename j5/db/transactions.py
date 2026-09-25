"""정본 거래 원본·정규화·수집 기록 (J5-014B-1). 데이터 사전 §7.1·§7.2, ADR-05.

- `load_run`: `j5 collect rt-sample/rt-fetch` 가 남긴 실행 기록(run-<실행>.json)과 원본 XML 을 정본에 옮긴다.
  실행 기록 → collection_runs/collection_pages, 응답 항목 행 → transaction_observations(불변), 정규화 → transactions.
  원본 파일의 sha256 이 기록과 다르면 거절한다. 같은 실행은 두 번 반영하지 않는다(변화 없음). 한 실행은 한 트랜잭션이다.
- 거래의 정체성: (제공자, 시군구, 계약월, 식별 해시, 순번). 식별 해시는 제공자가 나중에 채우거나 바꾸는 필드(취소·거래 유형·중개사 소재지·매수/매도 구분)를
  뺀 필드로 만든다. 같은 응답 안에서 식별 필드가 완전히 같은 행은 순번(0, 1, …)으로 구분해 별개 거래로 둔다(합치지 않는다).
- 같은 달을 다시 받은 실행: 다시 보인 거래는 last_seen 과 가변 필드를 갱신하고, 완전한(complete/empty) 응답에서 사라진 거래는 missing_since_run_id 만 적는다.
  사라졌다는 이유로 취소를 확정하지 않는다. 취소는 제공자의 cdealType 만 근거로 삼는다. 더 오래된 실행을 나중에 반영하면 새 거래만 넣고 최신 상태는 건드리지 않는다.
- 결측은 null 과 사유(missing_reasons_json). 금액·면적을 0 으로 채우지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from j5.collect.rt import OK_CODES, PROVIDER, RAW_DIR, CollectError, list_run_ids, parse_response
from j5.db import schema as S
from j5.db.store import Db, DbError
from j5.db.validate import parse_date

# 거래를 식별하는 제공자 필드. 나머지(cdealType, cdealDay, dealingGbn, estateAgentSggNm, buyerGbn, slerGbn)는 가변 필드다.
IDENTITY_FIELDS = ("sggCd", "umdNm", "jibun", "buildingAr", "plottageAr", "buildYear", "dealYear", "dealMonth", "dealDay", "dealAmount",
                   "buildingType", "buildingUse", "landUse", "floor", "shareDealingType")
BUILDING_KIND_BY_RAW = {"일반": "general", "집합": "strata"}
CANCEL_MARKS = ("O", "o", "Y", "y")
REAL_ENDPOINT_HOSTS = ("apis.data.go.kr", "openapi.molit.go.kr")
MAX_RUN_JSON_BYTES = 20 * 1024 * 1024


@dataclass
class RunLoadResult:
    run_id: str = ""
    outcome: str = "unchanged"  # applied / unchanged
    months: int = 0
    pages: int = 0
    observations: int = 0
    transactions_new: int = 0
    transactions_seen: int = 0
    transactions_changed: int = 0
    transactions_missing: int = 0
    dataset_version: int = 0
    source_document_id: str | None = None
    message: str = ""

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "outcome": self.outcome, "months": self.months, "pages": self.pages, "observations": self.observations,
                "transactions_new": self.transactions_new, "transactions_seen": self.transactions_seen, "transactions_changed": self.transactions_changed,
                "transactions_missing": self.transactions_missing, "dataset_version": self.dataset_version, "source_document_id": self.source_document_id,
                "message": self.message}

    def to_text(self) -> str:
        head = f"거래 반영 [{self.run_id}]: {'반영됨' if self.outcome == 'applied' else '변화 없음'}"
        if self.outcome == "applied":
            head += (f" · 월 {self.months}, 페이지 {self.pages}, 원본 행 {self.observations} · 거래 신규 {self.transactions_new}, 다시 확인 {self.transactions_seen}"
                     f" (가변 필드 변경 {self.transactions_changed}), 응답에서 사라짐 {self.transactions_missing} · dataset_version {self.dataset_version}")
        return head + (f"\n{self.message}" if self.message else "") + "\n"


def _canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(obj) -> str:
    return hashlib.sha256(_canon(obj).encode("utf-8")).hexdigest()


def identity_hash(content: dict) -> str:
    return _hash({k: content.get(k, "") for k in IDENTITY_FIELDS})


# ---------------------------------------------------------------- 정규화

_INT_RE = re.compile(r"^-?\d+$")
_DEC_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _clean(v: str | None) -> str:
    return (v or "").replace(",", "").strip()


def normalize(content: dict) -> dict:
    """제공자 행(태그명→문자열)을 transactions 열로 옮긴다. 해석할 수 없으면 null 과 사유."""
    missing: dict[str, str] = {}

    def num(field: str, kind: str):
        raw = _clean(content.get(field))
        if raw == "":
            missing[field] = "not_provided"
            return None
        if kind == "int" and _INT_RE.match(raw):
            return int(raw)
        if kind == "dec" and _DEC_RE.match(raw):
            return float(raw)
        missing[field] = "unparsable"
        return None

    amount_man = num("dealAmount", "int")
    amount_krw = amount_man * 10000 if amount_man is not None and amount_man >= 0 else None
    if amount_man is not None and amount_man < 0:
        missing["dealAmount"] = "unparsable"
    y, m, d = _clean(content.get("dealYear")), _clean(content.get("dealMonth")), _clean(content.get("dealDay"))
    deal_date = None
    if y and m and d and all(_INT_RE.match(x) for x in (y, m, d)):
        cand = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        deal_date = cand if parse_date(cand) is not None else None
    if deal_date is None:
        missing["dealDate"] = "not_provided" if not (y and m and d) else "unparsable"
    building_area = num("buildingAr", "dec")
    plottage_area = num("plottageAr", "dec")
    build_year = num("buildYear", "int")
    if build_year is not None and not (1800 <= build_year <= 2100):
        missing["buildYear"] = "unparsable"
        build_year = None
    for k in ("buildingAr", "plottageAr"):
        v = {"buildingAr": building_area, "plottageAr": plottage_area}[k]
        if v is not None and v < 0:
            missing[k] = "unparsable"
    if building_area is not None and building_area < 0:
        building_area = None
    if plottage_area is not None and plottage_area < 0:
        plottage_area = None
    jibun = (content.get("jibun") or "").strip()
    masked = "*" in jibun
    prefix_m = re.match(r"^\d+", jibun)
    jibun_prefix = prefix_m.group(0) if prefix_m else None
    btype = (content.get("buildingType") or "").strip()
    kind = BUILDING_KIND_BY_RAW.get(btype, "unknown")
    share = (content.get("shareDealingType") or "").strip() == "지분"
    cancel_mark = (content.get("cdealType") or "").strip()
    cancelled = cancel_mark in CANCEL_MARKS
    cancel_date = None
    if cancelled:
        cd = (content.get("cdealDay") or "").strip()
        mm = re.match(r"^(\d{2})\.(\d{2})\.(\d{2})$", cd)
        if mm:
            cand = f"20{mm.group(1)}-{mm.group(2)}-{mm.group(3)}"
            cancel_date = cand if parse_date(cand) is not None else None
        if cancel_date is None:
            missing["cdealDay"] = "not_provided" if not cd else "unparsable"
    scope = "partial_share" if share else ("strata_unit" if kind == "strata" else "unclear")
    return {"deal_date": deal_date, "amount_krw": amount_krw, "building_area_m2": building_area, "plottage_area_m2": plottage_area, "build_year": build_year,
            "missing_reasons_json": _canon(missing), "emd_name": (content.get("umdNm") or "").strip() or None, "jibun_raw": jibun or None,
            "jibun_masked": int(masked), "jibun_prefix": jibun_prefix, "building_kind": kind, "building_type_raw": btype or None,
            "building_use_raw": (content.get("buildingUse") or "").strip() or None, "land_use_raw": (content.get("landUse") or "").strip() or None,
            "floor_raw": (content.get("floor") or "").strip() or None, "share_deal": int(share), "cancel_status": "cancelled" if cancelled else "none",
            "cancel_date": cancel_date, "dealing_gbn_raw": (content.get("dealingGbn") or "").strip() or None,
            "agent_sgg_raw": (content.get("estateAgentSggNm") or "").strip() or None, "buyer_kind_raw": (content.get("buyerGbn") or "").strip() or None,
            "seller_kind_raw": (content.get("slerGbn") or "").strip() or None, "scope": scope, "scope_basis": "auto_provider_fields"}


# 다시 본 거래에서 갱신하는 열 (가변 필드와 그 정규화 결과). 식별 필드에서 온 열은 바뀌지 않는다.
MUTABLE_COLUMNS = ("cancel_status", "cancel_date", "dealing_gbn_raw", "agent_sgg_raw", "buyer_kind_raw", "seller_kind_raw", "missing_reasons_json")


# ---------------------------------------------------------------- 실행 기록 읽기

def read_run_record(data_home: Path, lawd_cd: str, run_id: str) -> dict:
    p = Path(data_home) / RAW_DIR / lawd_cd / f"run-{run_id}.json"
    if not p.is_file():
        raise DbError("run_missing", f"실행 기록이 없다: {p}")
    if p.stat().st_size > MAX_RUN_JSON_BYTES:
        raise DbError("run_too_big", f"실행 기록이 {p.stat().st_size} 바이트로 상한을 넘는다")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise DbError("run_unreadable", f"실행 기록을 읽을 수 없다: {p}: {e}") from e
    errs = _run_record_errors(doc, lawd_cd, run_id)
    if errs:
        raise DbError("run_invalid", f"실행 기록 {p.name} 형식 오류: " + "; ".join(errs[:5]))
    doc["_run_path"] = p.relative_to(Path(data_home)).as_posix()
    return doc


def _run_record_errors(doc, lawd_cd: str, run_id: str) -> list[str]:
    errs: list[str] = []
    if not isinstance(doc, dict):
        return ["최상위가 객체가 아니다"]
    if doc.get("run_id") != run_id:
        errs.append(f"run_id 가 파일 이름과 다르다 ({doc.get('run_id')!r})")
    if doc.get("lawd_cd") != lawd_cd:
        errs.append(f"lawd_cd 가 {lawd_cd} 가 아니다 ({doc.get('lawd_cd')!r})")
    for k in ("provider", "endpoint", "started_at", "finished_at"):
        if not isinstance(doc.get(k), str) or not doc[k]:
            errs.append(f"{k} 필요")
    months = doc.get("months")
    if not isinstance(months, list) or not months:
        return errs + ["months 목록 필요"]
    seen = set()
    for m in months:
        if not isinstance(m, dict) or not isinstance(m.get("deal_ymd"), str) or not re.match(r"^\d{6}$", m["deal_ymd"]):
            errs.append("월 항목의 deal_ymd 형식")
            continue
        if m["deal_ymd"] in seen:
            errs.append(f"같은 달이 두 번 있다: {m['deal_ymd']}")
        seen.add(m["deal_ymd"])
        if m.get("outcome") not in S.COLLECTION_OUTCOMES:
            errs.append(f"{m['deal_ymd']}: outcome {m.get('outcome')!r}")
        if not isinstance(m.get("pages"), list):
            errs.append(f"{m['deal_ymd']}: pages 목록 필요")
            continue
        for p in m["pages"]:
            if not isinstance(p, dict) or not isinstance(p.get("page_no"), int) or p.get("outcome") not in S.PAGE_OUTCOMES:
                errs.append(f"{m['deal_ymd']}: 페이지 항목 형식")
                continue
            if p["outcome"] in ("ok", "empty") and (not isinstance(p.get("path"), str) or not isinstance(p.get("sha256"), str)):
                errs.append(f"{m['deal_ymd']} p{p['page_no']}: 원본 경로·sha256 필요")
            if isinstance(p.get("path"), str) and (p["path"].startswith("/") or ".." in p["path"]):
                errs.append(f"{m['deal_ymd']} p{p['page_no']}: 원본 경로가 실데이터 홈 밖을 가리킨다")
    return errs


def is_real_endpoint(endpoint: str) -> bool:
    from urllib.parse import urlsplit
    host = (urlsplit(endpoint).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in REAL_ENDPOINT_HOSTS)


# ---------------------------------------------------------------- 반영

def load_run(db: Db, data_home: Path, lawd_cd: str, run_id: str) -> RunLoadResult:
    data_home = Path(data_home)
    doc = read_run_record(data_home, lawd_cd, run_id)
    r = RunLoadResult(run_id=run_id)
    if is_real_endpoint(doc["endpoint"]) and db.data_mode != "private_real":
        raise DbError("data_mode_mismatch", f"실제 제공자({doc['endpoint']})의 응답은 data_mode {db.data_mode} 정본에 넣지 않는다")
    already = db.conn.execute("SELECT COUNT(*) FROM collection_runs WHERE run_id = ? AND lawd_cd = ?", (run_id, lawd_cd)).fetchone()[0]
    if already:
        r.dataset_version = int(db.meta("dataset_version") or 0)
        r.message = "같은 실행이 이미 정본에 있다 (다시 반영하지 않음)"
        return r
    # 1) 원본을 모두 읽고 검증한다 (정본을 바꾸기 전에 거절할 것을 먼저 가린다)
    pages_data: dict[tuple[str, int], list[dict]] = {}
    for m in doc["months"]:
        for p in m["pages"]:
            if not isinstance(p.get("path"), str) or not isinstance(p.get("sha256"), str):
                continue  # 원본이 저장되지 않은 페이지 (HTTP·네트워크 오류)
            path = data_home / p["path"]
            try:
                raw = path.read_bytes()
            except OSError as e:
                raise DbError("raw_missing", f"원본 파일을 읽을 수 없다: {p['path']}: {e}") from e
            digest = hashlib.sha256(raw).hexdigest()
            if digest != p["sha256"]:
                raise DbError("raw_hash_mismatch", f"원본 {p['path']} 의 sha256 이 실행 기록과 다르다. 파일이 바뀌었거나 기록이 잘못됐다. 반영하지 않는다")
            if p["outcome"] not in ("ok", "empty"):
                continue  # API 오류·이상 응답의 원본은 경로·해시만 기록하고 행을 읽지 않는다
            try:
                parsed = parse_response(raw)
            except CollectError as e:
                raise DbError("raw_unparsable", f"원본 {p['path']} 를 해석할 수 없다: {e.message}") from e
            if parsed["result_code"] not in OK_CODES:
                raise DbError("raw_not_ok", f"원본 {p['path']} 의 resultCode 가 정상이 아니다 ({parsed['result_code']})")
            if len(parsed["items"]) != int(p.get("item_count") or 0):
                raise DbError("raw_count_mismatch", f"원본 {p['path']} 의 항목 수 {len(parsed['items'])} 가 기록 {p.get('item_count')} 과 다르다")
            pages_data[(m["deal_ymd"], p["page_no"])] = parsed["items"]
    now = db.now()
    with db.transaction():
        doc_id = str(uuid.uuid4())
        db.add_source_document({"document_id": doc_id, "document_kind": "official_api", "title": f"{doc['provider']} 실행 {run_id} (시군구 {lawd_cd})",
                                "location": doc["_run_path"], "sha256": None, "source_published_at": None, "collected_at": doc["finished_at"],
                                "notes": f"endpoint {doc['endpoint']} · 월 {len(doc['months'])}개 · 인증키 출처 {doc.get('key_source', '')}"})
        r.source_document_id = doc_id
        changed = False
        for m in doc["months"]:
            deal_ymd = m["deal_ymd"]
            db.conn.execute(
                "INSERT INTO collection_runs (run_id, provider, endpoint, lawd_cd, deal_ymd, outcome, total_count, items, pages, message, started_at, finished_at,"
                " run_path, source_document_id, data_mode, loaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, doc["provider"], doc["endpoint"], lawd_cd, deal_ymd, m["outcome"], m.get("total_count"), int(m.get("items") or 0), len(m["pages"]),
                 m.get("message"), doc["started_at"], doc["finished_at"], doc["_run_path"], doc_id, db.data_mode, now))
            r.months += 1
            rows: list[tuple[int, int, dict]] = []  # (page_no, row_index, content)
            for p in sorted(m["pages"], key=lambda x: x["page_no"]):
                items = pages_data.get((deal_ymd, p["page_no"]), [])
                fields: dict[str, int] = {}
                for it in items:
                    for k, v in it.items():
                        fields[k] = fields.get(k, 0) + (1 if v != "" else 0)
                db.conn.execute(
                    "INSERT INTO collection_pages (run_id, lawd_cd, deal_ymd, page_no, outcome, http_status, result_code, total_count, item_count, raw_path, raw_sha256,"
                    " bytes, fetched_at, fields_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, lawd_cd, deal_ymd, p["page_no"], p["outcome"], p.get("http_status"), p.get("result_code"), p.get("total_count"), len(items),
                     p.get("path") if isinstance(p.get("path"), str) else None, p.get("sha256") if isinstance(p.get("path"), str) else None,
                     p.get("bytes"), p.get("fetched_at") or doc["finished_at"], _canon(fields)))
                r.pages += 1
                rows.extend((p["page_no"], i, it) for i, it in enumerate(items))
            # 이 실행이 그 달의 최신 실행인가 (문자열 비교: 실행 ID 는 UTC 시각으로 시작한다)
            latest_before = db.conn.execute("SELECT MAX(last_seen_run_id) FROM transactions WHERE provider = ? AND lawd_cd = ? AND deal_ymd = ?",
                                            (doc["provider"], lawd_cd, deal_ymd)).fetchone()[0]
            is_newest = latest_before is None or run_id > latest_before
            seen_keys: set[tuple[str, int]] = set()
            ordinals: dict[str, int] = {}
            for page_no, row_index, content in rows:
                ih = identity_hash(content)
                ordinal = ordinals.get(ih, 0)
                ordinals[ih] = ordinal + 1
                ch = _hash(content)
                obs_id = str(uuid.uuid4())
                fetched = next((p.get("fetched_at") for p in m["pages"] if p["page_no"] == page_no), None) or doc["finished_at"]
                db.conn.execute(
                    "INSERT INTO transaction_observations (observation_id, run_id, lawd_cd, deal_ymd, page_no, row_index, content_json, content_hash, identity_hash,"
                    " ordinal, fetched_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (obs_id, run_id, lawd_cd, deal_ymd, page_no, row_index, _canon(content), ch, ih, ordinal, fetched, now))
                r.observations += 1
                changed = True
                seen_keys.add((ih, ordinal))
                cur = db.conn.execute("SELECT transaction_id, content_hash, last_seen_run_id FROM transactions WHERE provider = ? AND lawd_cd = ? AND deal_ymd = ? AND identity_hash = ? AND ordinal = ?",
                                      (doc["provider"], lawd_cd, deal_ymd, ih, ordinal)).fetchone()
                norm = normalize(content)
                if cur is None:
                    tid = str(uuid.uuid4())
                    db.conn.execute(
                        "INSERT INTO transactions (transaction_id, provider, lawd_cd, deal_ymd, identity_hash, ordinal, deal_date, amount_krw, building_area_m2, plottage_area_m2,"
                        " build_year, missing_reasons_json, emd_name, jibun_raw, jibun_masked, jibun_prefix, building_kind, building_type_raw, building_use_raw, land_use_raw,"
                        " floor_raw, share_deal, cancel_status, cancel_date, dealing_gbn_raw, agent_sgg_raw, buyer_kind_raw, seller_kind_raw, scope, scope_basis, link_status,"
                        " first_seen_run_id, last_seen_run_id, missing_since_run_id, first_observation_id, latest_observation_id, content_hash, data_mode, recorded_at, updated_at)"
                        " VALUES (:transaction_id, :provider, :lawd_cd, :deal_ymd, :identity_hash, :ordinal, :deal_date, :amount_krw, :building_area_m2, :plottage_area_m2,"
                        " :build_year, :missing_reasons_json, :emd_name, :jibun_raw, :jibun_masked, :jibun_prefix, :building_kind, :building_type_raw, :building_use_raw, :land_use_raw,"
                        " :floor_raw, :share_deal, :cancel_status, :cancel_date, :dealing_gbn_raw, :agent_sgg_raw, :buyer_kind_raw, :seller_kind_raw, :scope, :scope_basis, 'unlinked',"
                        " :run_id, :run_id, NULL, :obs_id, :obs_id, :content_hash, :data_mode, :now, :now)",
                        {**norm, "transaction_id": tid, "provider": doc["provider"], "lawd_cd": lawd_cd, "deal_ymd": deal_ymd, "identity_hash": ih, "ordinal": ordinal,
                         "run_id": run_id, "obs_id": obs_id, "content_hash": ch, "data_mode": db.data_mode, "now": now})
                    r.transactions_new += 1
                elif is_newest:
                    r.transactions_seen += 1
                    if cur["content_hash"] != ch:
                        r.transactions_changed += 1
                    db.conn.execute(
                        "UPDATE transactions SET " + ", ".join(f"{c} = :{c}" for c in MUTABLE_COLUMNS)
                        + ", last_seen_run_id = :run_id, missing_since_run_id = NULL, latest_observation_id = :obs_id, content_hash = :content_hash, updated_at = :now"
                        " WHERE transaction_id = :tid",
                        {**{c: norm[c] for c in MUTABLE_COLUMNS}, "run_id": run_id, "obs_id": obs_id, "content_hash": ch, "now": now, "tid": cur["transaction_id"]})
                else:
                    # 더 오래된 실행: 최신 상태는 건드리지 않고 최초 확인 실행만 앞당긴다
                    r.transactions_seen += 1
                    db.conn.execute("UPDATE transactions SET first_seen_run_id = MIN(first_seen_run_id, :run_id), first_observation_id = CASE WHEN :run_id < first_seen_run_id THEN :obs_id ELSE first_observation_id END,"
                                    " updated_at = :now WHERE transaction_id = :tid", {"run_id": run_id, "obs_id": obs_id, "now": now, "tid": cur["transaction_id"]})
            # 완전한 응답(complete/empty)에서 안 보인 기존 거래: 사라짐으로만 표시한다 (취소 확정 아님)
            if is_newest and m["outcome"] in ("complete", "empty"):
                existing = db.conn.execute("SELECT transaction_id, identity_hash, ordinal FROM transactions WHERE provider = ? AND lawd_cd = ? AND deal_ymd = ? AND missing_since_run_id IS NULL",
                                           (doc["provider"], lawd_cd, deal_ymd)).fetchall()
                for e in existing:
                    if (e["identity_hash"], e["ordinal"]) not in seen_keys:
                        db.conn.execute("UPDATE transactions SET missing_since_run_id = ?, updated_at = ? WHERE transaction_id = ?", (run_id, now, e["transaction_id"]))
                        r.transactions_missing += 1
                        changed = True
        r.outcome = "applied"
        r.dataset_version = db.bump_dataset_version()  # 수집 기록·출처 문서도 정본 데이터다: 반영했으면 버전을 올린다
        if not changed and not r.transactions_seen:
            r.message = "수집 기록만 반영됐다 (거래 행 없음)"
    return r


def unloaded_run_ids(db: Db, data_home: Path, lawd_cd: str) -> list[str]:
    """실데이터 홈에 있는 실행 기록 중 정본에 없는 것 (오래된 순)."""
    loaded = {r[0] for r in db.conn.execute("SELECT DISTINCT run_id FROM collection_runs WHERE lawd_cd = ?", (lawd_cd,))}
    return [rid for rid in list_run_ids(data_home, lawd_cd) if rid not in loaded]


# ---------------------------------------------------------------- 범위 현황

OUTCOME_RANK = {"complete": 3, "empty": 3, "partial": 2, "failed": 1}


def coverage(db: Db, lawd_cd: str, months: list[str]) -> dict:
    """월마다 가장 좋은 수집 결과·최신 실행·거래 수·취소·사라짐. 수집 완료(complete/empty)와 거래 연결은 별개로 센다."""
    out = []
    for ym in months:
        runs = db.conn.execute("SELECT run_id, outcome, total_count, items FROM collection_runs WHERE lawd_cd = ? AND deal_ymd = ? ORDER BY run_id", (lawd_cd, ym)).fetchall()
        best = max((r["outcome"] for r in runs), key=lambda o: OUTCOME_RANK[o], default=None)
        t = db.conn.execute(
            "SELECT COUNT(*) AS n, SUM(cancel_status = 'cancelled') AS cancelled, SUM(missing_since_run_id IS NOT NULL) AS missing,"
            " SUM(link_status = 'confirmed') AS linked, SUM(jibun_masked) AS masked FROM transactions WHERE lawd_cd = ? AND deal_ymd = ?", (lawd_cd, ym)).fetchone()
        out.append({"deal_ymd": ym, "collection": best or "none", "runs": len(runs), "latest_run_id": runs[-1]["run_id"] if runs else None,
                    "transactions": t["n"] or 0, "cancelled": t["cancelled"] or 0, "missing": t["missing"] or 0, "linked": t["linked"] or 0, "masked": t["masked"] or 0})
    complete = [m["deal_ymd"] for m in out if m["collection"] in ("complete", "empty")]
    return {"lawd_cd": lawd_cd, "months": out, "complete_months": len(complete), "gap_months": [m["deal_ymd"] for m in out if m["collection"] not in ("complete", "empty")],
            "transactions": sum(m["transactions"] for m in out), "linked": sum(m["linked"] for m in out)}


def coverage_text(c: dict) -> str:
    lines = [f"거래 수집 현황 시군구 {c['lawd_cd']}: 완전 수집 {c['complete_months']}/{len(c['months'])}개월 · 거래 {c['transactions']}건 · 물건 연결 확정 {c['linked']}건 (수집 완료와 연결 완료는 별개)"]
    for m in c["months"]:
        lines.append(f"  {m['deal_ymd'][:4]}-{m['deal_ymd'][4:]}: {m['collection']} · 실행 {m['runs']}회 · 거래 {m['transactions']} (취소 {m['cancelled']}, 사라짐 {m['missing']}, 지번 마스킹 {m['masked']}, 연결 {m['linked']})")
    if c["gap_months"]:
        lines.append("미완 월: " + ", ".join(f"{g[:4]}-{g[4:]}" for g in c["gap_months"]) + " (rt-fetch 로 다시 받는다)")
    return "\n".join(lines) + "\n"
