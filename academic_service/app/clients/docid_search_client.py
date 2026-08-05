# -*- coding: utf-8 -*-
"""docid 搜索接口客户端（/search）。

与 ``doc_fulltext_client`` 是两套完全不同的下游：
    - 不同 host/URL（配置 ``docid_search.url``）；
    - 不同 HMAC 签名：``timestamp={ts}&url={uri}&body={body}`` → SHA256 **hexdigest** → 请求头 ``authCode``；
    - 不同请求体（logid/query/lang/region/...）；query 由 docid 列表拼接为 ``docid:a,b``；
    - 同步返回（无 pending 轮询），响应 ``results[]`` 含 ``title`` 与 ``extrainfo.meta_data.chunks``。

可配置项（url、auth_key）来自 configs/config.yaml + 环境变量；其余模板字段为模块常量。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from academic_service.app.config import load_base_defaults

logger = logging.getLogger("docid_search.client")

# =====================================================================
# 常量
# =====================================================================

# ---- query 拼接 ----
QUERY_PREFIX = "docid:"

# ---- 请求头 ----
HEADER_CONTENT_TYPE = "Content-Type"
HEADER_AUTH_CODE = "authCode"
HEADER_TIMESTAMP = "timestamp"
HEADER_FOREIGN_SEARCH_LANG = "foreign_search_lang"
CONTENT_TYPE_JSON = "application/json"
FOREIGN_SEARCH_LANG_VALUE = "en"  # 网页 slb 分流字段

# ---- 请求体字段名 ----
FIELD_LOGID = "logid"
FIELD_QUERY = "query"
FIELD_LANG = "lang"
FIELD_REGION = "region"
FIELD_REGION_SREGION = "sregion"
FIELD_LANGS = "langs"
FIELD_LANGS_SETTING_LANG = "setting_lang"
FIELD_LANGS_LGD_RESULT = "lgd_result"
FIELD_LANGS_SPELL_LANG = "spell_lang"
FIELD_SPELL_TYPE = "type"
FIELD_SPELL_CONF = "conf"
FIELD_RETURN_TYPE = "return_type"
FIELD_USER_INFO = "user_info"
FIELD_USER_ID = "user_id"
FIELD_FILTER = "filter"
FIELD_EXTRAINFO = "extrainfo"

# ---- 响应字段名 ----
FIELD_RESULTS = "results"
FIELD_TITLE = "title"
FIELD_META_DATA = "meta_data"
FIELD_CHUNKS = "chunks"

# ---- 请求体默认值（模板常量）----
LANG = "en"
REGION_SREGION = "cn"
RETURN_TYPE = "json"
USER_ID = "*"
SPELL_CONF = 1.0
EXTRAINFO_SKIP_CACHE = "true"
EXTRAINFO_R_CHUNK_TYPE = "0"
EXTRAINFO_GPT_MODE = "2"
EXTRAINFO_SKIP_SW = "true"
EXTRAINFO_SKIP_DF = "true"

# ---- 网络参数 ----
DEFAULT_TIMEOUT_SEC = 30

# ---- 结果拼接 ----
CHUNK_JOIN = "\n"          # 同一篇 chunks 之间的分隔符
ENTRY_JOIN = "\n"          # 多篇结果之间的分隔符
ENTRY_FORMAT = "[{idx}]title:{title}|||content:{content}"


# =====================================================================
# 异常
# =====================================================================

class DocidSearchError(Exception):
    """docid 搜索接口异常基类。"""


class AuthError(DocidSearchError):
    """鉴权相关异常。"""


class RequestError(DocidSearchError):
    """网络 / HTTP 层异常。"""


class ResponseParseError(DocidSearchError):
    """响应解析异常（非法 JSON 等）。"""


# =====================================================================
# 配置
# =====================================================================

@dataclass
class DocidSearchConfig:
    """搜索服务连接配置；默认值取自 configs/config.yaml（经 load_base_defaults）。"""
    url: str = field(default_factory=lambda: load_base_defaults()["docid_search_url"])
    auth_key: str = field(default_factory=lambda: load_base_defaults()["docid_search_auth_key"])


@dataclass(frozen=True)
class DocidSearchDocument:
    """下游单篇论文的结构化结果。

    ``chunks`` 保留下游原始顺序；``metadata`` 只保存可安全回传的论文属性，
    不包含下游排序调试字段或原始大对象。
    """

    docid: str
    title: str
    chunks: List[str]
    metadata: Dict[str, Any]
    status: str = "ok"
    warnings: tuple[str, ...] = ()


# =====================================================================
# 纯函数：query 拼接 / 请求体构建 / 签名 / 结果拼接
# =====================================================================

def build_query(docids: List[str]) -> str:
    """docid 列表 → ``docid:a,b``。"""
    return QUERY_PREFIX + ",".join(docids)


def build_search_body(docids: List[str], logid: str) -> Dict[str, Any]:
    """构建下游 /search 请求体。logid 透传，query 由 docids 拼接，其余为模板常量。"""
    return {
        FIELD_LOGID: logid,
        FIELD_QUERY: build_query(docids),
        FIELD_LANG: LANG,
        FIELD_REGION: {FIELD_REGION_SREGION: REGION_SREGION},
        FIELD_LANGS: {
            FIELD_LANGS_SETTING_LANG: LANG,
            FIELD_LANGS_LGD_RESULT: {
                FIELD_LANGS_SPELL_LANG: [{FIELD_SPELL_TYPE: LANG, FIELD_SPELL_CONF: SPELL_CONF}]
            },
        },
        FIELD_RETURN_TYPE: RETURN_TYPE,
        FIELD_USER_INFO: {FIELD_USER_ID: USER_ID},
        FIELD_FILTER: {},
        FIELD_EXTRAINFO: {
            "skip_cache": EXTRAINFO_SKIP_CACHE,
            "r_chunk_type": EXTRAINFO_R_CHUNK_TYPE,
            "gpt_mode": EXTRAINFO_GPT_MODE,
            "skip_sw": EXTRAINFO_SKIP_SW,
            "skip_df": EXTRAINFO_SKIP_DF,
        },
    }


def sign_request(secret_key: str, timestamp: str, uri: str, body_str: str) -> str:
    """HMAC-SHA256 hexdigest 签名。

    签名串：``timestamp={ts}&url={uri}&body={body_str}``（与下游一致）。
    """
    if not secret_key:
        raise AuthError("auth_key 未配置，无法生成 authCode")
    value = f"timestamp={timestamp}&url={uri}&body={body_str}"
    return hmac.new(
        key=secret_key.encode("utf-8"),
        msg=value.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()


def _as_dict(value: Any) -> Dict[str, Any]:
    """把值规整为 dict：dict 原样；str 尝试 json.loads；其余返回 {}。

    真实下游可能把 extrainfo / meta_data 以 JSON 字符串形式返回，需容错解码。
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _extract_chunks(item: Dict[str, Any]) -> Optional[List[str]]:
    """从单个 result 取出 chunks。

    真实响应里 ``chunks`` 直接挂在 ``extrainfo`` 下；样例/兼容情形在 ``extrainfo.meta_data.chunks``。
    extrainfo / meta_data / chunks 均容忍 JSON 字符串。缺失/非法/空返回 None（调用方据此跳过）。
    任何异常都不向上抛。
    """
    try:
        extrainfo = _as_dict(item.get(FIELD_EXTRAINFO))
        # 优先：extrainfo.chunks（真实响应）
        raw = extrainfo.get(FIELD_CHUNKS)
        # 兼容：extrainfo.meta_data.chunks（样例结构）
        if raw is None:
            meta_data = _as_dict(extrainfo.get(FIELD_META_DATA))
            raw = meta_data.get(FIELD_CHUNKS)
        if raw is None:
            return None
        chunks = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(chunks, list):
            return None
        cleaned = [c for c in chunks if isinstance(c, str) and c.strip()]
        return cleaned or None
    except Exception:
        return None


def _extract_metadata(item: Dict[str, Any]) -> Dict[str, Any]:
    """提取适合公共响应的基础论文元数据。

    真实响应中的 ``meta_data`` 通常是 JSON 字符串；顶层与 extrainfo
    字段同时存在时，语义更明确的 meta_data 优先。
    """
    extrainfo = _as_dict(item.get(FIELD_EXTRAINFO))
    meta_data = _as_dict(extrainfo.get(FIELD_META_DATA))
    metadata: Dict[str, Any] = {}

    sources = (item, extrainfo, meta_data)
    aliases = {
        "url": ("url",),
        "abstract": ("abstract", "desc", "rdesc"),
        "author": ("author",),
        "publisher": ("publisher",),
        "journal": ("journal", "pJournal"),
        "doi": ("doi",),
        "publish_time": ("publish_time",),
        "sitename": ("sitename",),
        "lang": ("lang",),
    }
    for output_key, candidates in aliases.items():
        value = None
        # meta_data 应优先于 extrainfo 和顶层，因此反向遍历。
        for source in reversed(sources):
            for candidate in candidates:
                if source.get(candidate) not in (None, ""):
                    value = source[candidate]
                    break
            if value not in (None, ""):
                break
        if value not in (None, ""):
            metadata[output_key] = value
    return metadata


def extract_documents(
    results: Optional[List[Dict[str, Any]]],
    requested_docids: Optional[List[str]] = None,
) -> List[DocidSearchDocument]:
    """将下游 results 转为结构化论文列表。

    - 保留有元数据但 chunks 损坏的论文，并标为 ``no_content``；
    - 按 docid（缺失时按 URL，再缺失时按结果位置）首次出现去重；
    - 不在此处规范化或重新切分正文。
    """
    documents: List[DocidSearchDocument] = []
    seen: set[str] = set()
    for index, item in enumerate(results or []):
        if not isinstance(item, dict):
            continue
        extrainfo = _as_dict(item.get(FIELD_EXTRAINFO))
        meta_data = _as_dict(extrainfo.get(FIELD_META_DATA))
        docid = str(
            item.get("docid")
            or extrainfo.get("docid")
            or meta_data.get("docid")
            or (
                requested_docids[index]
                if requested_docids is not None and index < len(requested_docids)
                else ""
            )
            or ""
        ).strip()
        metadata = _extract_metadata(item)
        identity = docid or str(metadata.get("url") or f"result:{index}")
        if identity in seen:
            continue
        seen.add(identity)

        chunks = _extract_chunks(item) or []
        warnings: tuple[str, ...] = () if chunks else ("NO_VALID_CHUNKS",)
        documents.append(
            DocidSearchDocument(
                docid=docid,
                title=str(item.get(FIELD_TITLE) or item.get("rtitle") or ""),
                chunks=chunks,
                metadata=metadata,
                status="ok" if chunks else "no_content",
                warnings=warnings,
            )
        )
    return documents


def assemble_results(results: Optional[List[Dict[str, Any]]]) -> str:
    """把下游 results 拼接为 ``[i]title:..|||content:..`` 字符串。

    - 逐条提取 title + chunks；chunks 缺失/非法/空 → **跳过该条**（决策 C）；
    - 保留项按顺序 1-based 编号；
    - 同一篇 chunks 用 ``\\n`` 拼接；多篇用 ``\\n`` 分隔。
    """
    if not results:
        return ""
    entries: List[str] = []
    idx = 0
    for item in results:
        if not isinstance(item, dict):
            continue
        chunks = _extract_chunks(item)
        if not chunks:
            continue
        idx += 1
        title = item.get(FIELD_TITLE) or ""
        entries.append(ENTRY_FORMAT.format(idx=idx, title=title, content=CHUNK_JOIN.join(chunks)))
    return ENTRY_JOIN.join(entries)


# =====================================================================
# 客户端
# =====================================================================

class DocidSearchClient:
    """/search 接口客户端（同步，单次请求，无轮询）。"""

    def __init__(
        self,
        config: DocidSearchConfig,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        log_downstream_request: bool = False,
        log_downstream_response: bool = False,
    ) -> None:
        self.config = config
        self.timeout = timeout
        self.log_downstream_request = log_downstream_request
        self.log_downstream_response = log_downstream_response
        self._session = requests.Session()

    def _uri(self) -> str:
        up = urlparse(self.config.url)
        return up.path + ("?" + up.query if up.query else "")

    def _build_headers(self, timestamp: str, body_str: str) -> Dict[str, str]:
        auth_code = sign_request(self.config.auth_key, timestamp, self._uri(), body_str)
        return {
            HEADER_CONTENT_TYPE: CONTENT_TYPE_JSON,
            HEADER_AUTH_CODE: auth_code,
            HEADER_TIMESTAMP: timestamp,
            HEADER_FOREIGN_SEARCH_LANG: FOREIGN_SEARCH_LANG_VALUE,
        }

    def search(self, docids: List[str], logid: str) -> Dict[str, Any]:
        """发起一次 /search 请求，返回原始响应 dict。"""
        body_str = json.dumps(build_search_body(docids, logid))
        timestamp = str(int(time.time() * 1000))
        headers = self._build_headers(timestamp, body_str)
        if self.log_downstream_request:
            # 仅打印 url + body（不含 authCode 等鉴权头）
            logger.info("[downstream-request] url=%s body=%s", self.config.url, body_str)
        try:
            logger.debug("POST %s logid=%s", self.config.url, logid)
            resp = self._session.post(
                url=self.config.url,
                data=body_str.encode("utf-8"),  # 与签名串字节一致
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RequestError(f"请求失败: {exc}") from exc

        if self.log_downstream_response:
            logger.info("[downstream-response] status=%s body=%s", resp.status_code, resp.text)

        if resp.status_code != 200:
            raise RequestError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ResponseParseError(f"响应非合法 JSON: {exc}; body={resp.text[:200]}") from exc

    def fetch(self, docids: List[str], logid: str) -> str:
        """发起搜索并返回拼接后的 results 字符串。"""
        body = self.search(docids, logid)
        return assemble_results(body.get(FIELD_RESULTS) or [])

    def fetch_documents(self, docids: List[str], logid: str) -> List[DocidSearchDocument]:
        """发起搜索并返回结构化论文，供章节/chunk/rerank 流程使用。"""
        body = self.search(docids, logid)
        return extract_documents(body.get(FIELD_RESULTS) or [], requested_docids=docids)
