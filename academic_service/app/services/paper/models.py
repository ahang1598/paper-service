"""论文处理流水线的内部数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PaperDocument:
    docid: str
    title: str
    raw_chunks: list[str]
    metadata: dict[str, Any]
    status: str = "ok"
    warnings: list[str] = field(default_factory=list)
    raw_text: str = ""
    normalized_text: str = ""


@dataclass(frozen=True)
class PaperSection:
    section_id: str
    title: str
    path: tuple[str, ...]
    level: int
    order: int
    char_start: int
    char_end: int


@dataclass(frozen=True)
class PaperChunk:
    chunk_id: str
    docid: str
    section_id: str
    section_path: tuple[str, ...]
    order: int
    section_order: int
    char_start: int
    char_end: int
    text: str
    content_hash: str
    token_count: int


@dataclass(frozen=True)
class RankedChunk:
    index: int
    score: float


@dataclass(frozen=True)
class PaperSegment:
    segment_id: str
    section_path: tuple[str, ...]
    text: str
    char_start: int
    char_end: int
    source_chunk_ids: tuple[str, ...]
    seed_chunk_ids: tuple[str, ...]
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "section_path": list(self.section_path),
            "text": self.text,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "source_chunk_ids": list(self.source_chunk_ids),
            "seed_chunk_ids": list(self.seed_chunk_ids),
            "score": self.score,
        }
