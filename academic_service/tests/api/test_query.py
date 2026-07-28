# -*- coding: utf-8 -*-
"""查询端点测试：成功 + 全部错误状态映射。

错误状态映射：
    400 VALIDATION_ERROR                        参数错误 / stream=true 未走 WebSocket
    502 UPSTREAM_BUSINESS_ERROR                 上游业务错误 / 解析失败
    503 UPSTREAM_UNAVAILABLE                    上游网络重试耗尽
    504 PENDING_TIMEOUT                         pending 轮询超时
"""

from __future__ import annotations

import pytest

from academic_service.app.clients.doc_fulltext_client import (
    BusinessError,
    DocPendingTimeout,
    RequestError,
)
from academic_service.tests.conftest import make_chunk, make_success_body

# 新对外信封：默认只需传 query（文件ID），其余通过 options 覆盖。
URL = "/api/v1/navigator/query"


def _payload(**options):
    """构造请求体。默认 {"query": "a.docx", "options": {}}。"""
    return {"query": "a.docx", "options": dict(options)}


# =====================================================================
# 成功
# =====================================================================

def test_query_success(client, fake_client):
    fake_client.set_script(make_success_body([make_chunk(0, "A"), make_chunk(1, "B")]))
    resp = client.post(URL, json=_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    assert body["message"] == "success"
    assert body["data"]["results"] == "AB"
    assert body["data"]["chunk_count"] == 2
    assert body["data"]["doc_hash"] == "hash123"


def test_query_success_default_options_omitted(client, fake_client):
    """默认只需传 query，options 完全省略。"""
    fake_client.set_script(make_success_body([make_chunk(0, "A")]))
    resp = client.post(URL, json={"query": "a.docx"})
    assert resp.status_code == 200
    assert resp.json()["data"]["results"] == "A"


# =====================================================================
# 400 参数错误
# =====================================================================

def test_query_validation_error_missing_query(client):
    """query 字段缺失 → 400（Pydantic RequestValidationError）。"""
    resp = client.post(URL, json={"options": {}})
    assert resp.status_code == 400


def test_query_validation_error_invalid_splitter(client):
    resp = client.post(URL, json=_payload(splitter=99))
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION_ERROR"


def test_query_validation_error_negative_page(client):
    resp = client.post(URL, json=_payload(pages=[-1]))
    assert resp.status_code == 400


# =====================================================================
# 400 stream=true 应改用 WebSocket
# =====================================================================

def test_query_stream_true_rejected_with_guidance(client):
    """stream=true 时 HTTP 主动拒绝并引导到 WebSocket 端点。"""
    resp = client.post(URL, json={"query": "a.docx", "options": {}, "stream": True})
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION_ERROR"


def test_query_stream_false_ok(client, fake_client):
    """stream=false（默认）正常走 HTTP。"""
    fake_client.set_script(make_success_body([make_chunk(0, "A")]))
    resp = client.post(URL, json={"query": "a.docx", "options": {}, "stream": False})
    assert resp.status_code == 200


# =====================================================================
# 502 上游业务错误 / 解析失败
# =====================================================================

def test_query_upstream_business_error(client, fake_client):
    """下游 code=1 → 502。"""
    fake_client.set_script({"code": 1, "description": "splitter arguement invalid", "data": []})
    resp = client.post(URL, json=_payload())
    assert resp.status_code == 502
    assert resp.json()["code"] == "UPSTREAM_BUSINESS_ERROR"


def test_query_upstream_parse_fail(client, fake_client):
    """下游 status=fail → 502。"""
    fake_client.set_script({"code": 0, "status": "fail", "description": "parse failed"})
    resp = client.post(URL, json=_payload())
    assert resp.status_code == 502


# =====================================================================
# 503 上游不可用
# =====================================================================

def test_query_upstream_unavailable(client, fake_client):
    """下游网络异常 → 503。"""
    fake_client.set_script(RequestError("network down"))
    resp = client.post(URL, json=_payload())
    assert resp.status_code == 503
    assert resp.json()["code"] == "UPSTREAM_UNAVAILABLE"


# =====================================================================
# 504 pending 超时
# =====================================================================

def test_query_pending_timeout(client, fake_client):
    """下游持续 pending → 504。"""
    fake_client.set_script({"code": 0, "status": "pending", "data": []})
    resp = client.post(URL, json=_payload())
    assert resp.status_code == 504
    assert resp.json()["code"] == "PENDING_TIMEOUT"


# =====================================================================
# 错误响应结构一致性
# =====================================================================

@pytest.mark.parametrize("status_code", [400, 502, 503, 504])
def test_error_response_envelope_shape(client, fake_client, status_code):
    """所有错误响应都遵循统一 envelope：code/message/request_id/data。"""
    if status_code == 400:
        body = {"query": "a.docx", "options": {"splitter": 99}}
    elif status_code == 502:
        fake_client.set_script({"code": 1, "description": "err"})
        body = _payload()
    elif status_code == 503:
        fake_client.set_script(RequestError("x"))
        body = _payload()
    else:  # 504
        fake_client.set_script({"code": 0, "status": "pending"})
        body = _payload()

    resp = client.post(URL, json=body)
    assert resp.status_code == status_code
    data = resp.json()
    assert set(data.keys()) >= {"code", "message", "request_id", "data"}
    assert data["data"] is None


def test_error_response_no_content_leak(client, fake_client):
    """错误响应体不应泄露文档全文 content。"""
    fake_client.set_script({"code": 0, "status": "fail", "description": "SUPER_SECRET_FULLTEXT"})
    resp = client.post(URL, json=_payload())
    # description 可能出现在 message 中，但不应有 content 字段的数据
    assert "data" in resp.json()
    assert resp.json()["data"] is None
