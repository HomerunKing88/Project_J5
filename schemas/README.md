# schemas/

관측 이벤트·패키지 manifest·`assets.seed.json`·정본 기록의 JSON Schema 계약. 의미·근거·예외는 `docs/DATA_DICTIONARY.md`가 설명하고 자료형·필수값은 여기 스키마가 검증한다.

시작 릴리스: R1a (J5-003). 버전은 `package_schema`·`db_schema` 등 역할별로 구분한다.

## 파일 (package_schema 1.0.0)

- `assets_seed.schema.json`: R1a 임시 정본 `assets.seed.json`. 위치점 또는 주소 중 하나 이상 필수, `notes`는 null 허용·생략 불가.
- `observation_event.schema.json`: `observations.jsonl` 한 행. R1은 `field_observation`만 허용한다. 선택 필드도 `null`로 명시한다. 변화 확인 시 사진 또는 설명 하나를 요구한다.
- `package_manifest.schema.json`: `manifest.json`. 경로는 `observations.jsonl`과 `photos/<sha256>.<jpg|png|webp>`만 허용한다.

## 파일 (projection_schema 1.0.0, R1b J5-011)

- `view_manifest.schema.json`: 조회 파생본 `.j5view.zip`의 `manifest.json`. `source_dataset_version`·`projection_schema_version`·`generated_at`·`scope_ids`·`data_mode`(데이터 사전 §4)와 파일 목록(`assets.seed.json`·`assets.geojson`·`records.jsonl`·선택 `photos/`)의 바이트·해시.

이벤트 해시는 파일에 있는 행의 UTF-8 바이트를 그대로 sha256 한 값이다. 재직렬화하지 않는다. 검증은 jsonschema(고정 버전, `requirements-dev.txt`) Draft 2020-12로 한다. 스키마와 데이터 사전이 충돌하면 PR을 완료하지 않는다.
