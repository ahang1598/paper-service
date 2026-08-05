"""内外网 reranker 适配器、批处理编排与本地 BM25 降级。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import re
import time
from collections import Counter
from typing import Protocol
from urllib.parse import urlparse

import requests

from academic_service.app.config import Settings
from academic_service.app.services.paper.models import RankedChunk


logger = logging.getLogger("paper-service.paper.reranker")


def _debug_io(enabled: bool, stage: str, **payload: object) -> None:
    """输出 reranker 实际业务入出参，不记录鉴权头或密钥。"""
    if not enabled:
        return
    logger.info(
        "[paper-processing] stage=%s data=%s",
        stage,
        json.dumps(payload, ensure_ascii=False, default=str),
    )


class RerankerError(Exception):
    """reranker 请求或响应不满足完整排序契约。"""


class Reranker(Protocol):
    provider: str
    model: str

    def rank(self, query: str, documents: list[str]) -> list[RankedChunk]: ...


def _validate_ranked(results: list[RankedChunk], document_count: int) -> list[RankedChunk]:
    seen: set[int] = set()
    for result in results:
        if result.index < 0 or result.index >= document_count:
            raise RerankerError(f"reranker index 越界: {result.index}")
        if result.index in seen:
            raise RerankerError(f"reranker index 重复: {result.index}")
        if not math.isfinite(result.score):
            raise RerankerError("reranker score 必须为有限数值")
        seen.add(result.index)
    if len(results) != document_count:
        raise RerankerError(
            f"reranker 结果数量 {len(results)} 与输入 {document_count} 不一致"
        )
    return sorted(results, key=lambda item: (-item.score, item.index))


class SiliconFlowReranker:
    provider = "siliconflow"

    def __init__(self, settings: Settings) -> None:
        self.url = settings.siliconflow_rerank_url
        self.model = settings.siliconflow_rerank_model
        self.api_key = settings.siliconflow_api_key
        self.timeout = settings.siliconflow_timeout_sec
        self.max_retries = settings.reranker_max_retries
        self.retry_backoff = settings.reranker_retry_backoff_sec
        self.debug_logging = settings.debug_log_paper_processing
        self._session = requests.Session()

    def rank(self, query: str, documents: list[str]) -> list[RankedChunk]:
        if not self.api_key:
            raise RerankerError("SILICONFLOW_API_KEY 未配置")
        if not query.strip() or not documents:
            return []
        payload = {
            "model": self.model,
            "query": query,
            "documents": documents,
            "top_n": len(documents),
            "return_documents": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        _debug_io(
            self.debug_logging,
            "reranker.provider.request",
            provider=self.provider,
            url=self.url,
            payload=payload,
        )
        last_error = "unknown"
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._session.post(
                    self.url, headers=headers, json=payload, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = str(exc)
                retryable = True
                _debug_io(
                    self.debug_logging,
                    "reranker.provider.response",
                    provider=self.provider,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            else:
                retryable = response.status_code == 429 or response.status_code >= 500
                if response.status_code == 200:
                    try:
                        body = response.json()
                    except (ValueError, TypeError, AttributeError) as exc:
                        _debug_io(
                            self.debug_logging,
                            "reranker.provider.response",
                            provider=self.provider,
                            attempt=attempt,
                            status_code=response.status_code,
                            body=response.text,
                        )
                        raise RerankerError(f"SiliconFlow 响应格式非法: {exc}") from exc
                    _debug_io(
                        self.debug_logging,
                        "reranker.provider.response",
                        provider=self.provider,
                        attempt=attempt,
                        status_code=response.status_code,
                        body=body,
                    )
                    try:
                        raw_results = body.get("results") or []
                        ranked = [
                            RankedChunk(
                                index=int(item["index"]),
                                score=float(item["relevance_score"]),
                            )
                            for item in raw_results
                        ]
                    except (ValueError, TypeError, KeyError, AttributeError) as exc:
                        raise RerankerError(f"SiliconFlow 响应格式非法: {exc}") from exc
                    return _validate_ranked(ranked, len(documents))
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                _debug_io(
                    self.debug_logging,
                    "reranker.provider.response",
                    provider=self.provider,
                    attempt=attempt,
                    status_code=response.status_code,
                    body=response.text,
                )
                if not retryable:
                    raise RerankerError(last_error)
            if attempt < self.max_retries and retryable:
                time.sleep(self.retry_backoff * (2 ** (attempt - 1)))
        raise RerankerError(f"SiliconFlow 请求重试耗尽: {last_error}")


class InternalGTEReranker:
    provider = "internal"

    def __init__(self, settings: Settings) -> None:
        self.url = settings.internal_rerank_url
        self.app_id = settings.internal_rerank_app_id
        self.sign_key = settings.internal_rerank_sign_key
        self.bid = settings.internal_rerank_bid
        self.flow_id = settings.internal_rerank_flow_id
        self.uuid = settings.internal_rerank_uuid
        self.model = settings.internal_rerank_bid
        self.timeout = settings.internal_rerank_timeout_sec
        self.max_retries = settings.reranker_max_retries
        self.retry_backoff = settings.reranker_retry_backoff_sec
        self.debug_logging = settings.debug_log_paper_processing
        self._session = requests.Session()

    def _headers(self, payload: str) -> dict[str, str]:
        if not self.sign_key:
            raise RerankerError("INTERNAL_RERANK_SIGN_KEY 未配置")
        path = urlparse(self.url).path or "/"
        timestamp = int(time.time() * 1000)
        sign_text = f"POST&{path}&&{payload}&appid={self.app_id}&timestamp={timestamp}"
        signature = base64.b64encode(
            hmac.new(
                self.sign_key.encode("utf-8"),
                sign_text.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        return {
            "Content-Type": "application/json",
            "Authorization": (
                f'CLOUDSOA-HMAC-SHA256 appid={self.app_id}, '
                f'timestamp={timestamp}, signature="{signature}"'
            ),
        }

    def rank(self, query: str, documents: list[str]) -> list[RankedChunk]:
        if not query.strip() or not documents:
            return []
        payload = json.dumps(
            {
                "data": {
                    "query": query,
                    "docs": [{"answer": document} for document in documents],
                },
                "meta": {
                    "bId": self.bid,
                    "flowId": self.flow_id,
                    "uuId": self.uuid,
                },
                "version": "1.0.0",
            },
            ensure_ascii=False,
        )
        _debug_io(
            self.debug_logging,
            "reranker.provider.request",
            provider=self.provider,
            url=self.url,
            payload=json.loads(payload),
        )
        last_error = "unknown"
        for attempt in range(1, self.max_retries + 1):
            headers = self._headers(payload)
            try:
                response = self._session.post(
                    self.url,
                    headers=headers,
                    data=payload.encode("utf-8"),
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = str(exc)
                retryable = True
                _debug_io(
                    self.debug_logging,
                    "reranker.provider.response",
                    provider=self.provider,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            else:
                retryable = response.status_code == 429 or response.status_code >= 500
                if response.status_code == 200:
                    try:
                        body = response.json()
                    except (ValueError, TypeError, AttributeError) as exc:
                        _debug_io(
                            self.debug_logging,
                            "reranker.provider.response",
                            provider=self.provider,
                            attempt=attempt,
                            status_code=response.status_code,
                            body=response.text,
                        )
                        raise RerankerError(f"内网 GTE 响应格式非法: {exc}") from exc
                    _debug_io(
                        self.debug_logging,
                        "reranker.provider.response",
                        provider=self.provider,
                        attempt=attempt,
                        status_code=response.status_code,
                        body=body,
                    )
                    try:
                        scores = body["result"]["content"][0]["predictResult"]["content"]
                        if isinstance(scores, str):
                            scores = json.loads(scores)
                        if not isinstance(scores, list):
                            raise TypeError("predictResult.content 不是数组")
                        ranked = [
                            RankedChunk(index=index, score=float(score))
                            for index, score in enumerate(scores)
                        ]
                    except (ValueError, TypeError, KeyError, IndexError) as exc:
                        raise RerankerError(f"内网 GTE 响应格式非法: {exc}") from exc
                    return _validate_ranked(ranked, len(documents))
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                _debug_io(
                    self.debug_logging,
                    "reranker.provider.response",
                    provider=self.provider,
                    attempt=attempt,
                    status_code=response.status_code,
                    body=response.text,
                )
                if not retryable:
                    raise RerankerError(last_error)
            if attempt < self.max_retries and retryable:
                time.sleep(self.retry_backoff * (2 ** (attempt - 1)))
        raise RerankerError(f"内网 GTE 请求重试耗尽: {last_error}")


def create_reranker(settings: Settings) -> Reranker:
    provider = settings.reranker_provider.strip().lower()
    if provider == "siliconflow":
        return SiliconFlowReranker(settings)
    if provider == "internal":
        return InternalGTEReranker(settings)
    raise RerankerError(f"不支持的 reranker provider: {settings.reranker_provider}")


async def rank_in_batches(
    reranker: Reranker,
    query: str,
    documents: list[str],
    *,
    batch_size: int,
    max_concurrency: int,
) -> list[RankedChunk]:
    """并发批量打分，并把批内 index 还原为全局 index。"""
    if not documents:
        return []
    semaphore = asyncio.Semaphore(max(1, max_concurrency))
    effective_batch_size = max(1, batch_size)

    async def _one(offset: int, batch: list[str]) -> list[RankedChunk]:
        async with semaphore:
            local = await asyncio.to_thread(reranker.rank, query, batch)
        return [RankedChunk(index=offset + item.index, score=item.score) for item in local]

    tasks = [
        _one(offset, documents[offset: offset + effective_batch_size])
        for offset in range(0, len(documents), effective_batch_size)
    ]
    batches = await asyncio.gather(*tasks)
    merged = [item for batch in batches for item in batch]
    return _validate_ranked(merged, len(documents))


_ASCII_TERM_RE = re.compile(r"[A-Za-z0-9]+(?:[._/+:-][A-Za-z0-9]+)*")
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+")


def bm25_terms(text: str) -> list[str]:
    terms = [match.group(0).lower() for match in _ASCII_TERM_RE.finditer(text)]
    for match in _CJK_RUN_RE.finditer(text):
        run = match.group(0)
        if len(run) == 1:
            terms.append(run)
        else:
            terms.extend(run[index: index + 2] for index in range(len(run) - 1))
    return terms


def bm25_rank(query: str, documents: list[str]) -> list[RankedChunk]:
    """无外部依赖的本地 BM25，零关键词命中时返回空列表。"""
    query_terms = bm25_terms(query)
    if not query_terms or not documents:
        return []
    tokenized = [bm25_terms(document) for document in documents]
    avg_len = sum(len(tokens) for tokens in tokenized) / max(1, len(tokenized))
    document_frequency: Counter[str] = Counter()
    for tokens in tokenized:
        document_frequency.update(set(tokens))

    k1 = 1.5
    b = 0.75
    scores: list[RankedChunk] = []
    for index, tokens in enumerate(tokenized):
        frequencies = Counter(tokens)
        score = 0.0
        for term in query_terms:
            tf = frequencies.get(term, 0)
            if not tf:
                continue
            df = document_frequency[term]
            idf = math.log(1 + (len(documents) - df + 0.5) / (df + 0.5))
            denominator = tf + k1 * (1 - b + b * len(tokens) / max(avg_len, 1.0))
            score += idf * (tf * (k1 + 1)) / denominator
        if score > 0:
            scores.append(RankedChunk(index=index, score=score))
    return sorted(scores, key=lambda item: (-item.score, item.index))
