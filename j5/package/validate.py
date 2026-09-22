"""패키지 검사 (J5-008). 데이터 사전 §3.2 규칙 1·3, §3.3 한도를 확인한다.

0단계(컨테이너)는 첫 실패에서 멈추고, 이후 단계는 발견한 문제를 모두 모은다.
이벤트 해시 규약: observations.jsonl 의 행 바이트에 끝의 LF 1개를 포함해 sha256 한다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from j5 import SUPPORTED_PACKAGE_SCHEMA_VERSIONS
from j5.package.limits import DEFAULT_LIMITS, Limits
from j5.package.reader import ContainerError, open_package
from j5.package.report import Report
from j5.schemas_loader import schema_errors

MANIFEST = "manifest.json"
OBSERVATIONS = "observations.jsonl"
EXT_MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def sniff_image(head: bytes) -> str | None:
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def _no_dup_pairs(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError(f"중복 키 {k!r}")
        d[k] = v
    return d


def event_hash(line: bytes) -> str:
    """행 바이트(LF 포함)의 sha256."""
    return hashlib.sha256(line).hexdigest()


def inspect_package(path: Path, *, seed: list[dict] | None = None, study_id: str | None = None,
                    limits: Limits = DEFAULT_LIMITS) -> Report:
    report = Report(source=str(path))
    try:
        src = open_package(path, limits)
    except ContainerError as e:
        report.add("reject", e.code, e.path, e.message)
        return report
    report.kind = src.kind
    with src:
        try:
            _inspect(src, report, seed, study_id, limits)
        except ContainerError as e:
            report.add("reject", e.code, e.path, e.message)
    return report


def _inspect(src, report: Report, seed, study_id, limits: Limits) -> None:
    names = {e.name for e in src.entries()}

    # ---- 1. manifest ----
    manifest: dict | None = None
    files_map: dict[str, dict] = {}
    if MANIFEST not in names:
        report.add("reject", "manifest_missing", MANIFEST, "manifest.json 없음")
    else:
        raw, _ = src.read(MANIFEST, limits.manifest, code="manifest_too_large")
        try:
            manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dup_pairs)
        except (UnicodeDecodeError, ValueError) as e:
            report.add("reject", "manifest_not_json", MANIFEST, f"JSON 해석 실패: {e}")
            manifest = None
        if manifest is not None:
            errs = schema_errors("package_manifest.schema.json", manifest)
            for m in errs:
                report.add("reject", "manifest_schema", MANIFEST, m)
            if isinstance(manifest, dict):
                report.package_id = manifest.get("package_id") if isinstance(manifest.get("package_id"), str) else None
                report.study_id = manifest.get("study_id") if isinstance(manifest.get("study_id"), str) else None
                report.data_mode = manifest.get("data_mode") if isinstance(manifest.get("data_mode"), str) else None
                sv = manifest.get("schema_version")
                report.schema_version = sv if isinstance(sv, str) else None
                if isinstance(sv, str) and sv not in SUPPORTED_PACKAGE_SCHEMA_VERSIONS:
                    report.add("reject", "manifest_schema_version_unsupported", MANIFEST,
                               f"지원하지 않는 package_schema 버전 {sv}")
                if study_id is None:
                    report.add("info", "study_id_not_checked", MANIFEST, "study_id를 지정하지 않아 비교하지 않음")
                elif report.study_id != study_id:
                    report.add("reject", "manifest_study_id_mismatch", MANIFEST,
                               f"study_id {report.study_id!r} 가 기대값 {study_id!r} 과 다름")
                if errs:
                    manifest = None
                else:
                    for f in manifest["files"]:
                        if f["path"] in files_map:
                            report.add("reject", "manifest_duplicate_path", f["path"], "manifest에 같은 경로가 두 번 있음")
                        files_map[f["path"]] = f
            else:
                manifest = None

    # ---- 2. 파일 ----
    actual: dict[str, tuple[int, str]] = {}   # name -> (bytes, sha256)
    photo_names = sorted(n for n in names if n.startswith("photos/"))
    if len(photo_names) > limits.max_photos:
        report.add("reject", "photo_count_exceeded", "photos/", f"사진 수 {len(photo_names)}가 한도 {limits.max_photos}를 넘음")
    for name in photo_names:
        data, digest = src.read(name, limits.photo, code="photo_size_exceeded")
        actual[name] = (len(data), digest)
        stem, ext = name[len("photos/"):].rsplit(".", 1)
        if stem != digest:
            report.add("reject", "photo_name_not_content_hash", name, "파일명이 내용 해시와 다름")
        sniffed = sniff_image(data[:16])
        if sniffed is None:
            report.add("reject", "photo_magic_mismatch", name, "JPEG/PNG/WebP 형식이 아님 (HEIC 등 미지원 형식은 변환 후 다시 내보내야 함)")
        elif sniffed != ext:
            report.add("reject", "photo_magic_mismatch", name, f"내용은 {sniffed}인데 확장자는 {ext}")
    obs_raw: bytes | None = None
    if OBSERVATIONS in names:
        obs_raw, digest = src.read(OBSERVATIONS, limits.uncompressed)
        actual[OBSERVATIONS] = (len(obs_raw), digest)
    else:
        report.add("reject", "file_missing_in_package", OBSERVATIONS, "observations.jsonl 없음")

    if manifest is not None:
        for p, f in files_map.items():
            if p not in actual:
                report.add("reject", "file_missing_in_package", p, "manifest에 있으나 패키지에 없음")
                continue
            n, h = actual[p]
            if n != f["bytes"]:
                report.add("reject", "file_bytes_mismatch", p, f"실제 {n} 바이트, manifest {f['bytes']} 바이트")
            if h != f["sha256"]:
                report.add("reject", "file_hash_mismatch", p, "실제 해시가 manifest와 다름")
        for p in actual:
            if p not in files_map:
                report.add("reject", "file_not_in_manifest", p, "패키지에 있으나 manifest에 없음")

    # ---- 3. 이벤트 ----
    events: list[dict] = []
    id_hash: dict[str, str] = {}
    referenced: set[str] = set()
    n_lines = n_dup = n_conflict = 0
    if obs_raw is not None:
        if obs_raw.startswith(b"\xef\xbb\xbf"):
            report.add("reject", "obs_bom", OBSERVATIONS, "UTF-8 BOM 있음")
        if b"\r\n" in obs_raw:
            report.add("reject", "obs_crlf", OBSERVATIONS, "CRLF 줄바꿈. LF만 허용")
        if obs_raw and not obs_raw.endswith(b"\n"):
            report.add("reject", "obs_no_trailing_newline", OBSERVATIONS, "마지막 행에 줄바꿈 없음")
        segments = obs_raw.split(b"\n")
        if segments and segments[-1] == b"":
            segments = segments[:-1]
        for i, seg in enumerate(segments, start=1):
            loc = f"{OBSERVATIONS}:{i}"
            if seg == b"":
                report.add("reject", "obs_empty_line", loc, "빈 행")
                continue
            n_lines += 1
            line = seg + b"\n"
            h = event_hash(line)
            try:
                ev = json.loads(seg.decode("utf-8"), object_pairs_hook=_no_dup_pairs)
            except UnicodeDecodeError as e:
                report.add("reject", "obs_not_utf8", loc, f"UTF-8 아님: {e}")
                continue
            except ValueError as e:
                report.add("reject", "obs_line_not_json", loc, f"JSON 해석 실패: {e}")
                continue
            errs = schema_errors("observation_event.schema.json", ev)
            if errs:
                hint = " (HEIC는 미지원. JPEG/PNG/WebP로 변환 후 다시 내보내야 함)" if b"heic" in seg.lower() else ""
                for m in errs:
                    report.add("reject", "event_schema", loc, m + hint)
                continue
            eid = ev["event_id"]
            if eid in id_hash:
                if id_hash[eid] == h:
                    n_dup += 1
                    report.add("info", "event_duplicate_same_bytes", loc, f"event_id {eid} 가 같은 내용으로 반복됨. 반영기는 건너뜀")
                else:
                    n_conflict += 1
                    report.add("hold", "event_id_conflict", loc, f"event_id {eid} 가 다른 내용으로 반복됨. 전체 입력 보류")
                continue
            id_hash[eid] = h
            events.append(ev)
            if ev["corrects_event_id"] == eid:
                report.add("reject", "event_corrects_self", loc, "자기 자신을 정정하는 이벤트")
            for ref in ev["attachment_refs"]:
                p = ref["path"]
                referenced.add(p)
                ext = p.rsplit(".", 1)[-1]
                if EXT_MIME.get(ext) != ref["mime"]:
                    report.add("reject", "attachment_mime_ext_mismatch", loc, f"{p}: mime {ref['mime']} 와 확장자 불일치")
                if manifest is not None and p not in files_map:
                    report.add("reject", "attachment_not_in_manifest", loc, f"{p}: manifest에 없음")
                if p in actual:
                    n, ah = actual[p]
                    if ah != ref["sha256"]:
                        report.add("reject", "attachment_hash_mismatch", loc, f"{p}: 첨부 해시가 파일과 다름")
                    if n != ref["bytes"]:
                        report.add("reject", "attachment_bytes_mismatch", loc, f"{p}: 첨부 bytes {ref['bytes']}, 실제 {n}")
                elif manifest is None or p in files_map:
                    report.add("reject", "attachment_not_in_manifest", loc, f"{p}: 패키지에 파일 없음")
        for p in photo_names:
            if p not in referenced:
                report.add("warn", "photo_unreferenced", p, "어떤 이벤트도 참조하지 않는 사진")

    # ---- 4. 시드 ----
    ids_in_pkg = set(id_hash)
    if seed is not None:
        seed_ids = {a["asset_id"] for a in seed}
        seed_modes = {a["data_mode"] for a in seed}
        for ev in events:
            if ev["asset_id"] not in seed_ids:
                report.add("reject", "asset_not_in_seed", ev["event_id"], f"asset_id {ev['asset_id']} 가 시드에 없음")
        if report.data_mode and seed_modes and report.data_mode not in seed_modes:
            report.add("warn", "data_mode_differs_from_seed", MANIFEST,
                       f"패키지 data_mode {report.data_mode}, 시드 {sorted(seed_modes)}")
    for ev in events:
        c = ev["corrects_event_id"]
        if c and c != ev["event_id"]:
            if c in ids_in_pkg:
                report.add("info", "corrects_target_in_package", ev["event_id"], f"{c} 를 정정 (같은 패키지)")
            else:
                report.add("info", "corrects_target_unknown", ev["event_id"], f"{c} 를 정정 (패키지 밖. 정본에서 확인 필요)")

    report.counts = {
        "lines": n_lines,
        "events": len(events),
        "duplicates": n_dup,
        "conflicts": n_conflict,
        "photos": len(photo_names),
        "photos_referenced": len(referenced & set(photo_names)),
    }
