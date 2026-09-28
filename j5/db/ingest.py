"""VWorld 토지 자료 묶음 한 번에 넣기 (J5-031, ADR-19 운영 절차).

`j5 parcels inspect` 로 범위를 읽고 → 그 범위를 `j5 parcels convert --bbox` 에 옮겨 적고 → `j5 db parcels-load` 하던 세 단계를 한 명령으로 묶는다.

순서와 보장 (정본은 1·2 동안 읽기 전용으로 열어 마이그레이션도 적용하지 않는다, 리뷰 반영 PR #77):
1. 변환(정본에 쓰지 않음): 입력마다 VWorld 묶음인지 확인하고(아니면 거절), 연속지적도의 WGS84 범위 전체를 범위로 삼아 번들을 만든다.
   번들은 실데이터 홈 `parcels/bundles/<입력 이름>-<도형 기준일>-<키 8자>.j5parcels.json` 에 쓴다. 키는 입력 sha256·자료명·이용조건·자료 종류·해석 규칙의 판으로 만들며,
   이 넷이 같을 때만 있는 번들을 다시 쓴다(다시 쓸 때도 번들의 출처 표기를 대조한다). 자료명·이용조건을 고쳐 다시 실행하면 새 번들을 만든다.
   하나라도 실패하면 아무것도 반영하지 않고 멈춘다.
2. 백업(기본): 반영 전에, 대기 중인 마이그레이션보다도 먼저, 읽기 전용 연결에서 `backup` 과 같은 일관된 사본을 만든다. 실패하면 반영하지 않는다.
   `--no-backup` 은 사용자가 따로 백업했을 때만 쓴다.
3. 반영: 정본을 쓰기 모드로 열어(이때 대기 중인 마이그레이션이 적용되고 결과에 적는다) 번들마다 `parcels-load` 와 같은 트랜잭션. 중간에 실패하면 앞의 번들은 반영된 채로 남고(각각 완결), 결과에 어디까지 들어갔는지 적는다. 다시 실행하면 반영된 것은 변화 없음이다.
큰 묶음(번들 한 개의 상한 8,000필지를 넘는 시군구 단위 등)은 PNU 순으로 나눠 `…partNNofMM.j5parcels.json` 여러 개로 쓰고 차례로 반영한다(J5-040, ADR-23). 나누지 않는 묶음의 파일 이름은 이전과 같다.
토지소유 자료의 연령대·거주 구분은 읽지 않는다(vworld.PRIVATE_FIELDS). 실제 자료·번들은 실데이터 홈에만 둔다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from j5.db import schema as S
from j5.db.parcels import BUNDLE_SCHEMA, load_bundle, load_json
from j5.db.store import Db
from j5.parcels.convert import COORD_DECIMALS, MAX_FEATURES, SPLIT_MAX_FEATURES, Clip, ConvertError, ConvertOptions, convert, inspect_source, split_bundle, write_bundle
from j5.parcels import vworld
from j5.parcels.vworld import VWorldError, detect_vworld, read_attrs

BUNDLES_DIR = Path("parcels") / "bundles"
PART_SIZE = MAX_FEATURES   # 나눈 번들 한 개의 필지 수 (J5-040). 폰 계약의 번들 상한과 같다


@dataclass
class IngestItem:
    source: str
    bundle: str | None = None
    bundles: list[str] = field(default_factory=list)   # 나눈 번들이면 조각 전부 (J5-040). 한 개면 [bundle]
    reused: bool = False
    count: int = 0
    attrs: int = 0
    bbox: list[float] | None = None
    load: dict | None = None
    error: str | None = None


@dataclass
class IngestResult:
    outcome: str = "failed"            # applied / unchanged / failed
    stage: str = "convert"             # 실패한 단계: convert / backup / load (성공이면 done)
    items: list[IngestItem] = field(default_factory=list)
    backup_dir: str | None = None
    dataset_version: int = 0
    migrated_from: int | None = None   # 반영 단계에서 적용한 마이그레이션 (이전 → 도구 버전), 없으면 None
    migrated_to: int | None = None
    message: str = ""

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "stage": self.stage, "backup_dir": self.backup_dir, "dataset_version": self.dataset_version, "message": self.message,
                "migrated_from": self.migrated_from, "migrated_to": self.migrated_to, "items": [vars(i) for i in self.items]}

    def to_text(self) -> str:
        lines = []
        for i in self.items:
            head = f"- {Path(i.source).name}: "
            if i.error:
                lines.append(head + f"실패 · {i.error}")
                continue
            conv = f"필지 {i.count}개 · 속성 {i.attrs}개" + (" · 있던 번들 다시 씀" if i.reused else "")
            if i.load:
                ld = i.load
                conv += (f" → {'반영됨' if ld['outcome'] == 'applied' else '변화 없음'} (신규 {ld['inserted']}, 갱신 {ld['updated']}, 변화 없음 {ld['unchanged']}"
                         f"; 속성 스냅샷 신규 {ld['snapshots_inserted']})")
            else:
                conv += " → 반영 안 함"
            parts = f" · 번들 {len(i.bundles)}개로 나눔 (한 번들 상한 {MAX_FEATURES}필지): {i.bundles[0]} …" if len(i.bundles) > 1 else (f" · 번들 {i.bundle}" if i.bundle else "")
            lines.append(head + conv + parts)
        status = {"applied": "반영됨", "unchanged": "변화 없음", "failed": f"실패 ({self.stage} 단계)"}[self.outcome]
        out = [f"VWorld 묶음 넣기: {status} · 입력 {len(self.items)}개 · dataset_version {self.dataset_version}"] + lines
        if self.backup_dir:
            out.append(f"반영 전 백업: {self.backup_dir}")
        if self.migrated_from is not None:
            out.append(f"정본 스키마 마이그레이션 {self.migrated_from} → {self.migrated_to} 을 반영 단계에서 적용했다" + (" (그 전에 백업함)" if self.backup_dir else " (--no-backup: 따로 만든 백업이 있어야 한다)"))
        if self.message:
            out.append(self.message)
        if self.outcome != "failed":
            out.append("다음: `j5 db project` → `j5 db project-copy` 로 폰에 넣을 파일을 만든다. 경계가 겹친 필지는 변화 없음으로 들어간다(구역을 나눠 받은 경우).")
        return "\n".join(out) + "\n"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    if path.is_dir():
        for p in sorted(x for x in path.rglob("*") if x.is_file()):
            h.update(p.relative_to(path).as_posix().encode("utf-8") + b"\0")
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
        return h.hexdigest()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _bundle_path(home: Path, source: Path, geometry_version: str, *, name: str, license: str | None, bundle_mode: str) -> Path:
    """번들 파일 경로. 키는 입력 바이트·자료명·이용조건·자료 종류로 만든다: 출처 표기가 바뀌면 다른 번들이다 (리뷰 반영 PR #77)."""
    stem = source.name
    for suf in (".zip", ".shp"):
        if stem.lower().endswith(suf):
            stem = stem[: -len(suf)]
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in stem)[:80] or "vworld"
    # 해석 규칙의 판(J5-032)도 키에 넣어, 규칙이 바뀌면 옛 규칙으로 만든 번들을 다시 쓰지 않는다
    key = hashlib.sha256("\0".join((_sha256(source), name, license or "", bundle_mode, f"plan{vworld.PLAN_PARSER_VERSION}")).encode("utf-8")).hexdigest()[:8]
    return home / BUNDLES_DIR / f"{safe}-{geometry_version}-{key}.j5parcels.json"


def _merge_loads(loads: list[dict]) -> dict:
    """나눈 번들 조각들의 반영 결과를 입력 하나의 결과로 합친다. 조각 하나라도 반영됐으면 applied."""
    if len(loads) == 1:
        return loads[0]
    out = dict(loads[-1])
    for k in ("inserted", "updated", "unchanged", "attrs_inserted", "attrs_updated", "attrs_unchanged", "snapshots_inserted", "snapshots_replaced"):
        out[k] = sum(int(ld.get(k) or 0) for ld in loads)
    out["outcome"] = "applied" if any(ld["outcome"] == "applied" for ld in loads) else "unchanged"
    out["parts"] = len(loads)
    return out


def _part_paths(out: Path, n: int) -> list[Path]:
    """나눈 번들의 파일 경로 (J5-040). n == 1 이면 원래 경로 하나(나누지 않은 번들과 같은 이름이라 이전 번들을 그대로 다시 쓴다)."""
    if n == 1:
        return [out]
    base = out.name[: -len(".j5parcels.json")]
    return [out.with_name(f"{base}.part{i + 1:02d}of{n:02d}.j5parcels.json") for i in range(n)]


def _existing_parts(out: Path) -> list[Path] | None:
    """이미 만든 번들: 나누지 않은 한 개 또는 나눈 조각 전부. 없으면 None. 조각이 일부만 있으면 ConvertError (중간에 끊긴 변환)."""
    if out.exists():
        return [out]
    base = out.name[: -len(".j5parcels.json")]
    found = sorted(out.parent.glob(f"{base}.part*of*.j5parcels.json")) if out.parent.is_dir() else []
    if not found:
        return None
    ns = {p.name.rsplit(".part", 1)[1].split("of", 1)[1][:2] for p in found}
    n = int(next(iter(ns))) if len(ns) == 1 and next(iter(ns)).isdigit() else 0
    if n < 2 or [p.name for p in found] != [q.name for q in _part_paths(out, n)]:
        raise ConvertError("parts_incomplete", f"나눈 번들 조각이 일부만 있다 ({len(found)}개, {out.parent}). 그 조각들을 확인한 뒤 지우고 다시 실행한다")
    return found


def _convert_one(data_mode: str, home: Path, source: Path, *, geometry_version: str, source_name: str | None, license: str | None) -> tuple[IngestItem, list[dict] | None]:
    item = IngestItem(source=str(source))
    if not source.exists():
        item.error = "입력 파일·폴더가 없다"
        return item, None
    name = source_name or f"VWorld 토지 자료 {source.name}"
    bundle_mode = "synthetic" if data_mode == "synthetic" else "real"
    out = _bundle_path(home, source, geometry_version, name=name, license=license, bundle_mode=bundle_mode)

    def rel(p: Path) -> str:
        try:
            return p.relative_to(home).as_posix()
        except ValueError:
            return str(p)
    try:
        existing = _existing_parts(out)
    except ConvertError as e:
        item.error = f"[{e.code}] {e.message}"
        return item, None
    if existing:
        docs = [load_json(p, BUNDLE_SCHEMA) for p in existing]
        for p, doc in zip(existing, docs):
            src = doc["source"]
            if (src.get("name"), src.get("license"), src.get("geometry_version"), doc.get("data_mode")) != (name, license, geometry_version, bundle_mode):
                item.error = f"있던 번들 {rel(p)} 의 출처 표기가 요청과 다르다 (자료명·이용조건·기준일·자료 종류). 그 파일을 확인한 뒤 지우고 다시 실행한다"
                return item, None
        item.bundles = [rel(p) for p in existing]
        item.bundle, item.reused, item.count = item.bundles[0], True, sum(d["count"] for d in docs)
        item.attrs = sum(1 for d in docs for f in d["features"] if f["properties"].get("attrs"))
        item.bbox = docs[0].get("clip", {}).get("bbox")
        return item, docs
    vw = None
    try:
        vw = detect_vworld(source)
        if vw is None:
            item.error = "VWorld 토지 자료 묶음이 아니다 (다른 SHP 는 `j5 parcels convert --bbox` 로 범위를 정해 변환한다)"
            return item, None
        field_map = vw.mappings.get("cadastral")
        info = inspect_source(vw.cadastral, encoding="utf-8", field_map=field_map)
        if not info.get("crs") or not info.get("bbox_wgs84"):
            item.error = f"좌표계·범위를 읽지 못했다 ({info.get('crs_error') or '범위 없음'})"
            return item, None
        pad = 10 ** -COORD_DECIMALS
        b = info["bbox_wgs84"]
        clip = Clip.from_bbox(f"{b[0] - pad},{b[1] - pad},{b[2] + pad},{b[3] + pad}")
        attrs_by_pnu, attrs_sources = read_attrs(vw, encoding="utf-8")
        # 번들 한 개의 상한(폰 계약 MAX_FEATURES)을 넘는 큰 묶음(시군구 단위 등)은 PNU 순으로 나눠 여러 번들로 쓴다 (J5-040, ADR-23)
        opts = ConvertOptions(clip=clip, geometry_version=geometry_version, source_name=name, encoding="utf-8",
                              license=license, data_mode=bundle_mode, field_map=field_map,
                              attrs_by_pnu=attrs_by_pnu or None, attrs_sources=attrs_sources or None,
                              split=True, max_features=SPLIT_MAX_FEATURES)
        parts = split_bundle(convert(vw.cadastral, opts), PART_SIZE)
        paths = _part_paths(out, len(parts))
        for part, p in zip(parts, paths):
            write_bundle(part, p)
        docs = [load_json(p, BUNDLE_SCHEMA) for p in paths]
        item.bundles = [rel(p) for p in paths]
        item.bundle, item.count, item.bbox = item.bundles[0], sum(d["count"] for d in docs), b
        item.attrs = sum(1 for d in docs for f in d["features"] if f["properties"].get("attrs"))
        return item, docs
    except (ConvertError, VWorldError) as e:
        item.error = f"[{e.code}] {e.message}"
        return item, None
    finally:
        if vw is not None:
            vw.cleanup()


def ingest_vworld(db_path: Path, home: Path, sources: list[Path], *, geometry_version: str, source_name: str | None = None, license: str | None = None,
                  backup: bool = True) -> IngestResult:
    """VWorld 묶음 여러 개를 변환 → (백업) → 차례로 반영한다. 모듈 설명의 순서와 보장을 따른다.
    정본은 변환·백업 동안 읽기 전용으로 열어 두므로, 변환·백업이 실패하면 마이그레이션을 포함해 정본은 조금도 바뀌지 않는다."""
    home = Path(home)
    r = IngestResult()
    docs = []
    names = [source_name] * len(sources) if source_name is None or len(sources) == 1 else [f"{source_name} ({Path(s).name})" for s in sources]
    ro = Db.open_readonly(db_path)
    try:
        data_mode = ro.data_mode
        before_schema = ro.schema_version()
        r.dataset_version = int(ro.meta("dataset_version") or 0)
        for src, name in zip(sources, names):
            item, doc = _convert_one(data_mode, home, Path(src), geometry_version=geometry_version, source_name=name, license=license)
            r.items.append(item)
            docs.append(doc)
        if any(i.error for i in r.items):
            r.stage, r.message = "convert", "변환에 실패한 입력이 있어 아무것도 반영하지 않았다 (정본은 마이그레이션도 적용하지 않았다). 만든 번들은 다시 실행할 때 그대로 쓴다."
            return r
        if backup:
            from j5.db.backup import create_backup  # 순환 import 방지
            b = create_backup(ro, home)
            if b.outcome != "completed":
                r.stage, r.message = "backup", f"반영 전 백업이 실패해 반영하지 않았다: {b.message or b.outcome}. 원인을 고친 뒤 다시 실행한다 (따로 백업했다면 --no-backup)."
                return r
            r.backup_dir = b.backup_dir
    finally:
        ro.close()
    with Db.open(db_path) as db:   # 대기 중인 마이그레이션은 여기서 적용된다 (백업 뒤)
        if before_schema < S.DB_SCHEMA_VERSION:
            r.migrated_from, r.migrated_to = before_schema, db.schema_version()
        for item, parts in zip(r.items, docs):
            try:
                loads = [load_bundle(db, doc).to_dict() for doc in parts]
                item.load = _merge_loads(loads)
            except Exception as e:  # noqa: BLE001 - 반영 실패는 결과에 적고 멈춘다 (앞 번들은 각자 완결)
                item.error = f"반영 실패: {getattr(e, 'message', None) or '; '.join(getattr(e, 'errors', []) or []) or e}"
                r.stage = "load"
                r.dataset_version = int(db.meta("dataset_version") or 0)
                done = sum(1 for i in r.items if i.load)
                r.message = f"{done}개 번들은 반영됐고 이 입력부터는 반영하지 않았다. 원인을 고친 뒤 다시 실행하면 반영된 것은 변화 없음으로 지나간다."
                return r
        r.stage = "done"
        r.outcome = "applied" if any(i.load and i.load["outcome"] == "applied" for i in r.items) else "unchanged"
        r.dataset_version = int(db.meta("dataset_version") or 0)
    return r
