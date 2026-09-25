"""사진 연차 비교 (J5-015B, R4). 데이터 사전 §6·§8, 릴리스 계획 §8.

물건의 사진(정본 attachments)을 촬영 지점별 시계열로 묶는다. 묶는 순서: 1) 폰이 적은 `viewpoint_id`, 2) `previous_photo_sha256` 로 이어진 사슬(이전 사진의 묶음을 잇는다),
3) 나머지는 태그 조합. 묶음마다 연도별 첫 사진과 인접 연도 쌍을 "연차 비교" 로 낸다. 상태를 관측하지 않은 해는 비워 두고 보간하지 않는다.
`export_series` 는 파일을 해시 검증하며 `exports/private/photo_series/<물건>/<묶음>/<날짜>-<sha8>.<ext>` 로 복사해 파일 탐색기에서 나란히 볼 수 있게 한다(덮어쓰지 않음).
사진 원본·실데이터는 실데이터 홈에만 있으며 저장소에 넣지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from j5.db.store import Db
from j5.db.validate import ValidationError
from j5.schemas_loader import get_validator

EXPORT_DIR = Path("exports") / "private" / "photo_series"
_SAFE = re.compile(r"[^0-9A-Za-z가-힣_.-]+")


def photo_tags() -> tuple[str, ...]:
    """관측 이벤트 스키마의 사진 태그 목록 (schemas/observation_event.schema.json, 앱의 PHOTO_TAGS 와 같다)."""
    schema = get_validator("observation_event.schema.json").schema
    return tuple(schema["$defs"]["attachment_ref"]["properties"]["tags"]["items"]["enum"])


def _safe(s: str, limit: int = 40) -> str:
    out = _SAFE.sub("_", s).strip("_")
    return (out or "x")[:limit]


def photo_series(db: Db, asset_id: str, *, tag: str | None = None) -> dict:
    if tag is not None and tag not in photo_tags():
        raise ValueError(f"알 수 없는 사진 태그 {tag!r}. 허용: {', '.join(photo_tags())}")
    asset = db.conn.execute("SELECT asset_id, label FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
    if asset is None:
        raise ValidationError([f"물건 {asset_id} 이 정본에 없다"])
    rows = db.conn.execute(
        "SELECT a.attachment_id, a.sha256, a.rel_path, a.mime, a.bytes, a.taken_at, a.tags_json, a.viewpoint_id, a.heading_deg, a.previous_photo_sha256, a.recorded_at,"
        " r.record_id, r.observed_at, r.supersedes_id, json_extract(r.payload_json, '$.change_status') AS change_status"
        " FROM attachments a JOIN records r ON r.record_id = a.record_id WHERE r.subject_id = ? AND r.record_type = 'field_observation'"
        " ORDER BY COALESCE(a.taken_at, r.observed_at), r.observed_at, a.sha256", (asset_id,)).fetchall()
    items = []
    for r in rows:
        tags = json.loads(r["tags_json"])
        if tag and tag not in tags:
            continue
        when = (r["taken_at"] or r["observed_at"])[:10]
        items.append({"sha256": r["sha256"], "rel_path": r["rel_path"], "mime": r["mime"], "bytes": r["bytes"], "taken_at": r["taken_at"], "observed_at": r["observed_at"],
                      "date": when, "year": when[:4], "tags": tags, "viewpoint_id": r["viewpoint_id"], "heading_deg": r["heading_deg"], "previous_photo_sha256": r["previous_photo_sha256"],
                      "record_id": r["record_id"], "change_status": r["change_status"], "superseded": db.conn.execute("SELECT 1 FROM records WHERE supersedes_id = ?", (r["record_id"],)).fetchone() is not None})
    # 묶음 키는 첨부(기록+사진)마다 정한다. 같은 사진이 정정 전·후 관측에 모두 붙을 수 있으므로 사진 해시로 묶지 않는다.
    # 사슬(previous_photo_sha256)은 사진 해시로 잇되, 정정되지 않은 첨부의 묶음을 우선한다.
    # 1) 명시한 촬영 지점, 2) 이전 사진 사슬, 3) 태그 조합
    for i, it in enumerate(items):
        it["_id"] = i
    group_of: dict[int, str] = {}
    names: dict[str, str] = {}
    sha_group: dict[str, str] = {}

    def remember(it: dict, key: str) -> None:
        group_of[it["_id"]] = key
        if it["sha256"] not in sha_group or not it["superseded"]:
            sha_group[it["sha256"]] = key

    for it in items:
        if it["viewpoint_id"]:
            key = f"viewpoint:{it['viewpoint_id']}"
            names[key] = it["viewpoint_id"]
            remember(it, key)
    changed = True
    while changed:
        changed = False
        for it in items:
            if it["_id"] in group_of or not it["previous_photo_sha256"]:
                continue
            prev = sha_group.get(it["previous_photo_sha256"])
            if prev:
                remember(it, prev)
                changed = True
    for it in items:
        if it["_id"] in group_of:
            continue
        if it["previous_photo_sha256"]:
            key = f"chain:{it['previous_photo_sha256'][:8]}"
            names[key] = f"사슬 {it['previous_photo_sha256'][:8]}"
            sha_group.setdefault(it["previous_photo_sha256"], key)
            remember(it, key)
            continue
        key = "tags:" + ("+".join(sorted(it["tags"])) or "untagged")
        names[key] = "태그 " + ("·".join(sorted(it["tags"])) or "없음")
        remember(it, key)
    groups: dict[str, dict] = {}
    for it in items:
        key = group_of.pop(it["_id"])
        del it["_id"]
        g = groups.setdefault(key, {"key": key, "name": names.get(key, key), "basis": key.split(":", 1)[0], "photos": []})
        g["photos"].append(it)
    out_groups = []
    for g in groups.values():
        g["photos"].sort(key=lambda x: (x["date"], x["observed_at"], x["sha256"]))
        yearly: dict[str, str] = {}
        for it in g["photos"]:
            yearly.setdefault(it["year"], it["sha256"])
        years = sorted(yearly)
        g["yearly"] = {y: yearly[y] for y in years}
        g["comparisons"] = [{"from_year": a, "to_year": b, "from_sha256": yearly[a], "to_sha256": yearly[b], "gap_years": int(b) - int(a)} for a, b in zip(years, years[1:])]
        g["missing_years"] = [str(y) for y in range(int(years[0]), int(years[-1]) + 1) if str(y) not in yearly] if years else []
        out_groups.append(g)
    out_groups.sort(key=lambda g: ({"viewpoint": 0, "chain": 1, "tags": 2}[g["basis"]], g["name"]))
    return {"asset_id": asset_id, "label": asset["label"], "tag_filter": tag, "photos": len(items), "groups": out_groups,
            "note": "촬영 지점(viewpoint_id) → 이전 사진 사슬 → 태그 순으로 묶는다. 관측하지 않은 해는 비워 두고 보간하지 않는다. 정정된 관측의 사진은 superseded 로 표시한다"}


def series_text(s: dict) -> str:
    lines = [f"사진 시계열 {s['label']} ({s['asset_id']}): 사진 {s['photos']}장, 묶음 {len(s['groups'])}개" + (f" · 태그 {s['tag_filter']}" if s["tag_filter"] else "")]
    for g in s["groups"]:
        years = ", ".join(g["yearly"]) or "-"
        lines.append(f"  [{g['basis']}] {g['name']}: {len(g['photos'])}장 · 연도 {years}" + (f" · 빠진 해 {', '.join(g['missing_years'])}" if g["missing_years"] else ""))
        for it in g["photos"]:
            extra = "".join([f" · 방향 {it['heading_deg']:g}°" if it["heading_deg"] is not None else "", f" · 이전 {it['previous_photo_sha256'][:8]}" if it["previous_photo_sha256"] else "",
                             " · 정정됨" if it["superseded"] else ""])
            lines.append(f"    {it['date']} {it['change_status'] or ''} {'·'.join(it['tags']) or '태그 없음'} {it['sha256'][:8]}{extra}")
        for c in g["comparisons"]:
            lines.append(f"    비교 {c['from_year']} → {c['to_year']} ({c['gap_years']}년): {c['from_sha256'][:8]} → {c['to_sha256'][:8]}")
    lines.append(s["note"])
    return "\n".join(lines) + "\n"


def export_series(db: Db, data_home: Path, asset_id: str, *, tag: str | None = None, out_root: Path | None = None) -> dict:
    """묶음별 폴더에 사진을 해시 검증하며 복사하고 index.json 을 쓴다. 이미 있는 파일은 덮어쓰지 않는다(같은 내용이면 건너뜀, 다르면 거절)."""
    data_home = Path(data_home)
    s = photo_series(db, asset_id, tag=tag)
    root = Path(out_root) if out_root is not None else data_home / EXPORT_DIR
    dest = root / f"{_safe(s['label'])}-{asset_id[:8]}"
    dest.mkdir(parents=True, exist_ok=True)
    copied, skipped, problems = 0, 0, []
    for g in s["groups"]:
        gdir = dest / _safe(g["name"])
        gdir.mkdir(exist_ok=True)
        for it in g["photos"]:
            src = data_home / it["rel_path"]
            ext = it["rel_path"].rsplit(".", 1)[-1]
            target = gdir / f"{it['date']}-{it['sha256'][:8]}.{ext}"
            it["export_path"] = target.relative_to(root).as_posix()
            if not src.is_file():
                problems.append(f"{it['rel_path']}: 파일 없음")
                continue
            data = src.read_bytes()
            if hashlib.sha256(data).hexdigest() != it["sha256"]:
                problems.append(f"{it['rel_path']}: 해시 불일치")
                continue
            if target.exists():
                if hashlib.sha256(target.read_bytes()).hexdigest() == it["sha256"]:
                    skipped += 1
                    continue
                problems.append(f"{target.name}: 다른 내용의 파일이 이미 있다 (덮어쓰지 않음)")
                continue
            target.write_bytes(data)
            copied += 1
    index = {"asset_id": asset_id, "label": s["label"], "tag_filter": tag, "generated_at": db.now(), "groups": s["groups"], "problems": problems,
             "note": "파일 탐색기에서 묶음 폴더의 사진을 날짜순으로 나란히 본다. 사진은 실데이터이며 저장소·공개 배포에 넣지 않는다"}
    (dest / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"dest": dest.relative_to(data_home).as_posix() if _under(dest, data_home) else str(dest), "copied": copied, "skipped": skipped, "problems": problems,
            "groups": len(s["groups"]), "photos": s["photos"]}


def _under(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
