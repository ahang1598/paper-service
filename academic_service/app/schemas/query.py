# -*- coding: utf-8 -*-
"""统一查询请求/响应 schema。

这是接口兼容性的关键：
    - 所有查询（无论 action）共用同一套顶层请求/响应结构；
    - params 为 action 专属参数，由各 handler 的 params_schema 二次校验；
    - 新增 action 时，顶层结构不变，只需新增 params schema。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    """统一查询请求。

    Attributes:
        action: 查询类型，如 "doc_fulltext"。决定使用哪个 handler。
        params: action 专属参数（dict），由 handler 的 params_schema 校验。
        request_id: 可选，不传则服务端自动生成（srv_ 前缀）并回传。
        stream: 是否流式（true 时建议客户端改用 WebSocket）。HTTP 一次性查询忽略此字段。
    """

    action: str = Field(..., description="查询类型，如 doc_fulltext")
    params: dict[str, Any] = Field(default_factory=dict, description="action 专属参数")
    request_id: Optional[str] = Field(
        default=None, description="可选请求标识，不传则服务端生成"
    )
    stream: bool = Field(default=False, description="是否流式（建议用 WebSocket）")


class QueryResponse(BaseModel):
    """统一查询响应（成功）。"""
    code: int = 200
    message: str = "success"
    request_id: Optional[str] = None
    data: Optional[Any] = None
