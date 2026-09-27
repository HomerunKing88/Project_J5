"""VWorld 토지 자료 묶음 한 번에 넣기 (J5-031, ADR-19 운영 절차).

`j5 parcels inspect` 로 범위를 읽고 → 그 범위를 `j5 parcels convert --bbox` 에 옮겨 적고 → `j5 db parcels-load` 하던 세 단계를 한 명령으로 묶는다.

순서와 보장 (정본은 1·2 동안 읽기 전용으로 열어 마이그레이션도 적용하지 않는다, 리뷰 반영 PR #77):
1. 변환(정본에 쓰지 않음): 입력마다 VWorld 묶음인지 확인하고(아니면 거절), 연속지적도의 WGS84 범위 전체를 범위로 삼아 번들을 만든다.
   번들은 실데이터 홈 `parcels/bundles/<입력 이름>-<도형 기준일>-<키 8자>.j5parcels.json` 에 쓴다. 키는 입력 sha256·자료명·이용조건·자료 종류로 만들며,
   이 넷이 같을 때만 있는 번들을 다시 쓴다(다시 쓸 때도 번들의 출처 표기를 대조한다). 자료명·이용조건을 고쳐 다시 실행하면 새 번들을 만든다.
   하나라도 실패하면 아무것도 반영하지 않고 멈춘다.
2. 백업(기본): 반영 전에, 대기 중인 마이그레이션보다도 먼저, 읽기 전용 연결에서 `backup` 과 같은 일관된 사본을 만든다. 실패하면 반영하지 않는다.
   `--no-backup` 은 사용자가 따로 백업했을 때만 쓴다.
3. 반영: 정본을 쓰기 모드로 열어(이때 대기 중인 마이그레이션이 적용되고 결과에 적는다) 번들마다 `parcels-load` 와 같은 트랜잭션. 중간에 실패하면 앞의 번들은 반영된 채로 남고(각각 완결), 결과에 어디까지 들어갔는지 적는다. 다시 실행하면 반영된 것은 변화 없음이다.
토지소유 자료의 연령대·거주 구분은 읽지 않는다(vworld.PRIVATE_FIELDS). 실제 자료·번들은 실데이터 홈에만 둔다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from j5.db import schema as S
from j5.db.parcels import BUNDLE_SCHEMA, load_bundle, load_json
from j5.db.store import Db
from j5.parcels.convert import COORD_DECIMALS, Clip, ConvertError, ConvertOptions, convert, inspect_source, write_bundle
from j5.parcels.vworld import VWorldError, detect_vworld, read_attrs

BUNDLES_DIR = Path("parcels") / "bundles"


@dataclass
class IngestItem:
    source: str
    bundle: str | None = None
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
            lines.append(head + conv + (f" · 번들 {i.bundle}" if i.bundle else ""))
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
    key = hashlib.sha256("\0".join((_sha256(source), name, license or "", bundle_mode)).encode("utf-8")).hexdigest()[:8]
    return home / BUNDLES_DIR / f"{safe}-{geometry_version}-{key}.j5parcels.json"


def _convert_one(data_mode: str, home: Path, source: Path, *, geometry_version: str, source_name: str | None, license: str | None) -> tuple[IngestItem, dict | None]:
    item = IngestItem(source=str(source))
    if not source.exists():
        item.error = "입력 파일·폴더가 없다"
        return item, None
    name = source_name or f"VWorld 토지 자료 {source.name}"
    bundle_mode = "synthetic" if data_mode == "synthetic" else "real"
    out = _bundle_path(home, source, geometry_version, name=name, license=license, bundle_mode=bundle_mode)
    try:
        rel = out.relative_to(home).as_posix()
    except ValueError:
        rel = str(out)
    if out.exists():
        doc = load_json(out, BUNDLE_SCHEMA)
        src = doc["source"]
        if (src.get("name"), src.get("license"), src.get("geometry_version"), doc.get("data_mode")) != (name, license, geometry_version, bundle_mode):
            item.error = f"있던 번들 {rel} 의 출처 표기가 요청과 다르다 (자료명·이용조건·기준일·자료 종류). 그 파일을 확인한 뒤 지우고 다시 실행한다"
            return item, None
        item.bundle, item.reused, item.count = rel, True, doc["count"]
        item.attrs = sum(1 for f in doc["features"] if f["properties"].get("attrs"))
        item.bbox = doc.get("clip", {}).get("bbox")
        return item, doc
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
        opts = ConvertOptions(clip=clip, geometry_version=geometry_version, source_name=name, encoding="utf-8",
                              license=license, data_mode=bundle_mode, field_map=field_map,
                              attrs_by_pnu=attrs_by_pnu or None, attrs_sources=attrs_sources or None)
        bundle = convert(vw.cadastral, opts)
        write_bundle(bundle, out)
        doc = load_json(out, BUNDLE_SCHEMA)
        item.bundle, item.count, item.bbox = rel, doc["count"], b
        item.attrs = sum(1 for f in doc["features"] if f["properties"].get("attrs"))
        return item, doc
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
        for item, doc in zip(r.items, docs):
            try:
                item.load = load_bundle(db, doc).to_dict()
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
