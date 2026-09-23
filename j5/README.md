# j5/

PC용 Python CLI. 수집·정본 반영·검증·조회 패키지 생성·백업을 담당한다. 지원 중인 Python(3.11+)과 표준 라이브러리를 우선하며, JSON Schema 검증에 jsonschema 고정 버전을 쓴다.

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

이후: 조회 파생본(J5-011), 백업·복구(J5-012).
