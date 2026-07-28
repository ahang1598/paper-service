# -*- coding: utf-8 -*-
"""验收标准验证测试。

覆盖：
    1. HTTP 与 WebSocket 对同一输入得到相同最终结果；
    2. 并发查询时不阻塞事件循环（asyncio.to_thread 包装同步 requests）；
    3. 日志和错误响应不泄露密钥或全文。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from academic_service.app.clients.doc_fulltext_client import RequestError
from academic_service.tests.conftest import make_chunk, make_fail_body, make_success_body


def _payload():
    return {"query": "a.docx", "options": {}}


# =====================================================================
# 1. HTTP 与 WebSocket 同输入同结果
# =====================================================================

def test_http_and_ws_produce_same_result(client, fake_client):
    """同一输入下，HTTP /query 与 WS /api/v1/navigator/ws 的最终 data 完全相等。"""
    chunks = [make_chunk(2, "C"), make_chunk(0, "A"), make_chunk(1, "B")]
    expected_content = "ABC"

    # HTTP 路径
    fake_client.set_script(make_success_body(chunks))
    http_resp = client.post("/api/v1/navigator/query", json=_payload())
    assert http_resp.status_code == 200
    http_content = http_resp.json()["data"]["results"]

    # WS 路径（重置脚本，同样的成功响应）
    fake_client.set_script(make_success_body(chunks))
    with client.websocket_connect("/api/v1/navigator/ws") as ws:
        ws.send_json(_payload())
        ws.receive_json()  # started
        done = ws.receive_json()
        ws_content = done["data"]["results"]

    assert http_content == ws_content == expected_content
    # chunk_count 与 doc_hash 也一致
    fake_client.set_script(make_success_body(chunks))
    http_data = client.post("/api/v1/navigator/query", json=_payload()).json()["data"]
    fake_client.set_script(make_success_body(chunks))
    with client.websocket_connect("/api/v1/navigator/ws") as ws:
        ws.send_json(_payload())
        ws.receive_json()
        ws_data = ws.receive_json()["data"]
    assert http_data["chunk_count"] == ws_data["chunk_count"]
    assert http_data["doc_hash"] == ws_data["doc_hash"]


# =====================================================================
# 2. 并发查询不阻塞事件循环
# =====================================================================

def test_concurrent_queries_do_not_block_event_loop(client, fake_client, monkeypatch):
    """N 个并发查询总耗时应 ≈ 单次（非 N×），证明同步 requests 未阻塞事件循环。

    通过让 fake_client.post_once 引入可控的同步阻塞（模拟下游耗时），
    若未用 to_thread，并发会被串行化导致总耗时 ≈ N×单次。
    """
    import academic_service.app.services.document.fulltext_handler as fh
    import asyncio as _asyncio

    # 真实的同步阻塞（模拟 requests 耗时），用 time.sleep 而非 asyncio.sleep
    blocked = {"remaining": 0}

    class SlowClient:
        def __init__(self):
            self.calls = 0

        def post_once(self, request):
            self.calls += 1
            import time as _t
            _t.sleep(0.2)  # 同步阻塞 200ms（模拟下游 IO）
            return make_success_body([make_chunk(0, "A")])

        @staticmethod
        def check_response(body):
            from academic_service.app.clients.doc_fulltext_client import DocFullTextClient
            DocFullTextClient.check_response(body)

    from academic_service.app.api.v1.query import get_client_factory
    client.app.dependency_overrides[get_client_factory] = lambda: (lambda: SlowClient())

    async def _run_concurrent(n: int):
        # 用 ASGI 内部的并发：通过 TestClient 发起多个请求会被 portal 串行化，
        # 因此直接在 handler 层验证 to_thread 的并发性
        from academic_service.app.core.registry import get_handler_class
        from academic_service.app.core.registry import HandlerContext
        from academic_service.app.config import Settings

        handler_cls = get_handler_class("doc_fulltext")
        from academic_service.app.schemas.document import DocFullTextParams
        params = DocFullTextParams(file_id="a.docx")
        ctx = HandlerContext(
            client_factory=lambda: SlowClient(),
            default_device=None,
            request_id="rid",
            settings=None,
        )
        tasks = [handler_cls().execute(params, ctx) for _ in range(n)]
        start = time.monotonic()
        await asyncio.gather(*tasks)
        elapsed = time.monotonic() - start
        return elapsed

    n = 4
    single = 0.2
    # Python 3.10+ 弃用、3.12+ 移除 get_event_loop() 的隐式创建行为，
    # 改用 asyncio.run()（自动创建/关闭事件循环，语义等价）。
    elapsed = asyncio.run(_run_concurrent(n))
    # 并发执行：总耗时应远小于 N×single（=0.8s）。
    # 允许一定开销，断言 < N×single 的 60% 即证明未串行阻塞。
    assert elapsed < n * single * 0.6, f"并发耗时 {elapsed:.3f}s 过大，疑似阻塞事件循环"


# =====================================================================
# 3. 日志 / 错误响应不泄露密钥或全文
# =====================================================================

SECRET_KEY = "top-secret-doc-key"
SECRET_TOKEN = "top-secret-bearer"


def test_error_response_does_not_leak_secrets(auth_app, fake_client, caplog):
    """错误响应体不含密钥、token、全文 content。"""
    import logging
    from academic_service.app.api.v1.query import get_client_factory

    # 注入带敏感值的配置
    from academic_service.app.config import Settings
    sensitive_settings = Settings(
        auth_enabled=True,
        api_bearer_tokens=SECRET_TOKEN,
        doc_service_auth_key=SECRET_KEY,
    )
    auth_app.dependency_overrides[get_client_factory] = lambda: (lambda: fake_client)
    # 覆盖 settings 依赖
    from academic_service.app.config import get_settings
    auth_app.dependency_overrides[get_settings] = lambda: sensitive_settings

    from fastapi.testclient import TestClient
    caplog.set_level(logging.DEBUG)

    fake_client.set_script(make_fail_body("解析失败"))
    with TestClient(auth_app) as tc:
        resp = tc.post(
            "/api/v1/navigator/query",
            json=_payload(),
            headers={"Authorization": f"Bearer {SECRET_TOKEN}"},
        )
    body_text = resp.text

    # 错误响应体不含密钥与 token
    assert SECRET_KEY not in body_text
    assert SECRET_TOKEN not in body_text


def test_log_redaction_masks_secrets(caplog):
    """RedactingFormatter 应把已知敏感子串替换为 [REDACTED]。"""
    import logging
    from academic_service.app.middleware.logging import RedactingFormatter

    formatter = RedactingFormatter("%(message)s", secret_substrings=("MY_SECRET",))
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0,
        msg="token=MY_SECRET something", args=None, exc_info=None,
    )
    out = formatter.format(record)
    assert "MY_SECRET" not in out
    assert "[REDACTED]" in out


def test_sanitize_mapping_redacts_sensitive_fields():
    """sanitize_mapping 应脱敏 authorization/token/content 字段。"""
    from academic_service.app.middleware.logging import sanitize_mapping
    data = {
        "authorization": "Bearer abc",
        "token": "xyz",
        "content": "THE FULL DOCUMENT TEXT",
        "doc_service_auth_key": "k",
        "safe_field": "keep",
    }
    out = sanitize_mapping(data)
    assert out["authorization"] == "[REDACTED]"
    assert out["token"] == "[REDACTED]"
    assert out["doc_service_auth_key"] == "[REDACTED]"
    assert out["content"] == "[content omitted]"
    assert out["safe_field"] == "keep"
    # 原对象未被修改
    assert data["token"] == "xyz"


def test_sanitize_mapping_recursive():
    """嵌套 dict / list 也应脱敏。"""
    from academic_service.app.middleware.logging import sanitize_mapping
    data = {
        "outer": {"token": "t", "nested": [{"content": "c"}]},
        "list": [{"auth_key": "k"}],
    }
    out = sanitize_mapping(data)
    assert out["outer"]["token"] == "[REDACTED]"
    assert out["outer"]["nested"][0]["content"] == "[content omitted]"
    assert out["list"][0]["auth_key"] == "[REDACTED]"
