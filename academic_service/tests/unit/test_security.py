# -*- coding: utf-8 -*-
"""鉴权核心测试：AUTH_ENABLED 开/关两条路径 + token 校验。"""

from __future__ import annotations

import pytest

from academic_service.app.config import Settings
from academic_service.app.core.security import (
    extract_bearer_header,
    is_auth_enabled,
    verify_token,
)


def _settings(auth_enabled: bool, tokens: str = "tok-1,tok-2") -> Settings:
    return Settings(auth_enabled=auth_enabled, api_bearer_tokens=tokens, doc_service_auth_key="k")


# =====================================================================
# AUTH_ENABLED 开关
# =====================================================================

def test_auth_disabled_returns_true():
    s = _settings(auth_enabled=False)
    assert is_auth_enabled(s) is False
    # 关闭时无论什么 token 都放行
    assert verify_token("anything", s) is True
    assert verify_token(None, s) is True


def test_auth_enabled_flag():
    s = _settings(auth_enabled=True)
    assert is_auth_enabled(s) is True


# =====================================================================
# 开启鉴权时的 token 校验
# =====================================================================

def test_valid_token_accepted():
    s = _settings(auth_enabled=True)
    assert verify_token("tok-1", s) is True
    assert verify_token("tok-2", s) is True


def test_invalid_token_rejected():
    s = _settings(auth_enabled=True)
    assert verify_token("wrong", s) is False


def test_empty_token_rejected():
    s = _settings(auth_enabled=True)
    assert verify_token(None, s) is False
    assert verify_token("", s) is False


def test_empty_token_set_rejects_everything():
    """开启鉴权但未配置任何 token：所有请求被拒。"""
    s = Settings(auth_enabled=True, api_bearer_tokens="", doc_service_auth_key="k")
    assert verify_token("any", s) is False


# =====================================================================
# Authorization 头解析
# =====================================================================

def test_extract_bearer_valid():
    assert extract_bearer_header("Bearer abc123") == "abc123"


def test_extract_bearer_case_insensitive_scheme():
    assert extract_bearer_header("bearer abc123") == "abc123"


def test_extract_bearer_none():
    assert extract_bearer_header(None) is None


def test_extract_bearer_wrong_scheme():
    assert extract_bearer_header("Basic abc123") is None


def test_extract_bearer_empty_token():
    assert extract_bearer_header("Bearer ") is None


# =====================================================================
# token 解析（容忍空白）
# =====================================================================
def test_settings_strips_token_whitespace():
    s = Settings(auth_enabled=True, api_bearer_tokens=" a , , b ", doc_service_auth_key="k")
    assert s.bearer_tokens == ["a", "b"]
