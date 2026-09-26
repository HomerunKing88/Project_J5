"""연말 개방형 포맷 보존본 (J5-017B). 릴리스 계획 §8 R6 ("연말 개방형 포맷 보존본"), 데이터 사전 §12 ("연말 CSV/JSONL/GeoJSON 보존본을 유지한다").

create_archive(db, data_home, dest_root=None, photos=False): 정본의 읽기 스냅샷(Db.snapshot) 안에서 모든 사용자 표를 읽어 SQLite·j5 도구 없이도 열 수 있는
파일로 쓴다. 표마다 `tables/<표>.jsonl`(행마다 JSON 객체, 열 순서 그대로, NULL 은 null, BLOB 은 {"$hex": ...}: 정본 값을 그대로 보존)과
`tables/<표>.csv`(같은 행, NULL 은 빈 칸: 스프레드시트용 편의 사본이며 빈 문자열과 NULL 을 구분하지 못한다). 위치점이 있는 물건은 `assets.geojson`,
필지가 있으면 `parcels.geojson`(폰 번들 형식). 정본의 표·인덱스·트리거 정의 `schema.sql`, 저장소의 JSON Schema 사본 `schemas/`, 설명 `README.txt`,
목록·해시·버전 `archive_manifest.json`(schemas/archive_manifest.schema.json). `--photos` 면 첨부가 참조한 사진을 해시 검증하며 복사한다.
임시 폴더에 전량 쓰고 디스크에서 다시 읽어 검증(verify_archive_dir)한 뒤에만 최종 이름으로 바꾼다. 실패하면 failed-<run8>/ 에 남긴다.
정본·백업·파생본은 바꾸지 않는다. 보존본은 백업이 아니다(복구 명령은 백업 폴더만 받는다).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import platform
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from j5 import APP_VERSION
from j5.db.backup import BackupError, _append_log, _copy_verified, _sha256_file, _write_atomic
from j5.db.parcels import parcels_bundle_from_db
from j5.db.store import Db, DbError
from j5.db.validate import now_utc
from j5.schemas_loader import SCHEMAS_DIR, schema_errors

ARCHIVES_DIR = Path("exports") / "private" / "archives"
ARCHIVE_MANIFEST = "archive_manifest.json"
ARCHIVE_SCHEMA_VERSION = "1.0.0"
ARCHIVE_LOG = Path("logs") / "archive.log"
TABLES_DIR = "tables"
SCHEMAS_SUBDIR = "schemas"
SCHEMA_SQL = "schema.sql"
README_TXT = "README.txt"
ASSETS_GEOJSON = "assets.geojson"
PARCELS_GEOJSON = "parcels.geojson"
EXIT_BY_OUTCOME = {"completed": 0, "failed": 1}


class ArchiveError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class ArchiveResult:
    outcome: str = "failed"
    run_id: str | None = None
    generated_at: str | None = None
    dataset_version: int = 0
    db_schema_version: int = 0
    archive_dir: str | None = None   # data_home 기준 상대경로, 밖이면 절대경로
    manifest_sha256: str | None = None
    tables: int = 0
    rows: int = 0
    files: int = 0
    photos: int = 0
    bytes: int = 0
    same_device: bool | None = None
    findings: list[dict] = field(default_factory=list)
    message: str = ""

    def add(self, level: str, code: str, message: str) -> None:
        self.findings.append({"level": level, "code": code, "message": message})

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=True, sort_keys=True, indent=2) + "\n"

    def to_text(self) -> str:
        lines = [f"보존본: {'완료' if self.outcome == 'completed' else '실패 (정본·백업·파생본 그대로)'}",
                 f"정본 v{self.dataset_version} · db_schema {self.db_schema_version} · generated_at {self.generated_at}"]
        if self.archive_dir:
            lines.append(f"위치: {self.archive_dir} (manifest sha256 {self.manifest_sha256})")
            lines.append(f"표 {self.tables}개 · 행 {self.rows}개 · 파일 {self.files}개 · 사진 {self.photos}장 · {self.bytes} 바이트")
        if self.message:
            lines.append(self.message)
        for f in self.findings:
            if f["level"] in ("error", "warn"):
                lines.append(f"  [{f['level']}] {f['code']}: {f['message']}")
        if self.same_device:
            lines.append("주의: 정본과 같은 장치에 있다. 독립 위치 여부는 사용자가 확인한다.")
        lines.append("보존본은 백업이 아니다(복구는 백업 폴더로 한다). 보존본 작성은 백업·파생본 게시를 뜻하지 않는다.")
        return "\n".join(lines) + "\n"


# ---- 값 변환 ----

def _cell_json(v):
    if isinstance(v, (bytes, memoryview)):
        return {"$hex": bytes(v).hex()}
    return v


def _cell_csv(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (bytes, memoryview)):
        return bytes(v).hex()
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


def _table_defs(conn: sqlite3.Connection) -> list[dict]:
    """모든 사용자 표의 이름·열 정의 (sqlite_master 순서가 아니라 이름순)."""
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    out = []
    for t in names:
        cols = [{"name": r["name"], "type": r["type"], "notnull": bool(r["notnull"]), "pk": int(r["pk"])} for r in conn.execute(f'PRAGMA table_info("{t}")')]
        out.append({"name": t, "columns": cols})
    return out


def _order_by(cols: list[dict]) -> str:
    pk = [c["name"] for c in sorted((c for c in cols if c["pk"]), key=lambda c: c["pk"])]
    return ", ".join(f'"{c}"' for c in pk) if pk else "rowid"


def _schema_sql(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name").fetchall()
    return "".join(f"-- {r[0]} {r[1]}\n{r[2]};\n\n" for r in rows)


def _readme(meta: dict, tables: list[dict], *, photos: bool, generated_at: str) -> str:
    return "\n".join([
        "종로5가 투자지도 정본 보존본 (j5archive 1.0.0)",
        f"study_id {meta['study_id']} · data_mode {meta['data_mode']} · dataset_version {meta['dataset_version']} · db_schema {meta['db_schema_version']} · 생성 {generated_at}",
        f"작성 도구 j5 {APP_VERSION} · SQLite {sqlite3.sqlite_version} · Python {platform.python_version()}",
        "",
        "내용",
        "- tables/<표>.jsonl: 정본의 모든 사용자 표. 한 줄이 한 행이고 열 순서는 schema.sql 과 같다. NULL 은 null, BLOB 은 {\"$hex\": ...}. 값을 그대로 보존한 정본 사본이다.",
        "- tables/<표>.csv: 같은 행의 스프레드시트용 사본(UTF-8, 헤더 1행). NULL 은 빈 칸으로 적어 빈 문자열과 구분하지 못한다. 정확한 값은 jsonl 을 본다.",
        "- assets.geojson: 위치점이 있는 매입 검토 단위(WGS84 경도·위도). 위치점 없는 물건은 unlocated_asset_ids.",
        "- parcels.geojson: 정본 필지(있을 때만, 폰 번들 형식 j5parcels 1.0.0).",
        "- schema.sql: 이 보존본 시점의 표·인덱스·트리거 정의. schemas/: 저장소 JSON Schema 사본(payload_json 등의 계약).",
        "- photos/: 첨부가 참조한 사진 (" + ("포함" if photos else "이 보존본에는 넣지 않음. 사진은 백업 폴더에 있다") + ").",
        "- archive_manifest.json: 파일 목록·크기·sha256, 표별 행 수, 버전.",
        "",
        "표 목록: " + ", ".join(f"{t['name']}({t['rows']})" for t in tables),
        "",
        "이 보존본은 읽기용 스냅샷이다. 백업이 아니며 j5 db restore 의 입력이 아니다. 정정은 정본에 새 기록으로 넣고 다음 보존본에 반영된다.",
        "실데이터·사진·개인 메모가 들어 있으면 공개 저장소·배포에 올리지 않는다.",
        "",
    ])


def _assets_geojson(conn: sqlite3.Connection, meta: dict, generated_at: str) -> dict:
    features, unlocated = [], []
    for a in conn.execute("SELECT asset_id, label, data_mode, tracking_status, resolution_status, address, lon, lat FROM assets ORDER BY asset_id"):
        props = {k: a[k] for k in ("asset_id", "label", "data_mode", "tracking_status", "resolution_status", "address")}
        if a["lon"] is None:
            unlocated.append(a["asset_id"])
            continue
        features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [a["lon"], a["lat"]]}, "properties": props})
    return {"type": "FeatureCollection", "format": "j5archive", "study_id": meta["study_id"], "data_mode": meta["data_mode"], "source_dataset_version": meta["dataset_version"],
            "generated_at": generated_at, "unlocated_asset_ids": sorted(unlocated), "features": features}


# ---- 작성 ----

def _write_file(root: Path, rel: str, data: bytes, files: list[dict]) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    files.append({"path": rel, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})


def _display_path(p: Path, data_home: Path) -> str:
    try:
        return p.resolve().relative_to(data_home.resolve()).as_posix()
    except ValueError:
        return str(p.resolve())


def create_archive(db: Db, data_home: Path, *, dest_root: Path | None = None, photos: bool = False) -> ArchiveResult:
    data_home = Path(data_home)
    started = db.now()
    run_id = str(uuid.uuid4())
    r = ArchiveResult(run_id=run_id, generated_at=started)
    root = Path(dest_root) if dest_root is not None else data_home / ARCHIVES_DIR
    tmp = root / f".tmp-{run_id[:8]}"
    try:
        root.mkdir(parents=True, exist_ok=True)
        tmp.mkdir(exist_ok=False)
        files: list[dict] = []
        with db.snapshot():
            conn = db.conn
            meta = {"study_id": db.meta("study_id"), "data_mode": db.data_mode, "dataset_version": int(db.meta("dataset_version") or 0), "db_schema_version": db.schema_version()}
            r.dataset_version, r.db_schema_version = meta["dataset_version"], meta["db_schema_version"]
            defs = _table_defs(conn)
            tables = []
            total_rows = 0
            for t in defs:
                cols = [c["name"] for c in t["columns"]]
                rows = conn.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{t["name"]}" ORDER BY {_order_by(t["columns"])}').fetchall()
                jl = io.StringIO()
                cs = io.StringIO()
                w = csv.writer(cs, lineterminator="\n")
                w.writerow(cols)
                for row in rows:
                    jl.write(json.dumps({c: _cell_json(row[i]) for i, c in enumerate(cols)}, ensure_ascii=False, separators=(",", ":")) + "\n")
                    w.writerow([_cell_csv(row[i]) for i in range(len(cols))])
                _write_file(tmp, f"{TABLES_DIR}/{t['name']}.jsonl", jl.getvalue().encode("utf-8"), files)
                _write_file(tmp, f"{TABLES_DIR}/{t['name']}.csv", cs.getvalue().encode("utf-8"), files)
                tables.append({"name": t["name"], "columns": t["columns"], "rows": len(rows), "jsonl": f"{TABLES_DIR}/{t['name']}.jsonl", "csv": f"{TABLES_DIR}/{t['name']}.csv"})
                total_rows += len(rows)
            geo = _assets_geojson(conn, meta, started)
            _write_file(tmp, ASSETS_GEOJSON, (json.dumps(geo, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8"), files)
            n_parcels = 0
            if db._has_table("parcels"):
                pb = parcels_bundle_from_db(db, generated_at=started, source_dataset_version=meta["dataset_version"])
                if pb is not None:
                    pb = {**pb, "generated_at": started}
                    errs = schema_errors("parcels_bundle.schema.json", pb)
                    if errs:
                        raise ArchiveError("parcels_schema", "; ".join(errs[:3]))
                    _write_file(tmp, PARCELS_GEOJSON, (json.dumps(pb, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"), files)
                    n_parcels = pb["count"]
            _write_file(tmp, SCHEMA_SQL, _schema_sql(conn).encode("utf-8"), files)
            for sp in sorted(SCHEMAS_DIR.glob("*.schema.json")):
                _write_file(tmp, f"{SCHEMAS_SUBDIR}/{sp.name}", sp.read_bytes(), files)
            referenced = [dict(x) for x in conn.execute("SELECT DISTINCT rel_path, sha256, bytes FROM attachments ORDER BY rel_path")] if db._has_table("attachments") else []
        n_photos = 0
        if photos:
            problems = []
            for ph in referenced:
                try:
                    digest, n = _copy_verified(data_home / ph["rel_path"], tmp / ph["rel_path"], sha256=ph["sha256"], nbytes=ph["bytes"])
                except BackupError as e:
                    problems.append(f"{ph['rel_path']}: {e.code}")
                    continue
                files.append({"path": ph["rel_path"], "bytes": n, "sha256": digest})
                n_photos += 1
            if problems:
                raise ArchiveError("photos_incomplete", f"참조 사진 {len(problems)}건이 없거나 해시가 다르다: " + "; ".join(problems[:5]) + ". 사진 대사(check-photos)로 확인한다")
        _write_file(tmp, README_TXT, _readme(meta, tables, photos=photos, generated_at=started).encode("utf-8"), files)
        manifest = {
            "format": "j5archive", "archive_schema_version": ARCHIVE_SCHEMA_VERSION, **meta, "generated_at": started, "run_id": run_id,
            "tool": {"app_version": APP_VERSION, "sqlite_version": sqlite3.sqlite_version, "python_version": platform.python_version()},
            "photos_included": photos, "counts": {"tables": len(tables), "rows": total_rows, "photos": n_photos, "parcels": n_parcels, "assets_located": len(geo["features"])},
            "tables": tables, "files": sorted(files, key=lambda f: f["path"]),
        }
        errs = schema_errors("archive_manifest.schema.json", manifest)
        if errs:
            raise ArchiveError("manifest_schema", "; ".join(errs[:3]))
        _write_atomic(tmp / ARCHIVE_MANIFEST, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        v = verify_archive_dir(tmp)
        stamp = started.replace("-", "").replace(":", "").replace("T", "-").rstrip("Z")
        final = root / f"{stamp}-ds{meta['dataset_version']}-{run_id[:8]}"
        os.rename(tmp, final)
        r.archive_dir = _display_path(final, data_home)
        r.manifest_sha256 = v["manifest_sha256"]
        r.tables, r.rows, r.files, r.photos, r.bytes = len(tables), total_rows, len(files), n_photos, sum(f["bytes"] for f in files)
        try:
            r.same_device = final.stat().st_dev == db.path.stat().st_dev
        except OSError:
            r.same_device = None
        r.outcome = "completed"
        r.message = f"보존본 완료 (정본 v{meta['dataset_version']}, 표 {len(tables)}개 · 행 {total_rows}개). 디스크에서 다시 읽어 검증했다"
        return r
    except (ArchiveError, BackupError, DbError, OSError, ValueError, KeyError, sqlite3.Error) as e:
        code = getattr(e, "code", type(e).__name__)
        msg = getattr(e, "message", str(e))
        r.outcome = "failed"
        r.archive_dir = r.manifest_sha256 = None
        r.add("error", code, msg)
        if tmp.exists():
            failed_dir = root / f"failed-{run_id[:8]}"
            try:
                os.rename(tmp, failed_dir)
                r.add("warn", "partial_kept", f"부분 산출물을 {_display_path(failed_dir, data_home)} 에 남겼다 (보존본 아님, 자동 삭제하지 않음)")
            except OSError:
                pass
        r.message = f"보존본 실패 [{code}]: {msg}. 정본·백업·파생본은 그대로다"
        return r
    finally:
        _append_log(data_home / ARCHIVE_LOG, {"started_at": started, "finished_at": now_utc(), "run_id": run_id, "outcome": r.outcome, "dataset_version": r.dataset_version,
                                              "archive_dir": r.archive_dir, "manifest_sha256": r.manifest_sha256, "tables": r.tables, "rows": r.rows, "photos": r.photos, "message": r.message})


# ---- 검증 ----

def verify_archive_dir(adir: Path) -> dict:
    """보존본 폴더를 디스크에서 전부 읽어 검증한다: manifest 스키마, 파일 목록·크기·해시(누락·여분 없음), 표마다 jsonl·csv 행 수가 manifest 와 같음,
    jsonl 의 열이 표 정의와 같음, 사진이 있으면 attachments 표가 참조한 사진 전부가 같은 해시로 있음, schema.sql·README·스키마 사본 존재. 정본 연결이 필요 없다."""
    adir = Path(adir)
    mpath = adir / ARCHIVE_MANIFEST
    if not mpath.is_file():
        raise ArchiveError("manifest_missing", f"{mpath} 가 없다")
    raw = mpath.read_bytes()
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ArchiveError("manifest_unreadable", str(e)) from e
    errs = schema_errors("archive_manifest.schema.json", manifest)
    if errs:
        raise ArchiveError("manifest_schema", "; ".join(errs[:3]))
    listed = {f["path"]: f for f in manifest["files"]}
    for path, f in listed.items():
        p = adir / path
        if not p.is_file():
            raise ArchiveError("file_missing", path)
        digest, n = _sha256_file(p)
        if digest != f["sha256"] or n != f["bytes"]:
            raise ArchiveError("file_hash", f"{path} 의 크기·해시가 manifest 와 다르다")
    on_disk = {p.relative_to(adir).as_posix() for p in adir.rglob("*") if p.is_file()} - {ARCHIVE_MANIFEST}
    if on_disk != set(listed):
        raise ArchiveError("extra_or_missing", f"manifest 밖 파일 또는 누락: {sorted(on_disk ^ set(listed))}")
    for must in (SCHEMA_SQL, README_TXT, ASSETS_GEOJSON):
        if must not in listed:
            raise ArchiveError("file_missing", must)
    if not any(p.startswith(SCHEMAS_SUBDIR + "/") for p in listed):
        raise ArchiveError("schemas_missing", "JSON Schema 사본이 없다")
    total = 0
    attachments_rows: list[dict] = []
    for t in manifest["tables"]:
        cols = [c["name"] for c in t["columns"]]
        if t["jsonl"] not in listed or t["csv"] not in listed:
            raise ArchiveError("table_files_missing", t["name"])
        jl = [json.loads(l) for l in (adir / t["jsonl"]).read_bytes().split(b"\n") if l]
        if len(jl) != t["rows"]:
            raise ArchiveError("rows_jsonl", f"{t['name']}: jsonl {len(jl)}행, manifest {t['rows']}")
        for row in jl:
            if list(row.keys()) != cols:
                raise ArchiveError("columns_jsonl", f"{t['name']}: 열이 표 정의와 다르다")
        with open(adir / t["csv"], encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        if not rows or rows[0] != cols or len(rows) - 1 != t["rows"]:
            raise ArchiveError("rows_csv", f"{t['name']}: csv {max(len(rows) - 1, 0)}행·헤더 대조 실패, manifest {t['rows']}")
        total += t["rows"]
        if t["name"] == "attachments":
            attachments_rows = jl
    if total != manifest["counts"]["rows"] or len(manifest["tables"]) != manifest["counts"]["tables"]:
        raise ArchiveError("counts", "표·행 수가 manifest counts 와 다르다")
    photo_paths = {p for p in listed if p.startswith("photos/")}
    if manifest["photos_included"]:
        want = {(a["rel_path"], a["sha256"], a["bytes"]) for a in attachments_rows}
        for rel, sha, n in want:
            f = listed.get(rel)
            if f is None or f["sha256"] != sha or f["bytes"] != n:
                raise ArchiveError("photo_link", f"attachments 가 참조한 {rel} 가 보존본에 없거나 해시가 다르다")
        if photo_paths - {w[0] for w in want}:
            raise ArchiveError("photo_unreferenced", "참조되지 않은 사진이 있다")
        if len(photo_paths) != manifest["counts"]["photos"]:
            raise ArchiveError("counts", "사진 수가 manifest counts 와 다르다")
    elif photo_paths:
        raise ArchiveError("photo_unexpected", "photos_included 가 false 인데 사진 파일이 있다")
    geo = json.loads((adir / ASSETS_GEOJSON).read_text(encoding="utf-8"))
    if geo.get("source_dataset_version") != manifest["dataset_version"] or len(geo.get("features", [])) != manifest["counts"]["assets_located"]:
        raise ArchiveError("geojson", "assets.geojson 의 버전·점 수가 manifest 와 다르다")
    if (PARCELS_GEOJSON in listed) != (manifest["counts"]["parcels"] > 0):
        raise ArchiveError("parcels_file", "parcels.geojson 의 유무가 counts.parcels 와 맞지 않는다")
    return {"manifest": manifest, "manifest_sha256": hashlib.sha256(raw).hexdigest(), "tables": len(manifest["tables"]), "rows": total, "photos": len(photo_paths)}
