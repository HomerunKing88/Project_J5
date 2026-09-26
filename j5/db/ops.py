"""운영 점검·휴면 재개 (J5-017A). 릴리스 계획 §8 R6 (반기 운영 점검, 정본·사진 복구, 새 폰·PC 이전, "마지막 정상 앱·DB·스키마·도구 버전을 저장한다"),
데이터 사전 §12, AGENTS 정본 절.

ops_check(data_home): 정본 파일을 읽기 전용으로 열어(마이그레이션 없음) 다음을 한 번에 확인하고, 해야 할 일을 순서대로 낸다.
    1. 버전: 정본 db_schema 와 도구 db_schema. 정본이 오래됐으면 대기 중인 마이그레이션 목록(적용은 `j5 db status` 등 쓰기 명령이 열 때 한다),
       정본이 도구보다 새로우면 이 도구로는 열지 않는다(도구 갱신).
    2. 정본 무결성·외래키 검사 (읽기 전용).
    3. 백업 상태(포인터·manifest 실제 확인)와 마지막 백업 경과일. 파생본 상태. 사진 대사·수집 원본 대사.
    4. 마지막 활동: 반영(import_runs)·수집 반영(collection_runs)·파생본 게시(projection_runs)·백업(포인터). 표가 없는 구버전 정본은 '표 없음'.
    5. 마지막 정상 상태 파일 `ops/last_known_good.json`: 점검을 통과했을 때만 갱신한다(앱·도구·db_schema·패키지·파생본·백업 스키마·SQLite·Python 버전,
       dataset_version, 백업·파생본 위치). 통과하지 못하면 이전 파일을 그대로 두고 '마지막 정상' 으로 보여 준다. 매 실행은 logs/ops_check.log 에 남긴다.
    6. 반기 점검 기한: 마지막 통과 점검(또는 백업)이 CHECK_INTERVAL_DAYS 를 넘으면 기한 초과로 표시한다.

통과(ok) = 할 일이 없음: 무결성·외래키 정상, 도구·정본 스키마 같음, 백업 최신·접근 가능, 파생본 최신, 참조 사진·원본 전부 확인.
아무것도 바꾸지 않는다(정본·백업·파생본·사진). 점검 결과는 백업·복구 완료를 뜻하지 않는다.
"""

from __future__ import annotations

import json
import platform
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from j5 import APP_VERSION, SUPPORTED_PACKAGE_SCHEMA_VERSIONS
from j5.db import schema as S
from j5.db.backup import BACKUP_SCHEMA_VERSION, _append_log, _table_counts, _write_atomic, backup_status, check_photos, check_raw_files
from j5.db.projection import PROJECTION_SCHEMA_VERSION, projection_status
from j5.db.store import Db, DbError, default_db_path
from j5.db.validate import now_utc

OPS_DIR = Path("ops")
LAST_GOOD = "last_known_good.json"
OPS_LOG = Path("logs") / "ops_check.log"
LAST_GOOD_FORMAT = "j5lastgood"
LAST_GOOD_SCHEMA_VERSION = "1.0.0"
CHECK_INTERVAL_DAYS = 180  # 반기 운영 점검 (릴리스 계획 §8 R6)
EXIT_BY_OK = {True: 0, False: 1}


class OpsError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _parse_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _days_since(s: str | None, now: datetime) -> int | None:
    t = _parse_utc(s)
    return None if t is None else max(0, (now - t).days)


def _last(db: Db, table: str, sql: str) -> dict:
    """마지막 활동 시각. 표가 없는 구버전 정본은 '표 없음' 으로 구분한다(값 없음과 다르다)."""
    if not db._has_table(table):
        return {"at": None, "reason": f"표 없음 ({table})"}
    row = db.conn.execute(sql).fetchone()
    return {"at": row[0] if row and row[0] else None, "reason": None if row and row[0] else "기록 없음"}


def schema_state(current: int) -> dict:
    tool = S.DB_SCHEMA_VERSION
    pending = [{"version": v, "name": n} for v, n, _ in S.MIGRATIONS if v > current]
    if current == tool:
        return {"db_schema_version": current, "tool_schema_version": tool, "state": "same", "pending": [], "text": f"정본 db_schema {current} = 도구 {tool}"}
    if current < tool:
        names = ", ".join(f"{p['version']} {p['name']}" for p in pending)
        return {"db_schema_version": current, "tool_schema_version": tool, "state": "db_older", "pending": pending,
                "text": f"정본 db_schema {current} < 도구 {tool}: 마이그레이션 {len(pending)}건 대기 ({names})"}
    return {"db_schema_version": current, "tool_schema_version": tool, "state": "db_newer", "pending": [],
            "text": f"정본 db_schema {current} > 도구 {tool}: 이 도구로는 열지 않는다. 도구를 갱신한다"}


def read_last_good(data_home: Path) -> tuple[dict | None, str | None]:
    p = Path(data_home) / OPS_DIR / LAST_GOOD
    if not p.is_file():
        return None, None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"unreadable:{type(e).__name__}"
    if not isinstance(d, dict) or d.get("format") != LAST_GOOD_FORMAT:
        return None, "invalid"
    return d, None


def tool_versions() -> dict:
    return {"app_version": APP_VERSION, "db_schema_version": S.DB_SCHEMA_VERSION, "package_schema_versions": sorted(SUPPORTED_PACKAGE_SCHEMA_VERSIONS),
            "projection_schema_version": PROJECTION_SCHEMA_VERSION, "backup_schema_version": BACKUP_SCHEMA_VERSION,
            "sqlite_version": sqlite3.sqlite_version, "python_version": platform.python_version(), "platform": sys.platform}


def ops_check(data_home: Path, *, db_path: Path | None = None, now: str | None = None) -> dict:
    """읽기 전용 점검. 정본을 마이그레이션하지 않고 아무 파일도 고치지 않는다(점검 통과 시 ops/last_known_good.json 과 logs/ops_check.log 만 쓴다)."""
    data_home = Path(data_home)
    checked_at = now or now_utc()
    now_dt = _parse_utc(checked_at) or datetime.now(timezone.utc)
    path = Path(db_path) if db_path is not None else default_db_path(data_home)
    run_id = str(uuid.uuid4())
    r: dict = {"run_id": run_id, "checked_at": checked_at, "data_home": str(data_home), "db_path": str(path), "tool": tool_versions(),
               "ok": False, "actions": [], "warnings": [], "db": None, "schema": None, "backup": None, "projection": None, "photos": None, "raw": None,
               "last_activity": None, "last_good": None, "last_good_problem": None, "last_good_updated": False, "overdue": None}
    prev, prev_problem = read_last_good(data_home)
    r["last_good"], r["last_good_problem"] = prev, prev_problem
    if prev_problem:
        r["warnings"].append(f"마지막 정상 상태 파일을 읽을 수 없다 ({prev_problem}): 이번 점검이 통과하면 다시 쓴다")
    try:
        if not path.is_file():
            raise OpsError("db_missing", f"정본 파일이 없다: {path}. 백업에서 복구하려면 `j5 db restore <백업 폴더> <빈 폴더>`")
        try:
            db = Db.open_readonly(path)
        except DbError as e:
            raise OpsError(e.code, e.message) from e
        with db:
            _check_db(db, data_home, r, now_dt)
    except (OpsError, sqlite3.Error, OSError) as e:
        code = getattr(e, "code", type(e).__name__)
        msg = getattr(e, "message", str(e))
        r["db"] = {"ok": False, "problem": code, "message": msg}
        r["actions"].insert(0, {"code": code, "text": msg})
    r["ok"] = not r["actions"]
    lg_at = (prev or {}).get("checked_at")
    r["overdue"] = _overdue(lg_at, r["backup"], now_dt)
    if r["ok"]:
        r["last_good_updated"] = _write_last_good(data_home, r)
    _append_log(data_home / OPS_LOG, {"checked_at": checked_at, "run_id": run_id, "ok": r["ok"], "actions": [a["code"] for a in r["actions"]],
                                      "db_schema_version": (r["schema"] or {}).get("db_schema_version"), "dataset_version": (r["db"] or {}).get("dataset_version"),
                                      "app_version": APP_VERSION, "last_good_updated": r["last_good_updated"]})
    return r


def _check_db(db: Db, data_home: Path, r: dict, now_dt: datetime) -> None:
    current = db.schema_version()
    sc = schema_state(current)
    r["schema"] = sc
    if sc["state"] == "db_newer":
        r["actions"].append({"code": "tool_too_old", "text": sc["text"]})
    integrity = [row[0] for row in db.conn.execute("PRAGMA integrity_check")]
    fk = db.conn.execute("PRAGMA foreign_key_check").fetchall()
    meta = {row["key"]: row["value"] for row in db.conn.execute("SELECT key, value FROM meta")}
    for key in ("study_id", "data_mode", "dataset_version"):
        if key not in meta:
            raise OpsError("meta_missing", f"meta.{key} 가 없다")
    counts = _table_counts(db.conn)
    r["db"] = {"ok": integrity == ["ok"] and not fk, "integrity_check": integrity, "foreign_key_violations": len(fk), "study_id": meta["study_id"],
               "data_mode": meta["data_mode"], "dataset_version": int(meta["dataset_version"]), "created_at": meta.get("created_at"), "counts": counts, "problem": None, "message": None}
    if integrity != ["ok"]:
        r["actions"].append({"code": "db_integrity", "text": f"무결성 검사 실패: {'; '.join(integrity[:3])}. 마지막 정상 백업에서 빈 폴더에 복구한다(`j5 db restore`)"})
    if fk:
        r["actions"].append({"code": "db_foreign_keys", "text": f"외래키 위반 {len(fk)}건. 마지막 정상 백업과 대조한다"})
    if sc["state"] == "db_older":
        r["actions"].append({"code": "migrations_pending", "text": sc["text"] + ". 먼저 백업이 최신·접근 가능인지 확인한 뒤 `j5 db status` 로 열면 적용되고, 그 다음 다시 백업한다"})
    # 백업·파생본 (기존 상태 함수. 읽기 전용 연결에서도 조회만 한다)
    bs = backup_status(db, data_home)
    bs["days_since"] = _days_since(bs.get("backup_at"), now_dt)
    r["backup"] = bs
    if bs["stale"]:
        r["actions"].append({"code": "backup_" + (bs["problem"] or "stale"), "text": f"백업: {bs['state']}. `j5 db backup` (외장 위치면 --dest)"})
    ps = projection_status(db, data_home) if db._has_table("projection_runs") else {"state": "표 없음 (db_schema < 3)", "stale": True, "published_at": None}
    r["projection"] = ps
    if ps["stale"]:
        r["actions"].append({"code": "projection_stale", "text": f"파생본: {ps['state']}. `j5 db project`"})
    # 사진·수집 원본 대사 (삭제 없음)
    ph = check_photos(db, data_home)
    r["photos"] = {k: ph[k] for k in ("referenced", "ok", "missing", "mismatched", "unreferenced", "ok_all")}
    if not ph["ok_all"]:
        r["actions"].append({"code": "photos", "text": f"사진 누락 {len(ph['missing'])}장, 불일치 {len(ph['mismatched'])}장. 마지막 정상 백업에서 되찾는다 (`j5 db check-photos`)"})
    if ph["unreferenced"]:
        r["warnings"].append(f"참조되지 않은 사진 {len(ph['unreferenced'])}개 (정리대기 후보, 자동 삭제 없음)")
    rw = check_raw_files(db, data_home)
    r["raw"] = rw
    if not rw["ok_all"]:
        r["actions"].append({"code": "raw_files", "text": f"수집 원본 누락 {len(rw['missing'])}, 불일치 {len(rw['mismatched'])}. 마지막 정상 백업에서 되찾는다"})
    # 마지막 활동
    la = {"import_applied": _last(db, "import_runs", "SELECT MAX(finished_at) FROM import_runs WHERE outcome = 'applied'"),
          "collection_loaded": _last(db, "collection_runs", "SELECT MAX(loaded_at) FROM collection_runs"),
          "projection_published": _last(db, "projection_runs", "SELECT MAX(finished_at) FROM projection_runs WHERE status = 'published'"),
          "backup": {"at": bs.get("backup_at"), "reason": None if bs.get("backup_at") else "백업 없음"}}
    for v in la.values():
        v["days_since"] = _days_since(v["at"], now_dt)
    r["last_activity"] = la


def _overdue(last_good_at: str | None, bs: dict | None, now_dt: datetime) -> dict:
    """반기 점검 기한. 마지막 통과 점검이 없으면 마지막 백업을 기준으로 삼고, 둘 다 없으면 '기준 없음'."""
    ref, basis = last_good_at, "마지막 통과 점검"
    if ref is None and bs and bs.get("backup_at"):
        ref, basis = bs["backup_at"], "마지막 백업"
    if ref is None:
        return {"overdue": None, "basis": None, "days": None, "limit_days": CHECK_INTERVAL_DAYS, "text": "기준 없음 (통과한 점검·백업 기록 없음)"}
    days = _days_since(ref, now_dt)
    over = days is not None and days > CHECK_INTERVAL_DAYS
    return {"overdue": over, "basis": basis, "days": days, "limit_days": CHECK_INTERVAL_DAYS,
            "text": f"{basis} {ref} ({days}일 전){' · 반기 점검 기한 초과' if over else ''}"}


def _write_last_good(data_home: Path, r: dict) -> bool:
    d = {"format": LAST_GOOD_FORMAT, "last_good_schema_version": LAST_GOOD_SCHEMA_VERSION, "checked_at": r["checked_at"], "run_id": r["run_id"],
         **r["tool"], "study_id": r["db"]["study_id"], "data_mode": r["db"]["data_mode"], "dataset_version": r["db"]["dataset_version"],
         "db_schema_version_of_store": r["schema"]["db_schema_version"], "counts": r["db"]["counts"],
         "backup": {"dir": r["backup"].get("backup_dir"), "created_at": r["backup"].get("backup_at"), "dataset_version": r["backup"].get("backup_version")},
         "projection": {"dir": r["projection"].get("published_dir"), "published_at": r["projection"].get("published_at"), "dataset_version": r["projection"].get("published_version")}}
    (Path(data_home) / OPS_DIR).mkdir(parents=True, exist_ok=True)
    _write_atomic(Path(data_home) / OPS_DIR / LAST_GOOD, (json.dumps(d, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    return True


def ops_text(r: dict) -> str:
    t = r["tool"]
    lines = [f"운영 점검 {r['checked_at']} · 정본 {r['db_path']} · 실데이터 홈 {r['data_home']}",
             f"도구: j5 {t['app_version']} · db_schema {t['db_schema_version']} · package {'/'.join(t['package_schema_versions'])} · projection {t['projection_schema_version']}"
             f" · backup {t['backup_schema_version']} · SQLite {t['sqlite_version']} · Python {t['python_version']} ({t['platform']})"]
    d = r["db"]
    if d and d.get("problem"):
        lines.append(f"정본: 열 수 없음 [{d['problem']}] {d['message']}")
    elif d:
        lines.append(f"정본: study {d['study_id']} · {d['data_mode']} · dataset_version {d['dataset_version']} · integrity {', '.join(d['integrity_check'])} · 외래키 위반 {d['foreign_key_violations']}건"
                     f" · 기록 {d['counts'].get('records', 0)}건 · 첨부 {d['counts'].get('attachments', 0)}건")
    if r["schema"]:
        lines.append(f"스키마: {r['schema']['text']}")
    if r["backup"]:
        b = r["backup"]
        extra = f" · 마지막 백업 {b['backup_at']} ({b['days_since']}일 전, {b.get('backup_dir')})" if b.get("backup_at") else ""
        lines.append(f"백업: {b['state']}{extra}")
    if r["projection"]:
        p = r["projection"]
        lines.append(f"파생본: {p['state']}" + (f" · 마지막 게시 {p['published_at']}" if p.get("published_at") else ""))
    if r["photos"]:
        ph = r["photos"]
        lines.append(f"사진 대사: 참조 {ph['referenced']}장, 확인 {ph['ok']}장, 누락 {len(ph['missing'])}장, 불일치 {len(ph['mismatched'])}장, 미참조 {len(ph['unreferenced'])}개")
    if r["raw"]:
        rw = r["raw"]
        lines.append(f"수집 원본 대사: 참조 {rw['referenced']}개, 확인 {rw['ok']}개, 누락 {len(rw['missing'])}개, 불일치 {len(rw['mismatched'])}개")
    if r["last_activity"]:
        la = r["last_activity"]
        labels = (("import_applied", "마지막 반영"), ("collection_loaded", "마지막 수집 반영"), ("projection_published", "마지막 파생본 게시"), ("backup", "마지막 백업"))
        parts = []
        for k, label in labels:
            v = la[k]
            parts.append(f"{label} {v['at']} ({v['days_since']}일 전)" if v["at"] else f"{label} {v['reason']}")
        lines.append("마지막 활동: " + " · ".join(parts))
    if r["overdue"]:
        lines.append(f"점검 기한: {r['overdue']['text']}")
    lg = r["last_good"]
    if lg:
        lines.append(f"마지막 정상 상태: {lg['checked_at']} · j5 {lg['app_version']} · db_schema {lg['db_schema_version_of_store']} · dataset_version {lg['dataset_version']}"
                     f" · SQLite {lg['sqlite_version']} · Python {lg['python_version']}" + (" (이번 점검으로 갱신)" if r["last_good_updated"] else ""))
    elif r["last_good_updated"]:
        lines.append("마지막 정상 상태: 이번 점검으로 처음 기록")
    else:
        lines.append("마지막 정상 상태: 기록 없음")
    for w in r["warnings"]:
        lines.append(f"주의: {w}")
    if r["ok"]:
        lines.append("점검 통과: 할 일 없음. 정본·백업·파생본·사진이 일치한다 (외부 사본 보관 여부는 사용자가 확인한다)")
    else:
        lines.append(f"조치 필요 {len(r['actions'])}건 (순서대로):")
        for i, a in enumerate(r["actions"], 1):
            lines.append(f"  {i}. [{a['code']}] {a['text']}")
        lines.append("마지막 정상 상태 파일은 갱신하지 않았다.")
    return "\n".join(lines) + "\n"
