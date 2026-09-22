// 패키지 한도 (데이터 사전 §3.3). j5/package/limits.py 와 같은 숫자를 쓴다. 기기 시험 후 상수로 조정한다.
export const LIMITS = Object.freeze({
  compressed: 100_000_000,   // ZIP 파일 크기
  uncompressed: 250_000_000, // 항목 크기 합
  photo: 20_000_000,         // 사진 한 장. manifest 스키마의 files[].bytes 상한이기도 하므로 observations.jsonl 에도 적용
  maxPhotos: 1000,
});
