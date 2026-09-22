"""패키지 한도 (데이터 사전 §3.3). 실제 기기 시험 후 상수를 조정한다. CLI 플래그로 완화하지 않는다."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    compressed: int = 100_000_000       # ZIP 파일 크기
    uncompressed: int = 250_000_000     # 전체 해제 크기 (실제 읽은 바이트 기준)
    photo: int = 20_000_000             # 사진 한 장
    manifest: int = 10_000_000          # manifest.json
    max_photos: int = 1000              # 데이터 사전에 값이 없어 정한 선택값 (worklog J5-008)
    chunk: int = 1 << 20

    @property
    def max_entries(self) -> int:
        return self.max_photos + 3      # manifest, observations, photos/ 디렉터리 항목


DEFAULT_LIMITS = Limits()
