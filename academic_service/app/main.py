# -*- coding: utf-8 -*-
"""FastAPI 应用入口。

组装：
    - 日志脱敏配置（兜底替换已知敏感子串）
    - AUTH_ENABLED=False 启动警告
    - 路由挂载（health / query / ws）
    - 全局异常处理器（AppError → 统一错误响应）

启动：
    uvicorn app.main:app --host 0.0.0.0 --port 12135 --reload
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from academic_service.app.api.v1 import health as health_api
from academic_service.app.api.v1 import query as query_api
from academic_service.app.api.v1 import ws as ws_api
from academic_service.app.config import Settings, get_settings
from academic_service.app.core.exceptions import AppError, ValidationError
from academic_service.app.core.response import error_body
from academic_service.app.middleware.logging import configure_redacting_logger

logger = logging.getLogger("paper-service")


def _configure_logging(settings: Settings) -> None:
    """配置脱敏日志：把已知 token / 密钥原值作为兜底替换子串。"""
    level = getattr(logging, str(settings.log_level).upper(), logging.INFO)
    secret_substrings: tuple[str, ...] = tuple(
        t for t in (settings.bearer_tokens + [settings.doc_service_auth_key]) if t
    )
    configure_redacting_logger(secret_substrings=secret_substrings, level=level)


def _warn_if_auth_disabled(settings: Settings) -> None:
    """AUTH_ENABLED=False 时打印醒目警告。"""
    if not settings.auth_enabled:
        logger.warning(
            "======== 鉴权已关闭 (AUTH_ENABLED=False) ========\n"
            "  仅限测试 / 本地调试环境使用！\n"
            "  生产环境必须保持 AUTH_ENABLED=true。\n"
            "=================================================="
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造 FastAPI 应用（便于测试注入自定义 settings）。"""
    if settings is None:
        settings = get_settings()

    _configure_logging(settings)
    _warn_if_auth_disabled(settings)

    # 导入 services 包以触发 @register 装饰器（确保 handler 注册到 registry）
    import academic_service.app.services.document.fulltext_handler  # noqa: F401
    import academic_service.app.services.search.docid_search_handler  # noqa: F401

    application = FastAPI(
        title="paper-service",
        description="文档查询统一服务（HTTP + WebSocket），可对接 MCP",
        version="0.1.0",
    )

    # 挂载 settings 到 app.state，便于依赖/中间件读取
    application.state.settings = settings

    # 路由
    application.include_router(health_api.router)
    application.include_router(query_api.router)
    application.include_router(ws_api.router)

    # ---- 全局异常处理器 ----
    _register_exception_handlers(application)

    logger.info("paper-service 启动完成，已注册 actions: %s", _safe_list_actions())
    return application


def _safe_list_actions() -> list[str]:
    """列出已注册 action（容错，避免注册表为空时报错）。"""
    try:
        from academic_service.app.core.registry import list_actions
        return list_actions()
    except Exception:
        return []


def _register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器，统一错误响应格式。"""

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        request_id = _extract_request_id(request)
        logger.warning("AppError code=%s status=%s msg=%s", exc.code, exc.http_status, exc.message)
        return JSONResponse(status_code=exc.http_status, content=error_body(exc, request_id=request_id))

    @app.exception_handler(RequestValidationError)
    async def _req_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = _extract_request_id(request)
        err = ValidationError(f"请求体校验失败: {exc}")
        return JSONResponse(status_code=err.http_status, content=error_body(err, request_id=request_id))

    @app.exception_handler(StarletteHTTPException)
    async def _starlette_http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        request_id = _extract_request_id(request)
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": "HTTP_ERROR", "message": str(exc.detail), "request_id": request_id, "data": None},
        )


def _extract_request_id(request: Request) -> str | None:
    """尝试从请求体读取 request_id（异常处理时用于关联）。"""
    try:
        body = request.scope.get("_body")
        if body:
            import json
            data = json.loads(body)
            if isinstance(data, dict) and data.get("request_id"):
                return data["request_id"]
    except Exception:
        pass
    return None


# 模块级 app 实例（供 uvicorn app.main:app 使用）
app = create_app()
