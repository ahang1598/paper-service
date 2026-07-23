# -*- coding: utf-8 -*-
"""上游模拟测试：覆盖 DocFullTextClient 的 8 种场景。

通过 mock requests.Session.post 模拟下游响应，验证：
    1. 直接成功
    2. 多次 pending 后成功
    3. 解析失败 (status=fail)
    4. 业务错误 (code=1)
    5. 非法 JSON
    6. HTTP 错误 (非 200)
    7. 网络重试（前 K 次异常后成功 / 全失败耗尽）
    8. 重新签名（每次请求 timestamp/token 不同）
    9. pending 超时
"""

from __future__ import annotations

from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.clients.doc_fulltext_client import (
    BusinessError,
    DocFullTextClient,
    DocFullTextRequest,
    DocPendingTimeout,
    RequestError,
    ResponseParseError,
    ServiceConfig,
    SplitterType,
    DeviceInfo,
)


# =====================================================================
# 辅助
# =====================================================================

def _make_client(**kwargs) -> DocFullTextClient:
    defaults = dict(
        max_retries=3,
        retry_backoff_sec=0.0,   # 测试不等待
        poll_max_times=5,
        poll_interval_sec=0.0,   # 测试不等待
    )
    defaults.update(kwargs)
    return DocFullTextClient(ServiceConfig(auth_key="test-key"), **defaults)


def _make_request() -> DocFullTextRequest:
    return DocFullTextRequest(
        file_id="a.docx",
        device=DeviceInfo(device_id="pmq"),
        request_id="rid",
        splitter=SplitterType.SMALL_CHUNK,
    )


class FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("invalid json")
        return self._json


def _success_resp():
    return FakeResp(json_data={
        "code": 0, "status": "success", "request_id": "rid",
        "data": [{"content": "PART-", "metadata": {"chunk_id": 1}},
                 {"content": "A", "metadata": {"chunk_id": 0}}],
        "doc_hash": "h", "message": "ok",
    })


def _patch_post(client: DocFullTextClient, responses: List[Any]):
    """让 client._session.post 按顺序返回 responses 中的项。

    每项可以是 FakeResp、或一个 Exception（抛出）。
    """
    session = client._session  # type: ignore[attr-defined]
    post_mock = MagicMock(side_effect=list(responses))
    session.post = post_mock  # type: ignore[attr-defined]
    return post_mock


# =====================================================================
# 1. 直接成功
# =====================================================================

def test_direct_success():
    client = _make_client()
    _patch_post(client, [_success_resp()])
    content = client.fetch_full_text(_make_request())
    assert content == "APART-"  # chunk_id 0 先，1 后


# =====================================================================
# 2. 多次 pending 后成功
# =====================================================================

def test_pending_then_success():
    client = _make_client()
    pending = FakeResp(json_data={"code": 0, "status": "pending", "data": []})
    _patch_post(client, [pending, pending, _success_resp()])
    content = client.fetch_full_text(_make_request())
    assert content == "APART-"
    assert client._session.post.call_count == 3  # type: ignore[attr-defined]


# =====================================================================
# 3. 解析失败 (status=fail)
# =====================================================================

def test_parse_fail_raises_business_error():
    client = _make_client()
    fail = FakeResp(json_data={"code": 0, "status": "fail", "description": "parse failed"})
    _patch_post(client, [fail])
    with pytest.raises(BusinessError):
        client.fetch_full_text(_make_request())


# =====================================================================
# 4. 业务错误 (code=1)
# =====================================================================

def test_business_error_code_nonzero():
    client = _make_client()
    biz_err = FakeResp(json_data={"code": 1, "description": "splitter arguement invalid", "data": []})
    _patch_post(client, [biz_err])
    with pytest.raises(BusinessError):
        client.fetch_full_text(_make_request())


# =====================================================================
# 5. 非法 JSON
# =====================================================================

def test_invalid_json_raises_parse_error():
    client = _make_client()
    bad = FakeResp(status_code=200, text="not a json")
    _patch_post(client, [bad])
    with pytest.raises(ResponseParseError):
        client.fetch_full_text(_make_request())


# =====================================================================
# 6. HTTP 错误
# =====================================================================

def test_http_error_raises_request_error():
    client = _make_client()
    err = FakeResp(status_code=500, text="server error")
    _patch_post(client, [err])
    with pytest.raises(RequestError):
        client.fetch_full_text(_make_request())


# =====================================================================
# 7. 网络重试：前 K 次异常后成功
# =====================================================================

def test_network_retry_then_success():
    client = _make_client(max_retries=3, retry_backoff_sec=0.0)
    _patch_post(client, [
        requests.ConnectionError("boom"),
        requests.ConnectionError("boom"),
        _success_resp(),
    ])
    content = client.fetch_full_text(_make_request())
    assert content == "APART-"


def test_network_retry_exhausted():
    client = _make_client(max_retries=2, retry_backoff_sec=0.0)
    _patch_post(client, [
        requests.ConnectionError("boom"),
        requests.ConnectionError("boom"),
    ])
    with pytest.raises(RequestError):
        client.fetch_full_text(_make_request())


# =====================================================================
# 8. 重新签名：每次请求 timestamp/token 不同
# =====================================================================

def test_resign_per_request(monkeypatch):
    """每次 _post_once 都重新生成 timestamp/token（不复用缓存签名）。

    通过注入单调递增的时间戳，使每次请求可区分，从而稳定验证重签名机制。
    """
    import app.clients.doc_fulltext_client as client_mod

    counter = {"n": 1_700_000_000_000}

    def _fake_now():
        val = counter["n"]
        counter["n"] += 1
        return val

    monkeypatch.setattr(client_mod, "_now_timestamp_ms", _fake_now)

    client = _make_client()
    pending = FakeResp(json_data={"code": 0, "status": "pending", "data": []})
    post_mock = _patch_post(client, [pending, pending, _success_resp()])

    client.fetch_full_text(_make_request())

    timestamps = []
    tokens = []
    for call in post_mock.call_args_list:
        headers = call.kwargs.get("headers", {})
        timestamps.append(headers.get("timestamp"))
        tokens.append(headers.get("token"))

    # 3 次请求都带了非空 timestamp/token
    assert len(timestamps) == 3
    assert all(timestamps) and all(tokens)
    # 注入单调递增时间戳后，3 次签名应互不相同
    assert len(set(timestamps)) == 3
    assert len(set(tokens)) == 3


# =====================================================================
# 9. pending 超时
# =====================================================================

def test_pending_timeout():
    client = _make_client(poll_max_times=3, poll_interval_sec=0.0)
    pending = FakeResp(json_data={"code": 0, "status": "pending", "data": []})
    _patch_post(client, [pending, pending, pending])
    with pytest.raises(DocPendingTimeout):
        client.fetch_full_text(_make_request())


# =====================================================================
# 附：check_response 公开方法
# =====================================================================

def test_check_response_success():
    DocFullTextClient.check_response({"code": 0})


def test_check_response_fail():
    with pytest.raises(BusinessError):
        DocFullTextClient.check_response({"code": 1, "description": "bad"})
