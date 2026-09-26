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

## 파일 (archive_schema 1.0.0, R6 J5-017B)

- `archive_manifest.schema.json`: 연말 개방형 포맷 보존본 폴더의 `archive_manifest.json`(`j5 db archive`). 정본의 모든 사용자 표를 `tables/<표>.jsonl`(값 보존)·`tables/<표>.csv`(편의 사본)로, 위치점·필지를 GeoJSON 으로, 표 정의 `schema.sql`·JSON Schema 사본 `schemas/`·`README.txt` 와 함께 담고 파일 목록·해시, 표별 행 수·열 정의, 도구 버전을 기록한다(데이터 사전 §12). 백업이 아니며 `archive-verify` 가 SQLite 없이 검증한다.

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

## 파일 (readiness_recheck 1.0.0, R7 J5-018B)

- `readiness_recheck.schema.json`: 계약 직전·잔금 직전 재확인 입력(`j5 db case-recheck`, `kind: stage_recheck`)과 `readiness_rechecks` 행(db_schema 14). 단계, 재확인일, 결과(`cleared`/`issues_found`), 항목(권리·임대차와 세무·법적·규제 필수; `confirmed` 는 근거 문서 1건 이상, `changed`/`unconfirmed` 는 문제), 문제 목록. 문제가 있으면 purchase_ready 철회 이력이 뒤따른다(데이터 사전 §11).

## 파일 (acquisition_review 1.0.0, R7 J5-018A)

- `acquisition_review.schema.json`: 매입 준비 검토 기록 입력(`j5 db case-add`, `kind: acquisition_review`)과 저장 payload(`$defs/acquisition_review_payload`). 전략·범위·가격(미확인은 null 과 사유)·참조 기록·매물 사건·체크리스트 8항목(`$defs/check`: 상태 6종, 근거 문서, 검토자, 검토일, 유효기한, 재검토 조건 8종, 미해결 사항, 해당 없음 사유). `verified` 는 검토일, `not_applicable` 은 사유, `conditional`/`blocked` 는 미해결 사항이 필수(데이터 사전 §11). purchase_ready 전환은 이 기록이 아니라 `readiness_decisions`(db_schema 13)에 남는다.

## 파일 (plan_records 1.0.0, R5 J5-016C)

- `plan_records.schema.json`: 규제 검토·개발안·자금안 기록 입력(`j5 db plan-add`, `kind: plan_record`)과 `records.record_type` `regulation_review`·`development_plan`·`financing_plan` 의 payload(`$defs/*_payload`). 규제 검토는 발행기관·고시번호·문서 단계(결정고시/입안공고/심의결과/보도자료/기타)·발표일·효력일·종료일을 두며 미확인 항목이 있으면 사유, 공식 자료면 근거 문서 1건 이상이 필수다(데이터 사전 §6). 입력 payload(`$defs/*_input`)에는 결과를 넣지 않고, 저장 payload 는 입력 + 저장 시점 계산 결과(`far_result` / `equity_result` / `cash_result`) + `calculation_version` 이다. 계산 입력은 `calc_inputs.schema.json` 의 정의를 상대 `$ref` 로 참조한다(`j5/schemas_loader.py` 의 registry 가 푼다).

## 파일 (calc_inputs, R5 J5-016A)

- `calc_inputs.schema.json`: 계산기 입력(`j5 calc far / equity / cash / plans`). `far` 는 대지(공부·대장·법정 제외·법정 산정·배치 검토영역·제약 목록)·규제(적용 용적률·규제 버전·종료일 확인)·현재 연면적(용적률 산정용·총연면적), `equity` 는 가격 기준(`gross_contract / net_after_deposit / settlement_residual / unknown`)·대출(실제/시나리오, 보증금 공제 반영 여부)·승계 보증금·비용 5종, `cash` 는 시점별 현금흐름(종류 enum, `owner_equity` 는 계산기가 거절)·별도 예비현금, `plans`(J5-016B)는 후보 개발안 목록(후보마다 `equity`·`cash` 입력, 감정가, 공사기간·공실, 공동매입 항목, 건축사 검토). 면적·금액은 값과 근거, 미확인은 `null` 과 사유(데이터 사전 §9~§10, ADR-08). 예제는 `tests/fixtures/calc/`.

스키마끼리의 상대 `$ref` 는 `$id`(https://j5.local/schemas/<파일>) 기준으로 registry 에서 푼다. 이벤트 해시는 파일에 있는 행의 UTF-8 바이트를 그대로 sha256 한 값이다. 재직렬화하지 않는다. 검증은 jsonschema(고정 버전, `requirements-dev.txt`) Draft 2020-12로 한다. 스키마와 데이터 사전이 충돌하면 PR을 완료하지 않는다.
