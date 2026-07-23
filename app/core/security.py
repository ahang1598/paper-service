# -*- coding: utf-8 -*-
"""对外 Bearer 鉴权（HTTP 与 WebSocket 共用）。

- AUTH_ENABLED=False 时全局关闭鉴权（测试/调试），所有请求放行；
- AUTH_ENABLED=True 时校验 Authorization: Bearer <token>（HTTP）或 ?token=（WS）；
- token 比对使用 secrets.compare_digest 常量时间比较，防时序侧信道。
"""

from __future__ import annotations

import secrets

from app.config import Settings


def is_auth_enabled(settings: Settings) -> bool:
    """鉴权是否开启（单点判断，便于测试覆盖）。"""
    return bool(settings.auth_enabled)


def verify_token(token: str | None, settings: Settings) -> bool:
    """
    校验 token 是否在允许集合内。

    - 鉴权关闭：直接放行；
    - token 为空：拒绝；
    - 与任一允许 token 常量时间相等：通过。

    使用常量时间比较，避免通过响应耗时推断 token 正确前缀。
    """
    if not is_auth_enabled(settings):
        return True
    if not token:
        return False
    allowed = settings.bearer_tokens
    # 对每个允许 token 做常量时间比较；任一匹配即通过
    for candidate in allowed:
        if secrets.compare_digest(token, candidate):
            return True
    return False


def extract_bearer_header(authorization: str | None) -> str | None:
    """从 Authorization 头提取 token。

    接受 "Bearer <token>" 形式；其余返回 None。
    """
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None
