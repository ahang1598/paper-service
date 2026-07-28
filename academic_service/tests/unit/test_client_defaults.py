# -*- coding: utf-8 -*-
"""验证 doc_fulltext 客户端的默认值来自配置文件（configs/config.yaml），而非硬编码。

覆盖：
    1) ServiceConfig / DeviceInfo / DocFullTextClient 的默认值与 base 配置一致；
    2) 替换配置来源后默认值随之改变（证明是配置驱动，非硬编码）；
    3) 显式传参仍能覆盖默认（app 内 deps 的用法）。
"""

from __future__ import annotations

import academic_service.app.clients.doc_fulltext_client as client_mod
from academic_service.app.clients.doc_fulltext_client import (
    DeviceInfo,
    DocFullTextClient,
    ServiceConfig,
)
from academic_service.app.config import load_base_defaults


# =====================================================================
# 1. 默认值与 base 配置一致
# =====================================================================

def test_service_config_defaults_match_base_yaml():
    cfg = load_base_defaults()
    sc = ServiceConfig()
    assert sc.host == cfg["doc_service_host"]
    assert sc.port == cfg["doc_service_port"]
    assert sc.scheme == cfg["doc_service_scheme"]
    assert sc.url_path == cfg["doc_fulltext_url_path"]
    assert sc.timestamp_tolerance_ms == cfg["doc_auth_timestamp_tolerance_ms"]


def test_device_info_defaults_match_base_yaml():
    cfg = load_base_defaults()
    d = DeviceInfo()
    assert d.app_version == cfg["default_device_app_version"]
    assert d.device_id == cfg["default_device_id"]
    assert d.device_model == cfg["default_device_model"]
    assert d.device_type == cfg["default_device_type"]
    assert d.prd_pkg_name == cfg["default_device_prd_pkg_name"]


def test_client_runtime_defaults_match_base_yaml():
    cfg = load_base_defaults()
    client = DocFullTextClient(ServiceConfig())
    assert client.connect_timeout == cfg["doc_connect_timeout_sec"]
    assert client.read_timeout == cfg["doc_read_timeout_sec"]
    assert client.max_retries == cfg["doc_max_retries"]
    assert client.retry_backoff_sec == cfg["doc_retry_backoff_sec"]
    assert client.poll_max_times == cfg["doc_poll_max_times"]
    assert client.poll_interval_sec == cfg["doc_poll_interval_sec"]


# =====================================================================
# 2. 替换配置来源 → 默认值随之改变（证明配置驱动，非硬编码）
# =====================================================================

def _fake_cfg(**overrides):
    base = load_base_defaults()
    base.update(overrides)
    return base


def test_service_config_follows_config_source(monkeypatch):
    monkeypatch.setattr(
        client_mod, "load_base_defaults",
        lambda: _fake_cfg(doc_service_host="9.9.9.9", doc_service_port=1, doc_fulltext_url_path="/x/y"),
    )
    sc = ServiceConfig()
    assert sc.host == "9.9.9.9"
    assert sc.port == 1
    assert sc.url_path == "/x/y"


def test_device_info_follows_config_source(monkeypatch):
    monkeypatch.setattr(
        client_mod, "load_base_defaults",
        lambda: _fake_cfg(default_device_id="dev-x", default_device_app_version="9.9.9"),
    )
    d = DeviceInfo()
    assert d.device_id == "dev-x"
    assert d.app_version == "9.9.9"


def test_client_runtime_follows_config_source(monkeypatch):
    monkeypatch.setattr(
        client_mod, "load_base_defaults",
        lambda: _fake_cfg(doc_connect_timeout_sec=7, doc_poll_max_times=11),
    )
    client = DocFullTextClient(ServiceConfig())
    assert client.connect_timeout == 7
    assert client.poll_max_times == 11


# =====================================================================
# 3. 显式传参覆盖默认（app 内 deps 用法）
# =====================================================================

def test_explicit_args_override_config_defaults():
    client = DocFullTextClient(ServiceConfig(), connect_timeout=99, poll_max_times=5)
    assert client.connect_timeout == 99
    assert client.poll_max_times == 5
    # 未传的仍取配置默认
    cfg = load_base_defaults()
    assert client.read_timeout == cfg["doc_read_timeout_sec"]
