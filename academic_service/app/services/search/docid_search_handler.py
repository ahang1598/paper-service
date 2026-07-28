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
from academic_service.app.schemas.navigator import DocidSearchParams

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

    async def execute(self, params: DocidSearchParams, ctx: HandlerContext) -> dict[str, Any]:
        """一次性查询，返回 ``{"results": <拼接字符串>}``。"""
        client = self._get_client(ctx)
        docids = params.docids

        def _run() -> str:
            return client.fetch(docids, ctx.request_id)

        try:
            results = await asyncio.to_thread(_run)
        except Exception as exc:
            raise _convert_client_exception(exc) from exc
        return {"results": results}
