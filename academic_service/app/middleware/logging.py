# -*- coding: utf-8 -*-
"""日志脱敏工具与中间件。

目标：日志与错误响应中不出现敏感信息。
    - 密钥（auth_key）
    - Bearer token（Authorization 头、?token= 查询参数）
    - 文档全文内容（content）

提供：
    - sanitize()：对任意字符串做脱敏（命中敏感关键词的字段值替换为占位符）；
    - RedactingFormatter：日志格式化器，对日志记录做脱敏；
    - audit_log()：统一的安全审计日志输出（已脱敏）。

中间件层不在请求路径记录 body（避免全文落盘）；如需记录请求体，必须先脱敏。
"""

from __future__ import annotations

import logging
from typing import Any

# ---- 脱敏占位符 ----
REDACTED = "[REDACTED]"
CONTENT_OMITTED = "[content omitted]"

# ---- 敏感字段名（小写匹配）----
SENSITIVE_FIELD_NAMES = {
    "authorization",
    "token",
    "auth_key",
    "authcode",
    "auth_code",
    "apikey",
    "api_key",
    "secret",
    "password",
    "doc_service_auth_key",
    "docid_search_auth_key",
}

# ---- 内容字段名（单独标记，整段省略）----
CONTENT_FIELD_NAMES = {
    "content",
    "fulltext",
    "full_text",
    "text",
}


def _is_sensitive(field_name: str) -> bool:
    return field_name.lower() in SENSITIVE_FIELD_NAMES


def _is_content(field_name: str) -> bool:
    return field_name.lower() in CONTENT_FIELD_NAMES


def sanitize_value(field_name: str, value: Any) -> Any:
    """根据字段名对值脱敏。"""
    if value is None:
        return None
    if _is_sensitive(field_name):
        return REDACTED
    if _is_content(field_name) and isinstance(value, str) and value:
        return CONTENT_OMITTED
    return value


def sanitize_mapping(data: dict[str, Any]) -> dict[str, Any]:
    """递归对 dict 脱敏（浅拷贝，不修改原对象）。"""
    redacted: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            redacted[k] = sanitize_mapping(v)
        elif isinstance(v, list):
            redacted[k] = [
                sanitize_mapping(item) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            redacted[k] = sanitize_value(k, v)
    return redacted


class RedactingFormatter(logging.Formatter):
    """日志格式化器：对 message 做基础脱敏（替换已知敏感子串）。

    主要兜底：若不慎把敏感值拼进日志，做子串替换。
    优先级更高的防护是在记录前调用 sanitize_*。
    """

    def __init__(self, fmt: str | None = None, secret_substrings: tuple[str, ...] = ()) -> None:
        super().__init__(fmt)
        # 需要原样替换为 REDACTED 的子串（如配置的 token、密钥原值）
        self._secret_substrings = tuple(s for s in secret_substrings if s)

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        for secret in self._secret_substrings:
            if secret and secret in message:
                message = message.replace(secret, REDACTED)
        return message


def configure_redacting_logger(
    secret_substrings: tuple[str, ...] = (),
    level: int = logging.INFO,
) -> None:
    """配置根 logger 使用脱敏格式化器。

    secret_substrings：应传入运行期已知的敏感原值（如配置的 token、密钥），
    作为兜底子串替换。即便日志误拼，也能抹除。
    """
    formatter = RedactingFormatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        secret_substrings=secret_substrings,
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    # 清理已有 handler 避免重复输出
    root.handlers = [handler]
    root.setLevel(level)
