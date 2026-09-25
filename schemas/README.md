# schemas/

관측 이벤트·패키지 manifest·`assets.seed.json`·정본 기록의 JSON Schema 계약. 의미·근거·예외는 `docs/DATA_DICTIONARY.md`가 설명하고 자료형·필수값은 여기 스키마가 검증한다.

시작 릴리스: R1a (J5-003). 버전은 `package_schema`·`db_schema` 등 역할별로 구분한다.

## 파일 (package_schema 1.0.0)

- `assets_seed.schema.json`: R1a 임시 정본 `assets.seed.json`. 위치점 또는 주소 중 하나 이상 필수, `notes`는 null 허용·생략 불가.
- `observation_event.schema.json`: `observations.jsonl` 한 행. R1은 `field_observation`만 허용한다. 선택 필드도 `null`로 명시한다. 변화 확인 시 사진 또는 설명 하나를 요구한다. 첨부의 `viewpoint_id`·`heading_deg`·`previous_photo_sha256` 은 반복 촬영(연차 비교, J5-015B)용 선택 키이며 값이 있을 때만 넣는다(`null` 로 명시하지 않는다).
- `package_manifest.schema.json`: `manifest.json`. 경로는 `observations.jsonl`과 `photos/<sha256>.<jpg|png|webp>`만 허용한다.

## 파일 (projection_schema 1.0.0, R1b J5-011)

- `view_manifest.schema.json`: 조회 파생본 `.j5view.zip`의 `manifest.json`. `source_dataset_version`·`projection_schema_version`·`generated_at`·`scope_ids`·`data_mode`(데이터 사전 §4)와 파일 목록(`assets.seed.json`·`assets.geojson`·`records.jsonl`·선택 `parcels.geojson`(J5-013B-2, `counts.parcels`)·선택 `transactions.json`(J5-014B-3, `counts.transactions`)·선택 `photos/`)의 바이트·해시.

## 파일 (backup_schema 1.0.0, R1b J5-012)

- `backup_manifest.schema.json`: 정본 백업 폴더의 `backup_manifest.json`. `dataset_version`·`db_schema_version`·`created_at`·테이블별 행 수와 파일 목록(`db/j5.sqlite3`·`photos/<sha 앞 2자>/<sha>.<ext>`)의 바이트·해시(데이터 사전 §12). 복구 후 이 값과 대조한다.

## 파일 (survey_input 1.0, R2 J5-013A)

- `survey_input.schema.json`: 조사 경로·점포·표본틀·세션 수동 입력 파일(`j5 db survey-apply`). `kind` 로 구분한 네 종류(route_version / units / frame_version / session). 점포 관측 상태는 `occupied / vacant / closed_today / lease_ad_only / not_visited / unclear` 이며 앞의 둘만 확인(K)이다(데이터 사전 §5).

## 파일 (j5parcels 1.0.0, R2 J5-013B-1)

- `parcels_bundle.schema.json`: 필지 경계·지번 번들 `.j5parcels.json`(`j5 parcels convert` 출력, 폰 지도 입력). GeoJSON FeatureCollection 에 `j5parcels`·`data_mode(synthetic|real)`·`source`(자료명·파일·해시·좌표계·인코딩·도형 기준일·이용허락)·`clip`·`count`·`bbox`·`warnings` 를 더한 것. 필지 속성은 `pnu`(19자리)·`label`·`emd_code`·`emd_name`·`mountain`·`bon`·`bu`·`jimok`·`jibun_raw`·`jibun_mismatch`·`area_m2_geom`(도형면적, 공부면적 아님. 지리좌표 원본이면 null)·`area_missing_reason`·`bbox`. 상한 8,000 필지(ADR-13). 파생본(`j5 db project` 의 `parcels.geojson`)은 같은 형식에 필지마다 `geometry_version`·정본 연결 물건 `asset_ids` 를 더한다 (J5-013B-2).
- `asset_components_input.schema.json`: 물건↔필지 구성 연결 입력(`j5 db parcels-link`), `parcels-suggest` 의 출력 형식. `asset_id`·`pnu`·적용 기간·근거(`manual | location_point`)·비고. PNU 는 정본 parcels 에 먼저 있어야 한다.

## 파일 (zone_rules, R3 J5-014B-3)

- `zone_rules.schema.json`: 핵심·비교 범위 규칙 입력(`j5 db rt-zones apply`). `name`·`core`(법정동 이름 목록, 1개 이상)·`comparison`(비워도 됨)·`note`. 같은 법정동은 한 범위에만. 반영하면 새 규칙 버전이 되고 모든 거래를 다시 분류한다.
- 파생본 `transactions.json`(j5transactions 1.0.0, `j5 db project`): 핵심·비교 범위의 취소 확정 아닌 거래 목록. `view_manifest.schema.json` 의 파일 목록·`counts.transactions` 로 검증한다. 별도 스키마 파일 없이 검증기(`verify_projection_dir`)가 건수·버전·범위·연결 물건을 대조한다.

## 파일 (judgment_records 1.0.0, R4 J5-015A)

- `judgment_records.schema.json`: 목표 매수가·투자판단 기록 입력(`j5 db record-add`)과 `records.record_type` `target_price`·`investment_judgment` 의 payload(`$defs/target_price_payload`, `$defs/investment_judgment_payload`). 목표 매수가는 가격 종류·원 단위 가격·판단일·전략·구성 기준일·전제·비교 근거·유효 조건·수정 사유, 투자판단은 10항목과 status(draft/formal)·decision(none/approved/withdrawn). formal 은 10항목 모두, 승인은 formal 만(데이터 사전 §8).

## 파일 (judgment_recheck, R4 J5-015C)

- `judgment_recheck.schema.json`: 재확인 입력(`j5 db recheck-add`)과 `judgment_rechecks` 행. 현재 목표 매수가·투자판단 기록을 가리키고 결과(`reconfirmed` / `revision_needed`)·재확인일·조건 대조(`condition`, `holds` true/false/null, `note`)·반대 증거(`note`, 선택 `document_id`·`observed_on`)·메모를 둔다. 재확인 시점의 신호 요약은 저장소가 붙인다. 판단 자체는 바꾸지 않는다(데이터 사전 §8, 릴리스 계획 §8·§9).

## 파일 (calc_inputs, R5 J5-016A)

- `calc_inputs.schema.json`: 계산기 입력(`j5 calc far / equity / cash / plans`). `far` 는 대지(공부·대장·법정 제외·법정 산정·배치 검토영역·제약 목록)·규제(적용 용적률·규제 버전·종료일 확인)·현재 연면적(용적률 산정용·총연면적), `equity` 는 가격 기준(`gross_contract / net_after_deposit / settlement_residual / unknown`)·대출(실제/시나리오, 보증금 공제 반영 여부)·승계 보증금·비용 5종, `cash` 는 시점별 현금흐름(종류 enum, `owner_equity` 는 계산기가 거절)·별도 예비현금, `plans`(J5-016B)는 후보 개발안 목록(후보마다 `equity`·`cash` 입력, 감정가, 공사기간·공실, 공동매입 항목, 건축사 검토). 면적·금액은 값과 근거, 미확인은 `null` 과 사유(데이터 사전 §9~§10, ADR-08). 예제는 `tests/fixtures/calc/`.

이벤트 해시는 파일에 있는 행의 UTF-8 바이트를 그대로 sha256 한 값이다. 재직렬화하지 않는다. 검증은 jsonschema(고정 버전, `requirements-dev.txt`) Draft 2020-12로 한다. 스키마와 데이터 사전이 충돌하면 PR을 완료하지 않는다.
