"""검사 결과. 출력은 결정적이다(타임스탬프 없음, 정렬됨)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

LEVELS = ("reject", "hold", "warn", "info")
_ORDER = {lv: i for i, lv in enumerate(LEVELS)}


@dataclass(frozen=True)
class Finding:
    level: str
    code: str
    path: str
    message: str

    def __post_init__(self):
        if self.level not in LEVELS:
            raise ValueError(self.level)


@dataclass
class Report:
    source: str
    kind: str = ""
    package_id: str | None = None
    study_id: str | None = None
    data_mode: str | None = None
    schema_version: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: str, code: str, path: str, message: str) -> None:
        self.findings.append(Finding(level, code, path, message))

    def has(self, code: str) -> bool:
        return any(f.code == code for f in self.findings)

    def verdict(self) -> str:
        levels = {f.level for f in self.findings}
        if "reject" in levels:
            return "reject"
        if "hold" in levels:
            return "hold"
        return "ok"

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (_ORDER[f.level], f.code, f.path, f.message))

    def to_dict(self) -> dict:
        return {
            "app": "j5",
            "source": self.source,
            "kind": self.kind,
            "verdict": self.verdict(),
            "package_id": self.package_id,
            "study_id": self.study_id,
            "data_mode": self.data_mode,
            "schema_version": self.schema_version,
            "counts": dict(sorted(self.counts.items())),
            "findings": [f.__dict__ for f in self.sorted_findings()],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=True, sort_keys=True, indent=2) + "\n"

    def to_text(self) -> str:
        v = self.verdict()
        label = {"ok": "검사 통과", "hold": "보류 (같은 ID·다른 내용)", "reject": "거절"}[v]
        lines = [
            f"패키지: {self.source} ({self.kind or '?'})",
            f"판정: {v} - {label}",
            f"package_id: {self.package_id or '-'}  study_id: {self.study_id or '-'}  "
            f"data_mode: {self.data_mode or '-'}  schema_version: {self.schema_version or '-'}",
        ]
        if self.counts:
            lines.append("요약: " + ", ".join(f"{k}={v_}" for k, v_ in sorted(self.counts.items())))
        for f in self.sorted_findings():
            lines.append(f"[{f.level}] {f.code} {f.path}: {f.message}")
        lines.append("입력 파일은 수정·삭제하지 않았다. 이 결과는 정본 반영·백업 완료를 뜻하지 않는다.")
        return "\n".join(lines) + "\n"
