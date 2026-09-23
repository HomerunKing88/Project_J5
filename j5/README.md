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
