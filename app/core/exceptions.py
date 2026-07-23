# -*- coding: utf-8 -*-
"""应用层统一异常定义。

- AppError 为基类，携带 code（业务错误码，用于响应体）与 http_status（HTTP 状态码）；
- 具体子类对应不同错误场景，core/response.py 据此映射 HTTP 状态码；
- 下游 client 的异常（DocFullTextError 体系）在 handler 层转换为 AppError 体系。
"""

from __future__ import annotations


class AppError(Exception):
    """应用层异常基类。

    Attributes:
        code: 业务错误码（字符串），出现在响应体 code 字段。
        http_status: 对应 HTTP 状态码。
        message: 面向用户的错误描述（不含敏感信息）。
    """

    code: str = "INTERNAL_ERROR"
    http_status: int = 500

    def __init__(self, message: str, *, code: str | None = None, http_status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status


class AuthRequiredError(AppError):
    """未提供鉴权凭据。"""
    code = "AUTH_REQUIRED"
    http_status = 401


class AuthInvalidError(AppError):
    """鉴权凭据无效。"""
    code = "AUTH_INVALID"
    http_status = 401


class ValidationError(AppError):
    """请求参数校验失败。"""
    code = "VALIDATION_ERROR"
    http_status = 400


class ActionNotFoundError(AppError):
    """未知的 action 类型。"""
    code = "ACTION_NOT_FOUND"
    http_status = 400


class UpstreamBusinessError(AppError):
    """上游返回业务错误（解析失败、参数错误、未知 status 等）。"""
    code = "UPSTREAM_BUSINESS_ERROR"
    http_status = 502


class UpstreamUnavailableError(AppError):
    """上游不可用（网络重试耗尽 / HTTP 错误）。"""
    code = "UPSTREAM_UNAVAILABLE"
    http_status = 503


class UpstreamParseError(AppError):
    """上游响应解析失败（非法 JSON）。"""
    code = "UPSTREAM_PARSE_ERROR"
    http_status = 502


class PendingTimeoutError(AppError):
    """文档解析持续 pending，轮询超时。"""
    code = "PENDING_TIMEOUT"
    http_status = 504


class BusyError(AppError):
    """该连接已有查询进行中，拒绝再次提交。"""
    code = "BUSY"
    http_status = 409
