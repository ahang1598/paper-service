# -*- coding: utf-8 -*-
"""WebSocket 测试：使用 FastAPI 官方 TestClient.websocket_connect。

覆盖：
    - 完整事件顺序（started → progress(pending)×N → done）
    - 失败事件
    - 同连接重复查询
    - 查询进行中再次提交（BUSY）
    - 客户端断开取消
    - 鉴权失败（开启鉴权时 ?token= 缺失/错误）
    - 鉴权关闭时无 token 可连
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.clients.doc_fulltext_client import RequestError
from tests.conftest import make_chunk, make_fail_body, make_pending_body, make_success_body


def _msg(**options):
    return {"query": "a.docx", "options": dict(options)}


def _msg_with_rid(rid: str, **options):
    """带 request_id 的消息（request_id 放在 options 内）。"""
    opts = {"request_id": rid, **options}
    return {"query": "a.docx", "options": opts}


# =====================================================================
# 完整事件顺序：started → pending×N → done
# =====================================================================

def test_ws_full_event_sequence(client, fake_client):
    fake_client.set_script(
        make_pending_body(),
        make_pending_body(),
        make_success_body([make_chunk(1, "B"), make_chunk(0, "A")]),
    )
    with client.websocket_connect("/api/v1/navigator/ws") as ws:
        ws.send_json(_msg())
        events = []
        while True:
            ev = ws.receive_json()
            events.append(ev)
            if ev["type"] in ("done", "error"):
                break

    types = [e["type"] for e in events]
    # 至少含 started、若干 pending、最终 done
    assert types[0] == "progress" and events[0].get("message") == "started"
    assert types[-1] == "done"
    # done 的 data.results 按 chunk_id 排序拼接
    assert events[-1]["data"]["results"] == "AB"
    assert events[-1]["data"]["chunk_count"] == 2


def test_ws_direct_success(client, fake_client):
    fake_client.set_script(make_success_body([make_chunk(0, "A")]))
    with client.websocket_connect("/api/v1/navigator/ws") as ws:
        ws.send_json(_msg())
        ev = ws.receive_json()  # started
        assert ev["type"] == "progress"
        ev = ws.receive_json()  # done
        assert ev["type"] == "done"
        assert ev["data"]["results"] == "A"


# =====================================================================
# 失败事件
# =====================================================================

def test_ws_fail_event(client, fake_client):
    fake_client.set_script(make_fail_body("parse failed"))
    with client.websocket_connect("/api/v1/navigator/ws") as ws:
        ws.send_json(_msg())
        started = ws.receive_json()
        assert started["type"] == "progress"
        ev = ws.receive_json()
        assert ev["type"] == "error"
        assert ev["code"] == "UPSTREAM_BUSINESS_ERROR"


def test_ws_business_error_event(client, fake_client):
    fake_client.set_script({"code": 1, "description": "bad splitter"})
    with client.websocket_connect("/api/v1/navigator/ws") as ws:
        ws.send_json(_msg())
        ws.receive_json()  # started
        ev = ws.receive_json()
        assert ev["type"] == "error"


def test_ws_network_error_event(client, fake_client):
    fake_client.set_script(RequestError("down"))
    with client.websocket_connect("/api/v1/navigator/ws") as ws:
        ws.send_json(_msg())
        ws.receive_json()  # started
        ev = ws.receive_json()
        assert ev["type"] == "error"
        assert ev["code"] == "UPSTREAM_UNAVAILABLE"


def test_ws_pending_timeout_event(client, fake_client):
    fake_client.set_script(make_pending_body())  # 持续 pending
    with client.websocket_connect("/api/v1/navigator/ws") as ws:
        ws.send_json(_msg())
        ws.receive_json()  # started
        # 消费若干 pending 后应收到 timeout error
        ev = None
        for _ in range(20):
            ev = ws.receive_json()
            if ev["type"] in ("error", "done"):
                break
        assert ev is not None
        assert ev["type"] == "error"
        assert ev["code"] == "PENDING_TIMEOUT"


# =====================================================================
# 同连接重复查询
# =====================================================================

def test_ws_sequential_queries_same_connection(client, fake_client):
    fake_client.set_script(
        make_success_body([make_chunk(0, "FIRST")]),
        make_success_body([make_chunk(0, "SECOND")]),
    )
    with client.websocket_connect("/api/v1/navigator/ws") as ws:
        # 第一次查询
        ws.send_json(_msg_with_rid("r1"))
        ws.receive_json()  # started
        done1 = ws.receive_json()
        assert done1["type"] == "done"
        assert done1["data"]["results"] == "FIRST"
        assert done1["request_id"] == "r1"

        # 第二次查询（同连接）
        ws.send_json(_msg_with_rid("r2"))
        ws.receive_json()  # started
        done2 = ws.receive_json()
        assert done2["type"] == "done"
        assert done2["data"]["results"] == "SECOND"
        assert done2["request_id"] == "r2"


# =====================================================================
# 查询进行中再次提交 → BUSY（并发模型，同步 TestClient）
# =====================================================================

def test_ws_busy_when_query_in_progress(auth_client, auth_app, monkeypatch):
    """查询进行中再次提交应返回 BUSY。

    服务端采用并发模型（查询在独立 task，主循环继续读消息）。
    用一个会阻塞的 pending sleep 让第一次查询"进行中"，此时发第二条消息应立即 BUSY。
    """
    import asyncio as _asyncio

    class PendingClient:
        def post_once(self, request):
            return make_pending_body()

        @staticmethod
        def check_response(body):
            from app.clients.doc_fulltext_client import DocFullTextClient
            DocFullTextClient.check_response(body)

    from app.api.v1.query import get_client_factory
    auth_app.dependency_overrides[get_client_factory] = lambda: (lambda: PendingClient())

    # 让 pending 后的 sleep 永久挂起（用 Future 而非被 patch 的 sleep，避免递归），
    # 使第一次查询停在"进行中"，从而能测到并发读消息返回 BUSY。
    _real_sleep = _asyncio.sleep

    async def _blocking_sleep(sec):
        await _asyncio.Future()  # 永不自然完成，仅靠取消退出
    monkeypatch.setattr(_asyncio, "sleep", _blocking_sleep)

    try:
        with auth_client.websocket_connect("/api/v1/navigator/ws?token=secret-token-1") as ws:
            # 发第一条查询（进入 pending 后被永久挂起 = 进行中）
            ws.send_json(_msg_with_rid("first"))
            ws.receive_json()  # started
            ws.receive_json()  # pending progress

            # 查询进行中，发第二条 → 应立即收到 BUSY
            ws.send_json(_msg_with_rid("second"))
            busy_ev = ws.receive_json()
            assert busy_ev["type"] == "error"
            assert busy_ev["code"] == "BUSY"
            assert busy_ev["request_id"] == "second"
    finally:
        monkeypatch.setattr(_asyncio, "sleep", _real_sleep)


# =====================================================================
# 客户端断开取消（同步 TestClient + 并发模型）
# =====================================================================

def test_ws_client_disconnect_cancels(auth_client, auth_app, monkeypatch, caplog):
    """发起持续 pending 的查询后断开，服务端任务应被取消且不抛未处理异常。"""
    import asyncio as _asyncio
    import logging

    caplog.set_level(logging.INFO, logger="paper-service.api.ws")

    class PendingClient:
        def post_once(self, request):
            return make_pending_body()

        @staticmethod
        def check_response(body):
            from app.clients.doc_fulltext_client import DocFullTextClient
            DocFullTextClient.check_response(body)

    from app.api.v1.query import get_client_factory
    auth_app.dependency_overrides[get_client_factory] = lambda: (lambda: PendingClient())

    _real_sleep = _asyncio.sleep

    async def _long_sleep(sec):
        await _asyncio.Future()  # 永久挂起，仅靠取消退出
    monkeypatch.setattr(_asyncio, "sleep", _long_sleep)

    # 发起查询后立即断开；with 退出触发关闭。
    # 服务端的并发查询 task 应被取消（finally 中 cancel），不抛未处理异常。
    try:
        with auth_client.websocket_connect("/api/v1/navigator/ws?token=secret-token-1") as ws:
            ws.send_json(_msg_with_rid("doomed"))
            ws.receive_json()  # started
        # 退出 with 即断开；若服务端未正确取消，TestClient 上下文会抛错导致失败
    finally:
        monkeypatch.setattr(_asyncio, "sleep", _real_sleep)


# =====================================================================
# 鉴权：关闭时无 token 可连
# =====================================================================

def test_ws_auth_disabled_no_token_connects(client, fake_client):
    """鉴权关闭时，不带 ?token= 也能连接。"""
    fake_client.set_script(make_success_body([make_chunk(0, "A")]))
    with client.websocket_connect("/api/v1/navigator/ws") as ws:  # 无 token 参数
        ws.send_json(_msg())
        ws.receive_json()  # started
        ev = ws.receive_json()
        assert ev["type"] == "done"


# =====================================================================
# 鉴权：开启时 ?token= 缺失/错误被拒
# =====================================================================

def test_ws_auth_enabled_missing_token_rejected(auth_client):
    """开启鉴权，无 ?token= → 连接被拒。"""
    with pytest.raises(Exception):
        # 未鉴权直接关闭，websocket_connect 会抛异常（WebSocketDisconnect 等）
        with auth_client.websocket_connect("/api/v1/navigator/ws"):
            pass


def test_ws_auth_enabled_wrong_token_rejected(auth_client):
    with pytest.raises(Exception):
        with auth_client.websocket_connect("/api/v1/navigator/ws?token=wrong"):
            pass


def test_ws_auth_enabled_correct_token_connects(auth_client, fake_client):
    fake_client.set_script(make_success_body([make_chunk(0, "A")]))
    with auth_client.websocket_connect("/api/v1/navigator/ws?token=secret-token-1") as ws:
        ws.send_json(_msg())
        ws.receive_json()  # started
        ev = ws.receive_json()
        assert ev["type"] == "done"
