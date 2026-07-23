# -*- coding: utf-8 -*-
"""统一响应封装与异常→HTTP 状态映射。

- success() / error() 构造统一 envelope：{code, message, request_id, data}
- AppError 的 http_status 作为错误响应的 HTTP 状态码（集中映射在异常类自身定义）
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi.responses import JSONResponse

from app.core.exceptions import AppError

# ---- 响应业务码常量（与异常 code 区分：此处是成功/失败的顶层标记）----
# 注意：对外成功码用 200（对齐 HTTP 语义）；下游服务的 code 语义另见
# app/clients/doc_fulltext_client.RESP_CODE_SUCCESS（下游以 0 表示业务成功，互不影响）。
RESP_CODE_SUCCESS = 200
RESP_CODE_FAIL = 1

# 统一 envelope 字段名
FIELD_CODE = "code"
FIELD_MESSAGE = "message"
FIELD_REQUEST_ID = "request_id"
FIELD_DATA = "data"


def success(data: Any = None, *, request_id: Optional[str] = None) -> dict[str, Any]:
    """构造成功响应体。"""
    return {
        FIELD_CODE: RESP_CODE_SUCCESS,
        FIELD_MESSAGE: "success",
        FIELD_REQUEST_ID: request_id,
        FIELD_DATA: data,
    }


def error_body(err: AppError, *, request_id: Optional[str] = None) -> dict[str, Any]:
    """构造错误响应体（不含敏感信息）。"""
    return {
        FIELD_CODE: err.code,
        FIELD_MESSAGE: err.message,
        FIELD_REQUEST_ID: request_id,
        FIELD_DATA: None,
    }


def error_response(err: AppError, *, request_id: Optional[str] = None) -> JSONResponse:
    """构造错误 JSONResponse，HTTP 状态码取自异常。"""
    return JSONResponse(
        status_code=err.http_status,
        content=error_body(err, request_id=request_id),
    )
