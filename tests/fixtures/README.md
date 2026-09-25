# 가상 fixture (J5-003)

모두 가상자료다. `python tests/fixtures/make_packages.py`(패키지)와 `python tests/fixtures/make_parcels.py`(필지)로 결정적으로 다시 만든다. 사진은 1x1 PNG다.

- `assets.seed.synthetic.json`: 가상 물건 5개. 좌표·주소는 가상값이다.
- `parcels/`: 가상 필지 6개 (J5-013B-1). `python tests/fixtures/make_parcels.py` 가 `synthetic_shp/`(EPSG:5186 Shapefile: 구멍 1·두 조각 1·산 지번 1, 시도 코드 99 는 존재하지 않음)와 `synthetic.j5parcels.json`(= `web/data/parcels.synthetic.j5parcels.json`)을 만든다. 가상 시드 물건 1·2·4·5 가 필지 1·1-1·2·3 안에 있다.
- `survey/`: 조사 경로·점포·표본틀·세션 수동 입력 예제 8종 (J5-013A). 목록과 기대 집계는 `survey/README.md`.
- `calc/`: 계산기 입력 예제 6종 (J5-016A, 데이터 사전 §9~§10 가상 검증 사례): `far_gross`(검토 600·여유 360), `far_excluded`(A 90·검토 540·여유 300), `equity_gross`·`equity_net`(17억), `equity_unknown`(미출력), `cash_basic`(21억), `plans_compare`(J5-016B, 후보 4종 비교: 현상 유지 17억/18억, 리모델링 25억, 철거신축 33억, 공동매입 22.6억).
- `packages/<case>/`: ZIP을 풀어 놓은 형태의 패키지. `python -m j5 inspect <폴더>`로 검사할 수 있고, 테스트는 같은 폴더를 ZIP으로 묶어서도 검사한다.

| case | 내용 | 기대 결과 (걸러내는 단계) |
|---|---|---|
| valid | 이벤트 3개, 사진 2장(한 장은 두 이벤트 공유), 날짜 정밀도 1건 | 반영 |
| duplicate_same_content | 같은 event_id·같은 바이트 행 반복 | 두 번째 행 건너뜀 (R1b 반영기) |
| duplicate_conflict | 같은 event_id·다른 내용 | 전체 입력 보류, 원본 유지 (R1b 반영기) |
| corrupt_hash_mismatch | manifest의 사진 해시가 실제 파일과 다름 | 해시 검사에서 거절 (J5-008 검사 도구) |
| corrupt_path_traversal | manifest에 `../escape.png` | manifest 스키마에서 거절 |
| missing_attachment | 이벤트가 참조한 사진이 패키지에 없음 | 첨부 존재 검사에서 거절 (J5-008) |
| unsupported_heic | `image/heic` 첨부 | 이벤트 스키마에서 거절, 변환 안내 후 중단 |
| unknown_asset | 시드에 없는 asset_id | 스키마 통과, 시드 연결 검사에서 거절 |
| invalid_change_without_evidence | 변화 확인인데 사진·설명 없음 | 이벤트 스키마에서 거절 |
