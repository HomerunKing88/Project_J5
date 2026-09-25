"""정본 SQLite 스키마 (J5-009). 데이터 사전 §2 의 R1b 최소 테이블과 ADR-09 의 참조 무결성 규칙.

- 모든 테이블은 STRICT (열 타입 강제). 열거값·형식은 CHECK, 대상 존재는 복합 외래키로 검사한다.
- 정본 시각(recorded_at 등 PC 가 부여하는 시각)은 UTC `YYYY-MM-DDTHH:MM:SSZ` 로만 저장한다.
- records 는 불변이다. 수정은 새 기록 + supersedes_id 로 하며 UPDATE/DELETE 는 트리거로 막는다.
  (개인정보 오입력의 실제 삭제는 별도 절차로 다루며 이 트리거를 우회하는 명령을 두지 않는다.)
- 마이그레이션은 버전 순서대로 한 트랜잭션에 적용하고 schema_migrations 에 기록한다.
"""

from __future__ import annotations

HEX = "[0-9a-f]"
UUID_GLOB = "-".join(HEX * n for n in (8, 4, 4, 4, 12))
SHA256_GLOB = HEX * 64
UTC_GLOB = "[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]T[0-2][0-9]:[0-5][0-9]:[0-5][0-9]Z"
DATE_GLOB = "[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]"

SUBJECT_TYPES = ("asset", "parcel", "building", "survey_unit")
TRACKING_STATUSES = ("unreviewed", "background", "watch", "detailed_review", "purchase_ready", "hold", "excluded", "archived")
RESOLUTION_STATUSES = ("confirmed", "pending")
DATA_MODES = ("synthetic", "private_real")
RECORD_TYPES_V1 = ("field_observation",)  # 마이그레이션 1 의 표 정의에 고정된 목록. 기존 마이그레이션 문구는 바꾸지 않는다
RECORD_TYPES = ("field_observation", "target_price", "investment_judgment")  # 현재 허용 목록: R1b 임장 관측, R4(J5-015A) 목표 매수가·투자판단 (데이터 사전 §2·§8). 표는 마이그레이션 9 에서 재작성
SOURCE_KINDS = ("official_fact", "field_observation", "broker_report", "asking_price", "personal_estimate", "scenario_assumption", "calculated_result")
DOCUMENT_KINDS = ("field_package", "official_api", "official_file", "broker_report", "manual_entry")
VERIFICATION_STATUSES = ("unverified", "verified", "disputed")
PRECISIONS = ("datetime", "date")
MIMES = ("image/jpeg", "image/png", "image/webp")


def _in(values) -> str:
    return "IN (" + ", ".join(f"'{v}'" for v in values) + ")"


MIGRATION_0001 = f"""
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;

CREATE TABLE schema_migrations (
  version    INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  applied_at TEXT NOT NULL CHECK (applied_at GLOB '{UTC_GLOB}')
) STRICT;

CREATE TABLE subjects (
  subject_id   TEXT NOT NULL PRIMARY KEY CHECK (subject_id GLOB '{UUID_GLOB}'),
  subject_type TEXT NOT NULL CHECK (subject_type {_in(SUBJECT_TYPES)}),
  recorded_at  TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}'),
  UNIQUE (subject_id, subject_type)
) STRICT;

CREATE TABLE assets (
  asset_id          TEXT NOT NULL PRIMARY KEY,
  subject_type      TEXT NOT NULL DEFAULT 'asset' CHECK (subject_type = 'asset'),
  label             TEXT NOT NULL CHECK (length(label) > 0),
  tracking_status   TEXT NOT NULL DEFAULT 'unreviewed' CHECK (tracking_status {_in(TRACKING_STATUSES)}),
  resolution_status TEXT NOT NULL DEFAULT 'confirmed' CHECK (resolution_status {_in(RESOLUTION_STATUSES)}),
  lon               REAL CHECK (lon IS NULL OR (lon >= -180.0 AND lon <= 180.0)),
  lat               REAL CHECK (lat IS NULL OR (lat >= -90.0 AND lat <= 90.0)),
  address           TEXT CHECK (address IS NULL OR length(address) > 0),
  data_mode         TEXT NOT NULL CHECK (data_mode {_in(DATA_MODES)}),
  seed_created_at   TEXT NOT NULL CHECK (seed_created_at GLOB '{DATE_GLOB}'),
  notes             TEXT,
  recorded_at       TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}'),
  updated_at        TEXT NOT NULL CHECK (updated_at GLOB '{UTC_GLOB}'),
  CHECK ((lon IS NULL) = (lat IS NULL)),
  CHECK (lon IS NOT NULL OR address IS NOT NULL),
  FOREIGN KEY (asset_id, subject_type) REFERENCES subjects (subject_id, subject_type)
) STRICT;

CREATE TABLE source_documents (
  document_id         TEXT NOT NULL PRIMARY KEY CHECK (document_id GLOB '{UUID_GLOB}'),
  document_kind       TEXT NOT NULL CHECK (document_kind {_in(DOCUMENT_KINDS)}),
  title               TEXT NOT NULL CHECK (length(title) > 0),
  terms               TEXT,
  location            TEXT,
  sha256              TEXT CHECK (sha256 IS NULL OR sha256 GLOB '{SHA256_GLOB}'),
  source_published_at TEXT,
  collected_at        TEXT NOT NULL,
  recorded_at         TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}'),
  notes               TEXT
) STRICT;

CREATE TABLE records (
  record_id             TEXT NOT NULL PRIMARY KEY CHECK (record_id GLOB '{UUID_GLOB}'),
  subject_id            TEXT NOT NULL,
  subject_type          TEXT NOT NULL CHECK (subject_type {_in(SUBJECT_TYPES)}),
  record_type           TEXT NOT NULL CHECK (record_type {_in(RECORD_TYPES_V1)}),
  source_kind           TEXT NOT NULL CHECK (source_kind {_in(SOURCE_KINDS)}),
  schema_version        TEXT NOT NULL CHECK (schema_version GLOB '[0-9]*.[0-9]*.[0-9]*'),
  payload_json          TEXT NOT NULL CHECK (json_valid(payload_json) AND json_type(payload_json) = 'object'),
  observed_at           TEXT NOT NULL,
  observed_at_precision TEXT NOT NULL CHECK (observed_at_precision {_in(PRECISIONS)}),
  device_created_at     TEXT,
  source_published_at   TEXT,
  collected_at          TEXT,
  recorded_at           TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}'),
  effective_from        TEXT,
  effective_to          TEXT,
  supersedes_id         TEXT REFERENCES records (record_id),
  CHECK (supersedes_id IS NULL OR supersedes_id <> record_id),
  CHECK ((observed_at_precision = 'date') = (observed_at GLOB '{DATE_GLOB}')),
  FOREIGN KEY (subject_id, subject_type) REFERENCES subjects (subject_id, subject_type)
) STRICT;
CREATE INDEX records_by_subject ON records (subject_id, record_type, observed_at);
CREATE INDEX records_by_supersedes ON records (supersedes_id);

CREATE TRIGGER records_no_update BEFORE UPDATE ON records
BEGIN SELECT RAISE(ABORT, 'records 는 불변이다. 정정은 새 기록(supersedes_id)으로 추가한다'); END;
CREATE TRIGGER records_no_delete BEFORE DELETE ON records
BEGIN SELECT RAISE(ABORT, 'records 는 삭제하지 않는다. 개인정보 오입력은 별도 절차로 처리한다'); END;

CREATE TABLE record_evidence (
  evidence_id         INTEGER PRIMARY KEY,
  record_id           TEXT NOT NULL REFERENCES records (record_id),
  field_path          TEXT NOT NULL CHECK (field_path = '$' OR field_path GLOB '$.*'),
  document_id         TEXT NOT NULL REFERENCES source_documents (document_id),
  locator             TEXT,
  verification_status TEXT NOT NULL DEFAULT 'unverified' CHECK (verification_status {_in(VERIFICATION_STATUSES)}),
  recorded_at         TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}'),
  UNIQUE (record_id, field_path, document_id)
) STRICT;

CREATE TABLE attachments (
  attachment_id TEXT NOT NULL PRIMARY KEY CHECK (attachment_id GLOB '{UUID_GLOB}'),
  record_id     TEXT NOT NULL REFERENCES records (record_id),
  rel_path      TEXT NOT NULL CHECK (length(rel_path) > 0 AND rel_path NOT GLOB '/*' AND rel_path NOT GLOB '*\\*'
                                     AND rel_path NOT GLOB '../*' AND rel_path NOT GLOB '*/../*' AND rel_path NOT GLOB '*/..' AND rel_path <> '..'),
  sha256        TEXT NOT NULL CHECK (sha256 GLOB '{SHA256_GLOB}'),
  mime          TEXT NOT NULL CHECK (mime {_in(MIMES)}),
  bytes         INTEGER NOT NULL CHECK (bytes > 0),
  taken_at      TEXT,
  tags_json     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags_json) AND json_type(tags_json) = 'array'),
  original_ref  TEXT CHECK (original_ref IS NULL OR original_ref GLOB '{SHA256_GLOB}'),
  recorded_at   TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}'),
  UNIQUE (record_id, sha256)
) STRICT;
CREATE INDEX attachments_by_sha ON attachments (sha256);
"""

IMPORT_OUTCOMES = ("applied", "duplicate", "held", "rejected", "failed")
IMPORT_EVENT_OUTCOMES = ("inserted", "skipped_duplicate")

# J5-010 단방향 반영: 패키지·이벤트 수입 기록 (데이터 사전 §2 import_runs / import_events).
# import_events 의 inserted 행이 이벤트 대장이다: event_id 마다 한 번만 반영되고 고정 행 바이트·해시를 보관한다.
MIGRATION_0002 = f"""
CREATE TABLE import_runs (
  run_id                 TEXT NOT NULL PRIMARY KEY CHECK (run_id GLOB '{UUID_GLOB}'),
  package_name           TEXT NOT NULL CHECK (length(package_name) > 0),
  package_sha256         TEXT NOT NULL CHECK (package_sha256 GLOB '{SHA256_GLOB}'),
  package_id             TEXT CHECK (package_id IS NULL OR package_id GLOB '{UUID_GLOB}'),
  study_id               TEXT,
  data_mode              TEXT,
  package_schema_version TEXT,
  source_document_id     TEXT REFERENCES source_documents (document_id),
  outcome                TEXT NOT NULL CHECK (outcome {_in(IMPORT_OUTCOMES)}),
  events_total           INTEGER NOT NULL DEFAULT 0 CHECK (events_total >= 0),
  events_new             INTEGER NOT NULL DEFAULT 0 CHECK (events_new >= 0),
  events_skipped         INTEGER NOT NULL DEFAULT 0 CHECK (events_skipped >= 0),
  photos_stored          INTEGER NOT NULL DEFAULT 0 CHECK (photos_stored >= 0),
  photos_reused          INTEGER NOT NULL DEFAULT 0 CHECK (photos_reused >= 0),
  dataset_version_before INTEGER NOT NULL CHECK (dataset_version_before >= 0),
  dataset_version_after  INTEGER NOT NULL CHECK (dataset_version_after >= dataset_version_before),
  started_at             TEXT NOT NULL CHECK (started_at GLOB '{UTC_GLOB}'),
  finished_at            TEXT NOT NULL CHECK (finished_at GLOB '{UTC_GLOB}'),
  report_json            TEXT NOT NULL CHECK (json_valid(report_json) AND json_type(report_json) = 'object'),
  message                TEXT,
  CHECK ((outcome = 'applied') = (dataset_version_after > dataset_version_before)),
  CHECK (outcome <> 'applied' OR source_document_id IS NOT NULL)
) STRICT;
CREATE INDEX import_runs_by_package ON import_runs (package_sha256, outcome);

CREATE TABLE import_events (
  run_id     TEXT NOT NULL REFERENCES import_runs (run_id) DEFERRABLE INITIALLY DEFERRED,  -- 수입 기록 행은 배치 끝에 넣는다
  event_id   TEXT NOT NULL CHECK (event_id GLOB '{UUID_GLOB}'),
  event_hash TEXT NOT NULL CHECK (event_hash GLOB '{SHA256_GLOB}'),
  line       BLOB NOT NULL CHECK (length(line) > 0),
  outcome    TEXT NOT NULL CHECK (outcome {_in(IMPORT_EVENT_OUTCOMES)}),
  record_id  TEXT REFERENCES records (record_id),
  PRIMARY KEY (run_id, event_id),
  CHECK ((outcome = 'inserted') = (record_id IS NOT NULL))
) STRICT;
CREATE UNIQUE INDEX import_events_ledger ON import_events (event_id) WHERE outcome = 'inserted';
"""

PROJECTION_STATUSES = ("published", "failed")

# J5-011 조회 파생본: 생성 실행 기록 (데이터 사전 §2 projection_runs, §4). 정본 데이터가 아니라 생성 로그이므로 dataset_version 을 바꾸지 않는다.
MIGRATION_0003 = f"""
CREATE TABLE projection_runs (
  run_id                    TEXT NOT NULL PRIMARY KEY CHECK (run_id GLOB '{UUID_GLOB}'),
  source_dataset_version    INTEGER NOT NULL CHECK (source_dataset_version >= 0),
  projection_schema_version TEXT NOT NULL CHECK (projection_schema_version GLOB '[0-9]*.[0-9]*.[0-9]*'),
  data_mode                 TEXT NOT NULL CHECK (data_mode {_in(DATA_MODES)}),
  status                    TEXT NOT NULL CHECK (status {_in(PROJECTION_STATUSES)}),
  output_dir                TEXT CHECK (output_dir IS NULL OR (length(output_dir) > 0 AND output_dir NOT GLOB '/*' AND output_dir NOT GLOB '*..*')),
  zip_name                  TEXT,
  zip_sha256                TEXT CHECK (zip_sha256 IS NULL OR zip_sha256 GLOB '{SHA256_GLOB}'),
  scope_count               INTEGER NOT NULL DEFAULT 0 CHECK (scope_count >= 0),
  record_count              INTEGER NOT NULL DEFAULT 0 CHECK (record_count >= 0),
  started_at                TEXT NOT NULL CHECK (started_at GLOB '{UTC_GLOB}'),
  finished_at               TEXT NOT NULL CHECK (finished_at GLOB '{UTC_GLOB}'),
  message                   TEXT,
  report_json               TEXT NOT NULL CHECK (json_valid(report_json) AND json_type(report_json) = 'object'),
  CHECK ((status = 'published') = (output_dir IS NOT NULL AND zip_name IS NOT NULL AND zip_sha256 IS NOT NULL))
) STRICT;
CREATE INDEX projection_runs_by_status ON projection_runs (status, finished_at);
"""

UNIT_STATUSES = ("occupied", "vacant", "closed_today", "lease_ad_only", "not_visited", "unclear")  # 확인(K)은 occupied·vacant 뿐
UNIT_LINK_RELATIONS = ("split", "merge")

# J5-013A 조사 경로·점포·표본틀·세션·점포 관측 (데이터 사전 §5, ADR-04).
# - 경로 버전·표본틀 버전은 불변이다. 수정은 previous_version_id 로 연결한 새 버전이다.
# - 점포(survey_units)는 물리적 조사 단위이며 subjects(survey_unit) 의 자식이다. 업체명·업종은 관측값(unit_observations)이다.
# - unit_observations 는 (session_id, unit_id) 당 한 행이다: 한 점포가 여러 구간에 걸려도 같은 세션에서 두 번 세지 않는다.
# - record_survey_refs: 관측 이벤트가 적은 route_version_id·frame_version_id 를 records 를 바꾸지 않고 따로 둔다(records 는 불변).
#   기존 대장(import_events)의 고정 행 바이트에서 소급해 채운다. 외래키를 두지 않는다(폰이 먼저 적은 ID 가 정본에 아직 없을 수 있다).
MIGRATION_0004 = f"""
CREATE TABLE survey_routes (
  route_id    TEXT NOT NULL PRIMARY KEY CHECK (route_id GLOB '{UUID_GLOB}'),
  name        TEXT NOT NULL CHECK (length(name) > 0),
  purpose     TEXT,
  recorded_at TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}')
) STRICT;

CREATE TABLE survey_route_versions (
  route_version_id    TEXT NOT NULL PRIMARY KEY CHECK (route_version_id GLOB '{UUID_GLOB}'),
  route_id            TEXT NOT NULL REFERENCES survey_routes (route_id),
  version_no          INTEGER NOT NULL CHECK (version_no >= 1),
  segments_json       TEXT NOT NULL CHECK (json_valid(segments_json) AND json_type(segments_json) = 'array' AND json_array_length(segments_json) >= 1),
  effective_from      TEXT NOT NULL CHECK (effective_from GLOB '{DATE_GLOB}'),
  previous_version_id TEXT REFERENCES survey_route_versions (route_version_id),
  change_reason       TEXT,
  content_hash        TEXT NOT NULL CHECK (content_hash GLOB '{SHA256_GLOB}'),
  recorded_at         TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}'),
  UNIQUE (route_id, version_no),
  CHECK ((version_no = 1) = (previous_version_id IS NULL)),
  CHECK (previous_version_id IS NULL OR previous_version_id <> route_version_id)
) STRICT;

CREATE TABLE survey_units (
  unit_id       TEXT NOT NULL PRIMARY KEY,
  subject_type  TEXT NOT NULL DEFAULT 'survey_unit' CHECK (subject_type = 'survey_unit'),
  label         TEXT NOT NULL CHECK (length(label) > 0),
  floor         TEXT,
  lon           REAL CHECK (lon IS NULL OR (lon >= -180.0 AND lon <= 180.0)),
  lat           REAL CHECK (lat IS NULL OR (lat >= -90.0 AND lat <= 90.0)),
  asset_id      TEXT REFERENCES assets (asset_id),
  opened_on     TEXT CHECK (opened_on IS NULL OR opened_on GLOB '{DATE_GLOB}'),
  closed_on     TEXT CHECK (closed_on IS NULL OR closed_on GLOB '{DATE_GLOB}'),
  note          TEXT,
  recorded_at   TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}'),
  updated_at    TEXT NOT NULL CHECK (updated_at GLOB '{UTC_GLOB}'),
  CHECK ((lon IS NULL) = (lat IS NULL)),
  CHECK (opened_on IS NULL OR closed_on IS NULL OR closed_on >= opened_on),
  FOREIGN KEY (unit_id, subject_type) REFERENCES subjects (subject_id, subject_type)
) STRICT;

CREATE TABLE survey_unit_links (
  from_unit_id   TEXT NOT NULL REFERENCES survey_units (unit_id),
  to_unit_id     TEXT NOT NULL REFERENCES survey_units (unit_id),
  relation       TEXT NOT NULL CHECK (relation {_in(UNIT_LINK_RELATIONS)}),
  effective_from TEXT NOT NULL CHECK (effective_from GLOB '{DATE_GLOB}'),
  note           TEXT,
  recorded_at    TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}'),
  PRIMARY KEY (from_unit_id, to_unit_id, relation),
  CHECK (from_unit_id <> to_unit_id)
) STRICT;

CREATE TABLE survey_frame_versions (
  frame_version_id    TEXT NOT NULL PRIMARY KEY CHECK (frame_version_id GLOB '{UUID_GLOB}'),
  name                TEXT NOT NULL CHECK (length(name) > 0),
  selection_rule      TEXT NOT NULL CHECK (length(selection_rule) > 0),
  confirmed_on        TEXT NOT NULL CHECK (confirmed_on GLOB '{DATE_GLOB}'),
  previous_version_id TEXT REFERENCES survey_frame_versions (frame_version_id),
  change_reason       TEXT,
  content_hash        TEXT NOT NULL CHECK (content_hash GLOB '{SHA256_GLOB}'),
  recorded_at         TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}'),
  CHECK (previous_version_id IS NULL OR previous_version_id <> frame_version_id)
) STRICT;

CREATE TABLE survey_frame_members (
  frame_version_id TEXT NOT NULL REFERENCES survey_frame_versions (frame_version_id),
  unit_id          TEXT NOT NULL REFERENCES survey_units (unit_id),
  PRIMARY KEY (frame_version_id, unit_id)
) STRICT;

CREATE TABLE survey_sessions (
  session_id            TEXT NOT NULL PRIMARY KEY CHECK (session_id GLOB '{UUID_GLOB}'),
  route_version_id      TEXT NOT NULL REFERENCES survey_route_versions (route_version_id),
  frame_version_id      TEXT NOT NULL REFERENCES survey_frame_versions (frame_version_id),
  started_at            TEXT NOT NULL,
  ended_at              TEXT,
  visited_segments_json TEXT NOT NULL CHECK (json_valid(visited_segments_json) AND json_type(visited_segments_json) = 'array'),
  skipped_segments_json TEXT NOT NULL CHECK (json_valid(skipped_segments_json) AND json_type(skipped_segments_json) = 'array'),
  note                  TEXT,
  content_hash          TEXT NOT NULL CHECK (content_hash GLOB '{SHA256_GLOB}'),
  recorded_at           TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}')
) STRICT;

CREATE TABLE unit_observations (
  session_id    TEXT NOT NULL REFERENCES survey_sessions (session_id),
  unit_id       TEXT NOT NULL REFERENCES survey_units (unit_id),
  observed_at   TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status {_in(UNIT_STATUSES)}),
  business_name TEXT,
  business_type TEXT,
  note          TEXT,
  record_id     TEXT REFERENCES records (record_id),
  recorded_at   TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}'),
  PRIMARY KEY (session_id, unit_id)
) STRICT;

CREATE TABLE record_survey_refs (
  record_id        TEXT NOT NULL PRIMARY KEY REFERENCES records (record_id),
  route_version_id TEXT CHECK (route_version_id IS NULL OR route_version_id GLOB '{UUID_GLOB}'),
  frame_version_id TEXT CHECK (frame_version_id IS NULL OR frame_version_id GLOB '{UUID_GLOB}'),
  recorded_at      TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}'),
  CHECK (route_version_id IS NOT NULL OR frame_version_id IS NOT NULL)
) STRICT;
INSERT INTO record_survey_refs (record_id, route_version_id, frame_version_id, recorded_at)
  SELECT record_id, json_extract(CAST(line AS TEXT), '$.route_version_id'), json_extract(CAST(line AS TEXT), '$.frame_version_id'),
         strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
  FROM import_events
  WHERE outcome = 'inserted' AND record_id IS NOT NULL
    AND (json_extract(CAST(line AS TEXT), '$.route_version_id') IS NOT NULL OR json_extract(CAST(line AS TEXT), '$.frame_version_id') IS NOT NULL);
"""

ADDRESS_STATUSES = ("unverified", "verified", "disputed")
COMPONENT_TYPES = ("parcel", "building")
COMPONENT_BASES = ("manual", "location_point")
AREA_MISSING_REASONS = ("source_geographic_crs", "not_collected")

# J5-013B-2 필지 정본·물건 구성 (데이터 사전 §2 parcels / asset_components, §6, ADR-13).
# - parcels: PNU(외부 ID)와 parcel_id(내부 UUID)를 분리한다. subjects(parcel) 의 자식. 공부면적(registered_area_m2)과 도형면적(geom_area_m2),
#   좌표계(geometry_crs·source_crs), 도형 버전(geometry_version), 주소 확인 상태(address_status)를 분리해 둔다. 결측은 NULL + 사유.
# - asset_components: 매입 검토 단위(asset)의 구성 필지·건물과 적용 기간. 다대다. 연결 근거(basis)는 수동 확인 또는 위치점 포함 제안의 승인.
MIGRATION_0005 = f"""
CREATE TABLE parcels (
  parcel_id                      TEXT NOT NULL PRIMARY KEY,
  subject_type                   TEXT NOT NULL DEFAULT 'parcel' CHECK (subject_type = 'parcel'),
  pnu                            TEXT NOT NULL UNIQUE CHECK (pnu GLOB '{"[0-9]" * 19}'),
  label                          TEXT NOT NULL CHECK (length(label) > 0),
  emd_code                       TEXT NOT NULL CHECK (emd_code GLOB '{"[0-9]" * 10}'),
  emd_name                       TEXT,
  mountain                       INTEGER NOT NULL CHECK (mountain IN (0, 1)),
  bon                            INTEGER NOT NULL CHECK (bon >= 0),
  bu                             INTEGER NOT NULL CHECK (bu >= 0),
  jimok                          TEXT,
  jibun_raw                      TEXT,
  jibun_mismatch                 INTEGER NOT NULL DEFAULT 0 CHECK (jibun_mismatch IN (0, 1)),
  registered_area_m2             REAL CHECK (registered_area_m2 IS NULL OR registered_area_m2 >= 0),
  registered_area_missing_reason TEXT CHECK (registered_area_missing_reason IS NULL OR registered_area_missing_reason {_in(AREA_MISSING_REASONS)}),
  geom_area_m2                   REAL CHECK (geom_area_m2 IS NULL OR geom_area_m2 >= 0),
  geom_area_missing_reason       TEXT CHECK (geom_area_missing_reason IS NULL OR geom_area_missing_reason {_in(AREA_MISSING_REASONS)}),
  geometry_json                  TEXT NOT NULL CHECK (json_valid(geometry_json) AND json_type(geometry_json) = 'object'),
  geometry_crs                   TEXT NOT NULL DEFAULT 'EPSG:4326' CHECK (geometry_crs = 'EPSG:4326'),
  geometry_version               TEXT NOT NULL CHECK (geometry_version GLOB '{DATE_GLOB}'),
  bbox_json                      TEXT NOT NULL CHECK (json_valid(bbox_json) AND json_type(bbox_json) = 'array' AND json_array_length(bbox_json) = 4),
  source_name                    TEXT NOT NULL CHECK (length(source_name) > 0),
  source_crs                     TEXT,
  source_ellipsoid               TEXT CHECK (source_ellipsoid IS NULL OR source_ellipsoid IN ('GRS80', 'WGS84', 'Bessel')),
  source_datum_shift             TEXT CHECK (source_datum_shift IS NULL OR source_datum_shift = 'korean1985'),
  source_shp_sha256              TEXT CHECK (source_shp_sha256 IS NULL OR source_shp_sha256 GLOB '{SHA256_GLOB}'),
  source_license                 TEXT,
  source_document_id             TEXT REFERENCES source_documents (document_id),
  address_status                 TEXT NOT NULL DEFAULT 'unverified' CHECK (address_status {_in(ADDRESS_STATUSES)}),
  data_mode                      TEXT NOT NULL CHECK (data_mode {_in(DATA_MODES)}),
  content_hash                   TEXT NOT NULL CHECK (content_hash GLOB '{SHA256_GLOB}'),
  recorded_at                    TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}'),
  updated_at                     TEXT NOT NULL CHECK (updated_at GLOB '{UTC_GLOB}'),
  CHECK ((registered_area_m2 IS NULL) = (registered_area_missing_reason IS NOT NULL)),
  CHECK ((geom_area_m2 IS NULL) = (geom_area_missing_reason IS NOT NULL)),
  FOREIGN KEY (parcel_id, subject_type) REFERENCES subjects (subject_id, subject_type)
) STRICT;
CREATE INDEX parcels_by_emd ON parcels (emd_code, bon, bu);

CREATE TABLE asset_components (
  component_id         TEXT NOT NULL PRIMARY KEY CHECK (component_id GLOB '{UUID_GLOB}'),
  asset_id             TEXT NOT NULL REFERENCES assets (asset_id),
  component_subject_id TEXT NOT NULL,
  component_type       TEXT NOT NULL CHECK (component_type {_in(COMPONENT_TYPES)}),
  effective_from       TEXT NOT NULL CHECK (effective_from GLOB '{DATE_GLOB}'),
  effective_to         TEXT CHECK (effective_to IS NULL OR effective_to GLOB '{DATE_GLOB}'),
  basis                TEXT NOT NULL CHECK (basis {_in(COMPONENT_BASES)}),
  note                 TEXT,
  recorded_at          TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}'),
  updated_at           TEXT NOT NULL CHECK (updated_at GLOB '{UTC_GLOB}'),
  UNIQUE (asset_id, component_subject_id, effective_from),
  CHECK (effective_to IS NULL OR effective_to >= effective_from),
  FOREIGN KEY (component_subject_id, component_type) REFERENCES subjects (subject_id, subject_type)
) STRICT;
CREATE INDEX asset_components_by_component ON asset_components (component_subject_id, effective_from);
"""

COLLECTION_OUTCOMES = ("complete", "empty", "partial", "failed")
PAGE_OUTCOMES = ("ok", "empty", "api_error", "http_error", "network_error", "bad_response")
BUILDING_KINDS = ("general", "strata", "unknown")
CANCEL_STATUSES = ("none", "cancelled")
TRANSACTION_SCOPES = ("whole_asset", "multi_parcel_bundle", "land_only", "building_only", "partial_share", "strata_unit", "unclear")
SCOPE_BASES = ("auto_provider_fields", "manual_review")
LINK_STATUSES = ("unlinked", "candidate", "pending_evidence", "confirmed", "withdrawn")
RUN_ID_GLOB = "[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
LAWD_GLOB = "[0-9]" * 5
YM_GLOB = "[0-9][0-9][0-9][0-9][0-1][0-9]"

# J5-014B-1 거래 수집 기록·원본 행·정규화 거래 (데이터 사전 §7.1·§7.2, ADR-05).
# - collection_runs/collection_pages: `j5 collect rt-*` 의 실행 기록 파일(run-<실행>.json)을 정본에 옮긴 것. 실행 ID 는 파일 실행 ID 그대로다(체크포인트).
# - transaction_observations: 응답의 항목 행을 태그명→문자열 그대로 보존한다(불변). 같은 (실행, 월, 페이지, 행 번호)는 한 번만 넣는다.
# - transactions: (제공자, 시군구, 계약월, 식별 해시, 순번) 당 한 행. 식별 해시는 제공자가 나중에 채우거나 바꾸는 필드(취소·거래 유형·중개사·매수/매도 구분)를 뺀
#   필드로 만들고, 같은 응답 안의 완전히 같은 행은 순번으로 구분한다(같은 값의 별개 행을 합치지 않는다). 응답에서 사라진 행은 missing_since_run_id 로만 표시하고 취소로 확정하지 않는다.
# - 연결(transaction_links)·검토 결정은 다음 조각(J5-014B-2)에서 추가한다. link_status 는 미리 두되 이 조각에서는 'unlinked' 만 쓴다.
MIGRATION_0006 = f"""
CREATE TABLE collection_runs (
  run_id             TEXT NOT NULL CHECK (run_id GLOB '{RUN_ID_GLOB}'),
  provider           TEXT NOT NULL CHECK (length(provider) > 0),
  endpoint           TEXT NOT NULL CHECK (length(endpoint) > 0),
  lawd_cd            TEXT NOT NULL CHECK (lawd_cd GLOB '{LAWD_GLOB}'),
  deal_ymd           TEXT NOT NULL CHECK (deal_ymd GLOB '{YM_GLOB}'),
  outcome            TEXT NOT NULL CHECK (outcome {_in(COLLECTION_OUTCOMES)}),
  total_count        INTEGER CHECK (total_count IS NULL OR total_count >= 0),
  items              INTEGER NOT NULL CHECK (items >= 0),
  pages              INTEGER NOT NULL CHECK (pages >= 0),
  message            TEXT,
  started_at         TEXT NOT NULL CHECK (started_at GLOB '{UTC_GLOB}'),
  finished_at        TEXT NOT NULL CHECK (finished_at GLOB '{UTC_GLOB}'),
  run_path           TEXT NOT NULL CHECK (length(run_path) > 0 AND run_path NOT GLOB '/*' AND run_path NOT GLOB '*..*'),
  source_document_id TEXT NOT NULL REFERENCES source_documents (document_id),
  data_mode          TEXT NOT NULL CHECK (data_mode {_in(DATA_MODES)}),
  loaded_at          TEXT NOT NULL CHECK (loaded_at GLOB '{UTC_GLOB}'),
  PRIMARY KEY (run_id, lawd_cd, deal_ymd),
  CHECK (outcome <> 'complete' OR (total_count IS NOT NULL AND items = total_count))
) STRICT;
CREATE INDEX collection_runs_by_month ON collection_runs (lawd_cd, deal_ymd, run_id);

CREATE TABLE collection_pages (
  run_id      TEXT NOT NULL,
  lawd_cd     TEXT NOT NULL,
  deal_ymd    TEXT NOT NULL,
  page_no     INTEGER NOT NULL CHECK (page_no >= 1),
  outcome     TEXT NOT NULL CHECK (outcome {_in(PAGE_OUTCOMES)}),
  http_status INTEGER,
  result_code TEXT,
  total_count INTEGER CHECK (total_count IS NULL OR total_count >= 0),
  item_count  INTEGER NOT NULL DEFAULT 0 CHECK (item_count >= 0),
  raw_path    TEXT CHECK (raw_path IS NULL OR (length(raw_path) > 0 AND raw_path NOT GLOB '/*' AND raw_path NOT GLOB '*..*')),
  raw_sha256  TEXT CHECK (raw_sha256 IS NULL OR raw_sha256 GLOB '{SHA256_GLOB}'),
  bytes       INTEGER CHECK (bytes IS NULL OR bytes >= 0),
  fetched_at  TEXT NOT NULL CHECK (fetched_at GLOB '{UTC_GLOB}'),
  fields_json TEXT NOT NULL CHECK (json_valid(fields_json) AND json_type(fields_json) = 'object'),
  PRIMARY KEY (run_id, lawd_cd, deal_ymd, page_no),
  FOREIGN KEY (run_id, lawd_cd, deal_ymd) REFERENCES collection_runs (run_id, lawd_cd, deal_ymd),
  CHECK (outcome NOT IN ('ok', 'empty') OR raw_path IS NOT NULL),
  CHECK ((raw_path IS NULL) = (raw_sha256 IS NULL))
) STRICT;

CREATE TABLE transaction_observations (
  observation_id TEXT NOT NULL PRIMARY KEY CHECK (observation_id GLOB '{UUID_GLOB}'),
  run_id         TEXT NOT NULL,
  lawd_cd        TEXT NOT NULL,
  deal_ymd       TEXT NOT NULL,
  page_no        INTEGER NOT NULL,
  row_index      INTEGER NOT NULL CHECK (row_index >= 0),
  content_json   TEXT NOT NULL CHECK (json_valid(content_json) AND json_type(content_json) = 'object'),
  content_hash   TEXT NOT NULL CHECK (content_hash GLOB '{SHA256_GLOB}'),
  identity_hash  TEXT NOT NULL CHECK (identity_hash GLOB '{SHA256_GLOB}'),
  ordinal        INTEGER NOT NULL CHECK (ordinal >= 0),
  fetched_at     TEXT NOT NULL CHECK (fetched_at GLOB '{UTC_GLOB}'),
  recorded_at    TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}'),
  UNIQUE (run_id, lawd_cd, deal_ymd, page_no, row_index),
  FOREIGN KEY (run_id, lawd_cd, deal_ymd, page_no) REFERENCES collection_pages (run_id, lawd_cd, deal_ymd, page_no)
) STRICT;
CREATE INDEX transaction_observations_by_identity ON transaction_observations (lawd_cd, deal_ymd, identity_hash, ordinal);
CREATE TRIGGER transaction_observations_no_update BEFORE UPDATE ON transaction_observations BEGIN
  SELECT RAISE(ABORT, 'transaction_observations 는 불변이다');
END;
CREATE TRIGGER transaction_observations_no_delete BEFORE DELETE ON transaction_observations BEGIN
  SELECT RAISE(ABORT, 'transaction_observations 는 삭제하지 않는다');
END;

CREATE TABLE transactions (
  transaction_id        TEXT NOT NULL PRIMARY KEY CHECK (transaction_id GLOB '{UUID_GLOB}'),
  provider              TEXT NOT NULL CHECK (length(provider) > 0),
  lawd_cd               TEXT NOT NULL CHECK (lawd_cd GLOB '{LAWD_GLOB}'),
  deal_ymd              TEXT NOT NULL CHECK (deal_ymd GLOB '{YM_GLOB}'),
  identity_hash         TEXT NOT NULL CHECK (identity_hash GLOB '{SHA256_GLOB}'),
  ordinal               INTEGER NOT NULL CHECK (ordinal >= 0),
  deal_date             TEXT CHECK (deal_date IS NULL OR deal_date GLOB '{DATE_GLOB}'),
  amount_krw            INTEGER CHECK (amount_krw IS NULL OR amount_krw >= 0),
  building_area_m2      REAL CHECK (building_area_m2 IS NULL OR building_area_m2 >= 0),
  plottage_area_m2      REAL CHECK (plottage_area_m2 IS NULL OR plottage_area_m2 >= 0),
  build_year            INTEGER CHECK (build_year IS NULL OR (build_year >= 1800 AND build_year <= 2100)),
  missing_reasons_json  TEXT NOT NULL CHECK (json_valid(missing_reasons_json) AND json_type(missing_reasons_json) = 'object'),
  emd_name              TEXT,
  jibun_raw             TEXT,
  jibun_masked          INTEGER NOT NULL CHECK (jibun_masked IN (0, 1)),
  jibun_prefix          TEXT,
  building_kind         TEXT NOT NULL CHECK (building_kind {_in(BUILDING_KINDS)}),
  building_type_raw     TEXT,
  building_use_raw      TEXT,
  land_use_raw          TEXT,
  floor_raw             TEXT,
  share_deal            INTEGER NOT NULL CHECK (share_deal IN (0, 1)),
  cancel_status         TEXT NOT NULL CHECK (cancel_status {_in(CANCEL_STATUSES)}),
  cancel_date           TEXT CHECK (cancel_date IS NULL OR cancel_date GLOB '{DATE_GLOB}'),
  dealing_gbn_raw       TEXT,
  agent_sgg_raw         TEXT,
  buyer_kind_raw        TEXT,
  seller_kind_raw       TEXT,
  scope                 TEXT NOT NULL CHECK (scope {_in(TRANSACTION_SCOPES)}),
  scope_basis           TEXT NOT NULL CHECK (scope_basis {_in(SCOPE_BASES)}),
  link_status           TEXT NOT NULL DEFAULT 'unlinked' CHECK (link_status {_in(LINK_STATUSES)}),
  first_seen_run_id     TEXT NOT NULL CHECK (first_seen_run_id GLOB '{RUN_ID_GLOB}'),
  last_seen_run_id      TEXT NOT NULL CHECK (last_seen_run_id GLOB '{RUN_ID_GLOB}'),
  missing_since_run_id  TEXT CHECK (missing_since_run_id IS NULL OR missing_since_run_id GLOB '{RUN_ID_GLOB}'),
  first_observation_id  TEXT NOT NULL REFERENCES transaction_observations (observation_id),
  latest_observation_id TEXT NOT NULL REFERENCES transaction_observations (observation_id),
  content_hash          TEXT NOT NULL CHECK (content_hash GLOB '{SHA256_GLOB}'),
  data_mode             TEXT NOT NULL CHECK (data_mode {_in(DATA_MODES)}),
  recorded_at           TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}'),
  updated_at            TEXT NOT NULL CHECK (updated_at GLOB '{UTC_GLOB}'),
  UNIQUE (provider, lawd_cd, deal_ymd, identity_hash, ordinal),
  CHECK (cancel_status = 'cancelled' OR cancel_date IS NULL),
  CHECK (last_seen_run_id >= first_seen_run_id)
) STRICT;
CREATE INDEX transactions_by_month_emd ON transactions (lawd_cd, deal_ymd, emd_name);
CREATE INDEX transactions_by_link ON transactions (link_status, lawd_cd, deal_ymd);
"""

LINK_BASIS_KINDS = ("jibun_exact", "jibun_prefix", "manual", "document")
LINK_DECISIONS = ("candidate", "pending_evidence", "confirmed", "withdrawn")

# J5-014B-2 거래↔물건 연결과 검토 결정 (데이터 사전 §7.2 transaction_links, §2 review_decisions, §8 불변 검토기록).
# - transaction_links: 거래 하나에 유효한(철회되지 않은) 연결은 하나다(부분 UNIQUE). 다른 물건으로 바꾸면 이전 연결을 철회(superseded_by)하고 새 연결을 만든다.
# - review_decisions: 연결의 상태 변화(후보·근거 대기·확정·철회)마다 한 행. 불변(트리거). 현재 상태는 transaction_links 가 갖고 이력은 여기 남는다.
MIGRATION_0007 = f"""
CREATE TABLE transaction_links (
  link_id              TEXT NOT NULL PRIMARY KEY CHECK (link_id GLOB '{UUID_GLOB}'),
  transaction_id       TEXT NOT NULL REFERENCES transactions (transaction_id),
  asset_id             TEXT NOT NULL REFERENCES assets (asset_id),
  scope                TEXT NOT NULL CHECK (scope {_in(TRANSACTION_SCOPES)}),
  status               TEXT NOT NULL CHECK (status {_in(LINK_DECISIONS)}),
  basis_kind           TEXT NOT NULL CHECK (basis_kind {_in(LINK_BASIS_KINDS)}),
  basis_note           TEXT,
  evidence_document_id TEXT REFERENCES source_documents (document_id),
  reviewed_on          TEXT NOT NULL CHECK (reviewed_on GLOB '{DATE_GLOB}'),
  withdrawn_on         TEXT CHECK (withdrawn_on IS NULL OR withdrawn_on GLOB '{DATE_GLOB}'),
  withdrawn_reason     TEXT,
  superseded_by        TEXT REFERENCES transaction_links (link_id),
  recorded_at          TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}'),
  updated_at           TEXT NOT NULL CHECK (updated_at GLOB '{UTC_GLOB}'),
  CHECK ((status = 'withdrawn') = (withdrawn_on IS NOT NULL)),
  CHECK (superseded_by IS NULL OR status = 'withdrawn')
) STRICT;
CREATE UNIQUE INDEX transaction_links_active ON transaction_links (transaction_id) WHERE status <> 'withdrawn';
CREATE INDEX transaction_links_by_asset ON transaction_links (asset_id, status);

CREATE TABLE review_decisions (
  decision_id          TEXT NOT NULL PRIMARY KEY CHECK (decision_id GLOB '{UUID_GLOB}'),
  link_id              TEXT NOT NULL REFERENCES transaction_links (link_id),
  decision             TEXT NOT NULL CHECK (decision {_in(LINK_DECISIONS)}),
  scope                TEXT NOT NULL CHECK (scope {_in(TRANSACTION_SCOPES)}),
  asset_id             TEXT NOT NULL REFERENCES assets (asset_id),
  decided_on           TEXT NOT NULL CHECK (decided_on GLOB '{DATE_GLOB}'),
  rationale            TEXT,
  previous_decision_id TEXT REFERENCES review_decisions (decision_id),
  recorded_at          TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}')
) STRICT;
CREATE INDEX review_decisions_by_link ON review_decisions (link_id, recorded_at);
CREATE TRIGGER review_decisions_no_update BEFORE UPDATE ON review_decisions BEGIN
  SELECT RAISE(ABORT, 'review_decisions 는 불변이다');
END;
CREATE TRIGGER review_decisions_no_delete BEFORE DELETE ON review_decisions BEGIN
  SELECT RAISE(ABORT, 'review_decisions 는 삭제하지 않는다');
END;
"""

ZONES = ("core", "comparison", "outside", "unclassified")

# J5-014B-3 범위 분류 (데이터 사전 §7.1: 핵심/비교 범위 밖 행은 원본에 남기되 모바일·분석집계에 자동 포함하지 않는다).
# - zone_rules: 법정동 이름 목록으로 핵심(core)·비교(comparison) 범위를 정한 규칙의 버전. 새 버전을 반영하면 모든 거래를 다시 분류한다.
# - transactions.zone: 현재 규칙으로 분류한 결과. 규칙이 없으면 unclassified.
MIGRATION_0008 = f"""
CREATE TABLE zone_rules (
  version     INTEGER NOT NULL PRIMARY KEY CHECK (version >= 1),
  name        TEXT NOT NULL CHECK (length(name) > 0),
  rules_json  TEXT NOT NULL CHECK (json_valid(rules_json) AND json_type(rules_json) = 'object'),
  note        TEXT,
  recorded_at TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}')
) STRICT;
ALTER TABLE transactions ADD COLUMN zone TEXT NOT NULL DEFAULT 'unclassified' CHECK (zone {_in(ZONES)});
ALTER TABLE transactions ADD COLUMN zone_rule_version INTEGER REFERENCES zone_rules (version);
CREATE INDEX transactions_by_zone ON transactions (zone, lawd_cd, deal_ymd);
"""

# J5-015A 기록 종류 추가 (데이터 사전 §2 "records 는 종류별 전체 스냅샷", §8 목표 매수가·투자판단).
# records.record_type 의 CHECK 는 표 정의에 박혀 있어 넓히려면 표를 다시 만들어야 한다. 아래는 SQLite 권장 절차(외래키 검사를 끈 채 새 표 생성 → 복사 → 옛 표 삭제 → 이름 바꾸기 →
# 인덱스·트리거 재생성 → foreign_key_check)이며 store 가 FK_OFF_MIGRATIONS 에 따라 외래키를 끄고 한 트랜잭션으로 실행한 뒤 검사가 비어야 커밋한다 (ADR-14).
# 새 표의 자기 참조(supersedes_id)는 이름 바꾸기 뒤의 이름 'records' 를 가리키게 적는다(외래키가 꺼져 있으면 RENAME 이 참조 문구를 고치지 않는다).
MIGRATION_0009 = f"""
DROP TRIGGER records_no_update;
DROP TRIGGER records_no_delete;
DROP INDEX records_by_subject;
DROP INDEX records_by_supersedes;
CREATE TABLE records_new (
  record_id             TEXT NOT NULL PRIMARY KEY CHECK (record_id GLOB '{UUID_GLOB}'),
  subject_id            TEXT NOT NULL,
  subject_type          TEXT NOT NULL CHECK (subject_type {_in(SUBJECT_TYPES)}),
  record_type           TEXT NOT NULL CHECK (record_type {_in(RECORD_TYPES)}),
  source_kind           TEXT NOT NULL CHECK (source_kind {_in(SOURCE_KINDS)}),
  schema_version        TEXT NOT NULL CHECK (schema_version GLOB '[0-9]*.[0-9]*.[0-9]*'),
  payload_json          TEXT NOT NULL CHECK (json_valid(payload_json) AND json_type(payload_json) = 'object'),
  observed_at           TEXT NOT NULL,
  observed_at_precision TEXT NOT NULL CHECK (observed_at_precision {_in(PRECISIONS)}),
  device_created_at     TEXT,
  source_published_at   TEXT,
  collected_at          TEXT,
  recorded_at           TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}'),
  effective_from        TEXT,
  effective_to          TEXT,
  supersedes_id         TEXT REFERENCES records (record_id),
  CHECK (supersedes_id IS NULL OR supersedes_id <> record_id),
  CHECK ((observed_at_precision = 'date') = (observed_at GLOB '{DATE_GLOB}')),
  FOREIGN KEY (subject_id, subject_type) REFERENCES subjects (subject_id, subject_type)
) STRICT;
INSERT INTO records_new (record_id, subject_id, subject_type, record_type, source_kind, schema_version, payload_json, observed_at, observed_at_precision,
                         device_created_at, source_published_at, collected_at, recorded_at, effective_from, effective_to, supersedes_id)
  SELECT record_id, subject_id, subject_type, record_type, source_kind, schema_version, payload_json, observed_at, observed_at_precision,
         device_created_at, source_published_at, collected_at, recorded_at, effective_from, effective_to, supersedes_id FROM records;
DROP TABLE records;
ALTER TABLE records_new RENAME TO records;
CREATE INDEX records_by_subject ON records (subject_id, record_type, observed_at);
CREATE INDEX records_by_supersedes ON records (supersedes_id);
CREATE TRIGGER records_no_update BEFORE UPDATE ON records
BEGIN SELECT RAISE(ABORT, 'records 는 불변이다. 정정은 새 기록(supersedes_id)으로 추가한다'); END;
CREATE TRIGGER records_no_delete BEFORE DELETE ON records
BEGIN SELECT RAISE(ABORT, 'records 는 삭제하지 않는다. 개인정보 오입력은 별도 절차로 처리한다'); END;
"""

# J5-015B 사진 연차 비교 (데이터 사전 §6 "반복 촬영은 viewpoint_id·방향·이전 사진 참조를 선택 입력", 릴리스 계획 §8 R4).
# attachments 에 촬영 지점·방향·이전 사진 참조 열을 더하고, 이미 반영된 첨부는 수입 대장(import_events)의 고정 행 바이트에서 소급해 채운다.
MIGRATION_0010 = f"""
ALTER TABLE attachments ADD COLUMN viewpoint_id TEXT CHECK (viewpoint_id IS NULL OR length(viewpoint_id) > 0);
ALTER TABLE attachments ADD COLUMN heading_deg REAL CHECK (heading_deg IS NULL OR (heading_deg >= 0 AND heading_deg < 360));
ALTER TABLE attachments ADD COLUMN previous_photo_sha256 TEXT CHECK (previous_photo_sha256 IS NULL OR previous_photo_sha256 GLOB '{SHA256_GLOB}');
UPDATE attachments SET
  viewpoint_id = (SELECT json_extract(ref.value, '$.viewpoint_id') FROM import_events ie, json_each(CAST(ie.line AS TEXT), '$.attachment_refs') AS ref
                  WHERE ie.outcome = 'inserted' AND ie.record_id = attachments.record_id AND json_extract(ref.value, '$.sha256') = attachments.sha256 LIMIT 1),
  heading_deg = (SELECT json_extract(ref.value, '$.heading_deg') FROM import_events ie, json_each(CAST(ie.line AS TEXT), '$.attachment_refs') AS ref
                 WHERE ie.outcome = 'inserted' AND ie.record_id = attachments.record_id AND json_extract(ref.value, '$.sha256') = attachments.sha256 LIMIT 1),
  previous_photo_sha256 = (SELECT json_extract(ref.value, '$.previous_photo_sha256') FROM import_events ie, json_each(CAST(ie.line AS TEXT), '$.attachment_refs') AS ref
                           WHERE ie.outcome = 'inserted' AND ie.record_id = attachments.record_id AND json_extract(ref.value, '$.sha256') = attachments.sha256 LIMIT 1);
CREATE INDEX attachments_by_viewpoint ON attachments (viewpoint_id);
"""

# J5-015C 반대 증거·변경 조건 재확인 (데이터 사전 §8 "재확인 필요를 표시", 릴리스 계획 §8 R4 "반대 증거와 변경 조건", §9 "중요한 입력이 변경되면 영향받은 검토를 다시 요구하고 과거 승인 이력을 보존").
# 목표 매수가·투자판단 기록은 불변이므로 재확인은 별도 불변 기록이다. 재확인 시점에 본 신호(판단 뒤의 관측·연결·취소·범위 규칙·구성 변경)를 signals_json 으로 남겨
# 그 뒤 새 신호가 생기면 다시 "재확인 필요" 가 된다. 반대 증거는 자유 서술 + 선택 근거 문서(source_documents) 참조다.
RECHECK_OUTCOMES = ("reconfirmed", "revision_needed")
MIGRATION_0011 = f"""
CREATE TABLE judgment_rechecks (
  recheck_id              TEXT NOT NULL PRIMARY KEY CHECK (recheck_id GLOB '{UUID_GLOB}'),
  record_id               TEXT NOT NULL REFERENCES records (record_id),
  asset_id                TEXT NOT NULL REFERENCES assets (asset_id),
  outcome                 TEXT NOT NULL CHECK (outcome {_in(RECHECK_OUTCOMES)}),
  reviewed_on             TEXT NOT NULL CHECK (reviewed_on GLOB '{DATE_GLOB}'),
  conditions_checked_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(conditions_checked_json) AND json_type(conditions_checked_json) = 'array'),
  counter_evidence_json   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(counter_evidence_json) AND json_type(counter_evidence_json) = 'array'),
  signals_json            TEXT NOT NULL CHECK (json_valid(signals_json) AND json_type(signals_json) = 'object'),
  note                    TEXT,
  recorded_at             TEXT NOT NULL CHECK (recorded_at GLOB '{UTC_GLOB}')
) STRICT;
CREATE INDEX judgment_rechecks_by_record ON judgment_rechecks (record_id, recorded_at);
CREATE TRIGGER judgment_rechecks_no_update BEFORE UPDATE ON judgment_rechecks BEGIN
  SELECT RAISE(ABORT, 'judgment_rechecks 는 불변이다');
END;
CREATE TRIGGER judgment_rechecks_no_delete BEFORE DELETE ON judgment_rechecks BEGIN
  SELECT RAISE(ABORT, 'judgment_rechecks 는 삭제하지 않는다');
END;
"""

# (버전, 이름, SQL). 새 릴리스의 테이블은 새 항목으로 추가하고 기존 항목은 고치지 않는다.
MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (1, "r1b_minimum", MIGRATION_0001),
    (2, "r1b_import", MIGRATION_0002),
    (3, "r1b_projection", MIGRATION_0003),
    (4, "r2_survey", MIGRATION_0004),
    (5, "r2_parcels", MIGRATION_0005),
    (6, "r3_transactions", MIGRATION_0006),
    (7, "r3_links", MIGRATION_0007),
    (8, "r3_zones", MIGRATION_0008),
    (9, "r4_record_types", MIGRATION_0009),
    (10, "r4_photo_series", MIGRATION_0010),
    (11, "r4_rechecks", MIGRATION_0011),
)
# 표 재작성이 필요한 마이그레이션: 외래키 검사를 끈 채 한 트랜잭션으로 실행하고 foreign_key_check 가 비어야 커밋한다 (store._migrate).
FK_OFF_MIGRATIONS = frozenset({9})
DB_SCHEMA_VERSION = MIGRATIONS[-1][0]
MIN_SQLITE_VERSION = (3, 38, 0)  # STRICT 테이블(3.37)과 내장 json_valid/json_type(3.38)
