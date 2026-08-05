# -*- coding: utf-8 -*-
"""论文 relevant 流水线的邻居扩展、合并、去重和降级。"""

from __future__ import annotations

import logging

import pytest

from academic_service.app.clients.docid_search_client import DocidSearchDocument
from academic_service.app.config import Settings
from academic_service.app.services.paper.models import RankedChunk
from academic_service.app.services.paper.parser import normalize_text
from academic_service.app.services.paper.pipeline import (
    _effective_top_k,
    build_fulltext_response,
    build_relevant_response,
)


class KeywordReranker:
    provider = "test"
    model = "keyword"

    def rank(self, query, documents):
        ranked = [
            RankedChunk(index=index, score=1.0 if "TARGET" in document else 0.1 - index * 0.001)
            for index, document in enumerate(documents)
        ]
        return sorted(ranked, key=lambda item: (-item.score, item.index))


class UniformRelevantReranker:
    provider = "test"
    model = "uniform-relevant"

    def rank(self, query, documents):
        return [
            RankedChunk(index=index, score=1.0 - index * 0.001)
            for index in range(len(documents))
        ]


class LowScoreReranker:
    provider = "test"
    model = "low-score"

    def rank(self, query, documents):
        return [RankedChunk(index=index, score=0.19) for index in range(len(documents))]


def _settings(**overrides):
    values = dict(
        _env_file=None,
        reranker_top_k=2,
        reranker_top_k_min=2,
        reranker_neighbor_window=1,
        reranker_batch_size=32,
        paper_chunk_target_tokens=20,
        paper_chunk_max_tokens=24,
        paper_chunk_overlap_tokens=4,
    )
    values.update(overrides)
    return Settings(**values)


def _record(docid: str = "d1") -> DocidSearchDocument:
    paragraphs = [
        "Introduction sentence one. " * 12,
        "Background sentence two. " * 12,
        "TARGET method evidence. " * 12,
        "Supporting result sentence. " * 12,
        "Conclusion sentence. " * 12,
    ]
    return DocidSearchDocument(
        docid=docid,
        title="Test Paper",
        chunks=["# Test Paper\n\n## Method\n\n" + "\n\n".join(paragraphs)],
        metadata={"doi": "10.test/example"},
    )


@pytest.mark.parametrize(
    ("chunk_count", "expected"),
    [(0, 0), (3, 3), (5, 5), (20, 5), (60, 6), (100, 8)],
)
def test_dynamic_top_k_respects_minimum_chunk_count_ratio_and_cap(chunk_count, expected):
    assert _effective_top_k(
        chunk_count,
        minimum=5,
        cap=8,
        ratio=0.1,
    ) == expected


def test_fulltext_keeps_legacy_results_and_adds_papers():
    data = build_fulltext_response([_record()], _settings())
    assert data["results"].startswith("[1]title:Test Paper|||content:")
    assert data["papers"][0]["content"] == _record().chunks[0]
    assert data["processing"]["intent"] == "fulltext"


@pytest.mark.asyncio
async def test_relevant_merges_overlapping_neighbor_windows_without_duplicate_text():
    data = await build_relevant_response(
        [_record()],
        "TARGET method",
        _settings(),
        reranker=KeywordReranker(),
    )
    paper = data["papers"][0]
    assert data["processing"]["reranker"]["degraded"] is False
    assert paper["segments"]
    for segment in paper["segments"]:
        assert len(segment["source_chunk_ids"]) == len(set(segment["source_chunk_ids"]))
        assert segment["text"] == segment["text"].strip()
        assert segment["seed_chunk_ids"]
    assert "TARGET" in data["results"]


@pytest.mark.asyncio
async def test_any_reranker_failure_degrades_all_papers_to_bm25():
    class FailingReranker:
        provider = "test"
        model = "fail"

        def rank(self, query, documents):
            raise RuntimeError("down")

    data = await build_relevant_response(
        [_record("a"), _record("b")],
        "TARGET method",
        _settings(),
        reranker=FailingReranker(),
    )
    assert data["processing"]["reranker"]["degraded"] is True
    assert data["processing"]["reranker"]["provider"] == "bm25"
    assert all("RERANKER_DEGRADED_TO_BM25" in paper["warnings"] for paper in data["papers"])
    assert all(paper["segments"] for paper in data["papers"])


@pytest.mark.asyncio
async def test_low_relevance_returns_no_relevant_content_instead_of_forcing_top_k():
    data = await build_relevant_response(
        [_record()],
        "completely unrelated question",
        _settings(reranker_min_score=0.2),
        reranker=LowScoreReranker(),
    )
    paper = data["papers"][0]
    assert paper["segments"] == []
    assert "NO_RELEVANT_CONTENT" in paper["warnings"]
    assert data["results"] == ""
    assert data["processing"]["reranker"]["effective_top_k_per_paper"]["d1"] == 0


@pytest.mark.asyncio
async def test_score_equal_to_relevance_threshold_is_eligible():
    data = await build_relevant_response(
        [_record()],
        "threshold question",
        _settings(reranker_min_score=0.19),
        reranker=LowScoreReranker(),
    )
    assert data["papers"][0]["segments"]
    assert "NO_RELEVANT_CONTENT" not in data["papers"][0]["warnings"]


@pytest.mark.asyncio
async def test_top_k_is_dynamic_and_does_not_expand_most_of_small_paper():
    data = await build_relevant_response(
        [_record()],
        "method evidence",
        _settings(
            reranker_top_k=8,
            reranker_top_k_min=5,
            reranker_top_k_ratio=0.1,
            reranker_min_score=0.2,
        ),
        reranker=UniformRelevantReranker(),
    )
    paper = data["papers"][0]
    selection = data["processing"]["reranker"]
    effective = selection["effective_top_k_per_paper"]["d1"]
    assert effective == 5
    assert selection["top_k_min"] == 5
    assert sum(len(segment["seed_chunk_ids"]) for segment in paper["segments"]) == effective
    assert data["results"]
    returned_chars = sum(len(segment["text"]) for segment in paper["segments"])
    assert returned_chars < len(normalize_text(_record().chunks[0]))


@pytest.mark.asyncio
async def test_paper_processing_debug_log_switch_controls_all_pipeline_stages(caplog):
    caplog.set_level(logging.INFO, logger="paper-service.paper.pipeline")

    await build_relevant_response(
        [_record()],
        "TARGET method",
        _settings(debug_log_paper_processing=False),
        reranker=KeywordReranker(),
    )
    assert "[paper-processing]" not in caplog.text

    caplog.clear()
    await build_relevant_response(
        [_record()],
        "TARGET method",
        _settings(debug_log_paper_processing=True),
        reranker=KeywordReranker(),
    )
    logs = caplog.text
    for stage in (
        "fulltext.input",
        "parse.output",
        "structure.output",
        "chunk.output",
        "reranker.input",
        "reranker.output",
        "reranker.selection",
        "merge.output",
        "relevant.response",
    ):
        assert f"stage={stage}" in logs
    assert "TARGET method" in logs
    assert "chunk_id" in logs
    assert "score" in logs
