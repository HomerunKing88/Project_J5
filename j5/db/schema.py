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
RECORD_TYPES = ("field_observation",)  # R1b. 다른 종류는 해당 릴리스에서 추가한다 (데이터 사전 §2)
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

# (버전, 이름, SQL). 새 릴리스의 테이블은 새 항목으로 추가하고 기존 항목은 고치지 않는다.
MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (1, "r1b_minimum", MIGRATION_0001),
    (2, "r1b_import", MIGRATION_0002),
    (3, "r1b_projection", MIGRATION_0003),
)
DB_SCHEMA_VERSION = MIGRATIONS[-1][0]
MIN_SQLITE_VERSION = (3, 38, 0)  # STRICT 테이블(3.37)과 내장 json_valid/json_type(3.38)
