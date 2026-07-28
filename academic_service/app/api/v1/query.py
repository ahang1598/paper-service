# -*- coding: utf-8 -*-
"""HTTP 统一查询入口：POST /api/v1/navigator/query。

入参信封（新格式）：
    {
      "query": "1db8...docx",      # 文件ID（原 file_id），唯一必传
      "options": {                  # 可选覆盖项（默认空）
        "splitter": 1,
        "pages": [],
        "with_rect": false,
        "doc_hash": null,
        "device": null,
        "request_id": "可选"
      },
      "stream": false               # false=HTTP同步(默认)；true 应改用 WebSocket
    }

内部固定调用 doc_fulltext handler，请求体不再需要 action 字段。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable

from fastapi import APIRouter, Depends
from pydantic import ValidationError as PydanticValidationError

from academic_service.app.api.deps import make_default_device, require_auth
from academic_service.app.clients.doc_fulltext_client import DocFullTextClient
from academic_service.app.config import Settings, get_settings
from academic_service.app.core.exceptions import AppError, UpstreamBusinessError, ValidationError
from academic_service.app.core.registry import HandlerContext, get_handler_class
from academic_service.app.core.response import error_response, success
from academic_service.app.schemas.navigator import (
    NavigatorQueryRequest,
    NavigatorQueryResponse,
    extract_request_id,
    translate_to_fulltext_params,
)

logger = logging.getLogger("paper-service.api.query")

router = APIRouter(prefix="/api/v1/navigator", tags=["navigator"])

# request_id 自动生成前缀
REQUEST_ID_PREFIX = "srv"

# 固定的 action：当前对外只暴露 doc_fulltext
ACTION_DOC_FULLTEXT = "doc_fulltext"

# stream=true 引导到 WebSocket 端点
WS_ENDPOINT = "/api/v1/navigator/ws"


def _resolve_request_id(req: NavigatorQueryRequest, prefix: str = REQUEST_ID_PREFIX) -> str:
    """透传或自动生成 request_id（<prefix>_ 前缀，前缀由 Settings 提供）。"""
    rid = extract_request_id(req)
    if rid:
        return rid
    return f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}"


def get_client_factory(settings: Settings = Depends(get_settings)) -> Callable[[], DocFullTextClient]:
    """下游 client 工厂依赖。

    生产环境返回基于 settings 的工厂；测试可通过
    app.dependency_overrides[get_client_factory] 替换为返回 mock client 的工厂。
    """
    from academic_service.app.api.deps import make_doc_client_factory
    return make_doc_client_factory(settings)


@router.post("/query", response_model=NavigatorQueryResponse)
async def query(
    req: NavigatorQueryRequest,
    principal: str = Depends(require_auth),
    settings: Settings = Depends(get_settings),
    client_factory: Callable[[], DocFullTextClient] = Depends(get_client_factory),
) -> Any:
    """统一查询入口。

    流程：鉴权 → 校验 stream → 找 handler → 校验 params → 执行 → 返回统一 envelope。
    """
    request_id = _resolve_request_id(req, settings.request_id_prefix)

    # 1) stream 路由选择：true 时 HTTP 主动拒绝并引导到 WebSocket
    if req.stream:
        err = ValidationError(
            f"stream=true 需使用 WebSocket 端点（{WS_ENDPOINT}?token=<token>）发起流式查询"
        )
        return error_response(err, request_id=request_id)

    # 2) 固定 action：doc_fulltext
    handler_cls = get_handler_class(ACTION_DOC_FULLTEXT)
    if handler_cls is None:  # pragma: no cover - 注册由 main 触发，正常不会为空
        return error_response(
            UpstreamBusinessError("内部错误：handler 未注册"), request_id=request_id
        )

    # 3) 把新信封转译为 DocFullTextParams 并二次校验
    params_dict = translate_to_fulltext_params(req)
    try:
        params = handler_cls.params_schema.model_validate(params_dict)
    except PydanticValidationError as exc:
        err = ValidationError(f"参数校验失败: {exc}")
        return error_response(err, request_id=request_id)

    # 4) 构造上下文并执行
    handler = handler_cls()
    ctx = HandlerContext(
        client_factory=client_factory,
        default_device=make_default_device(settings),
        request_id=request_id,
        settings=settings,
    )

    logger.info("query request_id=%s principal=%s", request_id, principal)

    try:
        data = await handler.execute(params, ctx)
    except AppError as exc:
        return error_response(exc, request_id=request_id)
    except Exception:
        # 兜底：未预期异常
        logger.exception("query 未预期异常 request_id=%s", request_id)
        return error_response(UpstreamBusinessError("内部错误"), request_id=request_id)

    return success(data, request_id=request_id)
