# j5/

PC용 Python CLI. 공식 API 수집·정본 반영·검증·조회 패키지 생성·백업·필지 번들 변환을 담당한다. 지원 중인 Python(3.11+)과 표준 라이브러리를 우선하며, JSON Schema 검증에 jsonschema 고정 버전을 쓴다.

실데이터 경로는 환경변수 `J5_DATA_HOME`으로 받고 코드에 넣지 않는다. 이 도구는 입력 파일을 수정·삭제하지 않는다.

## 사용 (R1a, J5-008)

```text
python -m j5 inspect <package.j5field.zip 또는 풀어 놓은 폴더> [--seed assets.seed.json] [--study-id ID] [--json]
python -m j5 copy <package.j5field.zip> <대상 폴더>
```

- `inspect`: 데이터 사전 §3.2·§3.3의 검사(경로 탈출·크기 한도·manifest 스키마·파일 해시·사진 형식·이벤트 스키마·첨부 참조·중복/충돌·시드 연결). 종료 코드 0 ok / 1 reject / 2 hold(같은 ID·다른 내용) / 3 사용 오류. `--study-id`를 생략하면 `J5_STUDY_ID`를 쓰고, 둘 다 없으면 비교하지 않는다.
- `copy`: 독립 사본. 덮어쓰지 않고, 복사 후 다시 읽어 해시를 확인하며, `<이름>.sha256`을 남긴다. 사본 작성은 정본 반영·백업 완료가 아니다.

## 정본 SQLite (R1b, J5-009)

```text
python -m j5 db [--db 경로] init --study-id ID --data-mode synthetic|private_real
python -m j5 db [--db 경로] status [--json]
python -m j5 db [--db 경로] load-seed assets.seed.json
```

- 경로는 `--db` 또는 `J5_DATA_HOME/db/j5.sqlite3`. 저장소 안에 두지 않는다(`.gitignore`가 `*.sqlite3`를 막는다). `init`은 기존 파일을 덮어쓰지 않는다.
- 스키마(`j5/db/schema.py`, db_schema 1): `meta`, `schema_migrations`, `subjects`, `assets`, `source_documents`, `records`, `record_evidence`, `attachments`. 모든 테이블은 STRICT, 열거값·시각 모양·JSON 유효성은 CHECK, 대상 존재는 `subjects(subject_id, subject_type)` 복합 외래키(ADR-09). 모든 연결에서 외래키를 켜고 켜졌는지 확인한다. `records`는 트리거로 UPDATE·DELETE를 막는다(정정은 새 기록 + `supersedes_id`).
- `load-seed`: `assets.seed.json`의 `asset_id`를 그대로 승계해 `subjects/assets`에 넣는다. 전체 성공 또는 전체 거절. 같은 ID·같은 내용은 변화 없음, 서술 필드(label·위치·주소·비고)만 갱신하며 관심 단계·확인 상태는 시드로 바꾸지 않는다. 신규·갱신이 하나라도 있으면 `dataset_version`을 1 올리고(정본이 바뀌었으므로 이전 파생본은 구본), 변화 없음만이면 올리지 않는다(데이터 사전 §4).
- 정본 시각 `recorded_at`은 저장소가 항상 부여한다. 입력(기록·근거·첨부·출처 문서)이 가져온 `recorded_at`은 거절해 기기 입력 시각·관측 시각과 정본 반영 시각을 섞지 않는다. 트랜잭션은 바깥 `BEGIN IMMEDIATE`, 중첩은 `SAVEPOINT`라 중첩 단위의 실패는 바깥이 예외를 잡더라도 되돌려진다.
- `status`: 스키마 버전(정본·도구), study_id, data_mode, dataset_version, 행 수, `integrity_check`, `foreign_key_check`. 위반이 있으면 종료 코드 1.
- Python API(`j5.db.store.Db`): `create/open`, `transaction()`, `load_seed`, `add_source_document`, `insert_record(rec, evidence=, attachments=)`, `get_record/list_records/list_assets`. 공통 검증(`j5.db.validate`): UUID, 시간대 있는 ISO 8601, 날짜 정밀도, 정본 시각 UTC, record_type↔대상 타입, payload 스키마.

## 단방향 반영 (R1b, J5-010)

```text
python -m j5 db [--db 경로] import <package.j5field.zip 또는 폴더> [--data-home 경로] [--json]
```

- 검사(`inspect` 와 같은 규칙, 정본의 물건 목록·study_id·data_mode 대조) → 같은 파일 재입력·이벤트 대장 대조 → 새 이벤트가 참조한 사진을 `J5_DATA_HOME/photos/<sha 앞 2자>/<sha>.<ext>` 에 보관 → 기록·근거·첨부·수입 기록·`dataset_version` 을 한 트랜잭션으로 반영(db_schema 2: `import_runs`, `import_events`).
- 판정과 종료 코드: `applied`(0), `duplicate`(0, 새 이벤트 없음·버전 유지), `held`(2, 같은 ID·다른 내용 또는 정정 대상 없음: 전체 보류), `rejected`(1), `failed`(1, 파일·DB 실패: DB 롤백). 어느 경우에도 입력 파일은 바꾸지 않는다.
- DB 실패로 되돌린 뒤 이미 보관한 사진은 정리대기로 결과·`import_runs`·`logs/import.log` 에 남기고 자동 삭제하지 않는다. 재시도하면 같은 사진을 재사용한다.
- 반영은 백업·파생본 생성 완료가 아니다(J5-011·012).

PC에서 실행하려면 저장소 루트에서 `pip install -r requirements-dev.txt` 후 `python -m j5 ...`. `pip install -e .`를 하면 `j5` 명령으로도 쓸 수 있다. SQLite 3.38 이상이 필요하다(STRICT 테이블·내장 JSON 함수).

## 조회 파생본 (R1b, J5-011)

```text
python -m j5 db [--db 경로] project [--data-home 경로] [--photos] [--json]
python -m j5 db [--db 경로] project-copy <대상 폴더> [--data-home 경로] [--allow-stale]
```

- `project`: 고정 dataset_version 의 정본에서 `assets.seed.json`(시드 스키마 그대로)·`assets.geojson`·`records.jsonl`(·`photos/`)·`manifest.json` 을 전량 생성 → 다시 읽어 행수·참조·필수값·버전·해시 검증 → `.j5view.zip` 작성·항목 해시 대조 → `J5_DATA_HOME/exports/private/projections/ds<버전>-<run8>/` 게시 → `latest.json` 포인터 교체(db_schema 3: `projection_runs`). 실패하면 이전 파생본·포인터를 그대로 두고 부분 산출물은 `failed-<run8>/` 에 격리한다. 정본은 손대지 않는다.
- `status [--data-home]` 가 `파생본: 최신 / 구본 (정본 vN, 파생본 vM) / 손상·없음 (재생성 필요)` 과 마지막 게시·실패 시각을 보여준다. 포인터와 ZIP 파일을 실제로 확인하며, 실데이터 홈을 모르면 `파일 미확인` 으로 표시하고 '최신' 이라 하지 않는다.
- `project-copy`: 게시된 최신 ZIP 의 독립 사본(덮어쓰기 금지·재검증·sha256 사이드카). 파생본이 정본보다 오래되면 막고, `--allow-stale` 일 때만 구본임을 표시하며 내보낸다.
- 폰 앱은 ZIP 을 풀어 `assets.seed.json` 을 "시드 파일 불러오기" 로 넣는다(앱의 ZIP 직접 읽기는 이후 작업).

## 백업·복구·사진 대사 (R1b, J5-012)

```text
python -m j5 db [--db 경로] backup [--data-home 경로] [--dest 상위폴더] [--json]
python -m j5 db backup-verify <백업 폴더> [--json]
python -m j5 db restore <백업 폴더> <빈 폴더> [--json]
python -m j5 db [--db 경로] check-photos [--data-home 경로] [--json]
```

- `backup`: 정본 연결의 읽기 스냅샷 안에서 버전·행 수·첨부 목록을 읽고 SQLite 백업 API 로 일관된 사본을 만든다(그동안 다른 쓰기는 대기). 사본을 읽기 전용으로 열어 무결성·외래키·행 수·meta 를 원본과 대조하고, 첨부가 참조한 사진을 해시 검증하며 복사한 뒤 `backup_manifest.json`(`schemas/backup_manifest.schema.json`, 파일 목록·해시)을 쓴다. 전부 다시 읽어 검증한 뒤에만 `J5_DATA_HOME/backups/<YYYYMMDD-HHMMSS>-ds<버전>-<run8>/` 로 게시하고 `backups/latest.json` 포인터를 교체한다. `--dest` 로 외장 드라이브 등 다른 상위 폴더를 지정할 수 있다(포인터는 절대경로를 기록). 정본은 바꾸지 않는다.
- 참조 사진이 하나라도 없거나 해시가 다르면 백업은 실패다(사진 없는 DB 사본을 완전한 백업이라 하지 않는다). 실패하면 이전 백업·포인터는 그대로고 부분 산출물은 `failed-<run8>/` 에 남는다. 결과는 `logs/backup.log` 에 남으며, 백업 완료는 파생본 게시·외부 사본 보관을 뜻하지 않는다. 오래된 백업은 자동 삭제하지 않는다.
- `status [--data-home]` 가 `백업: 최신 (정본 vN) / 백업 필요 (정본 vN, 마지막 백업 vM) / 없음 / 손상 / 접근 불가` 와 마지막 백업 시각·위치를 보여준다. 백업 기록은 정본에 두지 않으므로 data_home 없이는 `파일 미확인` 이다.
- `backup-verify`: 백업 폴더를 전부 다시 읽어 검증한다(파일 해시, DB 사본 무결성·외래키·행 수·meta, 정본 사본이 참조한 사진 전부가 같은 해시로 있음). 정본 연결이 필요 없다.
- `restore`: 빈 폴더(또는 이전 복구 시도의 `logs/` 만 있는 폴더)에만 복구한다. 백업 검증 → 스테이징 복사(해시 재검증) → 복사한 정본을 열어 무결성·행 수·사진 연결 확인 → `db/`·`photos/` 를 제자리로 → 최종 확인 → `logs/restore.log`. 기존 정본·사진이 있는 폴더는 거절한다. 복구한 폴더에는 백업·파생본이 없으므로 `backup`·`project` 를 다시 실행한다. 도구가 백업보다 새로우면 여는 시점에 마이그레이션이 적용된다.
- `check-photos`(사진 대사): 정본이 참조한 사진의 존재·크기·해시와 `photos/` 의 미참조 파일을 보고한다. 아무것도 지우지 않는다(미참조 파일은 정리대기 후보로 표시만). 문제가 있으면 종료 코드 1.
- 운영 순서(데이터 사전 §12 "반영 배치마다 백업"): `import` → `backup` → `project`. 각 결과는 따로 표시되며 서로를 뜻하지 않는다.

## 조사 경로·점포·표본틀·세션 (R2, J5-013A)

```text
python -m j5 db [--db 경로] survey-apply <입력.json> [--json]
python -m j5 db [--db 경로] survey-vacancy <session_id> [--json]
python -m j5 db [--db 경로] survey-compare <session_a> <session_b> [--json]
python -m j5 db [--db 경로] survey-overview [--json]
```

- 입력은 `schemas/survey_input.schema.json` 의 JSON 파일 네 종류(`kind`: `route_version` 경로 버전·구간 목록 / `units` 점포와 분할·통합 링크 / `frame_version` 표본틀 버전·포함 점포 / `session` 조사 세션·방문·건너뜀 구간·점포 관측). 예제는 `tests/fixtures/survey/`. 편집기·길찾기는 없다(데이터 사전 §5, ADR-04).
- 경로 버전·표본틀 버전·세션은 불변이다. 같은 ID·같은 내용은 변화 없음, 같은 ID·다른 내용은 거절이며 수정은 `previous_version_id` 로 이은 새 버전(`change_reason` 필수)·새 세션이다. 점포의 서술 필드(위치·층·연결 물건·종료일)는 갱신할 수 있고 업체명·업종은 세션 관측값이다. 정본이 바뀐 배치마다 `dataset_version` 이 오른다.
- 세션 안 점포 관측은 점포당 한 행이다. 한 점포가 여러 구간에 걸쳐도 두 번 세지 않는다(같은 unit_id 두 번이면 거절).
- `survey-vacancy`: N(표본틀 점포 수)·K(occupied·vacant 로 확인한 수)·V(vacant)·관측표본 공실비율(V/K)·상태 확인률(K/N) 과 미확인(당일 휴무·임대 광고만·미방문·확인 불가)·기록 없음(미조사)·표본틀 밖 관측 수. K 또는 N 이 0 이면 비율은 null. 지역 전체 공실률이라고 부르지 않는다.
- `survey-compare`: 두 세션의 시점 비교. 양쪽 표본틀에 있고 두 세션 모두 확인된 공통 점포로 따로 계산하며, 두 시점 사이의 분할·통합 링크가 걸린 점포는 비교 단절로 표시하고 공통 표본에서 뺀다. 각 세션의 전체 값은 섞지 않고 함께 표시한다.
- 관측 이벤트가 적은 `route_version_id`·`frame_version_id` 는 `record_survey_refs` 에 남긴다(records 는 불변, 외래키 없음). 정본에 없는 경로 버전을 참조한 기록 수를 `survey-overview` 가 표시하며, 경로 버전 파일을 반영하면 연결된다.
- 필지·건축물 정규화와 외부 자료 수집(J5-013B)은 별도 작업이다. 조사 자료는 백업에 포함되고, 파생본(`project`)에는 아직 들어가지 않는다.

이후: J5-013B(필지·건축물), R1b 실기기 시험·두 차례 실사용.

## 필지 경계·지번 번들 (R2, J5-013B-1, ADR-13)

```text
python -m j5 parcels inspect <연속지적도.shp 또는 .zip> [--crs EPSG:5186] [--encoding cp949] [--layer 이름] [--json]
python -m j5 parcels convert <연속지적도.shp 또는 .zip> --out <이름>.j5parcels.json --geometry-version YYYY-MM-DD --source-name "연속지적도 서울특별시 종로구" \
    (--bbox minlon,minlat,maxlon,maxlat | --center lon,lat --radius-m 400) [--crs] [--encoding] [--license "…"] [--emd-name 1111017500=종로5가]… [--json]
```

- `inspect`: 필드·레코드 수·도형 종류·원본 bbox·좌표계 판정(.prj → 알려진 EPSG 5173~5188 표)·WGS84 bbox·표본 레코드. 변환 전에 WGS84 bbox 가 종로 부근인지 확인한다(좌표계를 잘못 고르면 수백 m 이상 어긋난다).
- `convert`: 범위와 겹치는 필지만 WGS84 GeoJSON 번들(`schemas/parcels_bundle.schema.json`)로 만든다. PNU(19자리)·지번 라벨·지목·도형면적(공부면적 아님)·bbox 를 속성으로 두고, 같은 PNU 는 MultiPolygon 으로 합친다. 원본은 수정하지 않고 출력은 임시 파일 검증 뒤 교체하며 덮어쓰지 않는다. 상한 8,000 필지(`--max-features`, 계약 상한 이하만). ZIP 은 항목·합계·압축비 상한을 검사한다.
- 좌표계 변환은 표준 라이브러리로 구현했다(횡축 메르카토르 Krüger 급수, Korean 1985 데이텀은 EPSG 공식 Molodensky-Badekas). pyproj 계산값과 mm 이내로 대조했다(`tests/test_j5_013b_parcels.py`). .prj 가 없으면 `--crs` 가 필요하다.
- 번들은 폰 앱의 "필지 파일 불러오기" 로 넣는다. 실제 번들·원본 SHP 는 `J5_DATA_HOME/raw/`, `J5_DATA_HOME/exports/private/` 등 실데이터 홈에 두고 저장소에 넣지 않는다. 이용허락 유형은 배포처에서 확인해 `--license` 로 기록한다.
- 정본 반영·연결 (J5-013B-2):

```text
python -m j5 db [--db 경로] parcels-load <번들.j5parcels.json> [--json]
python -m j5 db [--db 경로] parcels-suggest [--out 제안.json] [--effective-from YYYY-MM-DD]
python -m j5 db [--db 경로] parcels-link <제안.json> [--json]
```

  `parcels-load` 는 PNU 기준으로 정본 `parcels`(db_schema 5) 에 넣는다(같은 내용 변화 없음, 더 새로운 도형 기준일이면 갱신, 같은 기준일·다른 내용이나 더 오래된 기준일은 거절). 번들 data_mode 는 정본과 맞아야 하고(synthetic ↔ synthetic, real ↔ private_real), 반영한 번들은 `source_documents` 에 남는다. `parcels-suggest` 는 물건 위치점을 품는 필지를 찾아 연결 제안 파일(`schemas/asset_components_input.schema.json`)을 만들 뿐 정본에 쓰지 않는다. 검토·수정한 파일을 `parcels-link` 로 반영하면 `asset_components` 에 적용 기간·근거와 함께 저장되고 dataset_version 이 오른다. 이후 `project` 의 파생본에 `parcels.geojson`(필지 + 연결 물건 `asset_ids`)이 들어가며 폰의 "필지 파일 불러오기" 로 그대로 넣는다.

## 실거래 API 표본 수집 (R0 실측, J5-014A)

```text
python -m j5 collect rt-sample --lawd-cd 11110 --months 2026-08,2021-09,2006-03 [--endpoint URL] [--num-rows 1000] [--max-pages 50] [--data-home] [--config] [--json]
python -m j5 collect rt-report --lawd-cd 11110 --months 2026-08,2021-09,2006-03 [--run-id] [--dist umdNm] [--json]
```

- 인증키는 `J5_DATA_HOME/config.env` 의 `DATA_GO_KR_SERVICE_KEY=…`(권한 600) 또는 같은 이름의 환경변수에서만 읽는다. 로그·기록·요약·출력에 남기지 않는다. 저장소의 `.env`·예시 파일에는 값을 넣지 않는다.
- `rt-sample` 은 상업·업무용 부동산 매매 실거래 API 를 시군구(5자리)·계약월(YYYYMM)·페이지 단위로 호출해 원본 응답을 `raw/rt_nrg/<시군구>/<YYYYMM>/` 에 그대로 두고, 요청 기록(`run-*.json`, 키 가림)과 요약(`report-*.json`: 필드 채움 비율·`*` 마스킹·값 분포)을 만든다. 월 결과는 `complete / empty(정상 0건) / partial(페이지 누락) / failed` 로 구분한다. 정본에는 쓰지 않는다(정규화·연결은 R3).
- 기본 엔드포인트는 코드에 적힌 알려진 주소이며 공공데이터포털 API 상세 페이지의 주소와 대조한다. 호출은 포털 일일 트래픽을 쓴다.
- `rt-report` 는 그 실행의 기록 파일에서 totalCount·수집 결과를 함께 보인다. `--dist FIELD` 를 주면 그 필드는 값 종류가 20개를 넘어도 전체 분포를 보인다(법정동별 건수 등).
- 나에게 보낼 것은 `rt-report` 의 요약(또는 `report-*.json`)이다. 원본 행·인증키는 보내지 않는다.

## 거래 범위 수집·정본 반영·현황 (R3, J5-014B-1)

```text
python -m j5 collect rt-fetch --lawd-cd 11110 --from 2021-09 --to 2026-09 [--refresh] [--endpoint URL] [--num-rows] [--max-pages] [--data-home] [--config] [--json]
python -m j5 db [--db 경로] rt-load --lawd-cd 11110 [--run-id 실행ID] [--data-home 경로] [--json]
python -m j5 db [--db 경로] rt-coverage --lawd-cd 11110 --from 2006-01 --to 2026-09 [--json]
```

- `rt-fetch` 는 계약월 범위를 받되 실행 기록 파일(`run-*.json`)에서 이미 complete/empty 인 달을 건너뛴다. `--refresh` 는 취소·정정 점검용으로 다시 받는다. 원본은 `rt-sample` 과 같은 자리에 쌓이고 정본에는 쓰지 않는다.
- `rt-load` 는 정본에 없는 실행 기록을 오래된 순으로 반영한다(db_schema 6: `collection_runs`, `collection_pages`, `transaction_observations`, `transactions`). 원본 XML 의 sha256·항목 수가 기록과 다르거나 기록이 깨졌으면 거절하고 정본을 바꾸지 않는다. 같은 실행은 다시 반영하지 않는다. 실제 제공자 응답은 `private_real` 정본에만 넣는다.
- 거래는 (제공자, 시군구, 계약월, 식별 해시, 순번) 으로 한 행이다. 식별 해시는 취소·거래 유형·중개사·매수/매도 구분처럼 나중에 채워지는 필드를 빼고 만들며, 같은 응답 안의 완전히 같은 행은 순번으로 구분한다. 같은 달을 다시 받으면 다시 보인 거래의 가변 필드(취소 등)를 갱신하고, 완전한 응답에서 사라진 거래는 `missing_since_run_id` 로만 표시한다(취소 확정 아님).
- 금액은 만원 → 원(`amount_krw`), 계약일·면적·건축년도·취소일은 해석 불가 시 null + `missing_reasons_json` 사유. 지번 마스킹(`jibun_masked`)과 앞자리(`jibun_prefix`)를 따로 둔다. 초기 범위는 지분 → `partial_share`, 집합 → `strata_unit`, 그 외 `unclear`.
- `rt-coverage` 는 월별 수집 결과와 거래·취소·사라짐·마스킹·연결 수를 보인다. 거래·연결 등 현재 통계는 취소 확정 거래를 빼고 세며 취소 건수는 따로 보인다. 수집 완료 기간과 물건 연결 완료 건수는 별개다. 연결(후보 CSV·수동 연결)은 다음 조각.
- `backup` 은 정본이 참조한 수집 원본(실행 기록·응답 XML, `raw/rt_nrg/`)도 해시를 검증하며 함께 담고, `restore` 는 `raw/` 까지 제자리에 되돌려 대조한다. 원본이 없거나 바뀌었으면 백업을 완료로 표시하지 않는다.

## 거래↔물건 연결: 후보 CSV·수동 연결 (R3, J5-014B-2)

```text
python -m j5 db [--db 경로] rt-candidates --lawd-cd 11110 --from 2021-09 --to 2026-09 [--emd 종로5가,종로6가] [--unlinked-only] [--out 후보.csv] [--json]
python -m j5 db [--db 경로] rt-link 후보.csv [--json]
```

- `rt-candidates` 는 범위 안의 현재 거래(취소 확정 제외)마다 물건에 연결된 필지의 법정동·지번과 대조해 후보 물건을 붙인 CSV 를 낸다(db_schema 7). 지번이 온전하면 `jibun_exact`, 마스킹이면 앞자리·자릿수 범위로 `jibun_prefix`. 정본에 쓰지 않으며 `--out` 은 덮어쓰지 않는다.
- CSV 의 결정 열 `decision`(candidate / pending_evidence / confirmed / withdrawn)·`asset_id`·`decision_scope`·`basis_kind`(jibun_exact / jibun_prefix / manual / document)·`reviewed_on`·`note` 를 스프레드시트에서 채워 저장한 뒤 `rt-link` 로 반영한다. 결정 열이 빈 행은 건너뛴다. 한 행이라도 오류면 전체 거절.
- 거래당 유효한 연결은 하나다. 다른 물건으로 바꾸면 이전 연결을 철회(대체)하고 새 연결을 만들며, 상태 변화는 `review_decisions` 에 불변 이력으로 남는다. 앞자리 범위(`jibun_prefix`)만으로 `confirmed` 로 두지 않고, 취소 확정 거래는 확정하지 않는다.
- 릴리스 계획 §4 의 결정대로 연결 검토 화면 대신 이 CSV·수동 확인으로 운영한다.

## 범위 분류·취소/정정 점검·파생본 거래 목록 (R3, J5-014B-3)

```text
python -m j5 db [--db 경로] rt-zones show | apply <규칙.json>
python -m j5 collect rt-recheck --lawd-cd 11110 --recent 6 | --failed [--from 2006-01 --to 2026-09] [--endpoint URL] [--data-home] [--config] [--json]
python -m j5 db [--db 경로] rt-changes --lawd-cd 11110 --since-run <실행ID> [--zone core] [--json]
```

- `rt-zones apply` 는 법정동 목록으로 핵심(core)·비교(comparison) 범위를 정한 규칙(`schemas/zone_rules.schema.json`)을 새 버전으로 넣고 모든 거래를 다시 분류한다(db_schema 8, `transactions.zone`). 규칙 밖 법정동은 outside 다. `rt-load` 는 현재 규칙으로 새 거래를 분류한다. 지번 단위 경계는 두지 않는다.
- `rt-recheck` 는 취소·정정 점검용 재수집이다. `--recent N` 은 이번 달 포함 최근 N개월, `--failed` 는 실행 기록상 complete/empty 가 아닌 달을 다시 받는다. 반영은 `rt-load`, 변경은 `rt-changes` 로 본다(새 거래·취소로 바뀜·응답에서 사라짐). 사라짐은 취소 확정이 아니다.
- `project` 의 파생본에 `transactions.json`(핵심·비교 범위의 취소 확정 아닌 거래, 확정 연결의 `asset_id`)이 들어간다. 범위 규칙이 없으면 파일이 없다. 폰 화면 표시는 이후 조회 작업이다.
- 운영 주기(데이터 사전 §7.1): 월 1회 `rt-recheck --recent 6` → `rt-load` → `rt-changes`, 분기 `rt-recheck --failed`, 연 1회 전 기간 `rt-fetch --refresh`.

## 목표 매수가·투자판단 기록과 당시 기록 기준 조회 (R4, J5-015A)

```text
python -m j5 db [--db 경로] record-add <입력.json> [--json]
python -m j5 db [--db 경로] asof --asset <asset_id> --at YYYY-MM-DD [--known-by YYYY-MM-DD | --as-recorded] [--json]
```

- `record-add` 는 목표 매수가(`target_price`)·투자판단(`investment_judgment`, 10항목·초안 허용) 기록을 불변 기록으로 넣는다(`schemas/judgment_records.schema.json`, db_schema 9). 수정·승인·철회는 새 기록 + `supersedes_id` 다. 판단일은 `observed_at`(날짜), 정본 반영 시각은 저장소가 부여한다. 근거 문서는 `source_documents` 에 먼저 둔다.
- `asof` 는 물건의 시점 T 상태를 재구성한다. 기본은 관측 기준(현재 보유 근거 전부). `--known-by K`(또는 `--as-recorded`, K=T)는 정본 기록일이 K 이하인 기록·필지 연결·거래 연결의 검토 결정·거래·취소 표시만으로 만든다. 나중에 넣은 정정·연결 철회·소급 수집한 과거 거래는 당시 보기에 들어가지 않는다. 마지막 확인일과 "현재 경계 위의 과거 속성"(도형 기준일이 T 뒤) 을 표시한다.
- db_schema 9 는 `records` 표를 다시 만든다(ADR-14). 실제 정본에는 백업을 먼저 만든 뒤 `j5 db status` 로 버전 9·외래키 켜짐·무결성을 확인한다.

## 사진 연차 비교 (R4, J5-015B)

```text
python -m j5 db [--db 경로] photo-series --asset <asset_id> [--tag front|ground_floor|lease_ad|construction|road|parking|adjacency] [--export] [--data-home 경로] [--json]
```

- 물건의 사진(정본 `attachments`, db_schema 10)을 촬영 지점(`viewpoint_id`) → 이전 사진 사슬(`previous_photo_sha256`) → 태그 조합 순으로 묶고, 묶음마다 연도별 첫 사진·인접 연도 비교 쌍·빠진 해를 보인다. 관측하지 않은 해는 비워 두고 보간하지 않는다. 정정된 관측의 사진은 `정정됨` 으로 표시한다.
- `--export` 는 `J5_DATA_HOME/exports/private/photo_series/<물건>-<id 앞 8자>/<묶음>/<날짜>-<sha 앞 8자>.<ext>` 로 사진을 해시 검증하며 복사하고 `index.json` 을 쓴다. 이미 있는 파일은 같은 내용이면 건너뛰고 다른 내용이면 덮어쓰지 않고 문제로 보고한다(종료 코드 1). 내보낸 사진은 실데이터이며 저장소·공개 배포에 넣지 않는다.
- 폰 앱이 사진마다 촬영 지점·방향·이전 사진을 선택 입력한다(J5-006 이벤트 바이트는 그대로). db_schema 10 은 이미 반영된 첨부를 수입 대장의 행 바이트에서 소급해 채운다.

## 반대 증거·변경 조건 재확인 (R4, J5-015C)

```text
python -m j5 db [--db 경로] recheck --asset <asset_id> [--json]
python -m j5 db [--db 경로] recheck-add <입력.json> [--json]
```

- `recheck` 는 현재(수정되지 않은) 목표 매수가·투자판단 기록마다 조건 목록(유효 조건·전제·가격차 변화 조건·판단 변경 조건·반대 근거·다음 확인사항), 판단 기록이 정본에 들어온 뒤 들어온 새 정보(관측·연결 결정·취소 표시·비교 근거 거래 변동·범위 규칙·구성 변경·반박 근거), 마지막 재확인을 보이고 상태를 `새 신호 없음 / 재확인 필요 / 재확인됨 / 수정 필요` 로 표시한다. 조건이 깨졌는지는 사람이 대조한다.
- `recheck-add` 는 재확인 기록(`schemas/judgment_recheck.schema.json`, `kind: recheck`, db_schema 11)을 불변으로 넣는다. 결과(`reconfirmed` / `revision_needed`), 재확인일, 조건 대조, 반대 증거(선택 근거 문서는 `source_documents` 에 먼저), 그 시점의 신호 요약을 남긴다. 판단을 바꾸려면 `record-add` 로 새 기록(`supersedes_id`)을 넣는다. 수정된 기록은 재확인할 수 없다.

## 계산기: 여유면적·필요자기자본·최대 현금 (R5, J5-016A)

```text
python -m j5 calc far <입력.json> [--json]
python -m j5 calc equity <입력.json> [--json]
python -m j5 calc cash <입력.json> [--json]
python -m j5 calc plans <입력.json> [--csv 경로] [--json]
```

- 입력은 `schemas/calc_inputs.schema.json`(`kind: far / equity / cash`). 면적·금액은 값과 근거를 함께 두고 미확인은 `null` 과 사유다. 예제는 `tests/fixtures/calc/`(데이터 사전 §9~§10 의 가상 검증 사례).
- `far`: 검토면적 = A×F/100, 여유면적 = A×F/100 − C, 소진율 = C/(A×F/100). A 는 법정 산정 대지면적(직접 또는 공부면적 − 확인된 제외면적), 배치 검토영역은 A 를 대신하지 않는다. 미확인 입력(규제 버전 없음, 제약의 분모 제외 여부 미확인 포함)이면 결과 없음, 분모 0 은 오류, 음수 여유면적은 그대로.
- `equity`: P(보증금 차감 전 계약 총액) − L − D + T + B + V + R + E. 순액 입력은 확인된 차감액으로 P 를 복원하고, 잔금·기준 미확인은 P 를 확정하지 않는다. 보증금 공제가 순대출 한도에 들어 있으면 D 를 다시 빼지 않는다. 반환 의무는 따로 표시한다.
- `cash`: 시점별 CF_t 누적 최저로 max(0, −최저) + 별도 예비현금. 소유자 자기자본 투입은 유입에 넣지 않는다.
- `plans`(J5-016B): 현상 유지·리모델링·철거신축·공동매입 후보마다 `equity`·`cash` 입력을 품고 필요자기자본·최대 필요자기자본을 한 표로 낸다. 고르지 않는다. 공사기간 공실·부가세·비용 중복·승계 보증금 반환·감정가 차이·공동매입 항목·건축사 검토를 경고로 점검한다. `--csv` 는 후보별 현금표(덮어쓰지 않음).
- 종료 코드 0 계산 완료, 1 미확정(미확인 입력·오류, 알려진 항목만 표시), 3 입력 거절. 결과에 계산식 버전(1.0.0)을 남긴다. 여유면적·필요자기자본은 검토 보조값이며 신축 가능 여부·저평가·대출 승인을 뜻하지 않는다.
