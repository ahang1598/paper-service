# -*- coding: utf-8 -*-
"""navigator 统一查询入参/响应 schema。

新对外信封格式：
    {
      "query": "1db8f80c0b854613aa68d2c977891353.docx",  # 原 file_id，唯一必传
      "options": {                                         # 可选覆盖项（默认空）
        "splitter": 1,
        "pages": [],
        "with_rect": false,
        "doc_hash": null,
        "device": null,
        "request_id": "可选"
      },
      "stream": false                                      # false=HTTP同步(默认) true=改用 WebSocket
    }

设计要点：
    - 顶层只做结构校验；真正的业务校验延后到 ``DocFullTextParams``（由现有 handler 复用）。
    - ``query`` 即原 ``file_id``；``options`` 内的字段对应 ``DocFullTextParams`` 的可选覆盖项。
    - ``stream`` 是路由选择：HTTP 端收到 true 时主动拒绝并引导到 WebSocket 端点，
      避免静默忽略导致调用方误以为拿到了流式结果。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# options 内允许出现的键（用于向调用方提示，不做强制枚举校验，
# 校验交给 DocFullTextParams 的 field_validator / model_validator）
ALLOWED_OPTION_KEYS: frozenset[str] = frozenset(
    {
        "splitter",
        "pages",
        "with_rect",
        "doc_hash",
        "device",
        "request_id",
    }
)


class NavigatorQueryRequest(BaseModel):
    """navigator 统一查询请求。

    Attributes:
        query: 文件 ID（原 ``file_id``），唯一必传项。
        options: 可选参数覆盖项，键对应 ``DocFullTextParams`` 的可变部分
                 （splitter/pages/with_rect/doc_hash/device）；
                 还可放 ``request_id`` 用于透传。
        stream: 路由选择——``False``（默认）HTTP 同步返回；``True`` 时应改用 WebSocket。
    """

    query: str = Field(..., description="文件ID（原 file_id），唯一必传")
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="可选参数覆盖项（splitter/pages/with_rect/doc_hash/device/request_id）",
    )
    stream: bool = Field(
        default=False,
        description="false=HTTP同步(默认)；true=流式，应改用 WebSocket 端点",
    )


class NavigatorQueryResponse(BaseModel):
    """navigator 统一查询响应（成功）。"""

    code: int = 200
    message: str = "success"
    request_id: Optional[str] = None
    data: Optional[Any] = None


def translate_to_fulltext_params(req: NavigatorQueryRequest) -> dict[str, Any]:
    """把新信封转译为 ``DocFullTextParams.model_validate`` 可直接消费的 dict。

    - ``query`` → ``file_id``
    - ``options`` 内除 ``request_id`` 外的字段原样透传
    - 未在 options 中出现的字段由 ``DocFullTextParams`` 的默认值兜底
    """
    raw_options = req.options or {}
    params_dict: dict[str, Any] = {"file_id": req.query}
    for key in ("splitter", "pages", "with_rect", "doc_hash", "device"):
        if key in raw_options and raw_options[key] is not None:
            params_dict[key] = raw_options[key]
    return params_dict


def extract_request_id(req: NavigatorQueryRequest) -> Optional[str]:
    """从 options 中取出可选的 request_id（用于透传）。"""
    return (req.options or {}).get("request_id")
