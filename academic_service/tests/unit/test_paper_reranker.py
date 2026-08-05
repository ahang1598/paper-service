# -*- coding: utf-8 -*-
"""reranker 契约、BM25 和批次 index 还原。"""

from __future__ import annotations

import logging
import math

import pytest

from academic_service.app.config import Settings
from academic_service.app.services.paper.models import RankedChunk
from academic_service.app.services.paper.reranker import (
    InternalGTEReranker,
    RerankerError,
    SiliconFlowReranker,
    bm25_rank,
    rank_in_batches,
)


class ReverseReranker:
    provider = "test"
    model = "reverse"

    def rank(self, query, documents):
        return [RankedChunk(index=i, score=float(len(documents) - i)) for i in range(len(documents))]


@pytest.mark.asyncio
async def test_rank_in_batches_restores_global_indices():
    results = await rank_in_batches(
        ReverseReranker(),
        "q",
        [f"doc-{i}" for i in range(7)],
        batch_size=3,
        max_concurrency=2,
    )
    assert sorted(item.index for item in results) == list(range(7))
    assert all(math.isfinite(item.score) for item in results)


def test_bm25_returns_only_keyword_hits():
    results = bm25_rank(
        "intravenous epinephrine risk",
        [
            "Intravenous epinephrine caused atrial fibrillation.",
            "WSe2 membranes have a high Young's modulus.",
            "Dental treatment uses Biodentine.",
        ],
    )
    assert results
    assert results[0].index == 0
    assert all(item.index != 1 for item in results)


def test_bm25_handles_chinese_bigrams_and_empty_query():
    assert bm25_rank("培养温度", ["培养温度影响脂质生产", "无关文本"])[0].index == 0
    assert bm25_rank("   ", ["anything"]) == []


def test_siliconflow_rejects_duplicate_indices(monkeypatch):
    settings = Settings(
        _env_file=None,
        siliconflow_api_key="test-key",
        reranker_max_retries=1,
    )
    reranker = SiliconFlowReranker(settings)

    class Response:
        status_code = 200
        text = "ok"

        @staticmethod
        def json():
            return {
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.8},
                ]
            }

    monkeypatch.setattr(reranker._session, "post", lambda *args, **kwargs: Response())
    with pytest.raises(RerankerError, match="重复"):
        reranker.rank("query", ["a", "b"])


def test_internal_gte_rejects_score_count_mismatch(monkeypatch):
    settings = Settings(
        _env_file=None,
        internal_rerank_sign_key="test-key",
        reranker_max_retries=1,
    )
    reranker = InternalGTEReranker(settings)

    class Response:
        status_code = 200
        text = "ok"

        @staticmethod
        def json():
            return {"result": {"content": [{"predictResult": {"content": [0.5]}}]}}

    monkeypatch.setattr(reranker._session, "post", lambda *args, **kwargs: Response())
    with pytest.raises(RerankerError, match="数量"):
        reranker.rank("query", ["a", "b"])


def test_siliconflow_debug_logs_business_io_without_api_key(monkeypatch, caplog):
    settings = Settings(
        _env_file=None,
        siliconflow_api_key="never-log-this-key",
        reranker_max_retries=1,
        debug_log_paper_processing=True,
    )
    reranker = SiliconFlowReranker(settings)

    class Response:
        status_code = 200
        text = "ok"

        @staticmethod
        def json():
            return {
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.2},
                ]
            }

    monkeypatch.setattr(reranker._session, "post", lambda *args, **kwargs: Response())
    caplog.set_level(logging.INFO, logger="paper-service.paper.reranker")

    reranker.rank("paper question", ["first chunk", "second chunk"])

    logs = caplog.text
    assert "stage=reranker.provider.request" in logs
    assert "stage=reranker.provider.response" in logs
    assert "paper question" in logs
    assert "first chunk" in logs
    assert "relevance_score" in logs
    assert "never-log-this-key" not in logs
