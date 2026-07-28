#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文档全文接口（doc_fulltext）客户端。

接口功能：
    - 提供文档全文信息；
    - 支持对文档内图片、表格进行 OCR 识别并输出成文本；
    - 支持指定切分方式：大块（CHAPTER）/ 小块（SMALL_CHUNK）。

鉴权方式：
    - HMAC-SHA256 + Base64。
    - 签名串：`{method}&{url_path}&deviceId={device_id}&timestamp={timestamp}`
    - 签名密钥由服务方分配，放在请求头 `token` 中；时间戳放在请求头 `timestamp` 中。

结果处理：
    - 响应 `data` 为多个 chunk，每个 chunk 含 `content` 与 `metadata.chunk_id`；
    - 按 `chunk_id` 从小到大排序后，将所有 `content` 拼接为完整文本输出。

设计说明：
    - 协议常量（HTTP 方法、请求头、字段名、响应状态等）集中定义在模块顶部；
    - 可配置项（host、密钥、设备信息、超时、轮询参数等）通过 dataclass 显式声明，
      默认值统一取自 configs/config.yaml（经 app.config.load_base_defaults 读取），
      运行期覆盖（密钥、环境差异）由 Settings 经 deps 注入；
    - 网络层与业务层分离，异常分类捕获，便于定位与重试。
    - 本模块为纯客户端，可被上层服务（FastAPI handler）通过依赖注入使用，
      也可独立调用（见模块末尾 main 示例）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Optional

import requests

from academic_service.app.config import load_base_defaults

# =====================================================================
# 常量定义区（与可变变量区分，集中维护，便于后续演进）
# =====================================================================

# ---- HTTP 方法常量 ----
HTTP_METHOD_POST = "POST"

# ---- 请求头常量 ----
HEADER_CONTENT_TYPE = "Content-Type"
HEADER_TIMESTAMP = "timestamp"
HEADER_TOKEN = "token"
CONTENT_TYPE_JSON = "application/json"

# ---- 业务字段名常量 ----
FIELD_REQUEST_ID = "request_id"
FIELD_FILE_ID = "file_id"
FIELD_DOC_HASH = "doc_hash"
FIELD_DEVICE = "device"
FIELD_SPLITTER = "splitter"
FIELD_PAGES = "pages"
FIELD_WITH_RECT = "with_rect"
FIELD_CODE = "code"
FIELD_DESCRIPTION = "description"
FIELD_DATA = "data"
FIELD_STATUS = "status"
FIELD_MESSAGE = "message"
FIELD_CONTENT = "content"
FIELD_METADATA = "metadata"
FIELD_CHUNK_ID = "chunk_id"
FIELD_TOTAL_CHUNKS = "total_chunks"

# 子字段（device 信息）
DEVICE_FIELD_APP_VERSION = "x-app-version"
DEVICE_FIELD_DEVICE_ID = "x-device-id"
DEVICE_FIELD_DEVICE_MODEL = "x-device-model"
DEVICE_FIELD_DEVICE_TYPE = "x-device-type"
DEVICE_FIELD_PRD_PKG_NAME = "x-prd-pkg-name"

# ---- 响应状态码常量 ----
RESP_CODE_SUCCESS = 0
RESP_CODE_FAIL = 1

# ---- 响应 status 文本常量 ----
STATUS_SUCCESS = "success"
STATUS_PENDING = "pending"
STATUS_FAIL = "fail"

# ---- 日志配置 ----
logger = logging.getLogger("doc_fulltext.client")


# =====================================================================
# 枚举 / 数据类定义区（可变配置项，与常量区分）
# =====================================================================

class SplitterType(IntEnum):
    """文档切分方式（对应 proto 中的 SplitterType 枚举）。"""
    CHAPTER = 0       # 大块：15000 长，无 overlap
    SMALL_CHUNK = 1   # 小块：500 长，20 overlap


@dataclass
class ServiceConfig:
    """服务端连接配置。

    默认值取自 ``configs/config.yaml``（经 ``load_base_defaults`` 读取）；
    app 内由 Settings（含环境变量/.env 覆盖）经 deps 构造时显式传入。
    """
    host: str = field(default_factory=lambda: load_base_defaults()["doc_service_host"])
    port: int = field(default_factory=lambda: load_base_defaults()["doc_service_port"])
    scheme: str = field(default_factory=lambda: load_base_defaults()["doc_service_scheme"])
    # HMAC 鉴权密钥（明文或 Base64），由服务方分配；默认取配置（生产由环境变量注入）
    auth_key: str = field(default_factory=lambda: load_base_defaults()["doc_service_auth_key"])
    # 下游接口路径（参与 HMAC 签名串）
    url_path: str = field(default_factory=lambda: load_base_defaults()["doc_fulltext_url_path"])
    # 服务端允许的时间戳误差（毫秒）；客户端每次重新签名，不主动校验
    timestamp_tolerance_ms: int = field(
        default_factory=lambda: load_base_defaults()["doc_auth_timestamp_tolerance_ms"]
    )

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


@dataclass
class DeviceInfo:
    """设备信息（对应 proto KcDeviceInfo，使用业务侧实际字段名）。

    默认值取自 ``configs/config.yaml`` 的 default_device 段。
    """
    app_version: str = field(default_factory=lambda: load_base_defaults()["default_device_app_version"])
    device_id: str = field(default_factory=lambda: load_base_defaults()["default_device_id"])
    device_model: str = field(default_factory=lambda: load_base_defaults()["default_device_model"])
    device_type: str = field(default_factory=lambda: load_base_defaults()["default_device_type"])
    prd_pkg_name: str = field(default_factory=lambda: load_base_defaults()["default_device_prd_pkg_name"])

    def to_dict(self) -> dict[str, str]:
        return {
            DEVICE_FIELD_APP_VERSION: self.app_version,
            DEVICE_FIELD_DEVICE_ID: self.device_id,
            DEVICE_FIELD_DEVICE_MODEL: self.device_model,
            DEVICE_FIELD_DEVICE_TYPE: self.device_type,
            DEVICE_FIELD_PRD_PKG_NAME: self.prd_pkg_name,
        }


@dataclass
class DocFullTextRequest:
    """doc_fulltext 请求参数（对应 proto DocFullTextRequest）。

    file_id 与 doc_hash 二选一；同时传以 file_id 为准（服务侧行为）。
    """
    file_id: str = ""
    device: DeviceInfo = field(default_factory=DeviceInfo)
    request_id: str = field(
        default_factory=lambda: f"req_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    )
    doc_hash: Optional[str] = None
    splitter: SplitterType = SplitterType.SMALL_CHUNK
    pages: list[int] = field(default_factory=list)  # 空列表表示获取所有页
    with_rect: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            FIELD_REQUEST_ID: self.request_id,
            FIELD_FILE_ID: self.file_id,
            FIELD_DEVICE: self.device.to_dict(),
            FIELD_SPLITTER: int(self.splitter),
            FIELD_PAGES: list(self.pages),
            FIELD_WITH_RECT: self.with_rect,
        }
        if self.doc_hash:
            payload[FIELD_DOC_HASH] = self.doc_hash
        return payload


# =====================================================================
# 自定义异常区
# =====================================================================

class DocFullTextError(Exception):
    """接口异常基类。"""


class AuthError(DocFullTextError):
    """鉴权相关异常。"""


class RequestError(DocFullTextError):
    """网络 / HTTP 层异常。"""


class ResponseParseError(DocFullTextError):
    """响应解析异常（JSON 非法、结构不符合预期等）。"""


class BusinessError(DocFullTextError):
    """业务异常（code != 0 或 status 为 fail 等）。"""


class DocPendingTimeout(DocFullTextError):
    """文档一直处于 pending，轮询超时。"""


# =====================================================================
# 工具函数区
# =====================================================================

def _now_timestamp_ms() -> int:
    """当前时间戳（毫秒）。"""
    return int(time.time() * 1000)


def generate_auth_token(
    method: str,
    url_path: str,
    device_id: str,
    timestamp_ms: int,
    auth_key: str,
) -> str:
    """
    生成鉴权 token。

    签名串格式与服务端 CheckSignKgs 一致：
        `{method}&{url_path}&deviceId={device_id}&timestamp={timestamp_ms}`
    使用 HMAC-SHA256 计算后做 Base64 编码。
    """
    if not auth_key:
        raise AuthError("auth_key 未配置，无法生成 token")

    sign_str = f"{method}&{url_path}&deviceId={device_id}&timestamp={timestamp_ms}"
    digest = hmac.new(
        key=auth_key.encode("utf-8"),
        msg=sign_str.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def assemble_full_content(data: list[dict[str, Any]]) -> str:
    """
    将响应 data 中多个 content 按 metadata.chunk_id 从小到大排序后拼接。

    - data 为空时返回空串；
    - 缺失 chunk_id 的元素按 +∞ 排在最后，保证结果稳定；
    - chunk_id 相同则保持原相对顺序（稳定排序）。
    """
    if not data:
        return ""

    def _chunk_id_sort_key(item: dict[str, Any]) -> tuple[int, float]:
        metadata = item.get(FIELD_METADATA) or {}
        chunk_id = metadata.get(FIELD_CHUNK_ID)
        # 缺失 chunk_id 时排在最后
        return (0, chunk_id) if isinstance(chunk_id, (int, float)) else (1, float("inf"))

    sorted_data = sorted(data, key=_chunk_id_sort_key)
    return "".join(item.get(FIELD_CONTENT, "") for item in sorted_data)


# =====================================================================
# 客户端实现区
# =====================================================================

class DocFullTextClient:
    """doc_fulltext 接口客户端。"""

    def __init__(
        self,
        service_config: ServiceConfig,
        connect_timeout: Optional[float] = None,
        read_timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        retry_backoff_sec: Optional[float] = None,
        poll_max_times: Optional[int] = None,
        poll_interval_sec: Optional[float] = None,
    ) -> None:
        """构造客户端。

        所有运行参数缺省（None）时取自 ``configs/config.yaml`` 的 client 段；
        app 内由 deps 按 Settings（含环境覆盖）显式传入。
        """
        defaults = load_base_defaults()
        self.config = service_config
        self.connect_timeout = connect_timeout if connect_timeout is not None else defaults["doc_connect_timeout_sec"]
        self.read_timeout = read_timeout if read_timeout is not None else defaults["doc_read_timeout_sec"]
        self.max_retries = max_retries if max_retries is not None else defaults["doc_max_retries"]
        self.retry_backoff_sec = retry_backoff_sec if retry_backoff_sec is not None else defaults["doc_retry_backoff_sec"]
        self.poll_max_times = poll_max_times if poll_max_times is not None else defaults["doc_poll_max_times"]
        self.poll_interval_sec = poll_interval_sec if poll_interval_sec is not None else defaults["doc_poll_interval_sec"]
        self._session = requests.Session()

    # ---------- 鉴权 ----------
    def _build_headers(self, device_id: str) -> dict[str, str]:
        """每次请求重新生成 timestamp/token（保证不超时容差且便于重签名）。"""
        timestamp_ms = _now_timestamp_ms()
        token = generate_auth_token(
            method=HTTP_METHOD_POST,
            url_path=self.config.url_path,
            device_id=device_id,
            timestamp_ms=timestamp_ms,
            auth_key=self.config.auth_key,
        )
        return {
            HEADER_CONTENT_TYPE: CONTENT_TYPE_JSON,
            HEADER_TIMESTAMP: str(timestamp_ms),
            HEADER_TOKEN: token,
        }

    # ---------- 单次请求（含网络层重试） ----------
    def _post_once(self, request: DocFullTextRequest) -> dict[str, Any]:
        url = f"{self.config.base_url}{self.config.url_path}"
        headers = self._build_headers(device_id=request.device.device_id)
        payload = request.to_dict()

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.debug("POST %s attempt=%d", url, attempt)
                resp = self._session.post(
                    url=url,
                    headers=headers,
                    json=payload,
                    timeout=(self.connect_timeout, self.read_timeout),
                )
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning("网络异常 attempt=%d/%d: %s", attempt, self.max_retries, exc)
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff_sec * attempt)
                continue

            # HTTP 状态码层校验
            if resp.status_code != 200:
                raise RequestError(
                    f"HTTP {resp.status_code}: {resp.text[:200]}"
                )

            # 解析 JSON
            try:
                return resp.json()
            except ValueError as exc:
                raise ResponseParseError(f"响应非合法 JSON: {exc}; body={resp.text[:200]}") from exc

        # 重试耗尽
        raise RequestError(f"请求失败，已重试 {self.max_retries} 次: {last_exc}")

    # ---------- 公开别名（供上层 handler / 流式逻辑复用，保持私有实现不变） ----------
    def post_once(self, request: DocFullTextRequest) -> dict[str, Any]:
        """单次请求（含网络层重试）的公开入口。"""
        return self._post_once(request)

    # ---------- 业务层响应校验 ----------
    @staticmethod
    def _check_response(body: dict[str, Any]) -> None:
        """校验响应：code 非法、参数错误等直接抛业务异常。"""
        code = body.get(FIELD_CODE)
        if code != RESP_CODE_SUCCESS:
            description = body.get(FIELD_DESCRIPTION, "unknown")
            raise BusinessError(f"接口返回失败 code={code}, description={description}")

    @staticmethod
    def check_response(body: dict[str, Any]) -> None:
        """响应校验公开入口（供上层复用）。"""
        DocFullTextClient._check_response(body)

    # ---------- 对外主方法（一次性返回完整拼接结果） ----------
    def fetch_full_text(self, request: DocFullTextRequest) -> str:
        """
        获取文档全文完整内容。

        - 自动处理 pending（解析中）状态：按 poll_interval_sec 轮询，直到 success/fail 或超时；
        - 解析失败 / 参数错误 / 网络异常均抛出对应异常；
        - 成功后按 chunk_id 排序拼接 content 返回。

        Returns:
            完整拼接后的 content 文本。
        """
        data, _ = self.fetch_full_text_with_status(request)
        # fetch_full_text_with_status 在 success 时已保证 data 非空
        return assemble_full_content(data)

    # ---------- 对外主方法（返回原始 data，供上层 handler 获取完整结构） ----------
    def fetch_full_text_with_status(
        self, request: DocFullTextRequest
    ) -> tuple[list[dict[str, Any]], str]:
        """
        获取文档全文，返回 (data, status)。

        - success：返回非空 data 与 "success"；
        - fail / 参数错误 / 网络异常：抛出对应异常；
        - pending 轮询超时：抛出 DocPendingTimeout。

        供上层流式 handler 复用同一套轮询逻辑，逐次产出 progress 事件。
        """
        for poll_index in range(1, self.poll_max_times + 1):
            body = self._post_once(request)
            self._check_response(body)

            status = body.get(FIELD_STATUS)
            logger.info(
                "轮询第 %d/%d 次，status=%s, request_id=%s",
                poll_index, self.poll_max_times, status, body.get(FIELD_REQUEST_ID),
            )

            if status == STATUS_SUCCESS:
                data = body.get(FIELD_DATA) or []
                if not data:
                    # status=success 但 data 为空，视为异常
                    raise BusinessError(
                        f"status=success 但 data 为空: {body.get(FIELD_DESCRIPTION, '')}"
                    )
                return data, STATUS_SUCCESS

            if status == STATUS_FAIL:
                raise BusinessError(f"文档解析失败: {body.get(FIELD_DESCRIPTION, 'fail')}")

            if status == STATUS_PENDING:
                if poll_index < self.poll_max_times:
                    time.sleep(self.poll_interval_sec)
                continue

            # 未知 status
            raise BusinessError(f"未知的响应 status={status}, body={body}")

        raise DocPendingTimeout(
            f"文档解析持续 pending，已轮询 {self.poll_max_times} 次仍无结果"
        )


# =====================================================================
# 独立运行入口示例
# =====================================================================

def _main() -> None:
    """独立调用示例：构造请求并输出完整 content。

    连接 / 设备 / 运行参数的默认值统一取自 configs/config.yaml；
    密钥（auth_key）等运行期覆盖用环境变量 + get_settings() 读取。
    """

    # 1) 服务配置：结构性默认来自 config.yaml；auth_key 取运行期配置（环境变量/.env）
    from academic_service.app.config import get_settings

    service_config = ServiceConfig(auth_key=get_settings().doc_service_auth_key)

    # 2) 设备信息（默认来自 config.yaml）
    device = DeviceInfo()

    # 3) 构造请求参数
    request = DocFullTextRequest(
        file_id="1db8f80c0b854613aa68d2c977891353.docx",
        device=device,
        splitter=SplitterType.SMALL_CHUNK,
        pages=[],            # 获取所有页
        with_rect=False,
    )

    client = DocFullTextClient(service_config)

    # 4) 调用并处理异常
    try:
        full_content = client.fetch_full_text(request)
        print("====== 完整 content ======")
        print(full_content)
        print("====== 长度: %d ======" % len(full_content))
    except AuthError as e:
        logger.error("鉴权失败: %s", e)
    except RequestError as e:
        logger.error("请求失败: %s", e)
    except ResponseParseError as e:
        logger.error("响应解析失败: %s", e)
    except BusinessError as e:
        logger.error("业务异常: %s", e)
    except DocPendingTimeout as e:
        logger.error("解析超时: %s", e)
    except DocFullTextError as e:
        logger.error("其他接口异常: %s", e)


if __name__ == "__main__":
    _main()
