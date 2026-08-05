# -*- coding: utf-8 -*-
"""应用配置：YAML（结构化默认） + 环境变量/.env（密钥与环境覆盖）。

配置分层（优先级由低 → 高）：
    类默认 < YAML base < YAML 环境叠加 < .env < 真实环境变量 < 显式 kwargs

设计原则：
    - **结构化、非密钥**的可变配置集中在 ``configs/*.yaml``（提交到版本库）；
    - **密钥**（下游 HMAC ``auth_key``、对外 ``bearer_tokens``）**绝不进 YAML**，
      仅由环境变量 / ``.env``（已 gitignore）注入；
    - 生产安全：base 配置 ``auth.enabled: true``；通过 ``APP_ENV=dev`` 叠加关闭鉴权便于本地调试。

文件选择（``resolve_yaml_files``）：
    - ``APP_CONFIG_FILE``：显式指定一个或多个（逗号分隔）YAML 文件，**仅**加载它们；
    - 否则按 ``APP_ENV``（默认 ``prod``）：加载 ``config.yaml`` + ``config.<env>.yaml``。

YAML 采用嵌套结构（可读性），由 ``_YAML_PATHS`` 映射表展平为 Settings 字段名。
"""

from __future__ import annotations

import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Tuple

from pydantic import field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# =====================================================================
# 路径与 YAML → 字段映射
# =====================================================================

# 配置目录：相对本文件（app/config.py）的上级目录（项目根 academic_service/）下的 configs/
_CONFIGS_DIR: Path = Path(__file__).resolve().parent.parent / "configs"

# Settings 字段名 → YAML 嵌套路径。
# 增删 Settings 字段时同步维护此表。
_YAML_PATHS: Dict[str, Tuple[str, ...]] = {
    # 本服务
    "service_host": ("service", "host"),
    "service_port": ("service", "port"),
    "log_level": ("service", "log_level"),
    # 对外鉴权
    "auth_enabled": ("auth", "enabled"),
    "api_bearer_tokens": ("auth", "bearer_tokens"),
    # 下游文档全文服务
    "doc_service_scheme": ("doc_service", "scheme"),
    "doc_service_host": ("doc_service", "host"),
    "doc_service_port": ("doc_service", "port"),
    "doc_service_auth_key": ("doc_service", "auth_key"),
    # 默认设备信息
    "default_device_app_version": ("default_device", "app_version"),
    "default_device_id": ("default_device", "device_id"),
    "default_device_model": ("default_device", "device_model"),
    "default_device_type": ("default_device", "device_type"),
    "default_device_prd_pkg_name": ("default_device", "prd_pkg_name"),
    # 下游客户端运行参数
    "doc_connect_timeout_sec": ("client", "connect_timeout_sec"),
    "doc_read_timeout_sec": ("client", "read_timeout_sec"),
    "doc_max_retries": ("client", "max_retries"),
    "doc_retry_backoff_sec": ("client", "retry_backoff_sec"),
    "doc_poll_max_times": ("client", "poll_max_times"),
    "doc_poll_interval_sec": ("client", "poll_interval_sec"),
    # 下游 doc_fulltext 接口
    "doc_fulltext_url_path": ("doc_fulltext", "url_path"),
    "doc_auth_timestamp_tolerance_ms": ("doc_fulltext", "timestamp_tolerance_ms"),
    # 下游 docid 搜索服务
    "docid_search_url": ("docid_search", "url"),
    "docid_search_auth_key": ("docid_search", "auth_key"),
    # 论文解析 / chunk
    "paper_chunk_target_tokens": ("paper_processing", "chunk_target_tokens"),
    "paper_chunk_max_tokens": ("paper_processing", "chunk_max_tokens"),
    "paper_chunk_overlap_tokens": ("paper_processing", "chunk_overlap_tokens"),
    "paper_chunk_schema_version": ("paper_processing", "chunk_schema_version"),
    "paper_tokenizer_path": ("paper_processing", "tokenizer_path"),
    # reranker 公共配置
    "reranker_provider": ("reranker", "provider"),
    "reranker_top_k": ("reranker", "top_k"),
    "reranker_top_k_min": ("reranker", "top_k_min"),
    "reranker_top_k_ratio": ("reranker", "top_k_ratio"),
    "reranker_min_score": ("reranker", "min_score"),
    "reranker_neighbor_window": ("reranker", "neighbor_window"),
    "reranker_batch_size": ("reranker", "batch_size"),
    "reranker_max_concurrency": ("reranker", "max_concurrency"),
    "reranker_max_retries": ("reranker", "max_retries"),
    "reranker_retry_backoff_sec": ("reranker", "retry_backoff_sec"),
    # SiliconFlow reranker
    "siliconflow_rerank_url": ("reranker", "siliconflow", "url"),
    "siliconflow_rerank_model": ("reranker", "siliconflow", "model"),
    "siliconflow_api_key": ("reranker", "siliconflow", "api_key"),
    "siliconflow_timeout_sec": ("reranker", "siliconflow", "timeout_sec"),
    # 内网 GTE reranker
    "internal_rerank_url": ("reranker", "internal", "url"),
    "internal_rerank_app_id": ("reranker", "internal", "app_id"),
    "internal_rerank_sign_key": ("reranker", "internal", "sign_key"),
    "internal_rerank_bid": ("reranker", "internal", "bid"),
    "internal_rerank_flow_id": ("reranker", "internal", "flow_id"),
    "internal_rerank_uuid": ("reranker", "internal", "uuid"),
    "internal_rerank_timeout_sec": ("reranker", "internal", "timeout_sec"),
    # request_id 前缀
    "request_id_prefix": ("request", "id_prefix"),
    # 调试日志开关
    "debug_log_request": ("debug", "log_request"),
    "debug_log_downstream_request": ("debug", "log_downstream_request"),
    "debug_log_downstream_response": ("debug", "log_downstream_response"),
    "debug_log_paper_processing": ("debug", "log_paper_processing"),
}


# =====================================================================
# YAML 加载工具（纯函数，便于单测）
# =====================================================================

def _dig(data: Any, path: Tuple[str, ...]) -> Any:
    """按嵌套路径取值；任一层非 dict 或缺失返回 None。"""
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并：overlay 覆盖 base 同名键；dict 类型继续深合并，其余直接替换。

    返回新对象，不修改入参（不可变风格）。list 等非 dict 值整体替换（不拼接）。
    """
    result: Dict[str, Any] = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_yaml_settings(yaml_files: List[Path]) -> Dict[str, Any]:
    """加载并合并 YAML 文件，按 ``_YAML_PATHS`` 展平为 {字段名: 值}。

    - 文件按顺序加载，后者 deep-merge 覆盖前者（base → 环境叠加）；
    - 文件不存在 / 非法 / 非对象（非 dict）静默跳过（容错）；
    - 仅返回映射表内字段；YAML 中其它键被忽略。
    """
    merged: Dict[str, Any] = {}
    for raw_path in yaml_files:
        path = Path(raw_path)
        if not path.is_file():
            continue
        try:
            import yaml  # 延迟导入，避免无 yaml 时的硬依赖（仅在加载 YAML 时需要）
        except ImportError as exc:  # pragma: no cover - pyyaml 已声明为依赖
            raise RuntimeError("加载 YAML 配置需要 pyyaml，请安装 pyyaml>=6.0") from exc
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        merged = _deep_merge(merged, data)

    flat: Dict[str, Any] = {}
    for field_name, path in _YAML_PATHS.items():
        value = _dig(merged, path)
        if value is not None:
            flat[field_name] = value
    return flat


@lru_cache
def load_base_defaults() -> Dict[str, Any]:
    """读取 ``configs/config.yaml`` 的基础默认值（不含环境叠加 / env / .env）。

    用作独立客户端（ServiceConfig / DeviceInfo / DocFullTextClient）的默认值来源，
    保证客户端默认值稳定、已提交、不随运行期环境变量抖动。
    运行期的环境覆盖（密钥、地址等）由 ``Settings`` 经 ``deps`` 注入，不经过此处。
    """
    return load_yaml_settings([_CONFIGS_DIR / "config.yaml"])


def resolve_yaml_files() -> List[Path]:
    """根据 ``APP_CONFIG_FILE`` / ``APP_ENV`` 解析要加载的 YAML 文件列表。

    - ``APP_CONFIG_FILE``（逗号分隔多文件）：仅加载这些文件，忽略 APP_ENV；
    - 否则：``config.yaml``（base）+ ``config.<APP_ENV>.yaml``（叠加）；APP_ENV 缺省 prod。
    """
    explicit = os.environ.get("APP_CONFIG_FILE", "").strip()
    if explicit:
        return [Path(p.strip()) for p in explicit.split(",") if p.strip()]

    env = (os.environ.get("APP_ENV", "prod") or "prod").strip().lower()
    files: List[Path] = [_CONFIGS_DIR / "config.yaml"]
    overlay = {"dev": "config.dev.yaml", "prod": "config.prod.yaml"}.get(env)
    if overlay:
        files.append(_CONFIGS_DIR / overlay)
    return files


# =====================================================================
# YAML 自定义配置源（pydantic-settings）
# =====================================================================

class YamlConfigSettingsSource(PydanticBaseSettingsSource):
    """把 YAML（base + 叠加）作为低优先级配置源注入 pydantic-settings。"""

    def __init__(self, settings_cls: type[BaseSettings], yaml_files: List[Path] | None = None) -> None:
        super().__init__(settings_cls)
        self._files = yaml_files if yaml_files is not None else resolve_yaml_files()
        self._data: Dict[str, Any] = load_yaml_settings(self._files)

    def get_field_value(self, field, field_name: str) -> Tuple[Any, str, bool]:
        value = self._data.get(field_name)
        return value, field_name, value is not None

    def __call__(self) -> Dict[str, Any]:
        return dict(self._data)


# =====================================================================
# Settings
# =====================================================================

class Settings(BaseSettings):
    """全局配置。

    优先级（高 → 低）：显式 kwargs > 真实环境变量 > .env > YAML 叠加 > YAML base > 类默认。
    字段名不区分大小写（环境变量大写即可）。
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
    # 生产环境必须为 True；本地调试可通过 APP_ENV=dev 叠加 config.dev.yaml 关闭。
    auth_enabled: bool = True
    # 允许的 Bearer token 集合（逗号分隔）。密钥：从环境变量注入，切勿写进 YAML。
    api_bearer_tokens: str = ""

    # ---- 下游文档全文服务 ----
    doc_service_scheme: str = "http"
    doc_service_host: str = "10.34.236.1"
    doc_service_port: int = 9983
    # HMAC 鉴权密钥（由服务方分配）。密钥：从环境变量注入，切勿写进 YAML。
    doc_service_auth_key: str = ""

    # ---- 默认设备信息（请求下游时使用，可在请求级覆盖）----
    default_device_app_version: str = "12.1.8.410"
    default_device_id: str = "pmq"
    default_device_model: str = "ALN-AL00"
    default_device_type: str = "phone"
    default_device_prd_pkg_name: str = "com.huawei.vassistant"

    # ---- 客户端运行参数（可调）----
    doc_connect_timeout_sec: float = 5.0
    doc_read_timeout_sec: float = 30.0
    doc_max_retries: int = 3
    doc_retry_backoff_sec: float = 1.0
    # pending 轮询参数（流式与一次性查询共用）
    doc_poll_max_times: int = 30
    doc_poll_interval_sec: float = 2.0

    # ---- 下游 doc_fulltext 接口 ----
    doc_fulltext_url_path: str = "/copilot_for_docs/doc_fulltext"
    # 服务端允许的时间戳误差（毫秒），来源：limitMilliSecond = 600000
    doc_auth_timestamp_tolerance_ms: int = 600_000

    # ---- 下游 docid 搜索服务（/search）----
    docid_search_url: str = "http://10.37.0.140:6107/search"
    # HMAC 鉴权密钥（由服务方分配）。密钥：从环境变量注入，切勿写进 YAML。
    docid_search_auth_key: str = ""

    # ---- 论文规范化 / chunk ----
    paper_chunk_target_tokens: int = 400
    paper_chunk_max_tokens: int = 450
    paper_chunk_overlap_tokens: int = 60
    paper_chunk_schema_version: str = "v1"
    # 可选的 HuggingFace tokenizers tokenizer.json；为空时使用确定性多语言兜底计数器。
    paper_tokenizer_path: str = ""

    # ---- reranker ----
    reranker_provider: str = "internal"
    reranker_top_k: int = 8
    # 每篇实际 Top-K = min(chunk_count, top_k, max(top_k_min, ceil(chunk_count * top_k_ratio)))。
    reranker_top_k_min: int = 5
    reranker_top_k_ratio: float = 0.1
    # 低于门槛的 reranker/BM25 结果不作为 seed。
    reranker_min_score: float = 0.2
    reranker_neighbor_window: int = 1
    reranker_batch_size: int = 32
    reranker_max_concurrency: int = 3
    reranker_max_retries: int = 3
    reranker_retry_backoff_sec: float = 1.0

    # SiliconFlow（API key 仅从环境变量/.env 注入）
    siliconflow_rerank_url: str = "https://api.siliconflow.cn/v1/rerank"
    siliconflow_rerank_model: str = "BAAI/bge-reranker-v2-m3"
    siliconflow_api_key: str = ""
    siliconflow_timeout_sec: float = 60.0

    # 内网 GTE（sign key 仅从环境变量/.env 注入）
    internal_rerank_url: str = "http://10.33.111.223:8080/service"
    internal_rerank_app_id: str = "lf_test"
    internal_rerank_sign_key: str = ""
    internal_rerank_bid: str = "gte_multilingual_reranker_base_torch"
    internal_rerank_flow_id: str = "gte_multilingual_reranker_base_torch"
    internal_rerank_uuid: str = "12344525"
    internal_rerank_timeout_sec: float = 300.0

    # ---- 其它 ----
    # 服务端自动生成 request_id 时的前缀
    request_id_prefix: str = "srv"

    # ---- 调试日志开关（默认关；按需在 yaml/env 打开）----
    # 打印进入服务的请求入参（HTTP/WS 请求体）
    debug_log_request: bool = False
    # 打印访问下游（全文获取）时的入参（url + body）
    debug_log_downstream_request: bool = False
    # 打印下游出参（响应体）—— 单独开关，响应可能很大
    debug_log_downstream_response: bool = False
    # 打印论文全文解析、章节/chunk、reranker 入出参与合并结果；可能包含完整论文内容
    debug_log_paper_processing: bool = False

    # ----- 值归一化 -----
    @field_validator("api_bearer_tokens", mode="before")
    @classmethod
    def _coerce_tokens(cls, v: Any) -> Any:
        """YAML 列表 / 环境变量逗号串统一为逗号分隔字符串。"""
        if isinstance(v, (list, tuple, set)):
            return ",".join(str(t).strip() for t in v if str(t).strip())
        return v

    @field_validator("api_bearer_tokens")
    @classmethod
    def _strip_tokens(cls, v: str) -> str:
        """容忍空白与多余逗号。"""
        return ",".join(t.strip() for t in v.split(",") if t.strip())

    @field_validator(
        "paper_chunk_target_tokens",
        "paper_chunk_max_tokens",
        "reranker_top_k",
        "reranker_top_k_min",
        "reranker_batch_size",
        "reranker_max_concurrency",
        "reranker_max_retries",
    )
    @classmethod
    def _positive_processing_ints(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("论文处理与 reranker 的数量配置必须大于 0")
        return v

    @field_validator("paper_chunk_overlap_tokens", "reranker_neighbor_window")
    @classmethod
    def _non_negative_processing_ints(cls, v: int) -> int:
        if v < 0:
            raise ValueError("overlap 与 neighbor_window 不能为负数")
        return v

    @field_validator("reranker_provider")
    @classmethod
    def _check_reranker_provider(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in {"internal", "siliconflow"}:
            raise ValueError("reranker_provider 必须为 internal 或 siliconflow")
        return normalized

    @field_validator("reranker_top_k_ratio")
    @classmethod
    def _check_top_k_ratio(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0 or v > 1:
            raise ValueError("reranker_top_k_ratio 必须在 (0, 1] 范围内")
        return v

    @field_validator("reranker_min_score")
    @classmethod
    def _check_min_score(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("reranker_min_score 必须为有限数值")
        return v

    @model_validator(mode="after")
    def _check_chunk_limits(self) -> "Settings":
        if self.paper_chunk_max_tokens < self.paper_chunk_target_tokens:
            raise ValueError("paper_chunk_max_tokens 不能小于 target_tokens")
        if self.paper_chunk_overlap_tokens >= self.paper_chunk_target_tokens:
            raise ValueError("paper_chunk_overlap_tokens 必须小于 target_tokens")
        if self.reranker_top_k_min > self.reranker_top_k:
            raise ValueError("reranker_top_k_min 不能大于 reranker_top_k")
        return self

    @property
    def bearer_tokens(self) -> List[str]:
        """解析后的 token 列表。"""
        return [t for t in self.api_bearer_tokens.split(",") if t]

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        """YAML 作为最低优先级（在 dotenv 之后），密钥与环境覆盖始终高于 YAML。"""
        yaml_source = YamlConfigSettingsSource(settings_cls)
        # 顺序即优先级（前者更高）
        return (init_settings, env_settings, dotenv_settings, yaml_source, file_secret_settings)


@lru_cache
def get_settings() -> Settings:
    """获取全局单例 Settings。

    使用 lru_cache 缓存；测试若需覆盖配置，调用 get_settings.cache_clear() 后
    重新修改环境变量，或直接构造 Settings 实例注入。
    """
    return Settings()
