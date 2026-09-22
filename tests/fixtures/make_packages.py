"""가상 fixture 생성기 (J5-003).

고정 UUID·시각·1x1 PNG로 tests/fixtures/assets.seed.synthetic.json 과
tests/fixtures/packages/<case>/ 를 결정적으로 다시 만든다. 표준 라이브러리만 사용한다.

    python tests/fixtures/make_packages.py

각 case의 기대 결과는 tests/fixtures/README.md 에 있다. 실데이터를 넣지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import zlib
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKAGES = ROOT / "packages"

STUDY_ID = "j5-synthetic-study"
CREATED_AT = "2026-09-22T09:00:00+09:00"

# 가상 물건 5개 (릴리스 계획 §4). 좌표·주소는 모두 가상값이다.
ASSETS = [
    ("7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a50", "가상 물건 1", [126.9986, 37.5702], None),
    ("7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a51", "가상 물건 2", [126.9991, 37.5705], None),
    ("7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a52", "가상 물건 3", None, "서울 종로구 가상로 3 (가상 주소)"),
    ("7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a53", "가상 물건 4", [126.9997, 37.5709], "서울 종로구 가상로 4 (가상 주소)"),
    ("7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a54", "가상 물건 5", [127.0003, 37.5712], None),
]
UNKNOWN_ASSET = "7c1f4a0e-3b2d-4e5f-8a9b-0c1d2e3f4a99"
ROUTE_V1 = "9d2e6b1c-5f4a-4c3b-9e8d-7a6b5c4d3e2f"
EVENT = [
    "1a2b3c4d-0001-4000-8000-000000000001",
    "1a2b3c4d-0001-4000-8000-000000000002",
    "1a2b3c4d-0001-4000-8000-000000000003",
]


def png_1x1(rgb: tuple[int, int, int]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00" + bytes(rgb)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def event(event_id: str, asset_id: str, status: str, note: str | None, photos: list[bytes],
          precision: str = "datetime", corrects: str | None = None) -> dict:
    refs = []
    for i, p in enumerate(photos):
        h = sha(p)
        refs.append({"sha256": h, "path": f"photos/{h}.png", "mime": "image/png", "bytes": len(p),
                     "tags": ["front"] if i == 0 else ["ground_floor"]})
    return {
        "event_id": event_id,
        "record_type": "field_observation",
        "asset_id": asset_id,
        "observed_at": "2026-09-22T10:15:00+09:00" if precision == "datetime" else "2026-09-22",
        "observed_at_precision": precision,
        "device_created_at": "2026-09-22T10:16:30+09:00",
        "route_version_id": ROUTE_V1,
        "frame_version_id": None,
        "corrects_event_id": corrects,
        "payload": {"change_status": status, "note": note},
        "attachment_refs": refs,
    }


def line_bytes(ev: dict) -> bytes:
    # 첫 내보내기에서 고정되는 행 바이트. 이후 재내보내기는 이 바이트를 그대로 쓴다.
    return (json.dumps(ev, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def write_package(name: str, lines: list[bytes], photos: list[bytes], package_id: str,
                  manifest_override=None) -> None:
    d = PACKAGES / name
    if d.exists():
        shutil.rmtree(d)
    (d / "photos").mkdir(parents=True)
    obs = b"".join(lines)
    (d / "observations.jsonl").write_bytes(obs)
    files = [{"path": "observations.jsonl", "bytes": len(obs), "sha256": sha(obs)}]
    for p in photos:
        h = sha(p)
        (d / "photos" / f"{h}.png").write_bytes(p)
        files.append({"path": f"photos/{h}.png", "bytes": len(p), "sha256": h})
    manifest = {
        "format": "j5field", "schema_version": "1.0.0", "study_id": STUDY_ID,
        "package_id": package_id, "created_at": CREATED_AT, "data_mode": "synthetic", "files": files,
    }
    if manifest_override:
        manifest_override(manifest)
    (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    seed = []
    for asset_id, label, point, address in ASSETS:
        a = {"asset_id": asset_id, "label": label}
        if point is not None:
            a["location_point"] = point
        if address is not None:
            a["address"] = address
        a.update({"data_mode": "synthetic", "created_at": "2026-09-22", "notes": None})
        seed.append(a)
    (ROOT / "assets.seed.synthetic.json").write_text(
        json.dumps(seed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    red, blue = png_1x1((255, 0, 0)), png_1x1((0, 0, 255))
    a1, a2, a3 = ASSETS[0][0], ASSETS[1][0], ASSETS[2][0]

    e1 = event(EVENT[0], a1, "change_observed", "1층 임대 광고 부착", [red])
    e2 = event(EVENT[1], a2, "no_change", None, [], precision="date")
    e3 = event(EVENT[2], a3, "hard_to_confirm", "공사 가림막", [blue, red], corrects=None)
    L1, L2, L3 = line_bytes(e1), line_bytes(e2), line_bytes(e3)

    # 정상: 이벤트 3개, 사진 2장 (한 장은 두 이벤트가 공유)
    write_package("valid", [L1, L2, L3], [red, blue], "5e5e0000-0000-4000-8000-000000000001")

    # 중복(같은 ID·같은 바이트): 반영기는 두 번째 행을 건너뛴다
    write_package("duplicate_same_content", [L1, L1, L2], [red], "5e5e0000-0000-4000-8000-000000000002")

    # 충돌(같은 ID·다른 내용): 전체 입력 보류
    e1b = dict(e1); e1b["payload"] = {"change_status": "change_observed", "note": "다른 내용"}
    write_package("duplicate_conflict", [L1, line_bytes(e1b)], [red], "5e5e0000-0000-4000-8000-000000000003")

    # 손상: manifest의 사진 해시가 실제 파일과 다름
    def bad_hash(m):
        for f in m["files"]:
            if f["path"].startswith("photos/"):
                f["sha256"] = "0" * 64
    write_package("corrupt_hash_mismatch", [L1], [red], "5e5e0000-0000-4000-8000-000000000004", bad_hash)

    # 손상: 경로 탈출 항목 (스키마 단계에서 거절)
    def traversal(m):
        m["files"].append({"path": "../escape.png", "bytes": 1, "sha256": "1" * 64})
    write_package("corrupt_path_traversal", [L1], [red], "5e5e0000-0000-4000-8000-000000000005", traversal)

    # 첨부 누락: 이벤트가 참조한 사진이 패키지에 없음
    write_package("missing_attachment", [L1], [], "5e5e0000-0000-4000-8000-000000000006")
    # write_package는 photos가 없으면 manifest에 사진 항목도 없다 → 참조 불일치

    # 미지원: HEIC 첨부 (스키마 단계에서 거절, 변환 안내 후 중단)
    e_heic = event(EVENT[0], a1, "change_observed", None, [red])
    e_heic["attachment_refs"][0]["mime"] = "image/heic"
    e_heic["attachment_refs"][0]["path"] = "photos/" + sha(red) + ".heic"
    write_package("unsupported_heic", [line_bytes(e_heic)], [red], "5e5e0000-0000-4000-8000-000000000007")

    # 알 수 없는 물건: 시드에 없는 asset_id (스키마는 통과, 연결 검사에서 거절)
    write_package("unknown_asset", [line_bytes(event(EVENT[0], UNKNOWN_ASSET, "no_change", None, []))],
                  [], "5e5e0000-0000-4000-8000-000000000008")

    # 필수값 위반: 변화 확인인데 사진도 설명도 없음 (스키마 단계에서 거절)
    write_package("invalid_change_without_evidence",
                  [line_bytes(event(EVENT[0], a1, "change_observed", None, []))],
                  [], "5e5e0000-0000-4000-8000-000000000009")


if __name__ == "__main__":
    main()
