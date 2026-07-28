# -*- coding: utf-8 -*-
"""固定签名向量测试：验证 generate_auth_token 与预计算值一致。

向量参数与服务端 CheckSignKgs 签名串格式严格对应：
    `{method}&{url_path}&deviceId={device_id}&timestamp={timestamp_ms}`
HMAC-SHA256(key) → Base64。
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from academic_service.app.clients.doc_fulltext_client import generate_auth_token


def _expected(method, url, device_id, ts, key):
    """独立重算预期值（不依赖被测函数），用于交叉验证。"""
    sign_str = f"{method}&{url}&deviceId={device_id}&timestamp={ts}"
    digest = hmac.new(key.encode(), sign_str.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


@pytest.mark.parametrize(
    "method,url,device_id,ts,key",
    [
        ("POST", "/copilot_for_docs/doc_fulltext", "pmq", 1714000000000, "test-key"),
        ("POST", "/copilot_for_docs/doc_fulltext", "pmq", 0, "k"),
        ("POST", "/copilot_for_docs/doc_fulltext", "device-xyz", 1714000000123, "another-secret"),
    ],
)
def test_sign_vector_matches(method, url, device_id, ts, key):
    got = generate_auth_token(method, url, device_id, ts, key)
    expected = _expected(method, url, device_id, ts, key)
    assert got == expected


def test_sign_changes_with_timestamp():
    """不同 timestamp 应产生不同 token（重签名基础）。"""
    t1 = generate_auth_token("POST", "/copilot_for_docs/doc_fulltext", "pmq", 1000, "k")
    t2 = generate_auth_token("POST", "/copilot_for_docs/doc_fulltext", "pmq", 1001, "k")
    assert t1 != t2


def test_sign_changes_with_key():
    """不同 key 应产生不同 token。"""
    t1 = generate_auth_token("POST", "/copilot_for_docs/doc_fulltext", "pmq", 1000, "k1")
    t2 = generate_auth_token("POST", "/copilot_for_docs/doc_fulltext", "pmq", 1000, "k2")
    assert t1 != t2


def test_sign_empty_key_raises():
    from academic_service.app.clients.doc_fulltext_client import AuthError
    with pytest.raises(AuthError):
        generate_auth_token("POST", "/x", "pmq", 1, "")


def test_sign_deterministic():
    """相同输入应得到相同 token。"""
    args = ("POST", "/copilot_for_docs/doc_fulltext", "pmq", 1714000000000, "test-key")
    assert generate_auth_token(*args) == generate_auth_token(*args)
