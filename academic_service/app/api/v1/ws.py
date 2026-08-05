# -*- coding: utf-8 -*-
"""WebSocket 流式查询入口：WS /api/v1/navigator/ws?token=xxx。

入参信封（新格式，与 HTTP /api/v1/navigator/query 一致）：
    {"query": "1db8...docx", "options": {...}, "stream": true}
其中 query 即原 file_id（唯一必传），options 为可选覆盖项。WS 天然流式，
不依赖 stream 字段。

事件协议（服务端→客户端，JSON）：
    - {"type":"progress", "message":"...", ...}    进度（含 pending 轮询）
    - {"type":"chunk", ...}                         （预留：增量分块）
    - {"type":"done", "data":{...}, "request_id": "..."}   完成
    - {"type":"error", "code":"...", "message":"...", "request_id": "..."}  错误

鉴权：AUTH_ENABLED=True 时校验 ?token=，缺失/错误则直接关闭连接（不 accept）。
并发：同一连接同一时刻只允许一个查询进行；进行中再提交返回 error code=BUSY。
取消：客户端断开时，正在进行的流式任务通过 CancelledError 清理并记录日志。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import ValidationError as PydanticValidationError

from academic_service.app.api.deps import make_default_device
from academic_service.app.api.v1.query import get_client_factory, get_docid_client_factory, TYPE_ACTION_MAP
from academic_service.app.config import Settings, get_settings
from academic_service.app.core.registry import HandlerContext, get_handler_class
from academic_service.app.core.security import verify_token
from academic_service.app.schemas.navigator import (
    ALLOWED_QUERY_TYPES,
    DOCID_INTENT_FULLTEXT,
    QUERY_TYPE_DOCID,
    QUERY_TYPE_FILEID,
)

logger = logging.getLogger("paper-service.api.ws")

router = APIRouter(tags=["ws"])

REQUEST_ID_PREFIX = "srv"

# options 内允许覆盖 DocFullTextParams 的字段（fileid 模式信封转译用）
_OPTION_KEYS = ("splitter", "pages", "with_rect", "doc_hash", "device")


def _resolve_request_id(req_id: Optional[str], prefix: str = REQUEST_ID_PREFIX) -> str:
    if req_id:
        return req_id
    return f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}"


def _ws_unauthenticated_close_code() -> int:
    """自定义关闭码：4401 表示未鉴权。"""
    return 4401


@router.websocket("/api/v1/navigator/ws")
async def ws_query(
    websocket: WebSocket,
    settings: Settings = Depends(get_settings),
    client_factory=Depends(get_client_factory),
    docid_client_factory=Depends(get_docid_client_factory),
) -> None:
    """WebSocket 统一查询入口。

    settings 与 client_factory 均通过依赖注入获取（支持 app.dependency_overrides 覆盖）。
    """
    # ---- 鉴权 ----
    # AUTH_ENABLED=False 放行；否则校验 ?token=
    token = websocket.query_params.get("token")
    if settings.auth_enabled and not verify_token(token, settings):
        # 未鉴权：直接关闭连接（不 accept）
        logger.warning("WS 鉴权失败，拒绝连接")
        await websocket.close(code=_ws_unauthenticated_close_code())
        return

    await websocket.accept()

    # ---- 并发模型 ----
    # 主循环持续读消息；查询在独立 task 中执行。同一连接同一时刻只允许一个查询，
    # 查询进行中再提交新消息立即返回 BUSY（不中断当前查询）。
    current_task: Optional[asyncio.Task] = None
    # 用于通知主循环当前查询已结束
    query_done = asyncio.Event()

    async def _run_query(request_id: str, raw: dict) -> None:
        """执行单次查询并推送事件。异常转 error 事件；结束时设置 query_done。"""
        if settings.debug_log_request:
            logger.info("[ws-request] %s", raw)
        msg_type = raw.get("type") or QUERY_TYPE_FILEID
        if msg_type not in ALLOWED_QUERY_TYPES:
            await websocket.send_json({
                "type": "error", "code": "VALIDATION_ERROR",
                "message": f"type 必须为 {sorted(ALLOWED_QUERY_TYPES)} 之一", "request_id": request_id,
            })
            return

        action = TYPE_ACTION_MAP[msg_type]
        handler_cls = get_handler_class(action)
        if handler_cls is None:  # pragma: no cover - 注册由 main 触发
            await websocket.send_json({
                "type": "error", "code": "INTERNAL_ERROR",
                "message": "内部错误：handler 未注册", "request_id": request_id,
            })
            return

        # 按类型转译入参 + 选择 client 工厂
        if msg_type == QUERY_TYPE_DOCID:
            queries = [q.strip() for q in (raw.get("queries") or []) if isinstance(q, str) and q.strip()]
            q_raw = raw.get("query")
            docids = queries or ([q_raw.strip()] if isinstance(q_raw, str) and q_raw.strip() else [])
            if not docids:
                await websocket.send_json({
                    "type": "error", "code": "VALIDATION_ERROR",
                    "message": "docid 模式需要 query 或 queries", "request_id": request_id,
                })
                return
            options = raw.get("options") or {}
            params_dict = {
                "docids": docids,
                "intent": options.get("intent", DOCID_INTENT_FULLTEXT),
                "question": options.get("question"),
            }
            factory = docid_client_factory
        else:  # fileid
            query = raw.get("query")
            if not query:
                await websocket.send_json({
                    "type": "error", "code": "VALIDATION_ERROR",
                    "message": "fileid 模式 query 字段必传（文件ID）", "request_id": request_id,
                })
                return
            options = raw.get("options") or {}
            params_dict = {"file_id": query, **{
                k: v for k, v in options.items()
                if k in _OPTION_KEYS and v is not None
            }}
            factory = client_factory

        try:
            params = handler_cls.params_schema.model_validate(params_dict)
        except PydanticValidationError as exc:
            await websocket.send_json({
                "type": "error", "code": "VALIDATION_ERROR",
                "message": f"参数校验失败: {exc}", "request_id": request_id,
            })
            return

        handler = handler_cls()
        ctx = HandlerContext(
            client_factory=factory,
            default_device=make_default_device(settings),
            request_id=request_id,
            settings=settings,
        )
        try:
            async for event in handler.stream(params, ctx):
                event_dict = dict(event)
                event_dict.setdefault("request_id", request_id)
                await websocket.send_json(event_dict)
        except asyncio.CancelledError:
            logger.info("WS 查询被取消 request_id=%s", request_id)
            raise
        except WebSocketDisconnect:
            logger.info("WS 客户端在查询过程中断开 request_id=%s", request_id)
            raise
        except Exception as exc:
            logger.exception("WS 查询异常 request_id=%s", request_id)
            await websocket.send_json({
                "type": "error", "code": "INTERNAL_ERROR",
                "message": "内部错误", "request_id": request_id,
            })

    try:
        while True:
            # 读消息：若当前有查询在跑，仍要能读到新消息以返回 BUSY
            receive_task = asyncio.create_task(websocket.receive_json())
            # 若有查询进行中，等待"任一"完成（查询结束 或 新消息到达）
            if current_task and not current_task.done():
                done, pending = await asyncio.wait(
                    {receive_task, current_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if current_task in done:
                    # 查询先结束：取消读消息任务，回到循环顶部重新读
                    receive_task.cancel()
                    current_task = None
                    continue
            # 等待一条新消息（无查询时直接等）
            try:
                raw = await receive_task
            except WebSocketDisconnect:
                logger.info("WS 客户端正常断开")
                if current_task and not current_task.done():
                    current_task.cancel()
                break
            except asyncio.CancelledError:
                break

            # request_id 支持顶层或 options 内透传
            opts = raw.get("options") or {}
            request_id = _resolve_request_id(
                raw.get("request_id") or opts.get("request_id"),
                settings.request_id_prefix,
            )

            # 进行中再提交 → 拒绝（不中断当前查询）
            if current_task and not current_task.done():
                await websocket.send_json({
                    "type": "error", "code": "BUSY",
                    "message": "当前连接已有查询进行中，请等待完成",
                    "request_id": request_id,
                })
                continue

            # 启动查询任务（不 await，让主循环继续读消息）
            current_task = asyncio.create_task(_run_query(request_id, raw))
    except WebSocketDisconnect:
        logger.info("WS 连接关闭")
    except asyncio.CancelledError:
        logger.info("WS 任务被取消")
        if current_task and not current_task.done():
            current_task.cancel()
        raise
    finally:
        if current_task and not current_task.done():
            current_task.cancel()
