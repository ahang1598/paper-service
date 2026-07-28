# -*- coding: utf-8 -*-
"""FastAPI 依赖注入。

- 鉴权依赖（HTTP）：基于 AUTH_ENABLED 短路；
- registry 查找依赖：根据 action 找到 handler 类；
- client 工厂依赖：构造下游 DocFullTextClient（可被测试 override）；
- HandlerContext 构造。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status

from academic_service.app.clients.doc_fulltext_client import DeviceInfo, DocFullTextClient, ServiceConfig
from academic_service.app.clients.docid_search_client import DocidSearchClient, DocidSearchConfig
from academic_service.app.config import Settings, get_settings
from academic_service.app.core.exceptions import (
    ActionNotFoundError,
    AuthInvalidError,
    AuthRequiredError,
    ValidationError,
)
from academic_service.app.core.registry import HandlerContext, get_handler_class
from academic_service.app.core.security import extract_bearer_header, verify_token

logger = logging.getLogger("paper-service.deps")


# =====================================================================
# 鉴权依赖（HTTP）
# =====================================================================

def require_auth(
    authorization: Optional[str] = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> str:
    """HTTP Bearer 鉴权依赖。

    - AUTH_ENABLED=False：放行，返回占位 principal；
    - 否则校验 Authorization: Bearer <token>。

    返回 principal 字符串（鉴权关闭时为 "anonymous"）。
    鉴权失败抛 AuthInvalidError / AuthRequiredError（由全局异常处理器映射为 401）。
    """
    if not settings.auth_enabled:
        return "anonymous"

    token = extract_bearer_header(authorization)
    if not authorization:
        raise AuthRequiredError("缺少 Authorization 头")
    if not verify_token(token, settings):
        raise AuthInvalidError("无效的鉴权凭据")
    return token or "bearer"


# =====================================================================
# 下游 client 工厂
# =====================================================================

def make_doc_client_factory(settings: Settings):
    """构造一个 client 工厂闭包（按 settings 创建下游 client）。

    工厂模式：每次调用返回一个 DocFullTextClient，便于复用 session 与按需重建。
    测试可通过 app.dependency_overrides 替换本依赖。
    """

    def _factory() -> DocFullTextClient:
        service_config = ServiceConfig(
            host=settings.doc_service_host,
            port=settings.doc_service_port,
            scheme=settings.doc_service_scheme,
            auth_key=settings.doc_service_auth_key,
            url_path=settings.doc_fulltext_url_path,
            timestamp_tolerance_ms=settings.doc_auth_timestamp_tolerance_ms,
        )
        return DocFullTextClient(
            service_config=service_config,
            connect_timeout=settings.doc_connect_timeout_sec,
            read_timeout=settings.doc_read_timeout_sec,
            max_retries=settings.doc_max_retries,
            retry_backoff_sec=settings.doc_retry_backoff_sec,
            poll_max_times=settings.doc_poll_max_times,
            poll_interval_sec=settings.doc_poll_interval_sec,
        )

    return _factory


def make_default_device(settings: Settings) -> DeviceInfo:
    """根据 settings 构造默认设备信息。"""
    return DeviceInfo(
        app_version=settings.default_device_app_version,
        device_id=settings.default_device_id,
        device_model=settings.default_device_model,
        device_type=settings.default_device_type,
        prd_pkg_name=settings.default_device_prd_pkg_name,
    )


def make_docid_search_client_factory(settings: Settings):
    """构造 docid 搜索 client 工厂闭包（按 settings 创建 DocidSearchClient）。

    测试可通过 app.dependency_overrides 替换。
    """

    def _factory() -> DocidSearchClient:
        config = DocidSearchConfig(
            url=settings.docid_search_url,
            auth_key=settings.docid_search_auth_key,
        )
        return DocidSearchClient(config=config)

    return _factory
