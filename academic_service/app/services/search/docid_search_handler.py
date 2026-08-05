# -*- coding: utf-8 -*-
"""docid 搜索查询处理器（@register("docid_search")）。

职责：
    1) 接收 DocidSearchParams（docid 列表）；
    2) 调用 DocidSearchClient.fetch(docids, request_id)，把 client 异常映射为 AppError；
    3) execute() 一次性返回 ``{"results": "<拼接字符串>"}``（HTTP 用）；
    4) stream() 复用基类默认实现（同步下游 → 单个 done 事件），供 WS 用。

同步 requests 调用全部包装到 asyncio.to_thread，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from academic_service.app.clients.docid_search_client import (
    AuthError,
    DocidSearchClient,
    RequestError,
    ResponseParseError,
)
from academic_service.app.core.exceptions import (
    UpstreamBusinessError,
    UpstreamParseError,
    UpstreamUnavailableError,
)
from academic_service.app.core.registry import BaseQueryHandler, HandlerContext, register
from academic_service.app.core.registry import Event
from academic_service.app.schemas.navigator import (
    DOCID_INTENT_RELEVANT,
    DocidSearchParams,
)
from academic_service.app.services.paper.pipeline import (
    build_fulltext_response,
    build_relevant_response,
)

logger = logging.getLogger("paper-service.handler.docid_search")

# action 注册名
ACTION_DOCID_SEARCH = "docid_search"


def _convert_client_exception(exc: Exception) -> Exception:
    """把 docid 搜索 client 异常映射为 AppError 体系。"""
    if isinstance(exc, AuthError):
        return UpstreamBusinessError(f"下游鉴权失败: {exc}")
    if isinstance(exc, ResponseParseError):
        return UpstreamParseError(str(exc))
    if isinstance(exc, RequestError):
        return UpstreamUnavailableError(str(exc))
    if isinstance(exc, Exception):
        return UpstreamBusinessError(f"未知下游异常: {exc}")
    return exc


@register(ACTION_DOCID_SEARCH)
class DocidSearchHandler(BaseQueryHandler):
    """docid 搜索查询处理器。"""

    params_schema = DocidSearchParams

    def _get_client(self, ctx: HandlerContext) -> DocidSearchClient:
        """从上下文获取 client；ctx.client_factory 由 deps 注入（docid 工厂）。"""
        if not ctx.client_factory:
            raise UpstreamUnavailableError("下游 client 未配置")
        client = ctx.client_factory()
        if client is None:
            raise UpstreamUnavailableError("下游 client 未配置")
        return client

    async def _fetch_documents(self, params: DocidSearchParams, ctx: HandlerContext):
        client = self._get_client(ctx)
        docids = params.docids

        def _run():
            return client.fetch_documents(docids, ctx.request_id)

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:
            raise _convert_client_exception(exc) from exc

    async def execute(self, params: DocidSearchParams, ctx: HandlerContext) -> dict[str, Any]:
        """按 fulltext/relevant 意图返回兼容和结构化论文数据。"""
        documents = await self._fetch_documents(params, ctx)
        if ctx.settings is None:
            raise UpstreamUnavailableError("论文处理 settings 未配置")
        if params.intent == DOCID_INTENT_RELEVANT:
            return await build_relevant_response(
                documents,
                params.question or "",
                ctx.settings,
            )
        return build_fulltext_response(documents, ctx.settings)

    async def stream(self, params: DocidSearchParams, ctx: HandlerContext):
        """WS 阶段进度；最终 data 与 HTTP execute 保持一致。"""
        yield Event({"type": "progress", "message": "started"})
        if params.intent == DOCID_INTENT_RELEVANT:
            yield Event({"type": "progress", "message": "fetching"})
        documents = await self._fetch_documents(params, ctx)
        if ctx.settings is None:
            raise UpstreamUnavailableError("论文处理 settings 未配置")
        if params.intent == DOCID_INTENT_RELEVANT:
            yield Event({"type": "progress", "message": "parsing"})
            yield Event({"type": "progress", "message": "reranking"})
            data = await build_relevant_response(
                documents,
                params.question or "",
                ctx.settings,
            )
            yield Event({"type": "progress", "message": "merging"})
        else:
            data = build_fulltext_response(documents, ctx.settings)
        yield Event({"type": "done", "data": data})
