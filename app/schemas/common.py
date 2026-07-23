# -*- coding: utf-8 -*-
"""通用 schema。"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    """错误响应体（与 core/response.error_body 结构一致）。"""
    code: str
    message: str
    request_id: Optional[str] = None
    data: Optional[Any] = None


class SuccessResponse(BaseModel):
    """成功响应体。"""
    code: int = 200
    message: str = "success"
    request_id: Optional[str] = None
    data: Optional[Any] = None
