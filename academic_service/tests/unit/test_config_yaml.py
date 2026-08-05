# -*- coding: utf-8 -*-
"""YAML 配置加载测试。

验证：
    1) 嵌套 YAML → Settings 字段的展平映射；
    2) base + 环境叠加的 deep-merge（叠加层覆盖 base）；
    3) 优先级：kwargs > 真实环境变量 > .env > YAML；
    4) bearer_tokens 列表/逗号串归一化；
    5) APP_ENV / APP_CONFIG_FILE 文件解析；
    6) 文件缺失不报错（容错）。

通过 _env_file=None 关闭 .env 干扰，用 APP_CONFIG_FILE 指向临时文件隔离测试。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from academic_service.app.config import (
    Settings,
    _CONFIGS_DIR,
    _YAML_PATHS,
    load_yaml_settings,
    resolve_yaml_files,
)


# =====================================================================
# 1. 展平映射
# =====================================================================

def test_yaml_paths_cover_all_settings_fields():
    """映射表应覆盖所有「可从 YAML 读取」的 Settings 字段（密钥/开关除外亦可）。"""
    # 关键字段必须在映射表中
    must_have = {
        "service_host", "service_port", "log_level",
        "auth_enabled", "api_bearer_tokens",
        "doc_service_host", "doc_service_port", "doc_service_scheme",
        "doc_service_auth_key",
        "default_device_app_version", "default_device_id",
        "doc_poll_max_times", "doc_poll_interval_sec", "doc_max_retries",
        "doc_connect_timeout_sec", "doc_read_timeout_sec", "doc_retry_backoff_sec",
        "doc_fulltext_url_path", "doc_auth_timestamp_tolerance_ms",
        "request_id_prefix",
        "debug_log_paper_processing",
    }
    assert must_have <= set(_YAML_PATHS)


def test_flatten_maps_nested_yaml_to_fields(tmp_path: Path):
    base = tmp_path / "base.yaml"
    base.write_text(
        """
service:
  host: "0.0.0.0"
  port: 12135
  log_level: INFO
auth:
  enabled: true
  bearer_tokens: []
doc_service:
  scheme: http
  host: "10.34.236.1"
  port: 9983
  auth_key: ""
default_device:
  app_version: "12.1.8.410"
  device_id: pmq
client:
  connect_timeout_sec: 5
  read_timeout_sec: 30
  retry_backoff_sec: 1.0
doc_fulltext:
  url_path: "/copilot_for_docs/doc_fulltext"
  timestamp_tolerance_ms: 600000
request:
  id_prefix: srv
""",
        encoding="utf-8",
    )

    flat = load_yaml_settings([base])

    assert flat["service_host"] == "0.0.0.0"
    assert flat["service_port"] == 12135
    assert flat["log_level"] == "INFO"
    assert flat["auth_enabled"] is True
    assert flat["doc_service_host"] == "10.34.236.1"
    assert flat["doc_service_port"] == 9983
    assert flat["default_device_app_version"] == "12.1.8.410"
    assert flat["doc_connect_timeout_sec"] == 5
    assert flat["doc_fulltext_url_path"] == "/copilot_for_docs/doc_fulltext"
    assert flat["doc_auth_timestamp_tolerance_ms"] == 600000
    assert flat["request_id_prefix"] == "srv"


def test_flatten_ignores_keys_not_in_map(tmp_path: Path):
    """YAML 中不在映射表里的键应被静默忽略。"""
    f = tmp_path / "c.yaml"
    f.write_text("service:\n  host: 1.2.3.4\nunknown_section:\n  foo: bar\n", encoding="utf-8")
    flat = load_yaml_settings([f])
    assert flat == {"service_host": "1.2.3.4"}


# =====================================================================
# 2. 叠加合并
# =====================================================================

def test_overlay_deep_merges_and_overrides_base(tmp_path: Path):
    base = tmp_path / "base.yaml"
    base.write_text(
        "service:\n  host: base-host\n  port: 1\nauth:\n  enabled: true\n",
        encoding="utf-8",
    )
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "service:\n  port: 2\nauth:\n  enabled: false\n",  # 只覆盖部分键
        encoding="utf-8",
    )

    flat = load_yaml_settings([base, overlay])

    # 覆盖的键取叠加值
    assert flat["service_port"] == 2
    assert flat["auth_enabled"] is False
    # 未覆盖的键保留 base 值（deep merge，不是整段替换）
    assert flat["service_host"] == "base-host"


# =====================================================================
# 3. 优先级：kwargs > env > .env > YAML
# =====================================================================

@pytest.fixture
def yaml_with_values(tmp_path: Path) -> Path:
    f = tmp_path / "values.yaml"
    f.write_text(
        "service:\n  port: 11111\nauth:\n  enabled: true\n"
        "client:\n  read_timeout_sec: 30\n",
        encoding="utf-8",
    )
    return f


def test_settings_loads_from_app_config_file(yaml_with_values: Path, monkeypatch):
    monkeypatch.setenv("APP_CONFIG_FILE", str(yaml_with_values))
    s = Settings(_env_file=None)  # 关闭 .env
    assert s.service_port == 11111
    assert s.auth_enabled is True
    assert s.doc_read_timeout_sec == 30


def test_real_env_overrides_yaml(yaml_with_values: Path, monkeypatch):
    monkeypatch.setenv("APP_CONFIG_FILE", str(yaml_with_values))
    monkeypatch.setenv("DOC_READ_TIMEOUT_SEC", "99")  # 真实 env > YAML
    s = Settings(_env_file=None)
    assert s.doc_read_timeout_sec == 99


def test_kwargs_override_env_and_yaml(yaml_with_values: Path, monkeypatch):
    monkeypatch.setenv("APP_CONFIG_FILE", str(yaml_with_values))
    monkeypatch.setenv("AUTH_ENABLED", "true")  # env 层
    # kwargs 层（最高）
    s = Settings(_env_file=None, auth_enabled=False)
    assert s.auth_enabled is False


# =====================================================================
# 4. bearer_tokens 归一化
# =====================================================================

def test_bearer_tokens_list_coerced_to_string(tmp_path: Path, monkeypatch):
    f = tmp_path / "t.yaml"
    f.write_text(
        "auth:\n  bearer_tokens:\n    - tok-a\n    - tok-b\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_CONFIG_FILE", str(f))
    s = Settings(_env_file=None)
    # 列表被归一化为逗号串
    assert s.api_bearer_tokens == "tok-a,tok-b"
    assert s.bearer_tokens == ["tok-a", "tok-b"]


def test_bearer_tokens_env_string_still_works(monkeypatch):
    monkeypatch.setenv("API_BEARER_TOKENS", "x, y ,,z")
    s = Settings(_env_file=None)
    assert s.api_bearer_tokens == "x,y,z"
    assert s.bearer_tokens == ["x", "y", "z"]


def test_paper_processing_debug_log_switch_can_be_enabled_by_env(monkeypatch):
    monkeypatch.setenv("DEBUG_LOG_PAPER_PROCESSING", "true")
    s = Settings(_env_file=None)
    assert s.debug_log_paper_processing is True


# =====================================================================
# 5. APP_ENV / APP_CONFIG_FILE 文件解析
# =====================================================================

def test_resolve_files_default_is_base_plus_prod(monkeypatch):
    monkeypatch.delenv("APP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    files = resolve_yaml_files()
    names = [p.name for p in files]
    # 默认 prod：base + prod 叠加
    assert "config.yaml" in names
    assert "config.prod.yaml" in names
    assert "config.dev.yaml" not in names


def test_resolve_files_dev_env(monkeypatch):
    monkeypatch.delenv("APP_CONFIG_FILE", raising=False)
    monkeypatch.setenv("APP_ENV", "dev")
    files = resolve_yaml_files()
    names = [p.name for p in files]
    assert "config.yaml" in names
    assert "config.dev.yaml" in names
    assert "config.prod.yaml" not in names


def test_resolve_files_app_config_file_takes_precedence(tmp_path: Path, monkeypatch):
    only = tmp_path / "only.yaml"
    only.write_text("service:\n  host: only-host\n", encoding="utf-8")
    monkeypatch.setenv("APP_CONFIG_FILE", str(only))
    monkeypatch.setenv("APP_ENV", "dev")  # 应被 APP_CONFIG_FILE 覆盖
    files = resolve_yaml_files()
    assert files == [only]


# =====================================================================
# 6. 容错：文件缺失不报错
# =====================================================================

def test_missing_file_is_ignored(tmp_path: Path):
    missing = tmp_path / "does-not-exist.yaml"
    assert load_yaml_settings([missing]) == {}


def test_configs_dir_exists_and_has_base():
    """提交的 configs 目录应存在并包含基础配置。"""
    assert _CONFIGS_DIR.is_dir()
    assert (_CONFIGS_DIR / "config.yaml").is_file()


def test_default_settings_auth_on_in_prod(monkeypatch):
    """生产默认（APP_ENV 未设/prod）下 auth_enabled 应为 True（生产安全）。"""
    monkeypatch.delenv("APP_CONFIG_FILE", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    s = Settings(_env_file=None)
    assert s.auth_enabled is True
    assert s.doc_fulltext_url_path == "/copilot_for_docs/doc_fulltext"
    assert s.doc_auth_timestamp_tolerance_ms == 600000
