# -*- coding: utf-8 -*-
"""doc_fulltext 查询处理器。

职责：
    1) 把 DocFullTextParams（schema）转换为下游 DocFullTextRequest（client）；
    2) 调用 client，把 client 的异常体系转换为 AppError 体系；
    3) execute()：一次性返回完整结果（HTTP 用）；
    4) stream()：异步产出 progress（pending 轮询）→ done（最终结果），供 WS 用。

并发安全：每次 execute/stream 都通过 ctx.client_factory 获取或复用 client，
同步的 requests 调用全部包装到 asyncio.to_thread，避免阻塞事件循环。

一致性：execute 与 stream 共享同一套轮询逻辑 _iter_poll，保证
"HTTP 与 WebSocket 对同一输入得到相同最终结果"。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Iterator

from academic_service.app.clients.doc_fulltext_client import (
    BusinessError,
    DeviceInfo,
    DocFullTextClient,
    DocFullTextRequest,
    DocPendingTimeout,
    RequestError,
    ResponseParseError,
    SplitterType,
    STATUS_FAIL,
    STATUS_PENDING,
    STATUS_SUCCESS,
    assemble_full_content,
)
from academic_service.app.core.exceptions import (
    PendingTimeoutError,
    UpstreamBusinessError,
    UpstreamParseError,
    UpstreamUnavailableError,
)
from academic_service.app.core.registry import (
    BaseQueryHandler,
    Event,
    HandlerContext,
    register,
)
from academic_service.app.schemas.document import DocFullTextParams

logger = logging.getLogger("paper-service.handler.doc_fulltext")

# action 注册名
ACTION_DOC_FULLTEXT = "doc_fulltext"

# 流式轮询间隔（秒），用 asyncio.sleep 不阻塞事件循环
STREAM_POLL_INTERVAL_SEC = 2.0
# 流式最大轮询次数（兜底，避免无限轮询）
STREAM_POLL_MAX_TIMES = 30


def _convert_client_exception(exc: Exception) -> Exception:
    """把下游 client 异常映射为 AppError 体系。"""
    if isinstance(exc, BusinessError):
        return UpstreamBusinessError(str(exc))
    if isinstance(exc, ResponseParseError):
        return UpstreamParseError(str(exc))
    if isinstance(exc, RequestError):
        return UpstreamUnavailableError(str(exc))
    if isinstance(exc, DocPendingTimeout):
        return PendingTimeoutError(str(exc))
    if isinstance(exc, Exception):
        return UpstreamBusinessError(f"未知下游异常: {exc}")
    return exc


def build_downstream_request(
    params: DocFullTextParams,
    *,
    request_id: str,
    default_device: DeviceInfo,
) -> DocFullTextRequest:
    """把 schema 参数转换为下游 client 请求（纯函数，便于单测）。"""
    device = default_device
    if params.device:
        # 用请求级覆盖填充默认设备信息
        device = DeviceInfo(
            app_version=params.device.app_version or default_device.app_version,
            device_id=params.device.device_id or default_device.device_id,
            device_model=params.device.device_model or default_device.device_model,
            device_type=params.device.device_type or default_device.device_type,
            prd_pkg_name=params.device.prd_pkg_name or default_device.prd_pkg_name,
        )

    return DocFullTextRequest(
        file_id=params.file_id or "",
        doc_hash=params.doc_hash,
        device=device,
        request_id=request_id,
        splitter=SplitterType(params.splitter),
        pages=list(params.pages),
        with_rect=params.with_rect,
    )


def _result_from_body(body: dict[str, Any]) -> dict[str, Any]:
    """把下游成功 body 整理为统一 envelope 的 data 字段（含 doc_hash）。"""
    data = body.get("data") or []
    return {
        "results": assemble_full_content(data),
        "chunk_count": len(data),
        "doc_hash": body.get("doc_hash"),
    }


def _iter_poll(
    client: DocFullTextClient,
    request: DocFullTextRequest,
    max_times: int,
) -> Iterator[dict[str, Any]]:
    """同步轮询生成器：逐次 post，产出每次的 body，直到 success/fail/超时。

    - 每次 yield 一个 body（供调用方判断 status、产出 progress）；
    - 遇到 success：yield 该 body 后停止；
    - 遇到 fail / 业务错误 / 网络/解析错误：抛出对应异常；
    - 达到 max_times 仍 pending：抛 DocPendingTimeout。

    execute 与 stream 共用此函数，保证结果一致。
    """
    for _ in range(max_times):
        body = client.post_once(request)
        DocFullTextClient.check_response(body)  # code != 0 抛 BusinessError
        status = body.get("status")
        if status == STATUS_SUCCESS:
            data = body.get("data") or []
            if not data:
                raise BusinessError("status=success 但 data 为空")
            yield body
            return
        if status == STATUS_FAIL:
            raise BusinessError(f"文档解析失败: {body.get('description', 'fail')}")
        if status == STATUS_PENDING:
            yield body  # 供 stream 产出 progress
            continue
        # 未知 status
        raise BusinessError(f"未知 status={status}")
    raise DocPendingTimeout(f"文档解析持续 pending，已轮询 {max_times} 次仍无结果")


@register(ACTION_DOC_FULLTEXT)
class DocFullTextHandler(BaseQueryHandler):
    """doc_fulltext 查询处理器。"""

    params_schema = DocFullTextParams

    def _get_client(self, ctx: HandlerContext) -> DocFullTextClient:
        """从上下文获取 client；ctx.client_factory 由 deps 注入。"""
        if not ctx.client_factory:
            raise UpstreamUnavailableError("下游 client 未配置")
        client = ctx.client_factory()
        if client is None:
            raise UpstreamUnavailableError("下游 client 未配置")
        return client

    async def execute(self, params: DocFullTextParams, ctx: HandlerContext) -> dict[str, Any]:
        """一次性查询，返回完整结果（含 doc_hash）。

        同步 client 调用包装到 asyncio.to_thread，避免阻塞事件循环。
        """
        client = self._get_client(ctx)
        downstream_request = build_downstream_request(
            params,
            request_id=ctx.request_id,
            default_device=ctx.default_device,
        )
        max_times = _effective_poll_max_times(ctx)

        def _run() -> dict[str, Any]:
            # 消费生成器直到拿到 success body
            final_body: dict[str, Any] | None = None
            for body in _iter_poll(client, downstream_request, max_times):
                final_body = body
            assert final_body is not None  # success 时必已赋值
            return _result_from_body(final_body)

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:
            raise _convert_client_exception(exc) from exc

    async def stream(
        self, params: DocFullTextParams, ctx: HandlerContext
    ) -> AsyncIterator[Event]:
        """流式查询：逐次轮询产出 progress，最终产出 done。

        自己控制 asyncio.sleep（非阻塞），不使用 client 内部的阻塞 sleep。
        每次 post 包到 to_thread，事件循环保持响应。
        """
        client = self._get_client(ctx)
        downstream_request = build_downstream_request(
            params,
            request_id=ctx.request_id,
            default_device=ctx.default_device,
        )
        max_times = _effective_poll_max_times(ctx)

        yield Event({"type": "progress", "message": "started"})

        attempt = 0
        try:
            while True:
                attempt += 1
                # 单次 post（同步），包到 to_thread
                try:
                    body = await asyncio.to_thread(client.post_once, downstream_request)
                    DocFullTextClient.check_response(body)
                except Exception as exc:
                    app_err = _convert_client_exception(exc)
                    yield Event({
                        "type": "error",
                        "code": app_err.code,  # type: ignore[attr-defined]
                        "message": app_err.message,  # type: ignore[attr-defined]
                    })
                    return

                status = body.get("status")
                if status == STATUS_SUCCESS:
                    data = body.get("data") or []
                    if not data:
                        yield Event({
                            "type": "error",
                            "code": "UPSTREAM_BUSINESS_ERROR",
                            "message": "status=success 但 data 为空",
                        })
                        return
                    yield Event({"type": "done", "data": _result_from_body(body)})
                    return

                if status == STATUS_FAIL:
                    yield Event({
                        "type": "error",
                        "code": "UPSTREAM_BUSINESS_ERROR",
                        "message": f"文档解析失败: {body.get('description', 'fail')}",
                    })
                    return

                if status == STATUS_PENDING:
                    yield Event({"type": "progress", "attempt": attempt, "message": "pending"})
                    if attempt >= max_times:
                        yield Event({
                            "type": "error",
                            "code": "PENDING_TIMEOUT",
                            "message": f"文档解析持续 pending，已轮询 {max_times} 次",
                        })
                        return
                    await asyncio.sleep(_effective_poll_interval_sec(ctx))
                    continue

                # 未知 status
                yield Event({
                    "type": "error",
                    "code": "UPSTREAM_BUSINESS_ERROR",
                    "message": f"未知 status={status}",
                })
                return
        except asyncio.CancelledError:
            logger.info("doc_fulltext 流式查询被取消 request_id=%s", ctx.request_id)
            raise


def _effective_poll_max_times(ctx: HandlerContext) -> int:
    """从 settings 读取轮询上限，缺省用 STREAM_POLL_MAX_TIMES。"""
    settings = ctx.settings
    if settings is not None:
        val = getattr(settings, "doc_poll_max_times", None)
        if isinstance(val, int) and val > 0:
            return val
    return STREAM_POLL_MAX_TIMES


def _effective_poll_interval_sec(ctx: HandlerContext) -> float:
    """从 settings 读取轮询间隔（秒），缺省用 STREAM_POLL_INTERVAL_SEC。"""
    settings = ctx.settings
    if settings is not None:
        val = getattr(settings, "doc_poll_interval_sec", None)
        if isinstance(val, (int, float)) and val >= 0:
            return float(val)
    return STREAM_POLL_INTERVAL_SEC
