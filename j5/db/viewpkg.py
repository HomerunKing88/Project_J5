"""구파생본(조회 패키지 .j5view.zip) 열람·검증 (J5-017C). 릴리스 계획 §8 R6 ("구패키지 열람"), 데이터 사전 §4·§12 ("구본 보존용 내보내기는 별도 명령과 구버전 표시").

inspect_view(path, db=None): 게시된 폴더 또는 `.j5view.zip` 을 정본 없이 읽어 검증하고 내용을 요약한다.
    ZIP 은 항목 이름(허용 목록·경로 탈출·백슬래시·제어문자)·암호화·심볼릭 링크·중복·항목 수·선언 크기·실제 읽은 바이트 한도(j5.package.limits)를
    확인하며 임시 폴더에 풀고, 파생본 검증기(verify_projection_dir: manifest 스키마·파일 해시·행수·참조·버전)를 그대로 돌린다.
    요약: 형식·projection_schema 버전(도구가 아는 버전인지)·study_id·data_mode·정본 버전·생성 시각·범위(물건 수)·기록 수·종류별 기록 수·관측일 범위·사진 포함 여부·파일 목록.
    정본(--db)을 주면 study_id·data_mode 가 같은지, 정본 dataset_version 과 비교해 `최신 / 구본 (정본 vN, 파생본 vM) / 정본보다 새로움 / 다른 정본` 을 표시한다.
    정본을 주지 않으면 최신 여부는 '미확인' 이다(파일만으로는 알 수 없다).
입력 파일을 수정·삭제하지 않는다. 열람 결과는 게시·최신 여부·백업을 뜻하지 않는다.
"""

from __future__ import annotations

import json
import re
import sqlite3
import stat
import tempfile
import zipfile
from pathlib import Path

from j5.db.projection import MANIFEST, PROJECTION_SCHEMA_VERSION, ProjectionError, RECORDS_FILE, verify_projection_dir
from j5.db.store import Db
from j5.package.limits import DEFAULT_LIMITS, Limits
from j5.package.reader import ContainerError, sha256_stream

SUPPORTED_PROJECTION_SCHEMA_VERSIONS = frozenset({PROJECTION_SCHEMA_VERSION})
# 파생본은 누적 자료(전체 물건·기록·사진 이력)라 관측 패키지 한 개의 한도(§3.3, 100MB)보다 크다. 사진 한 장 한도는 같고 나머지는 누적을 감안해 넓게 둔다.
# 값은 도구가 만든 파생본을 도구가 거절하지 않도록 정한 안전 상한이며(worklog J5-017), 실제 크기를 기록한 뒤 조정한다.
VIEW_LIMITS = Limits(compressed=4_000_000_000, uncompressed=8_000_000_000, photo=DEFAULT_LIMITS.photo, manifest=500_000_000, max_photos=50_000)
VIEW_ENTRY = re.compile(r"^(manifest\.json|assets\.seed\.json|assets\.geojson|records\.jsonl|parcels\.geojson|transactions\.json|photos/[0-9a-f]{64}\.(jpg|png|webp))$")
EXIT_BY_VERDICT = {"ok": 0, "invalid": 1}


class ViewError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _check_view_entry(name: str) -> None:
    """항목 이름 안전성. 공통 검사(절대경로·백슬래시·제어문자·경로 탈출)는 관측 패키지 검사기와 같고 허용 목록만 파생본용이다."""
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise ContainerError("entry_absolute", name, "절대경로 항목")
    if "\\" in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ContainerError("entry_bad_name", name, "백슬래시 또는 제어문자 포함 이름")
    if ".." in name.split("/") or "." in name.split("/"):
        raise ContainerError("entry_traversal", name, "경로 탈출 항목")
    if name.endswith("/"):
        if name != "photos/":
            raise ContainerError("entry_dir_not_photos", name, "photos/ 외 디렉터리 항목")
        return
    if not VIEW_ENTRY.match(name):
        raise ContainerError("entry_not_allowed", name, "파생본에 허용되지 않은 파일 이름")


def extract_view_zip(zpath: Path, dest: Path, limits: Limits = VIEW_LIMITS) -> list[str]:
    """`.j5view.zip` 을 안전하게 임시 폴더에 푼다. 헤더의 크기를 믿지 않고 실제 읽은 바이트로 한도를 강제한다. 입력은 바꾸지 않는다."""
    size = zpath.stat().st_size
    if size > limits.compressed:
        raise ContainerError("zip_compressed_size", str(zpath), f"ZIP 크기 {size} 바이트가 한도 {limits.compressed}를 넘음")
    try:
        zf = zipfile.ZipFile(zpath, "r")
    except zipfile.BadZipFile as e:
        raise ContainerError("zip_bad_file", str(zpath), f"ZIP 파일이 아니거나 손상됨: {e}") from e
    names: list[str] = []
    with zf:
        infos = zf.infolist()
        if len(infos) > limits.max_entries + 5:
            raise ContainerError("zip_entry_count", str(zpath), f"항목 수 {len(infos)}가 한도를 넘음")
        if sum(i.file_size for i in infos) > limits.uncompressed:
            raise ContainerError("zip_declared_total", str(zpath), "선언된 해제 크기가 한도를 넘음")
        seen: set[str] = set()
        total = 0
        for info in infos:
            name = info.filename
            _check_view_entry(name)
            if info.flag_bits & 0x1:
                raise ContainerError("entry_encrypted", name, "암호화된 항목")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ContainerError("entry_compression", name, f"지원하지 않는 압축 방식 {info.compress_type}")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ContainerError("entry_symlink", name, "심볼릭 링크 항목")
            if name in seen:
                raise ContainerError("entry_duplicate_name", name, "중복 항목 이름")
            seen.add(name)
            if name.endswith("/"):
                continue
            cap = limits.photo if name.startswith("photos/") else limits.manifest
            try:
                with zf.open(name, "r") as f:
                    data, _ = sha256_stream(f, cap, limits.chunk, name)
            except zipfile.BadZipFile as e:
                raise ContainerError("zip_crc", name, f"항목 손상(CRC·길이 불일치): {e}") from e
            total += len(data)
            if total > limits.uncompressed:
                raise ContainerError("total_size_exceeded", name, "전체 해제 크기 한도 초과")
            out = dest / name
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "xb") as fo:
                fo.write(data)
            names.append(name)
    return sorted(names)


def _summarize(out: Path, manifest: dict) -> dict:
    rows = [json.loads(l) for l in (out / RECORDS_FILE).read_bytes().split(b"\n") if l]
    by_type: dict[str, int] = {}
    observed = []
    photos_with_path = 0
    for r in rows:
        # 파생본 검증기는 키 존재만 보므로 값의 형을 여기서 확인한다. 형이 다르면 검증 실패(트레이스백 아님)
        if not isinstance(r.get("record_type"), str) or not isinstance(r.get("observed_at"), str) or len(r["observed_at"]) < 10 or not isinstance(r.get("attachments"), list):
            raise ViewError("record_field_type", f"{r.get('record_id')}: record_type·observed_at·attachments 의 값 형이 맞지 않는다")
        if any(not isinstance(a, dict) for a in r["attachments"]):
            raise ViewError("record_field_type", f"{r.get('record_id')}: attachments 항목이 객체가 아니다")
        by_type[r["record_type"]] = by_type.get(r["record_type"], 0) + 1
        observed.append(r["observed_at"][:10])
        photos_with_path += sum(1 for a in r["attachments"] if "path" in a)
    return {"records_by_type": dict(sorted(by_type.items())), "observed_from": min(observed) if observed else None, "observed_to": max(observed) if observed else None,
            "photos_included": manifest["counts"].get("photos", 0) > 0, "attachments_with_photo": photos_with_path}


def _compare(manifest: dict, db: Db | None) -> dict:
    if db is None:
        return {"checked": False, "state": "미확인 (정본을 주지 않아 최신 여부를 알 수 없다)", "stale": None}
    if manifest["study_id"] != db.meta("study_id") or manifest["data_mode"] != db.data_mode:
        return {"checked": True, "state": f"다른 정본의 파생본 (파생본 {manifest['study_id']}/{manifest['data_mode']}, 정본 {db.meta('study_id')}/{db.data_mode})", "stale": None}
    cur = int(db.meta("dataset_version") or 0)
    v = manifest["source_dataset_version"]
    if v == cur:
        return {"checked": True, "state": f"최신 (정본 v{cur})", "stale": False, "dataset_version": cur}
    if v < cur:
        return {"checked": True, "state": f"구본 (정본 v{cur}, 파생본 v{v})", "stale": True, "dataset_version": cur}
    return {"checked": True, "state": f"정본보다 새로움 (정본 v{cur}, 파생본 v{v}). 정본이 복구본이면 파생본을 다시 만든다", "stale": True, "dataset_version": cur}


def inspect_view(path: Path, *, db: Db | None = None, limits: Limits = VIEW_LIMITS) -> dict:
    path = Path(path)
    r: dict = {"source": str(path), "kind": None, "verdict": "invalid", "problem": None, "message": None, "manifest": None, "summary": None, "freshness": None,
               "schema_supported": None, "tool_projection_schema_version": PROJECTION_SCHEMA_VERSION}
    try:
        if path.is_dir():
            r["kind"] = "dir"
            manifest = verify_projection_dir(path)
            r["summary"] = _summarize(path, manifest)
        elif path.is_file():
            r["kind"] = "zip"
            with tempfile.TemporaryDirectory(prefix="j5view-") as td:
                out = Path(td) / "pkg"
                out.mkdir()
                extract_view_zip(path, out, limits)
                if not (out / MANIFEST).is_file():
                    raise ViewError("manifest_missing", "ZIP 에 manifest.json 이 없다")
                manifest = verify_projection_dir(out)
                r["summary"] = _summarize(out, manifest)
        else:
            raise ViewError("not_found", f"파일·폴더가 없다: {path}")
        r["manifest"] = {k: manifest[k] for k in ("format", "projection_schema_version", "study_id", "data_mode", "source_dataset_version", "generated_at", "run_id", "counts")}
        r["manifest"]["scope_count"] = len(manifest["scope_ids"])
        r["manifest"]["files"] = [f["path"] for f in manifest["files"]]
        r["schema_supported"] = manifest["projection_schema_version"] in SUPPORTED_PROJECTION_SCHEMA_VERSIONS
        r["freshness"] = _compare(manifest, db)
        r["verdict"] = "ok"
        r["message"] = "파생본 검증 통과 (파일 해시·행수·참조·버전). 열람 결과는 최신 여부·백업을 뜻하지 않는다"
    except (ProjectionError, ContainerError, ViewError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as e:
        code = getattr(e, "code", type(e).__name__)
        r["problem"] = code
        r["message"] = f"파생본 검증 실패 [{code}]: {getattr(e, 'message', e)}"
    return r


def view_text(r: dict) -> str:
    lines = [f"파생본: {r['source']} ({r['kind'] or '?'})"]
    if r["verdict"] != "ok":
        lines.append(f"판정: invalid - {r['message']}")
        return "\n".join(lines) + "\n"
    m, s, f = r["manifest"], r["summary"], r["freshness"]
    lines.append(f"판정: ok - {r['message']}")
    lines.append(f"projection_schema {m['projection_schema_version']} ({'도구가 아는 버전' if r['schema_supported'] else '도구가 모르는 버전: 도구를 갱신한다'}) · study {m['study_id']} · {m['data_mode']}"
                 f" · 정본 v{m['source_dataset_version']} · 생성 {m['generated_at']}")
    lines.append("요약: " + ", ".join(f"{k}={v}" for k, v in sorted(m["counts"].items())) + f", scope={m['scope_count']}")
    lines.append("기록 종류: " + (", ".join(f"{k} {v}" for k, v in s["records_by_type"].items()) or "없음")
                 + (f" · 관측일 {s['observed_from']} ~ {s['observed_to']}" if s["observed_from"] else ""))
    lines.append(f"사진: {'포함' if s['photos_included'] else '미포함 (참조만)'} · 파일 {len(m['files'])}개")
    lines.append(f"최신 여부: {f['state']}")
    lines.append("폰에는 assets.seed.json 을 '시드 파일 불러오기' 로, parcels.geojson 을 '필지 파일 불러오기' 로 넣는다. 구본이면 구본임을 알고 쓴다.")
    return "\n".join(lines) + "\n"
