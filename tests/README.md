# tests/

단위·통합·기기 시험 절차와 가상 fixture. GitHub Actions에서는 가상자료만 사용하며 실제 정본·사진을 올리지 않는다.

- `test_repo_allowlist.py`: 저장소에 실데이터·비밀키 형식 파일이 들어오지 않았는지 검사한다(J5-002).
- `test_j5_003_schemas.py`: 스키마 3종과 fixture 검증, 관측↔시드 asset_id 연결 확인.
- `test_j5_008_inspect.py` / `test_j5_008_copy.py`: PC 검사 도구. fixture 9종(폴더·ZIP)과 적대적 ZIP, CLI 종료 코드, 독립 사본.
- `conftest.py`: fixture 폴더를 결정적으로 ZIP으로 묶는 헬퍼.
- `fixtures/`: 정상·중복·손상·미지원 입력 예제. 목록과 기대 결과는 `fixtures/README.md`.
