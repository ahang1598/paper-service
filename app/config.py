# -*- coding: utf-8 -*-
"""应用配置：基于 pydantic-settings 从环境变量 / .env 加载。

配置项说明见 .env.example。所有可变配置集中于此，便于按环境覆盖与演进。
"""

from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置。

    通过环境变量或 .env 文件注入；字段名不区分大小写。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- 本服务监听 ----
    service_host: str = "0.0.0.0"
    service_port: int = 12135
    log_level: str = "INFO"

    # ---- 对外鉴权 ----
    # 是否开启 Bearer 鉴权（HTTP 与 WebSocket 共用）。
    # 生产环境必须为 True；测试/本地调试可设为 False 关闭鉴权。
    auth_enabled: bool = True
    # 允许的 Bearer token 集合（逗号分隔）。
    api_bearer_tokens: str = ""

    # ---- 下游文档全文服务 ----
    doc_service_scheme: str = "http"
    doc_service_host: str = "10.34.236.1"
    doc_service_port: int = 9983
    # HMAC 鉴权密钥（由服务方分配）。
    doc_service_auth_key: str = "xxxx"

    # ---- 默认设备信息（请求下游时使用，可在请求级覆盖）----
    default_device_app_version: str = "12.1.8.410"
    default_device_id: str = "pmq"
    default_device_model: str = "ALN-AL00"
    default_device_type: str = "phone"
    default_device_prd_pkg_name: str = "com.huawei.vassistant"

    # ---- 客户端运行参数（可调）----
    # pending 轮询参数（流式与一次性查询共用，测试时可调小）
    doc_poll_max_times: int = 30
    doc_poll_interval_sec: float = 2.0
    doc_max_retries: int = 3

    @field_validator("api_bearer_tokens")
    @classmethod
    def _strip_tokens(cls, v: str) -> str:
        """容忍空白与多余逗号。"""
        return ",".join(t.strip() for t in v.split(",") if t.strip())

    @property
    def bearer_tokens(self) -> List[str]:
        """解析后的 token 列表。"""
        return [t for t in self.api_bearer_tokens.split(",") if t]


@lru_cache
def get_settings() -> Settings:
    """获取全局单例 Settings。

    使用 lru_cache 缓存；测试若需覆盖配置，调用 get_settings.cache_clear() 后
    重新修改环境变量，或直接构造 Settings 实例注入。
    """
    return Settings()
