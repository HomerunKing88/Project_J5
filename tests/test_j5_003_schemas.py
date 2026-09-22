"""J5-003: 관측·manifest·assets.seed.json 스키마와 가상 fixture 검증.

스키마 통과는 실제 기기·API·법률 검증이 아니다. 중복·충돌·누락의 처리 자체는 R1b(J5-010)에서 구현하며,
여기서는 fixture가 각 경우를 실제로 담고 있는지와 스키마·연결 규칙이 의도대로 걸러내는지만 확인한다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

REPO = Path(__file__).resolve().parent.parent
SCHEMAS = REPO / "schemas"
FIXTURES = REPO / "tests" / "fixtures"
PACKAGES = FIXTURES / "packages"


def validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


SEED_V = validator("assets_seed.schema.json")
EVENT_V = validator("observation_event.schema.json")
MANIFEST_V = validator("package_manifest.schema.json")


def errors(v: Draft202012Validator, doc) -> list[str]:
    return [e.message for e in v.iter_errors(doc)]


def load_seed() -> list[dict]:
    return json.loads((FIXTURES / "assets.seed.synthetic.json").read_text(encoding="utf-8"))


def read_events(pkg: Path) -> list[tuple[bytes, dict]]:
    raw = (pkg / "observations.jsonl").read_bytes()
    lines = [ln for ln in raw.split(b"\n") if ln]
    return [(ln + b"\n", json.loads(ln.decode("utf-8"))) for ln in lines]


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---- assets.seed.json ----

def test_seed_fixture_is_valid():
    assert errors(SEED_V, load_seed()) == []


def test_seed_has_five_synthetic_assets_with_unique_ids():
    seed = load_seed()
    assert len(seed) == 5
    assert len({a["asset_id"] for a in seed}) == 5
    assert all(a["data_mode"] == "synthetic" for a in seed)


@pytest.mark.parametrize("mutate,reason", [
    (lambda a: a.pop("location_point") if "location_point" in a else a.pop("address"), "위치점·주소 모두 없음"),
    (lambda a: a.update(asset_id="not-a-uuid"), "asset_id UUID 아님"),
    (lambda a: a.update(data_mode="real"), "data_mode 허용값 아님"),
    (lambda a: a.update(extra_field=1), "임의 필드 추가"),
    (lambda a: a.pop("notes"), "notes 누락(null 허용, 생략 불가)"),
    (lambda a: a.update(location_point=[37.57, 126.99, 0]), "좌표 원소 수 초과"),
])
def test_seed_rejects_invalid_asset(mutate, reason):
    a = dict(load_seed()[0])
    mutate(a)
    assert errors(SEED_V, [a]), reason


# ---- 정상 패키지 ----

def test_valid_package_manifest_and_events_validate():
    pkg = PACKAGES / "valid"
    manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
    assert errors(MANIFEST_V, manifest) == []
    for _, ev in read_events(pkg):
        assert errors(EVENT_V, ev) == [], ev["event_id"]


def test_valid_package_file_hashes_and_sizes_match():
    pkg = PACKAGES / "valid"
    manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
    for f in manifest["files"]:
        data = (pkg / f["path"]).read_bytes()
        assert len(data) == f["bytes"], f["path"]
        assert sha(data) == f["sha256"], f["path"]
    # 사진 파일명은 내용 해시다
    for p in (pkg / "photos").iterdir():
        assert p.stem == sha(p.read_bytes())


def test_valid_package_attachments_exist_and_assets_link_to_seed():
    pkg = PACKAGES / "valid"
    manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
    listed = {f["path"]: f["sha256"] for f in manifest["files"]}
    seed_ids = {a["asset_id"] for a in load_seed()}
    for _, ev in read_events(pkg):
        assert ev["asset_id"] in seed_ids, "관측의 asset_id는 시드의 asset_id와 같아야 한다"
        for ref in ev["attachment_refs"]:
            assert ref["path"] in listed and listed[ref["path"]] == ref["sha256"]
            assert (pkg / ref["path"]).exists()


def test_event_hash_is_sha256_of_fixed_line_bytes():
    """이벤트 해시는 행 바이트로 계산한다. 재직렬화가 아니라 파일의 바이트가 기준이다."""
    pkg = PACKAGES / "valid"
    for line, ev in read_events(pkg):
        h = sha(line)
        assert len(h) == 64
        reserialized = (json.dumps(ev, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        assert sha(reserialized) != h, "다른 직렬화는 다른 바이트다. 고정 바이트를 보관해야 한다"


def test_valid_package_event_ids_are_unique():
    ids = [ev["event_id"] for _, ev in read_events(PACKAGES / "valid")]
    assert len(ids) == len(set(ids))


# ---- 중복·손상·미지원 패키지 ----

def test_duplicate_same_content_has_identical_repeated_line():
    lines = [ln for ln, _ in read_events(PACKAGES / "duplicate_same_content")]
    assert lines[0] == lines[1]
    assert sha(lines[0]) == sha(lines[1]), "같은 ID·같은 해시 → 건너뛰기 대상"


def test_duplicate_conflict_has_same_id_different_bytes():
    (l1, e1), (l2, e2) = read_events(PACKAGES / "duplicate_conflict")
    assert e1["event_id"] == e2["event_id"]
    assert l1 != l2 and sha(l1) != sha(l2), "같은 ID·다른 해시 → 전체 보류 대상"
    assert errors(EVENT_V, e1) == [] and errors(EVENT_V, e2) == [], "각 행은 개별로는 유효하다"


def test_corrupt_hash_mismatch_is_detected_by_content_check_not_schema():
    pkg = PACKAGES / "corrupt_hash_mismatch"
    manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
    assert errors(MANIFEST_V, manifest) == [], "형식은 정상이다"
    photo = next(f for f in manifest["files"] if f["path"].startswith("photos/"))
    assert sha((pkg / photo["path"]).read_bytes()) != photo["sha256"]


def test_corrupt_path_traversal_is_rejected_by_manifest_schema():
    manifest = json.loads((PACKAGES / "corrupt_path_traversal" / "manifest.json").read_text(encoding="utf-8"))
    assert any("../escape.png" in m for m in errors(MANIFEST_V, manifest))


@pytest.mark.parametrize("bad_path", ["/abs/photos/x.png", "photos/../x.png", "photos/notahash.png",
                                      "observations.jsonl.bak", "photos/" + "a" * 64 + ".heic"])
def test_manifest_schema_rejects_bad_paths(bad_path):
    manifest = json.loads((PACKAGES / "valid" / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"].append({"path": bad_path, "bytes": 1, "sha256": "0" * 64})
    assert errors(MANIFEST_V, manifest)


def test_missing_attachment_is_detected_by_reference_check():
    pkg = PACKAGES / "missing_attachment"
    manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
    listed = {f["path"] for f in manifest["files"]}
    refs = [r["path"] for _, ev in read_events(pkg) for r in ev["attachment_refs"]]
    assert refs and all(r not in listed and not (pkg / r).exists() for r in refs)


def test_unsupported_heic_is_rejected_by_event_schema():
    (_, ev), = read_events(PACKAGES / "unsupported_heic")
    msgs = errors(EVENT_V, ev)
    assert any("image/heic" in m for m in msgs)
    assert any(".heic" in m for m in msgs)


def test_unknown_asset_passes_schema_but_fails_seed_link():
    (_, ev), = read_events(PACKAGES / "unknown_asset")
    assert errors(EVENT_V, ev) == []
    assert ev["asset_id"] not in {a["asset_id"] for a in load_seed()}


def test_change_observed_requires_photo_or_note():
    (_, ev), = read_events(PACKAGES / "invalid_change_without_evidence")
    assert errors(EVENT_V, ev)
    ev["payload"]["note"] = "설명만 있음"
    assert errors(EVENT_V, ev) == []


# ---- 이벤트 스키마 세부 규칙 ----

def base_event() -> dict:
    return read_events(PACKAGES / "valid")[0][1]


@pytest.mark.parametrize("mutate,reason", [
    (lambda e: e.update(observed_at="2026-09-22T10:15:00"), "시간대 없는 시각"),
    (lambda e: e.update(observed_at="2026-09-22", observed_at_precision="datetime"), "정밀도와 값 불일치"),
    (lambda e: e.update(observed_at="2026-09-22T00:00:00+09:00", observed_at_precision="date"), "날짜 정밀도인데 시각을 만들어냄"),
    (lambda e: e.update(record_type="price_update"), "R1 밖 기록 종류"),
    (lambda e: e.update(sql="UPDATE assets"), "임의 필드·명령"),
    (lambda e: e.pop("route_version_id"), "선택 필드도 null로 명시해야 함"),
    (lambda e: e["attachment_refs"][0].update(tags=["selfie"]), "허용되지 않은 사진 태그"),
    (lambda e: e["attachment_refs"][0].update(bytes=20000001), "사진 20MB 초과"),
    (lambda e: e["payload"].update(change_status="vacant"), "관측 상태 허용값 아님"),
])
def test_event_schema_rejects(mutate, reason):
    ev = base_event()
    mutate(ev)
    assert errors(EVENT_V, ev), reason


def test_event_schema_accepts_date_precision_and_correction():
    ev = base_event()
    ev.update(observed_at="2026-09-21", observed_at_precision="date",
              event_id="1a2b3c4d-0001-4000-8000-0000000000aa",
              corrects_event_id=base_event()["event_id"])
    assert errors(EVENT_V, ev) == []
