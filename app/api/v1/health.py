# -*- coding: utf-8 -*-
"""健康检查端点。"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """健康检查。无需鉴权。"""
    return {"status": "ok"}
