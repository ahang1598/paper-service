"""docid 论文 fulltext/relevant 数据处理与返回组装。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import defaultdict
from typing import Any, Iterable

from academic_service.app.clients.docid_search_client import DocidSearchDocument
from academic_service.app.config import Settings
from academic_service.app.services.paper.models import (
    PaperChunk,
    PaperDocument,
    PaperSegment,
    RankedChunk,
)
from academic_service.app.services.paper.parser import (
    TokenCounter,
    chunk_sections,
    normalize_text,
    parse_sections,
)
from academic_service.app.services.paper.reranker import (
    Reranker,
    bm25_rank,
    create_reranker,
    rank_in_batches,
)


ENTRY_FORMAT = "[{idx}]title:{title}|||content:{content}"
logger = logging.getLogger("paper-service.paper.pipeline")


def _debug_stage(settings: Settings, stage: str, **payload: Any) -> None:
    """按独立开关输出论文流水线数据；日志可能包含完整论文内容。"""
    if not settings.debug_log_paper_processing:
        return
    logger.info(
        "[paper-processing] stage=%s data=%s",
        stage,
        json.dumps(payload, ensure_ascii=False, default=str),
    )


def _section_debug_payload(section: Any, normalized_text: str) -> dict[str, Any]:
    return {
        "section_id": section.section_id,
        "title": section.title,
        "path": list(section.path),
        "level": section.level,
        "order": section.order,
        "char_start": section.char_start,
        "char_end": section.char_end,
        "text": normalized_text[section.char_start:section.char_end],
    }


def _chunk_debug_payload(chunk: PaperChunk) -> dict[str, Any]:
    return {
        "chunk_id": chunk.chunk_id,
        "section_id": chunk.section_id,
        "section_path": list(chunk.section_path),
        "order": chunk.order,
        "section_order": chunk.section_order,
        "char_start": chunk.char_start,
        "char_end": chunk.char_end,
        "token_count": chunk.token_count,
        "content_hash": chunk.content_hash,
        "text": chunk.text,
    }


def to_paper_document(record: DocidSearchDocument) -> PaperDocument:
    raw_text = "\n".join(record.chunks)
    return PaperDocument(
        docid=record.docid,
        title=record.title,
        raw_chunks=list(record.chunks),
        metadata=dict(record.metadata),
        status=record.status,
        warnings=list(record.warnings),
        raw_text=raw_text,
    )


def _base_paper_payload(paper: PaperDocument) -> dict[str, Any]:
    return {
        "docid": paper.docid,
        "title": paper.title,
        "status": paper.status,
        "metadata": paper.metadata,
        "warnings": list(dict.fromkeys(paper.warnings)),
    }


def _assemble_results(papers: Iterable[dict[str, Any]], content_key: str) -> str:
    entries: list[str] = []
    for paper in papers:
        content = paper.get(content_key)
        if content_key == "segments":
            content = "\n".join(
                segment.get("text", "") for segment in (content or []) if segment.get("text")
            )
        if not content:
            continue
        entries.append(
            ENTRY_FORMAT.format(
                idx=len(entries) + 1,
                title=paper.get("title") or "",
                content=content,
            )
        )
    return "\n".join(entries)


def build_fulltext_response(
    records: list[DocidSearchDocument], settings: Settings
) -> dict[str, Any]:
    papers: list[dict[str, Any]] = []
    for record in records:
        paper = to_paper_document(record)
        _debug_stage(
            settings,
            "fulltext.input",
            docid=paper.docid,
            title=paper.title,
            raw_chunks=paper.raw_chunks,
            raw_text=paper.raw_text,
        )
        payload = _base_paper_payload(paper)
        payload["content"] = paper.raw_text
        papers.append(payload)
    response = {
        "results": _assemble_results(papers, "content"),
        "papers": papers,
        "processing": {
            "intent": "fulltext",
            "chunk_schema_version": settings.paper_chunk_schema_version,
        },
    }
    _debug_stage(settings, "fulltext.response", response=response)
    return response


def _expand_and_merge(
    paper: PaperDocument,
    chunks: list[PaperChunk],
    ranked: list[RankedChunk],
    *,
    top_k: int,
    neighbor_window: int,
) -> list[PaperSegment]:
    seeds = ranked[:top_k]
    if not seeds:
        return []

    chunks_by_section: dict[str, list[PaperChunk]] = defaultdict(list)
    for chunk in chunks:
        chunks_by_section[chunk.section_id].append(chunk)
    for section_chunks in chunks_by_section.values():
        section_chunks.sort(key=lambda chunk: chunk.section_order)

    chunk_positions: dict[str, tuple[list[PaperChunk], int]] = {}
    for section_chunks in chunks_by_section.values():
        for position, chunk in enumerate(section_chunks):
            chunk_positions[chunk.chunk_id] = (section_chunks, position)

    seed_scores = {chunks[item.index].chunk_id: item.score for item in seeds}
    selected_ids: set[str] = set()
    for seed in seeds:
        seed_chunk = chunks[seed.index]
        section_chunks, position = chunk_positions[seed_chunk.chunk_id]
        start = max(0, position - neighbor_window)
        end = min(len(section_chunks), position + neighbor_window + 1)
        selected_ids.update(chunk.chunk_id for chunk in section_chunks[start:end])

    # 同章节内按 section_order 合并连续窗口。
    groups: list[list[PaperChunk]] = []
    for section_chunks in chunks_by_section.values():
        selected = [chunk for chunk in section_chunks if chunk.chunk_id in selected_ids]
        if not selected:
            continue
        current = [selected[0]]
        for chunk in selected[1:]:
            if chunk.section_order == current[-1].section_order + 1:
                current.append(chunk)
            else:
                groups.append(current)
                current = [chunk]
        groups.append(current)

    segments: list[PaperSegment] = []
    for group in groups:
        char_start = min(chunk.char_start for chunk in group)
        char_end = max(chunk.char_end for chunk in group)
        text = paper.normalized_text[char_start:char_end]
        group_seed_ids = tuple(
            chunk.chunk_id for chunk in group if chunk.chunk_id in seed_scores
        )
        if not group_seed_ids:
            continue
        score = max(seed_scores[chunk_id] for chunk_id in group_seed_ids)
        segment_digest = hashlib.sha256(
            json.dumps(
                [paper.docid, group[0].section_path, char_start, char_end, text],
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()[:20]
        segments.append(
            PaperSegment(
                segment_id=f"seg_{segment_digest}",
                section_path=group[0].section_path,
                text=text,
                char_start=char_start,
                char_end=char_end,
                source_chunk_ids=tuple(chunk.chunk_id for chunk in group),
                seed_chunk_ids=group_seed_ids,
                score=score,
            )
        )

    # 同论文内按区间或内容哈希去重；优先高分，再优先正文位置。
    ordered = sorted(segments, key=lambda item: (-item.score, item.char_start, item.char_end))
    seen_ranges: set[tuple[int, int]] = set()
    seen_content: set[str] = set()
    deduped: list[PaperSegment] = []
    for segment in ordered:
        range_key = (segment.char_start, segment.char_end)
        content_key = hashlib.sha256(segment.text.encode("utf-8")).hexdigest()
        if range_key in seen_ranges or content_key in seen_content:
            continue
        seen_ranges.add(range_key)
        seen_content.add(content_key)
        deduped.append(segment)
    return deduped


async def build_relevant_response(
    records: list[DocidSearchDocument],
    question: str,
    settings: Settings,
    *,
    reranker: Reranker | None = None,
) -> dict[str, Any]:
    tokenizer = TokenCounter(settings.paper_tokenizer_path)
    prepared: list[tuple[PaperDocument, list[PaperChunk]]] = []
    for record in records:
        paper = to_paper_document(record)
        _debug_stage(
            settings,
            "fulltext.input",
            docid=paper.docid,
            title=paper.title,
            raw_chunks=paper.raw_chunks,
            raw_text=paper.raw_text,
        )
        if not paper.raw_text:
            _debug_stage(
                settings,
                "parse.output",
                docid=paper.docid,
                normalized_text="",
            )
            prepared.append((paper, []))
            continue
        paper.normalized_text = normalize_text(paper.raw_text)
        _debug_stage(
            settings,
            "parse.output",
            docid=paper.docid,
            normalized_text=paper.normalized_text,
        )
        sections = parse_sections(paper.normalized_text, paper.docid, paper.title)
        _debug_stage(
            settings,
            "structure.output",
            docid=paper.docid,
            section_count=len(sections),
            sections=[
                _section_debug_payload(section, paper.normalized_text)
                for section in sections
            ],
        )
        chunks = chunk_sections(
            paper.normalized_text,
            paper.docid,
            sections,
            tokenizer=tokenizer,
            target_tokens=settings.paper_chunk_target_tokens,
            max_tokens=settings.paper_chunk_max_tokens,
            overlap_tokens=settings.paper_chunk_overlap_tokens,
            schema_version=settings.paper_chunk_schema_version,
        )
        _debug_stage(
            settings,
            "chunk.output",
            docid=paper.docid,
            chunk_count=len(chunks),
            chunks=[_chunk_debug_payload(chunk) for chunk in chunks],
        )
        if not chunks:
            paper.status = "no_content"
            paper.warnings.append("NO_VALID_CHUNKS")
        prepared.append((paper, chunks))

    degraded = False
    active_reranker = reranker
    ranked_per_paper: list[list[RankedChunk]] = []
    try:
        active_reranker = active_reranker or create_reranker(settings)
        for paper, chunks in prepared:
            _debug_stage(
                settings,
                "reranker.input",
                docid=paper.docid,
                provider=getattr(active_reranker, "provider", "unknown"),
                model=getattr(active_reranker, "model", "unknown"),
                question=question,
                documents=[
                    {"index": index, **_chunk_debug_payload(chunk)}
                    for index, chunk in enumerate(chunks)
                ],
            )
        tasks = [
            rank_in_batches(
                active_reranker,
                question,
                [chunk.text for chunk in chunks],
                batch_size=settings.reranker_batch_size,
                max_concurrency=settings.reranker_max_concurrency,
            )
            if chunks
            else asyncio.sleep(0, result=[])
            for _, chunks in prepared
        ]
        ranked_per_paper = await asyncio.gather(*tasks)
        for (paper, chunks), ranked in zip(prepared, ranked_per_paper):
            _debug_stage(
                settings,
                "reranker.output",
                docid=paper.docid,
                provider=getattr(active_reranker, "provider", "unknown"),
                model=getattr(active_reranker, "model", "unknown"),
                results=[
                    {
                        "index": item.index,
                        "chunk_id": chunks[item.index].chunk_id,
                        "score": item.score,
                    }
                    for item in ranked
                ],
            )
    except Exception as exc:
        degraded = True
        logger.warning(
            "reranker 全请求降级 provider=%s error_type=%s",
            getattr(active_reranker, "provider", settings.reranker_provider),
            type(exc).__name__,
        )
        _debug_stage(
            settings,
            "reranker.error",
            provider=getattr(active_reranker, "provider", settings.reranker_provider),
            error_type=type(exc).__name__,
            error=str(exc),
        )

    if degraded:
        ranked_per_paper = [
            bm25_rank(question, [chunk.text for chunk in chunks])
            for _, chunks in prepared
        ]
        for (paper, chunks), ranked in zip(prepared, ranked_per_paper):
            _debug_stage(
                settings,
                "reranker.output",
                docid=paper.docid,
                provider="bm25",
                model="local-bm25",
                degraded=True,
                results=[
                    {
                        "index": item.index,
                        "chunk_id": chunks[item.index].chunk_id,
                        "score": item.score,
                    }
                    for item in ranked
                ],
            )

    papers: list[dict[str, Any]] = []
    for (paper, chunks), ranked in zip(prepared, ranked_per_paper):
        payload = _base_paper_payload(paper)
        if degraded:
            payload["warnings"].append("RERANKER_DEGRADED_TO_BM25")
        segments = _expand_and_merge(
            paper,
            chunks,
            ranked,
            top_k=settings.reranker_top_k,
            neighbor_window=settings.reranker_neighbor_window,
        )
        if not segments:
            payload["warnings"].append("NO_RELEVANT_CONTENT")
        payload["warnings"] = list(dict.fromkeys(payload["warnings"]))
        payload["segments"] = [segment.to_dict() for segment in segments]
        _debug_stage(
            settings,
            "merge.output",
            docid=paper.docid,
            segments=payload["segments"],
        )
        papers.append(payload)

    provider = "bm25" if degraded else getattr(active_reranker, "provider", "unknown")
    model = "local-bm25" if degraded else getattr(active_reranker, "model", "unknown")
    response = {
        "results": _assemble_results(papers, "segments"),
        "papers": papers,
        "processing": {
            "intent": "relevant",
            "chunk_schema_version": settings.paper_chunk_schema_version,
            "reranker": {
                "provider": provider,
                "model": model,
                "top_k_per_paper": settings.reranker_top_k,
                "neighbor_window": settings.reranker_neighbor_window,
                "degraded": degraded,
            },
        },
    }
    _debug_stage(settings, "relevant.response", response=response)
    return response
