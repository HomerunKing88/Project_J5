"""조사 경로·점포·표본틀·세션·점포 관측과 공실 집계 (J5-013A). 데이터 사전 §5, ADR-04.

입력은 `schemas/survey_input.schema.json` 의 JSON 파일 네 종류(route_version / units / frame_version / session)다. 편집기는 두지 않는다.
- 경로 버전·표본틀 버전·세션은 불변이다. 같은 ID·같은 내용은 변화 없음, 같은 ID·다른 내용은 거절이며 수정은 새 버전(previous_version_id)·새 세션이다.
- 점포(survey_units)는 물리적 조사 단위다. 서술 필드는 갱신할 수 있고 업체명·업종은 세션의 관측값에만 둔다. 분할·통합은 링크로 남긴다.
- 세션 안 점포 관측은 (session_id, unit_id) 당 한 행이다. 한 점포가 여러 구간에 걸려도 두 번 세지 않는다.
- 집계: N = 표본틀 점포 수, K = occupied·vacant 로 확인한 수, V = vacant 수. 미방문·확인 불가·당일 휴무·임대 광고만은 확인이 아니다. 기록이 없는 점포(미조사)도 확인이 아니다.
- 시점 비교: 양쪽 표본틀에 있고 두 세션 모두 확인된 공통 점포로 따로 계산한다. 두 세션 사이에 분할·통합 링크가 걸린 점포는 비교 단절로 표시하고 공통 표본에서 뺀다.
정본 변경(신규·갱신)이 있으면 dataset_version 을 1 올린다. 변화 없음만이면 올리지 않는다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from j5.db import schema as S
from j5.db.store import Db, DbError
from j5.db.validate import ValidationError, parse_date, parse_datetime
from j5.schemas_loader import schema_errors

CONFIRMED_STATUSES = ("occupied", "vacant")


@dataclass
class ApplyResult:
    kind: str
    outcome: str = "unchanged"          # applied / unchanged
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    dataset_version: int = 0
    ids: list[str] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "outcome": self.outcome, "inserted": self.inserted, "updated": self.updated, "unchanged": self.unchanged,
                "dataset_version": self.dataset_version, "ids": self.ids, "message": self.message}

    def to_text(self) -> str:
        return (f"조사 입력 [{self.kind}]: {'반영됨' if self.outcome == 'applied' else '변화 없음'} · 신규 {self.inserted}, 갱신 {self.updated}, 변화 없음 {self.unchanged}"
                f" · dataset_version {self.dataset_version}\n{self.message}\n")


def _canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(obj) -> str:
    return hashlib.sha256(_canon(obj).encode("utf-8")).hexdigest()


def _require_dates(pairs: list[tuple[str, object]]) -> None:
    """스키마의 format=date 와 별개로 코드에서도 달력상 존재하는 날짜인지 확인한다 (2026-02-31 같은 값을 막는다)."""
    bad = [f"{name}: {value}" for name, value in pairs if value is not None and parse_date(value) is None]
    if bad:
        raise ValidationError(["달력상 존재하지 않는 날짜: " + "; ".join(bad)])


def load_input(path: Path) -> dict:
    path = Path(path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ValidationError([f"입력 파일을 읽을 수 없다: {e}"]) from e
    errs = schema_errors("survey_input.schema.json", doc)
    if errs:
        raise ValidationError(["입력이 survey_input 스키마에 맞지 않는다"] + errs[:10])
    return doc


def apply_input(db: Db, doc: dict) -> ApplyResult:
    errs = schema_errors("survey_input.schema.json", doc)
    if errs:
        raise ValidationError(errs[:10])
    kind = doc["kind"]
    fn = {"route_version": _apply_route_version, "units": _apply_units, "frame_version": _apply_frame_version, "session": _apply_session}[kind]
    r = ApplyResult(kind=kind)
    with db.transaction():
        fn(db, doc, r)
        if r.inserted or r.updated:
            r.outcome = "applied"
            r.dataset_version = db.bump_dataset_version()
        else:
            r.dataset_version = int(db.meta("dataset_version") or 0)
    return r


# ---- 경로 버전 ----

def _apply_route_version(db: Db, doc: dict, r: ApplyResult) -> None:
    now = db.now()
    route, rvid = doc["route"], doc["route_version_id"]
    seg_ids = [s["segment_id"] for s in doc["segments"]]
    if len(set(seg_ids)) != len(seg_ids):
        raise ValidationError(["segments 의 segment_id 가 중복된다"])
    _require_dates([("effective_from", doc["effective_from"])])
    content = {"route_id": route["route_id"], "effective_from": doc["effective_from"], "previous_version_id": doc["previous_version_id"],
               "change_reason": doc["change_reason"], "segments": doc["segments"]}
    h = _hash(content)
    cur = db.conn.execute("SELECT route_id, content_hash FROM survey_route_versions WHERE route_version_id = ?", (rvid,)).fetchone()
    existing_route = db.conn.execute("SELECT name, purpose FROM survey_routes WHERE route_id = ?", (route["route_id"],)).fetchone()
    if existing_route is None:
        db.conn.execute("INSERT INTO survey_routes (route_id, name, purpose, recorded_at) VALUES (?, ?, ?, ?)", (route["route_id"], route["name"], route["purpose"], now))
        r.inserted += 1
    elif dict(existing_route) != {"name": route["name"], "purpose": route["purpose"]}:
        db.conn.execute("UPDATE survey_routes SET name = ?, purpose = ? WHERE route_id = ?", (route["name"], route["purpose"], route["route_id"]))
        r.updated += 1
    if cur is not None:
        if cur["content_hash"] != h or cur["route_id"] != route["route_id"]:
            raise DbError("route_version_immutable", f"경로 버전 {rvid} 는 이미 다른 내용으로 있다. 수정은 새 route_version_id 와 previous_version_id 로 한다")
        r.unchanged += 1
        r.ids.append(rvid)
        r.message = "같은 경로 버전이 이미 있다"
        return
    prev = doc["previous_version_id"]
    if prev is None:
        version_no = 1
        if db.conn.execute("SELECT 1 FROM survey_route_versions WHERE route_id = ?", (route["route_id"],)).fetchone():
            raise ValidationError(["이 경로에 이미 버전이 있다. previous_version_id 로 이전 버전을 잇는다"])
    else:
        p = db.conn.execute("SELECT route_id, version_no FROM survey_route_versions WHERE route_version_id = ?", (prev,)).fetchone()
        if p is None or p["route_id"] != route["route_id"]:
            raise ValidationError([f"previous_version_id {prev} 가 이 경로의 버전이 아니다"])
        version_no = int(db.conn.execute("SELECT MAX(version_no) FROM survey_route_versions WHERE route_id = ?", (route["route_id"],)).fetchone()[0]) + 1
        if not doc["change_reason"]:
            raise ValidationError(["새 경로 버전에는 change_reason 이 필요하다"])
    db.conn.execute(
        "INSERT INTO survey_route_versions (route_version_id, route_id, version_no, segments_json, effective_from, previous_version_id, change_reason, content_hash, recorded_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rvid, route["route_id"], version_no, _canon(doc["segments"]), doc["effective_from"], prev, doc["change_reason"], h, now))
    r.inserted += 1
    r.ids.append(rvid)
    r.message = f"경로 버전 v{version_no} 등록 ({len(seg_ids)}구간)"


# ---- 점포 ----

def _apply_units(db: Db, doc: dict, r: ApplyResult) -> None:
    now = db.now()
    ids = [u["unit_id"] for u in doc["units"]]
    if len(set(ids)) != len(ids):
        raise ValidationError(["units 의 unit_id 가 중복된다"])
    for u in doc["units"]:
        uid = u["unit_id"]
        _require_dates([(f"{uid}.opened_on", u.get("opened_on")), (f"{uid}.closed_on", u.get("closed_on"))])
        if u.get("asset_id") and db.conn.execute("SELECT 1 FROM assets WHERE asset_id = ?", (u["asset_id"],)).fetchone() is None:
            raise ValidationError([f"{uid}: asset_id {u['asset_id']} 가 정본 물건에 없다"])
        lon, lat = (u["location_point"] if u.get("location_point") else (None, None))
        new = {"label": u["label"], "floor": u.get("floor"), "lon": lon, "lat": lat, "asset_id": u.get("asset_id"),
               "opened_on": u.get("opened_on"), "closed_on": u.get("closed_on"), "note": u.get("note")}
        subj = db.conn.execute("SELECT subject_type FROM subjects WHERE subject_id = ?", (uid,)).fetchone()
        if subj is None:
            db.conn.execute("INSERT INTO subjects (subject_id, subject_type, recorded_at) VALUES (?, 'survey_unit', ?)", (uid, now))
        elif subj[0] != "survey_unit":
            raise DbError("subject_type_conflict", f"{uid} 는 이미 {subj[0]} 로 등록되어 있다")
        cur = db.conn.execute("SELECT label, floor, lon, lat, asset_id, opened_on, closed_on, note FROM survey_units WHERE unit_id = ?", (uid,)).fetchone()
        if cur is None:
            db.conn.execute(
                "INSERT INTO survey_units (unit_id, label, floor, lon, lat, asset_id, opened_on, closed_on, note, recorded_at, updated_at)"
                " VALUES (:unit_id, :label, :floor, :lon, :lat, :asset_id, :opened_on, :closed_on, :note, :now, :now)", {**new, "unit_id": uid, "now": now})
            r.inserted += 1
        elif dict(cur) == new:
            r.unchanged += 1
        else:
            db.conn.execute(
                "UPDATE survey_units SET label = :label, floor = :floor, lon = :lon, lat = :lat, asset_id = :asset_id, opened_on = :opened_on,"
                " closed_on = :closed_on, note = :note, updated_at = :now WHERE unit_id = :unit_id", {**new, "unit_id": uid, "now": now})
            r.updated += 1
        r.ids.append(uid)
    for ln in doc.get("links", []):
        _require_dates([(f"link.{ln['from_unit_id']}.effective_from", ln["effective_from"])])
        for k in ("from_unit_id", "to_unit_id"):
            if db.conn.execute("SELECT 1 FROM survey_units WHERE unit_id = ?", (ln[k],)).fetchone() is None:
                raise ValidationError([f"링크의 {k} {ln[k]} 가 점포 목록에 없다"])
        cur = db.conn.execute("SELECT effective_from, note FROM survey_unit_links WHERE from_unit_id = ? AND to_unit_id = ? AND relation = ?",
                              (ln["from_unit_id"], ln["to_unit_id"], ln["relation"])).fetchone()
        if cur is None:
            db.conn.execute("INSERT INTO survey_unit_links (from_unit_id, to_unit_id, relation, effective_from, note, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (ln["from_unit_id"], ln["to_unit_id"], ln["relation"], ln["effective_from"], ln.get("note"), now))
            r.inserted += 1
        elif dict(cur) != {"effective_from": ln["effective_from"], "note": ln.get("note")}:
            raise DbError("link_immutable", f"링크 {ln['from_unit_id']}→{ln['to_unit_id']} ({ln['relation']}) 가 이미 다른 내용으로 있다")
        else:
            r.unchanged += 1
    r.message = f"점포 {len(ids)}개, 링크 {len(doc.get('links', []))}건 처리"


# ---- 표본틀 버전 ----

def _apply_frame_version(db: Db, doc: dict, r: ApplyResult) -> None:
    now = db.now()
    fvid = doc["frame_version_id"]
    _require_dates([("confirmed_on", doc["confirmed_on"])])
    members = sorted(doc["unit_ids"])
    content = {"name": doc["name"], "selection_rule": doc["selection_rule"], "unit_ids": members, "confirmed_on": doc["confirmed_on"],
               "previous_version_id": doc["previous_version_id"], "change_reason": doc["change_reason"]}
    h = _hash(content)
    cur = db.conn.execute("SELECT content_hash FROM survey_frame_versions WHERE frame_version_id = ?", (fvid,)).fetchone()
    if cur is not None:
        if cur["content_hash"] != h:
            raise DbError("frame_version_immutable", f"표본틀 버전 {fvid} 는 이미 다른 내용으로 있다. 수정은 새 frame_version_id 와 previous_version_id 로 한다")
        r.unchanged += 1
        r.ids.append(fvid)
        r.message = "같은 표본틀 버전이 이미 있다"
        return
    for uid in members:
        if db.conn.execute("SELECT 1 FROM survey_units WHERE unit_id = ?", (uid,)).fetchone() is None:
            raise ValidationError([f"unit_id {uid} 가 점포 목록에 없다. units 파일을 먼저 반영한다"])
    prev = doc["previous_version_id"]
    if prev is not None:
        if db.conn.execute("SELECT 1 FROM survey_frame_versions WHERE frame_version_id = ?", (prev,)).fetchone() is None:
            raise ValidationError([f"previous_version_id {prev} 가 없다"])
        if not doc["change_reason"]:
            raise ValidationError(["새 표본틀 버전에는 change_reason 이 필요하다"])
    db.conn.execute(
        "INSERT INTO survey_frame_versions (frame_version_id, name, selection_rule, confirmed_on, previous_version_id, change_reason, content_hash, recorded_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (fvid, doc["name"], doc["selection_rule"], doc["confirmed_on"], prev, doc["change_reason"], h, now))
    db.conn.executemany("INSERT INTO survey_frame_members (frame_version_id, unit_id) VALUES (?, ?)", [(fvid, u) for u in members])
    r.inserted += 1
    r.ids.append(fvid)
    diff = ""
    if prev is not None:
        before = {x[0] for x in db.conn.execute("SELECT unit_id FROM survey_frame_members WHERE frame_version_id = ?", (prev,))}
        diff = f" (이전 대비 추가 {len(set(members) - before)}, 탈락 {len(before - set(members))})"
    r.message = f"표본틀 버전 등록: 점포 {len(members)}개{diff}"


# ---- 세션·점포 관측 ----

def _apply_session(db: Db, doc: dict, r: ApplyResult) -> None:
    now = db.now()
    sid = doc["session_id"]
    if db.conn.execute("SELECT 1 FROM survey_route_versions WHERE route_version_id = ?", (doc["route_version_id"],)).fetchone() is None:
        raise ValidationError([f"route_version_id {doc['route_version_id']} 가 없다"])
    if db.conn.execute("SELECT 1 FROM survey_frame_versions WHERE frame_version_id = ?", (doc["frame_version_id"],)).fetchone() is None:
        raise ValidationError([f"frame_version_id {doc['frame_version_id']} 가 없다"])
    seg_ids = {s["segment_id"] for s in json.loads(db.conn.execute("SELECT segments_json FROM survey_route_versions WHERE route_version_id = ?", (doc["route_version_id"],)).fetchone()[0])}
    unknown = [s for s in doc["visited_segments"]] + [s["segment_id"] for s in doc["skipped_segments"]]
    unknown = [s for s in unknown if s not in seg_ids]
    if unknown:
        raise ValidationError([f"경로 버전에 없는 구간: {unknown}"])
    if set(doc["visited_segments"]) & {s["segment_id"] for s in doc["skipped_segments"]}:
        raise ValidationError(["같은 구간이 방문과 건너뜀에 동시에 있다"])
    started = parse_datetime(doc["started_at"])
    ended = parse_datetime(doc["ended_at"]) if doc["ended_at"] else None
    if started is None or (doc["ended_at"] and ended is None):
        raise ValidationError(["started_at·ended_at 은 시간대 오프셋이 있는 ISO 8601"])
    if ended is not None and ended < started:
        raise ValidationError(["ended_at 이 started_at 보다 이르다"])
    obs_ids = [o["unit_id"] for o in doc["observations"]]
    if len(set(obs_ids)) != len(obs_ids):
        raise ValidationError(["같은 unit_id 의 관측이 한 세션에 두 번 있다. 점포는 세션마다 한 번만 센다"])
    for o in doc["observations"]:
        if db.conn.execute("SELECT 1 FROM survey_units WHERE unit_id = ?", (o["unit_id"],)).fetchone() is None:
            raise ValidationError([f"관측의 unit_id {o['unit_id']} 가 점포 목록에 없다"])
        obs_at = parse_datetime(o["observed_at"])
        if obs_at is None:
            raise ValidationError([f"{o['unit_id']}: observed_at 은 시간대 오프셋이 있는 ISO 8601"])
        if obs_at < started or (ended is not None and obs_at > ended):
            raise ValidationError([f"{o['unit_id']}: observed_at {o['observed_at']} 이 세션 시간({doc['started_at']} ~ {doc['ended_at'] or '진행 중'}) 밖이다. 다른 시점의 관측은 그 시점의 세션에 넣는다"])
        if o.get("record_id") and db.conn.execute("SELECT 1 FROM records WHERE record_id = ?", (o["record_id"],)).fetchone() is None:
            raise ValidationError([f"{o['unit_id']}: record_id {o['record_id']} 가 정본 기록에 없다"])
    header = {"route_version_id": doc["route_version_id"], "frame_version_id": doc["frame_version_id"], "started_at": doc["started_at"], "ended_at": doc["ended_at"],
              "visited_segments": doc["visited_segments"], "skipped_segments": doc["skipped_segments"], "note": doc.get("note")}
    h = _hash(header)
    cur = db.conn.execute("SELECT content_hash FROM survey_sessions WHERE session_id = ?", (sid,)).fetchone()
    if cur is None:
        db.conn.execute(
            "INSERT INTO survey_sessions (session_id, route_version_id, frame_version_id, started_at, ended_at, visited_segments_json, skipped_segments_json, note, content_hash, recorded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, doc["route_version_id"], doc["frame_version_id"], doc["started_at"], doc["ended_at"], _canon(doc["visited_segments"]), _canon(doc["skipped_segments"]), doc.get("note"), h, now))
        r.inserted += 1
    elif cur["content_hash"] != h:
        raise DbError("session_immutable", f"세션 {sid} 의 머리 정보가 이미 다른 내용으로 있다. 정정은 새 session_id 로 한다")
    else:
        r.unchanged += 1
    for o in doc["observations"]:
        new = {"observed_at": o["observed_at"], "status": o["status"], "business_name": o.get("business_name"), "business_type": o.get("business_type"),
               "note": o.get("note"), "record_id": o.get("record_id")}
        ex = db.conn.execute("SELECT observed_at, status, business_name, business_type, note, record_id FROM unit_observations WHERE session_id = ? AND unit_id = ?",
                             (sid, o["unit_id"])).fetchone()
        if ex is None:
            db.conn.execute(
                "INSERT INTO unit_observations (session_id, unit_id, observed_at, status, business_name, business_type, note, record_id, recorded_at)"
                " VALUES (:session_id, :unit_id, :observed_at, :status, :business_name, :business_type, :note, :record_id, :now)",
                {**new, "session_id": sid, "unit_id": o["unit_id"], "now": now})
            r.inserted += 1
        elif dict(ex) != new:
            raise DbError("observation_immutable", f"세션 {sid} 의 점포 {o['unit_id']} 관측이 이미 다른 내용으로 있다. 정정은 새 세션으로 한다")
        else:
            r.unchanged += 1
    r.ids.append(sid)
    in_frame = {x[0] for x in db.conn.execute("SELECT unit_id FROM survey_frame_members WHERE frame_version_id = ?", (doc["frame_version_id"],))}
    outside = [u for u in obs_ids if u not in in_frame]
    r.message = f"세션 등록: 관측 {len(obs_ids)}건" + (f", 표본틀 밖 점포 관측 {len(outside)}건 (집계 분모에 넣지 않음)" if outside else "")


# ---- 집계 ----

def vacancy(db: Db, session_id: str) -> dict:
    """세션의 공실 집계 (데이터 사전 §5). N·K·V 와 제외·미확인 수를 함께 돌려주며 비율은 K 또는 N 이 0이면 null."""
    s = db.conn.execute("SELECT * FROM survey_sessions WHERE session_id = ?", (session_id,)).fetchone()
    if s is None:
        raise DbError("session_missing", f"세션 {session_id} 가 없다")
    frame = {x[0] for x in db.conn.execute("SELECT unit_id FROM survey_frame_members WHERE frame_version_id = ?", (s["frame_version_id"],))}
    obs = {r["unit_id"]: dict(r) for r in db.conn.execute("SELECT unit_id, status FROM unit_observations WHERE session_id = ?", (session_id,))}
    by_status = {st: 0 for st in S.UNIT_STATUSES}
    for u in frame:
        if u in obs:
            by_status[obs[u]["status"]] += 1
    not_recorded = sorted(u for u in frame if u not in obs)
    outside = sorted(u for u in obs if u not in frame)
    n = len(frame)
    k = by_status["occupied"] + by_status["vacant"]
    v = by_status["vacant"]
    return {
        "session_id": session_id, "frame_version_id": s["frame_version_id"], "route_version_id": s["route_version_id"], "started_at": s["started_at"],
        "N": n, "K": k, "V": v,
        "vacancy_rate": (v / k) if k else None, "confirmation_rate": (k / n) if n else None,
        "unconfirmed": {st: by_status[st] for st in ("closed_today", "lease_ad_only", "not_visited", "unclear")},
        "not_recorded": len(not_recorded), "not_recorded_units": not_recorded,
        "outside_frame": len(outside), "outside_frame_units": outside,
        "label": "관측표본 공실비율 (지역 전체 공실률이 아님)",
    }


def compare(db: Db, session_a: str, session_b: str) -> dict:
    """두 세션의 시점 비교. 양쪽 표본틀에 있고 두 세션 모두 확인된 공통 점포로 따로 계산한다.
    두 세션 사이에 분할·통합 링크가 걸린 점포는 비교 단절로 표시하고 공통 표본에서 뺀다. 각 세션의 전체 값은 섞지 않고 함께 돌려준다."""
    a, b = vacancy(db, session_a), vacancy(db, session_b)
    fa = {x[0] for x in db.conn.execute("SELECT unit_id FROM survey_frame_members WHERE frame_version_id = ?", (a["frame_version_id"],))}
    fb = {x[0] for x in db.conn.execute("SELECT unit_id FROM survey_frame_members WHERE frame_version_id = ?", (b["frame_version_id"],))}
    oa = {r["unit_id"]: r["status"] for r in db.conn.execute("SELECT unit_id, status FROM unit_observations WHERE session_id = ?", (session_a,))}
    ob = {r["unit_id"]: r["status"] for r in db.conn.execute("SELECT unit_id, status FROM unit_observations WHERE session_id = ?", (session_b,))}
    # 링크의 effective_from 은 현지 달력 날짜다. 세션 시작 시각의 오프셋을 그대로 둔 현지 날짜와 비교한다 (UTC 로 바꾸면 자정 근처에서 날짜가 밀린다)
    start_a = parse_datetime(a["started_at"]).date().isoformat()
    start_b = parse_datetime(b["started_at"]).date().isoformat()
    lo, hi = min(start_a, start_b), max(start_a, start_b)
    broken: set[str] = set()
    breaks = []
    for ln in db.conn.execute("SELECT from_unit_id, to_unit_id, relation, effective_from FROM survey_unit_links WHERE effective_from > ? AND effective_from <= ?", (lo, hi)):
        broken.update((ln["from_unit_id"], ln["to_unit_id"]))
        breaks.append(dict(ln))
    both = fa & fb
    common = sorted(u for u in both if oa.get(u) in CONFIRMED_STATUSES and ob.get(u) in CONFIRMED_STATUSES and u not in broken)
    va = sum(1 for u in common if oa[u] == "vacant")
    vb = sum(1 for u in common if ob[u] == "vacant")
    kc = len(common)
    return {
        "session_a": {k: a[k] for k in ("session_id", "frame_version_id", "started_at", "N", "K", "V", "vacancy_rate")},
        "session_b": {k: b[k] for k in ("session_id", "frame_version_id", "started_at", "N", "K", "V", "vacancy_rate")},
        "common": {"units": kc, "vacant_a": va, "vacant_b": vb, "rate_a": (va / kc) if kc else None, "rate_b": (vb / kc) if kc else None,
                   "delta": ((vb - va) / kc) if kc else None},
        "in_both_frames": len(both), "only_in_a": len(fa - fb), "only_in_b": len(fb - fa),
        "unconfirmed_in_either": len([u for u in both if u not in broken and not (oa.get(u) in CONFIRMED_STATUSES and ob.get(u) in CONFIRMED_STATUSES)]),
        "breaks": breaks, "broken_units": sorted(broken & (fa | fb)),
        "label": "공통 표본 비교 (현재 표본 전체의 값과 섞지 않음)",
    }


def overview(db: Db) -> dict:
    routes = [dict(r) for r in db.conn.execute(
        "SELECT r.route_id, r.name, COUNT(v.route_version_id) AS versions, MAX(v.version_no) AS latest_version FROM survey_routes r"
        " LEFT JOIN survey_route_versions v ON v.route_id = r.route_id GROUP BY r.route_id ORDER BY r.name")]
    frames = [dict(r) for r in db.conn.execute(
        "SELECT f.frame_version_id, f.name, f.confirmed_on, f.previous_version_id, COUNT(m.unit_id) AS units FROM survey_frame_versions f"
        " LEFT JOIN survey_frame_members m ON m.frame_version_id = f.frame_version_id GROUP BY f.frame_version_id ORDER BY f.confirmed_on, f.recorded_at")]
    sessions = [dict(r) for r in db.conn.execute(
        "SELECT s.session_id, s.started_at, s.route_version_id, s.frame_version_id, COUNT(o.unit_id) AS observations FROM survey_sessions s"
        " LEFT JOIN unit_observations o ON o.session_id = s.session_id GROUP BY s.session_id ORDER BY s.started_at")]
    units = db.conn.execute("SELECT COUNT(*), SUM(closed_on IS NOT NULL) FROM survey_units").fetchone()
    refs = db.conn.execute("SELECT COUNT(*) FROM record_survey_refs x WHERE x.route_version_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM survey_route_versions v WHERE v.route_version_id = x.route_version_id)").fetchone()[0]
    frefs = db.conn.execute("SELECT COUNT(*) FROM record_survey_refs x WHERE x.frame_version_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM survey_frame_versions f WHERE f.frame_version_id = x.frame_version_id)").fetchone()[0]
    return {"routes": routes, "frames": frames, "sessions": sessions, "units": units[0], "units_closed": units[1] or 0,
            "records_with_unknown_route_version": refs, "records_with_unknown_frame_version": frefs}


def vacancy_text(v: dict) -> str:
    rate = "null" if v["vacancy_rate"] is None else f"{v['vacancy_rate']:.3f}"
    conf = "null" if v["confirmation_rate"] is None else f"{v['confirmation_rate']:.3f}"
    u = v["unconfirmed"]
    lines = [f"세션 {v['session_id']} ({v['started_at']}) · 표본틀 {v['frame_version_id']}",
             f"N {v['N']} · K {v['K']} · V {v['V']} · {v['label']} {rate} · 상태 확인률 {conf}",
             f"미확인: 당일 휴무 {u['closed_today']}, 임대 광고만 {u['lease_ad_only']}, 미방문 {u['not_visited']}, 확인 불가 {u['unclear']}, 기록 없음(미조사) {v['not_recorded']}",
             f"표본틀 밖 관측 {v['outside_frame']}건 (분모 제외)"]
    return "\n".join(lines) + "\n"


def compare_text(c: dict) -> str:
    def rate(x):
        return "null" if x is None else f"{x:.3f}"
    a, b, cm = c["session_a"], c["session_b"], c["common"]
    lines = [f"A {a['session_id']} ({a['started_at']}): N {a['N']} K {a['K']} V {a['V']} 비율 {rate(a['vacancy_rate'])}",
             f"B {b['session_id']} ({b['started_at']}): N {b['N']} K {b['K']} V {b['V']} 비율 {rate(b['vacancy_rate'])}",
             f"{c['label']}: 공통 확인 점포 {cm['units']} · A 공실 {cm['vacant_a']} ({rate(cm['rate_a'])}) · B 공실 {cm['vacant_b']} ({rate(cm['rate_b'])}) · 변화 {rate(cm['delta'])}",
             f"양쪽 표본틀 {c['in_both_frames']} · A 에만 {c['only_in_a']} · B 에만 {c['only_in_b']} · 한쪽 미확인 {c['unconfirmed_in_either']} · 비교 단절(분할·통합) {len(c['broken_units'])}"]
    for br in c["breaks"]:
        lines.append(f"  단절: {br['from_unit_id']} → {br['to_unit_id']} ({br['relation']}, {br['effective_from']})")
    return "\n".join(lines) + "\n"


def overview_text(o: dict) -> str:
    lines = [f"경로 {len(o['routes'])}개, 표본틀 버전 {len(o['frames'])}개, 세션 {len(o['sessions'])}개, 점포 {o['units']}개 (종료 {o['units_closed']})"]
    for r in o["routes"]:
        lines.append(f"  경로 {r['route_id']} {r['name']} · 버전 {r['versions']} (최신 v{r['latest_version']})")
    for f in o["frames"]:
        lines.append(f"  표본틀 {f['frame_version_id']} {f['name']} · 점포 {f['units']} · 확정 {f['confirmed_on']}" + (f" · 이전 {f['previous_version_id']}" if f["previous_version_id"] else ""))
    for s in o["sessions"]:
        lines.append(f"  세션 {s['session_id']} {s['started_at']} · 관측 {s['observations']}")
    if o["records_with_unknown_route_version"]:
        lines.append(f"정본에 없는 경로 버전을 참조한 현장 기록 {o['records_with_unknown_route_version']}건 (경로 버전 파일을 반영하면 연결된다)")
    if o["records_with_unknown_frame_version"]:
        lines.append(f"정본에 없는 표본틀 버전을 참조한 현장 기록 {o['records_with_unknown_frame_version']}건 (표본틀 파일을 반영하면 연결된다)")
    return "\n".join(lines) + "\n"
