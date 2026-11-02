from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(slots=True)
class RawItem:
    source_id: str
    external_id: str
    url: str
    title: str
    kind: str
    author: str | None = None
    published_at: str | None = None
    summary: str | None = None
    signals: dict = field(default_factory=dict)
    payload: dict = field(default_factory=dict)
    # 单条覆盖来源级权威分（例如同一 RSS 源下的泛科技子频道）
    authority: str | None = None


class SourceAdapter(Protocol):
    source_id: str

    def fetch(self) -> list[RawItem]: ...
