# -*- coding: utf-8 -*-
"""pytest 公共配置与 fixture。

约定：
    - 默认功能测试关闭鉴权（AUTH_ENABLED=False），无需关心 token；
    - 鉴权专用测试用 auth_enabled_app fixture 显式开启；
    - 上游交互全部 mock，零真实网络。
"""

from __future__ import annotations

import os
from typing import Any, Callable, Iterator, List, Optional

import pytest

# 确保测试在仓库根目录可导入 app 包
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from academic_service.app.clients.doc_fulltext_client import (
    BusinessError,
    DocFullTextClient,
    DocFullTextRequest,
    RequestError,
)
from academic_service.app.config import Settings, get_settings
from academic_service.app.main import create_app


# =====================================================================
# Settings 构造
# =====================================================================

def make_settings(
    *,
    auth_enabled: bool = False,
    bearer_tokens: str = "secret-token-1",
    doc_service_auth_key: str = "test-key",
    doc_poll_max_times: int = 5,
    doc_poll_interval_sec: float = 0.0,
    doc_max_retries: int = 3,
) -> Settings:
    """构造测试用 Settings（默认关闭鉴权，轮询间隔为 0 加速）。"""
    return Settings(
        auth_enabled=auth_enabled,
        api_bearer_tokens=bearer_tokens,
        doc_service_auth_key=doc_service_auth_key,
        doc_service_host="127.0.0.1",
        doc_service_port=9999,
        doc_poll_max_times=doc_poll_max_times,
        doc_poll_interval_sec=doc_poll_interval_sec,
        doc_max_retries=doc_max_retries,
        reranker_provider="internal",
        internal_rerank_sign_key="",
        reranker_max_retries=1,
        reranker_retry_backoff_sec=0.0,
    )


@pytest.fixture
def settings() -> Settings:
    """默认关闭鉴权的测试 settings。"""
    return make_settings()


@pytest.fixture
def auth_settings() -> Settings:
    """开启鉴权的测试 settings（鉴权专用测试用）。"""
    return make_settings(auth_enabled=True, bearer_tokens="secret-token-1,secret-token-2")


# =====================================================================
# 上游响应构造器（mock requests）
# =====================================================================

class FakeResponse:
    """模拟 requests.Response。"""

    def __init__(self, status_code: int = 200, json_data: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text if text else (str(json_data) if json_data is not None else "")

    def json(self) -> Any:
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data


def make_chunk(chunk_id: int, content: str, total: int = 3) -> dict[str, Any]:
    """构造一个 chunk。"""
    return {
        "content": content,
        "metadata": {
            "chunk_id": chunk_id,
            "total_chunks": total,
        },
    }


def make_success_body(chunks: List[dict[str, Any]], doc_hash: str = "hash123") -> dict[str, Any]:
    return {
        "code": 0,
        "request_id": "rid",
        "data": chunks,
        "status": "success",
        "doc_hash": doc_hash,
        "message": "ok",
    }


def make_pending_body() -> dict[str, Any]:
    return {"code": 0, "request_id": "rid", "data": [], "status": "pending"}


def make_fail_body(reason: str = "parse failed") -> dict[str, Any]:
    return {"code": 0, "request_id": "rid", "data": [], "status": "fail", "description": reason}


def make_biz_error_body(description: str = "splitter arguement invalid") -> dict[str, Any]:
    return {"code": 1, "request_id": "rid", "data": [], "description": description}


# =====================================================================
# Mock client factory
# =====================================================================

class FakeDocClient:
    """可控的下游 client 替身。

    通过 script 设置每次 post_once 的返回/异常序列；不进行真实网络请求。
    记录每次请求的 headers（用于"重签名"断言）与请求体。
    """

    def __init__(self) -> None:
        # 每次 post_once 的脚本：返回 dict 或 抛异常
        self.script: List[Any] = []
        # 脚本耗尽后的默认响应（用于 pending/失败持续场景）。None 表示抛错。
        self.default_response: Optional[Any] = None
        self.calls: List[dict[str, Any]] = []  # 记录 (headers, payload)

    def set_script(self, *items: Any) -> "FakeDocClient":
        self.script = list(items)
        # 默认把脚本最后一项作为耗尽后的续传响应（便于 pending 持续场景）
        if items:
            self.default_response = items[-1] if not isinstance(items[-1], Exception) else None
        return self

    def post_once(self, request: DocFullTextRequest) -> dict[str, Any]:
        headers = self._build_headers(request)
        payload = request.to_dict()
        self.calls.append({"headers": headers, "payload": payload})
        if self.script:
            item = self.script.pop(0)
        elif self.default_response is not None:
            item = self.default_response
        else:
            raise AssertionError("FakeDocClient.post_once: script 已耗尽且无默认响应")
        if isinstance(item, Exception):
            raise item
        return item

    # 兼容 handler 流式调用的公开方法
    def _build_headers(self, request: DocFullTextRequest) -> dict[str, str]:
        # 生成与真实 client 相同结构的 header（每次不同 timestamp/token）
        import time as _t
        ts = int(_t.time() * 1000) + len(self.calls)
        return {"timestamp": str(ts), "token": f"tok_{ts}"}

    @staticmethod
    def check_response(body: dict[str, Any]) -> None:
        DocFullTextClient.check_response(body)

    def fetch_full_text_with_status(self, request: DocFullTextRequest):
        """模拟一次性查询：消费脚本直到 success/fail/超时。"""
        for _ in range(50):
            body = self.post_once(request)
            DocFullTextClient.check_response(body)
            status = body.get("status")
            if status == "success":
                return body.get("data") or [], "success"
            if status == "fail":
                raise BusinessError(f"文档解析失败: {body.get('description', 'fail')}")
            # pending 继续
            continue
        from academic_service.app.clients.doc_fulltext_client import DocPendingTimeout
        raise DocPendingTimeout("timeout")


@pytest.fixture
def fake_client() -> FakeDocClient:
    return FakeDocClient()


@pytest.fixture
def fake_client_factory(fake_client: FakeDocClient) -> Callable[[], Any]:
    """返回总是产出同一 fake_client 的工厂。"""
    def _factory():
        return fake_client
    return _factory


# =====================================================================
# Mock docid 搜索 client
# =====================================================================

class FakeDocidClient:
    """可控的 docid 搜索 client 替身。

    fetch/fetch_documents 按 script 返回拼接字符串或结构化论文。
    记录每次调用的 docids/logid，便于断言路由与入参拼接。
    """

    def __init__(self) -> None:
        self.calls: List[dict[str, Any]] = []
        self.script: List[Any] = []

    def set_script(self, *items: Any) -> "FakeDocidClient":
        self.script = list(items)
        return self

    def _consume(self, docids, logid: str):
        self.calls.append({"docids": list(docids), "logid": logid})
        if self.script:
            item = self.script.pop(0)
        else:
            item = []
        if isinstance(item, Exception):
            raise item
        return item

    def fetch(self, docids, logid: str) -> str:
        from academic_service.app.clients.docid_search_client import assemble_results
        item = self._consume(docids, logid)
        return assemble_results(item)

    def fetch_documents(self, docids, logid: str):
        from academic_service.app.clients.docid_search_client import extract_documents
        item = self._consume(docids, logid)
        return extract_documents(item, requested_docids=list(docids))


@pytest.fixture
def fake_docid_client() -> FakeDocidClient:
    return FakeDocidClient()


@pytest.fixture
def fake_docid_client_factory(fake_docid_client: FakeDocidClient) -> Callable[[], Any]:
    """返回总是产出同一 fake_docid_client 的工厂。"""
    def _factory():
        return fake_docid_client
    return _factory


# =====================================================================
# App / Client fixtures
# =====================================================================

@pytest.fixture
def app(settings: Settings, fake_client_factory: Callable[[], Any], fake_docid_client_factory: Callable[[], Any]):
    """构建测试 app：关闭鉴权，注入 fake client 工厂（fileid + docid）。"""
    application = create_app(settings)
    # 覆盖 client 工厂依赖
    from academic_service.app.api.v1.query import get_client_factory, get_docid_client_factory
    application.dependency_overrides[get_client_factory] = lambda: fake_client_factory
    application.dependency_overrides[get_docid_client_factory] = lambda: fake_docid_client_factory
    # 也覆盖 get_settings，确保各依赖拿到测试 settings
    application.dependency_overrides[get_settings] = lambda: settings
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
def auth_app(auth_settings: Settings, fake_client_factory: Callable[[], Any], fake_docid_client_factory: Callable[[], Any]):
    """开启鉴权的测试 app。"""
    application = create_app(auth_settings)
    from academic_service.app.api.v1.query import get_client_factory, get_docid_client_factory
    application.dependency_overrides[get_client_factory] = lambda: fake_client_factory
    application.dependency_overrides[get_docid_client_factory] = lambda: fake_docid_client_factory
    application.dependency_overrides[get_settings] = lambda: auth_settings
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    """默认（关闭鉴权）的 TestClient。"""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_client(auth_app) -> Iterator[TestClient]:
    """开启鉴权的 TestClient。"""
    with TestClient(auth_app) as c:
        yield c
