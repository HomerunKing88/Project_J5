"""조회 파생본 (J5-011). 정본의 고정 dataset_version 에서 조회 패키지(.j5view.zip)를 전량 생성해 검증한 뒤 게시한다. 데이터 사전 §4, ADR-03.

흐름: 고정 버전의 일관된 읽기(단일 작성자 잠금) → 임시 폴더에 assets.seed.json·assets.geojson·records.jsonl(·photos/) 전량 생성
      → manifest 작성·스키마 검증 → 파일을 다시 읽어 행수·참조·필수값·버전·해시 검증 → ZIP 작성·항목 해시 검증
      → 정본 버전이 그대로인지 재확인 → 버전 폴더로 게시 → 최신본 포인터(latest.json) 를 같은 파일시스템에서 교체.
실패하면 이전 파생본과 포인터를 그대로 두고, 부분 산출물은 failed-<run> 폴더로 남긴다(정상 조회자료로 제공하지 않음). 정본은 손대지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from j5.db.store import Db
from j5.package.preserve import PreserveError, copy_package
from j5.schemas_loader import schema_errors

PROJECTION_SCHEMA_VERSION = "1.0.0"
PROJECTIONS_DIR = Path("exports") / "private" / "projections"
LATEST_POINTER = "latest.json"
MANIFEST = "manifest.json"
SEED_FILE = "assets.seed.json"
GEOJSON_FILE = "assets.geojson"
RECORDS_FILE = "records.jsonl"
EXIT_BY_OUTCOME = {"published": 0, "failed": 1}


class ProjectionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class ProjectionResult:
    outcome: str = "failed"
    run_id: str | None = None
    source_dataset_version: int = 0
    projection_schema_version: str = PROJECTION_SCHEMA_VERSION
    generated_at: str | None = None
    output_dir: str | None = None   # J5_DATA_HOME 기준 상대경로
    zip_name: str | None = None
    zip_sha256: str | None = None
    counts: dict = field(default_factory=dict)
    findings: list[dict] = field(default_factory=list)
    message: str = ""

    def add(self, level: str, code: str, message: str) -> None:
        self.findings.append({"level": level, "code": code, "message": message})

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=True, sort_keys=True, indent=2) + "\n"

    def to_text(self) -> str:
        lines = [f"파생본: {'게시됨' if self.outcome == 'published' else '실패 (이전 파생본·포인터 유지, 정본 그대로)'}",
                 f"source_dataset_version {self.source_dataset_version} · projection_schema {self.projection_schema_version}"]
        if self.generated_at:
            lines.append(f"generated_at {self.generated_at}")
        if self.output_dir:
            lines.append(f"위치: {self.output_dir} / {self.zip_name} (sha256 {self.zip_sha256})")
        if self.counts:
            lines.append("요약: " + ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items())))
        if self.message:
            lines.append(self.message)
        for f in self.findings:
            if f["level"] in ("error", "warn"):
                lines.append(f"  [{f['level']}] {f['code']}: {f['message']}")
        lines.append("파생본 게시는 백업 완료를 뜻하지 않는다.")
        return "\n".join(lines) + "\n"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _asset_to_seed(a: dict) -> dict:
    d = {"asset_id": a["asset_id"], "label": a["label"]}
    if a["lon"] is not None:
        d["location_point"] = [a["lon"], a["lat"]]
    if a["address"] is not None:
        d["address"] = a["address"]
    d.update({"data_mode": a["data_mode"], "created_at": a["seed_created_at"], "notes": a["notes"]})
    return d


def _read_snapshot(db: Db) -> dict:
    """고정 버전의 일관된 읽기. 단일 작성자 잠금(BEGIN IMMEDIATE) 안에서 전부 읽는다."""
    with db.transaction():
        version = int(db.meta("dataset_version") or 0)
        assets = db.list_assets()
        records = [dict(r) for r in db.conn.execute("SELECT * FROM records ORDER BY observed_at, record_id")]
        atts: dict[str, list[dict]] = {}
        for r in db.conn.execute("SELECT record_id, sha256, mime, bytes, tags_json, taken_at, rel_path FROM attachments ORDER BY record_id, sha256"):
            atts.setdefault(r["record_id"], []).append(dict(r))
        return {"version": version, "study_id": db.meta("study_id"), "data_mode": db.data_mode, "assets": assets, "records": records, "attachments": atts}


def _summaries(records: list[dict]) -> dict[str, dict]:
    """물건별 요약. 정정으로 대체된 기록(다른 기록의 supersedes_id 대상)은 최신 판단에서 제외한다."""
    superseded = {r["supersedes_id"] for r in records if r["supersedes_id"]}
    out: dict[str, dict] = {}
    for r in records:
        s = out.setdefault(r["subject_id"], {"observation_count": 0, "last_observed_at": None, "last_change_status": None})
        s["observation_count"] += 1
        if r["record_id"] in superseded:
            continue
        key = (r["observed_at"][:10], r["observed_at"])
        if s["last_observed_at"] is None or key >= (s["last_observed_at"][:10], s["last_observed_at"]):
            s["last_observed_at"] = r["observed_at"]
            s["last_change_status"] = json.loads(r["payload_json"]).get("change_status")
    return out


def _generate(snapshot: dict, out: Path, *, photos: bool, data_home: Path, run_id: str, generated_at: str) -> dict:
    """임시 폴더에 전량 생성하고 manifest 를 돌려준다."""
    assets, records, atts = snapshot["assets"], snapshot["records"], snapshot["attachments"]
    if not assets:
        raise ProjectionError("no_assets", "정본에 물건이 없다. 빈 파생본은 만들지 않는다")
    seed = [_asset_to_seed(a) for a in assets]
    errs = schema_errors("assets_seed.schema.json", seed)
    if errs:
        raise ProjectionError("seed_schema", "물건 목록이 시드 스키마에 맞지 않는다: " + "; ".join(errs[:3]))
    files: dict[str, bytes] = {SEED_FILE: (json.dumps(seed, ensure_ascii=False, indent=2) + "\n").encode("utf-8")}

    summaries = _summaries(records)
    features, unlocated = [], []
    for a in assets:
        props = {"asset_id": a["asset_id"], "label": a["label"], "data_mode": a["data_mode"], "tracking_status": a["tracking_status"],
                 "resolution_status": a["resolution_status"], "address": a["address"],
                 **summaries.get(a["asset_id"], {"observation_count": 0, "last_observed_at": None, "last_change_status": None})}
        if a["lon"] is None:
            unlocated.append(a["asset_id"])
            continue
        features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [a["lon"], a["lat"]]}, "properties": props})
    geojson = {"type": "FeatureCollection", "source_dataset_version": snapshot["version"], "projection_schema_version": PROJECTION_SCHEMA_VERSION,
               "generated_at": generated_at, "data_mode": snapshot["data_mode"], "unlocated_asset_ids": sorted(unlocated), "features": features}
    files[GEOJSON_FILE] = (json.dumps(geojson, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")

    photo_files: dict[str, str] = {}  # rel path in package → sha
    lines: list[bytes] = []
    n_atts = 0
    for r in records:
        rec_atts = []
        for at in atts.get(r["record_id"], []):
            n_atts += 1
            ext = at["rel_path"].rsplit(".", 1)[-1]
            entry = {"sha256": at["sha256"], "mime": at["mime"], "bytes": at["bytes"], "tags": json.loads(at["tags_json"]), "taken_at": at["taken_at"]}
            if photos:
                pkg_path = f"photos/{at['sha256']}.{ext}"
                entry["path"] = pkg_path
                if pkg_path not in photo_files:
                    src = data_home / at["rel_path"]
                    if not src.is_file():
                        raise ProjectionError("photo_missing", f"{at['rel_path']} 가 사진 보관소에 없다")
                    data = src.read_bytes()
                    if _sha256(data) != at["sha256"]:
                        raise ProjectionError("photo_hash_mismatch", f"{at['rel_path']} 의 해시가 정본과 다르다")
                    files[pkg_path] = data
                    photo_files[pkg_path] = at["sha256"]
            rec_atts.append(entry)
        line = {"record_id": r["record_id"], "asset_id": r["subject_id"], "record_type": r["record_type"], "source_kind": r["source_kind"],
                "schema_version": r["schema_version"], "observed_at": r["observed_at"], "observed_at_precision": r["observed_at_precision"],
                "device_created_at": r["device_created_at"], "collected_at": r["collected_at"], "recorded_at": r["recorded_at"],
                "supersedes_id": r["supersedes_id"], "payload": json.loads(r["payload_json"]), "attachments": rec_atts}
        lines.append((_canon(line) + "\n").encode("utf-8"))
    files[RECORDS_FILE] = b"".join(lines)

    out.mkdir(parents=True, exist_ok=False)
    for name, data in files.items():
        p = out / name
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    manifest = {
        "format": "j5view", "projection_schema_version": PROJECTION_SCHEMA_VERSION, "study_id": snapshot["study_id"], "data_mode": snapshot["data_mode"],
        "source_dataset_version": snapshot["version"], "generated_at": generated_at, "run_id": run_id,
        "scope_ids": sorted(a["asset_id"] for a in assets),
        "counts": {"assets": len(assets), "located": len(features), "records": len(records), "attachments": n_atts, "photos": len(photo_files)},
        "files": [{"path": name, "bytes": len(data), "sha256": _sha256(data)} for name, data in sorted(files.items())],
    }
    errs = schema_errors("view_manifest.schema.json", manifest)
    if errs:
        raise ProjectionError("manifest_schema", "; ".join(errs[:3]))
    (out / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify_projection_dir(out: Path, *, expected_version: int | None = None, expected_counts: dict | None = None) -> dict:
    """게시 전 검증(데이터 사전 §4): 행수·참조·필수값·버전·파일 해시. 파일을 디스크에서 다시 읽는다. 통과하면 manifest 를 돌려준다."""
    manifest = json.loads((out / MANIFEST).read_text(encoding="utf-8"))
    errs = schema_errors("view_manifest.schema.json", manifest)
    if errs:
        raise ProjectionError("verify_manifest_schema", "; ".join(errs[:3]))
    if expected_version is not None and manifest["source_dataset_version"] != expected_version:
        raise ProjectionError("verify_version", f"manifest 버전 {manifest['source_dataset_version']} ≠ 정본 {expected_version}")
    listed = {f["path"] for f in manifest["files"]}
    for f in manifest["files"]:
        p = out / f["path"]
        if not p.is_file():
            raise ProjectionError("verify_file_missing", f["path"])
        data = p.read_bytes()
        if len(data) != f["bytes"] or _sha256(data) != f["sha256"]:
            raise ProjectionError("verify_hash", f"{f['path']} 의 크기·해시가 manifest 와 다르다")
    # 게시 폴더에는 manifest 와 ZIP(산출물 묶음)도 함께 있다. 둘은 파일 목록 대상이 아니다.
    on_disk = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file() and not p.name.endswith(".j5view.zip")} - {MANIFEST}
    if on_disk != listed:
        raise ProjectionError("verify_extra_or_missing", f"manifest 밖 파일 또는 누락: {sorted(on_disk ^ listed)}")
    seed = json.loads((out / SEED_FILE).read_text(encoding="utf-8"))
    if schema_errors("assets_seed.schema.json", seed):
        raise ProjectionError("verify_seed_schema", "assets.seed.json 이 시드 스키마에 맞지 않는다")
    ids = [a["asset_id"] for a in seed]
    if sorted(ids) != manifest["scope_ids"] or len(ids) != manifest["counts"]["assets"]:
        raise ProjectionError("verify_scope", "assets.seed.json 의 물건이 scope_ids·counts 와 다르다")
    geo = json.loads((out / GEOJSON_FILE).read_text(encoding="utf-8"))
    if geo.get("source_dataset_version") != manifest["source_dataset_version"] or geo.get("projection_schema_version") != manifest["projection_schema_version"]:
        raise ProjectionError("verify_version", "assets.geojson 의 버전이 manifest 와 다르다")
    feat_ids = [f["properties"]["asset_id"] for f in geo["features"]]
    if len(feat_ids) != manifest["counts"]["located"] or len(feat_ids) + len(geo["unlocated_asset_ids"]) != len(ids) or not set(feat_ids) <= set(ids):
        raise ProjectionError("verify_geojson", "assets.geojson 의 점·미배치 목록이 물건 목록과 맞지 않는다")
    raw = (out / RECORDS_FILE).read_bytes()
    rows = [json.loads(l) for l in raw.split(b"\n") if l]
    if len(rows) != manifest["counts"]["records"]:
        raise ProjectionError("verify_records_count", f"records.jsonl {len(rows)}행, manifest {manifest['counts']['records']}")
    n_atts = 0
    for row in rows:
        for key in ("record_id", "asset_id", "record_type", "observed_at", "observed_at_precision", "recorded_at", "payload", "attachments"):
            if key not in row:
                raise ProjectionError("verify_record_required", f"{row.get('record_id')} 에 {key} 없음")
        if row["asset_id"] not in ids:
            raise ProjectionError("verify_record_ref", f"{row['record_id']} 의 asset_id 가 물건 목록에 없다")
        for at in row["attachments"]:
            n_atts += 1
            if "path" in at and at["path"] not in listed:
                raise ProjectionError("verify_attachment_ref", f"{at['path']} 가 패키지에 없다")
    if n_atts != manifest["counts"]["attachments"]:
        raise ProjectionError("verify_attachments_count", "첨부 수가 manifest 와 다르다")
    if expected_counts is not None:
        for k in ("assets", "records", "attachments"):
            if manifest["counts"][k] != expected_counts[k]:
                raise ProjectionError("verify_counts", f"{k}: 파생본 {manifest['counts'][k]}, 정본 {expected_counts[k]}")
    return manifest


def _write_zip(out: Path, manifest: dict, zip_name: str) -> str:
    """폴더의 파일을 ZIP 으로 묶고(결정적 시각), 항목을 다시 읽어 manifest 해시와 대조한다. ZIP 의 sha256 을 돌려준다."""
    gen = datetime.strptime(manifest["generated_at"], "%Y-%m-%dT%H:%M:%SZ")
    stamp = (gen.year, gen.month, gen.day, gen.hour, gen.minute, gen.second)
    zpath = out / zip_name
    names = [MANIFEST] + [f["path"] for f in manifest["files"]]
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, (out / name).read_bytes())
    expected = {f["path"]: f["sha256"] for f in manifest["files"]}
    with zipfile.ZipFile(zpath, "r") as zf:
        if zf.testzip() is not None:
            raise ProjectionError("zip_corrupt", "ZIP 항목 손상")
        got = {n: _sha256(zf.read(n)) for n in zf.namelist() if n != MANIFEST}
        if got != expected:
            raise ProjectionError("zip_hash", "ZIP 항목 해시가 manifest 와 다르다")
        if json.loads(zf.read(MANIFEST).decode("utf-8")) != manifest:
            raise ProjectionError("zip_manifest", "ZIP 안 manifest 가 다르다")
    return _sha256(zpath.read_bytes())


def read_latest(data_home: Path) -> dict | None:
    p = Path(data_home) / PROJECTIONS_DIR / LATEST_POINTER
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _record_run(db: Db, run_id: str, r: ProjectionResult, status: str, started: str, snapshot: dict | None) -> None:
    with db.transaction():
        db.conn.execute(
            "INSERT INTO projection_runs (run_id, source_dataset_version, projection_schema_version, data_mode, status, output_dir, zip_name, zip_sha256,"
            " scope_count, record_count, started_at, finished_at, message, report_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, r.source_dataset_version, r.projection_schema_version, (snapshot or {}).get("data_mode") or db.data_mode, status,
             r.output_dir, r.zip_name, r.zip_sha256, r.counts.get("assets", 0), r.counts.get("records", 0), started, db.now(), r.message or None,
             json.dumps({"findings": r.findings, "counts": r.counts}, ensure_ascii=False, sort_keys=True)))


def build_projection(db: Db, data_home: Path, *, photos: bool = False) -> ProjectionResult:
    data_home = Path(data_home)
    started = db.now()
    run_id = str(uuid.uuid4())
    result = ProjectionResult(run_id=run_id, generated_at=started)
    base = data_home / PROJECTIONS_DIR
    tmp = base / f".tmp-{run_id[:8]}"
    snapshot: dict | None = None
    try:
        snapshot = _read_snapshot(db)
        result.source_dataset_version = snapshot["version"]
        manifest = _generate(snapshot, tmp, photos=photos, data_home=data_home, run_id=run_id, generated_at=started)
        expected = {"assets": len(snapshot["assets"]), "records": len(snapshot["records"]), "attachments": sum(len(v) for v in snapshot["attachments"].values())}
        verify_projection_dir(tmp, expected_version=snapshot["version"], expected_counts=expected)
        stamp = started.replace("-", "").replace(":", "").replace("T", "-").rstrip("Z")
        zip_name = f"{_safe(snapshot['study_id'])}-ds{snapshot['version']}-{stamp}.j5view.zip"
        zip_sha = _write_zip(tmp, manifest, zip_name)
        # 정본이 생성 중에 바뀌지 않았는지 재확인 (서로 다른 정본 버전이 섞인 산출물은 게시하지 않는다)
        if int(db.meta("dataset_version") or 0) != snapshot["version"]:
            raise ProjectionError("version_changed", "생성 중 정본 dataset_version 이 바뀌었다. 다시 생성한다")
        final = base / f"ds{snapshot['version']}-{run_id[:8]}"
        os.rename(tmp, final)
        result.output_dir = final.relative_to(data_home).as_posix()
        result.zip_name, result.zip_sha256, result.counts = zip_name, zip_sha, manifest["counts"]
        pointer = {"source_dataset_version": snapshot["version"], "projection_schema_version": PROJECTION_SCHEMA_VERSION, "generated_at": started,
                   "run_id": run_id, "study_id": snapshot["study_id"], "data_mode": snapshot["data_mode"], "dir": result.output_dir,
                   "zip": zip_name, "zip_sha256": zip_sha}
        ptmp = base / (LATEST_POINTER + ".tmp")
        with open(ptmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(pointer, ensure_ascii=False, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(ptmp, base / LATEST_POINTER)
        result.outcome = "published"
        result.message = f"파생본 게시 (정본 v{snapshot['version']}). 최신본 포인터 교체"
        _record_run(db, run_id, result, "published", started, snapshot)
        return result
    except (ProjectionError, OSError, ValueError, KeyError) as e:
        code = getattr(e, "code", type(e).__name__)
        msg = getattr(e, "message", str(e))
        result.outcome = "failed"
        result.output_dir = result.zip_name = result.zip_sha256 = None
        result.add("error", code, msg)
        failed_dir = base / f"failed-{run_id[:8]}"
        if tmp.exists():
            try:
                os.rename(tmp, failed_dir)
                result.add("warn", "partial_kept", f"부분 산출물을 {failed_dir.relative_to(data_home).as_posix()} 에 남겼다 (조회자료 아님, 자동 삭제하지 않음)")
            except OSError:
                pass
        prev = read_latest(data_home)
        keep = f" 이전 파생본 v{prev['source_dataset_version']}({prev['generated_at']}) 과 포인터는 그대로다." if prev else " 게시된 파생본이 없다 (지도 자료 없음·재생성 필요)."
        result.message = f"파생본 재생성 실패 [{code}]: {msg}. 정본은 반영된 그대로다.{keep}"
        try:
            _record_run(db, run_id, result, "failed", started, snapshot)
        except Exception as e2:  # noqa: BLE001
            result.add("warn", "run_log_failed", f"실행 기록을 남기지 못했다: {type(e2).__name__}: {e2}")
        return result


def _safe(study_id: str) -> str:
    s = "".join(c if c.isalnum() or c in "-_" else "-" for c in (study_id or "study")).strip("-")
    return (s or "study")[:40]


def projection_status(db: Db, data_home: Path | None) -> dict:
    """정본 버전과 게시된 파생본 버전의 차이, 마지막 성공·실패 시각. 화면 표시용."""
    current = int(db.meta("dataset_version") or 0)
    pub = db.conn.execute("SELECT source_dataset_version, finished_at, output_dir, zip_name FROM projection_runs WHERE status = 'published' ORDER BY finished_at DESC, rowid DESC LIMIT 1").fetchone()
    fail = db.conn.execute("SELECT finished_at, message FROM projection_runs WHERE status = 'failed' ORDER BY finished_at DESC, rowid DESC LIMIT 1").fetchone()
    pointer = read_latest(data_home) if data_home else None
    st = {"dataset_version": current, "published_version": pub[0] if pub else None, "published_at": pub[1] if pub else None,
          "published_dir": pub[2] if pub else None, "published_zip": pub[3] if pub else None,
          "last_failed_at": fail[0] if fail else None, "last_failed_message": fail[1] if fail else None,
          "pointer_version": pointer["source_dataset_version"] if pointer else None}
    st["stale"] = pub is None or pub[0] != current
    st["state"] = "없음 (재생성 필요)" if pub is None else ("최신" if not st["stale"] else f"구본 (정본 v{current}, 파생본 v{pub[0]})")
    return st


def copy_latest(db: Db, data_home: Path, dest_dir: Path, *, allow_stale: bool = False) -> dict:
    """게시된 최신 파생본 ZIP 의 독립 사본. 정본보다 오래된 파생본은 --allow-stale 없이는 내보내지 않는다 (데이터 사전 §4)."""
    st = projection_status(db, data_home)
    pointer = read_latest(data_home)
    if pointer is None:
        raise ProjectionError("no_projection", "게시된 파생본이 없다. 먼저 `j5 db project` 로 만든다")
    if st["stale"] and not allow_stale:
        raise ProjectionError("stale", f"파생본이 구본이다 ({st['state']}). 최신용 내보내기를 막는다. 다시 생성하거나 구본 보존용이면 --allow-stale 을 준다")
    src = Path(data_home) / pointer["dir"] / pointer["zip"]
    if _sha256(src.read_bytes()) != pointer["zip_sha256"]:
        raise ProjectionError("zip_hash", "게시된 ZIP 의 해시가 포인터와 다르다. 다시 생성한다")
    try:
        r = copy_package(src, Path(dest_dir))
    except PreserveError as e:
        raise ProjectionError(e.code, e.message) from e
    return {"dest": str(r.dest), "sha256": r.sha256, "bytes": r.bytes, "stale": st["stale"], "state": st["state"],
            "source_dataset_version": pointer["source_dataset_version"], "generated_at": pointer["generated_at"]}
