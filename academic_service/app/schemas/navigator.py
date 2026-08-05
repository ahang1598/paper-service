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

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---- 查询类型（type 路由）----
# fileid: 原 doc_fulltext 流程（按文件ID查全文，默认）
# docid:  新增，按 docid 列表查搜索服务（返回 title + chunks）
QUERY_TYPE_FILEID = "fileid"
QUERY_TYPE_DOCID = "docid"
ALLOWED_QUERY_TYPES: frozenset[str] = frozenset({QUERY_TYPE_FILEID, QUERY_TYPE_DOCID})

# ---- docid 数据意图 ----
DOCID_INTENT_FULLTEXT = "fulltext"
DOCID_INTENT_RELEVANT = "relevant"
ALLOWED_DOCID_INTENTS: frozenset[str] = frozenset(
    {DOCID_INTENT_FULLTEXT, DOCID_INTENT_RELEVANT}
)


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
        "intent",
        "question",
    }
)


class NavigatorQueryRequest(BaseModel):
    """navigator 统一查询请求。

    Attributes:
        query: 单个 id。fileid 模式为文件ID；docid 模式为单个 docid（与 queries 二选一）。
        queries: docid 列表（仅 docid 模式；fileid 模式不允许，由端点拒绝）。
        type: 查询类型路由——``fileid``（默认，doc_fulltext）/ ``docid``（搜索服务）。
        options: fileid 参数覆盖，或 docid 的 intent/question。
        stream: ``False``（默认）HTTP 同步返回；``True`` 时应改用 WebSocket。
    """

    query: Optional[str] = Field(default=None, description="单个 id（fileid=文件ID；docid=单个docid），与 queries 二选一")
    queries: Optional[List[str]] = Field(default=None, description="docid 列表（仅 docid 模式）")
    type: str = Field(default=QUERY_TYPE_FILEID, description="查询类型：fileid(默认,doc_fulltext) / docid(搜索)")
    options: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "fileid 覆盖项（splitter/pages/with_rect/doc_hash/device/request_id），"
            "或 docid 论文意图（intent=fulltext|relevant、question）"
        ),
    )
    stream: bool = Field(
        default=False,
        description="false=HTTP同步(默认)；true=流式，应改用 WebSocket 端点",
    )

    @field_validator("type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        if v not in ALLOWED_QUERY_TYPES:
            raise ValueError(f"type 必须为 {sorted(ALLOWED_QUERY_TYPES)} 之一")
        return v

    @field_validator("queries")
    @classmethod
    def _strip_queries(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        """去除空白/空串元素；全空则归一为 None。"""
        if v is None:
            return v
        cleaned = [x.strip() for x in v if isinstance(x, str) and x.strip()]
        return cleaned or None

    @model_validator(mode="after")
    def _check_query_or_queries(self) -> "NavigatorQueryRequest":
        """query 与 queries 至少传一个。"""
        if not self.query and not self.queries:
            raise ValueError("query 与 queries 至少传一个")
        return self


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


def effective_docids(req: NavigatorQueryRequest) -> List[str]:
    """docid 模式下生效的 docid 列表：queries 优先，否则 [query]。

    调用前应已确保处于 docid 模式且 query/queries 至少有一个（schema 已保证）。
    """
    if req.queries:
        return list(req.queries)
    return [req.query] if req.query else []


class DocidSearchParams(BaseModel):
    """docid 搜索 handler 的 action 专属参数。"""

    docids: List[str] = Field(..., description="docid 列表，至少一个")
    intent: Literal["fulltext", "relevant"] = Field(
        default=DOCID_INTENT_FULLTEXT,
        description="论文数据意图：fulltext(默认) / relevant",
    )
    question: Optional[str] = Field(
        default=None,
        description="relevant 意图的检索问题",
    )

    @field_validator("docids")
    @classmethod
    def _check_docids(cls, v: List[str]) -> List[str]:
        # 去空白并按首次出现去重，避免向下游重复请求同一篇论文。
        cleaned = list(dict.fromkeys(
            x.strip() for x in v if isinstance(x, str) and x.strip()
        ))
        if not cleaned:
            raise ValueError("docids 至少需要一个非空值")
        return cleaned

    @model_validator(mode="after")
    def _check_relevant_question(self) -> "DocidSearchParams":
        if self.question is not None:
            self.question = self.question.strip() or None
        if self.intent == DOCID_INTENT_RELEVANT and not self.question:
            raise ValueError("relevant 意图必须提供非空 question")
        return self


def translate_to_docid_params(req: NavigatorQueryRequest) -> dict[str, Any]:
    """把 navigator 信封转译为 ``DocidSearchParams`` 可消费的字典。

    ``query/queries`` 继续只表示 docid；自然语言问题只从 options.question
    读取，避免改变单 docid 调用的既有语义。
    """
    options = req.options or {}
    return {
        "docids": effective_docids(req),
        "intent": options.get("intent", DOCID_INTENT_FULLTEXT),
        "question": options.get("question"),
    }
