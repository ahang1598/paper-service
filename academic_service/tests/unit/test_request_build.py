# -*- coding: utf-8 -*-
"""请求构建测试：file_id/doc_hash 二选一、splitter 映射、非法页码。

覆盖 schema 层 (DocFullTextParams) 与 handler 层 (build_downstream_request)。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from academic_service.app.clients.doc_fulltext_client import (
    DeviceInfo,
    DocFullTextRequest,
    SplitterType,
)
from academic_service.app.schemas.document import DocFullTextParams
from academic_service.app.services.document.fulltext_handler import build_downstream_request


def _default_device():
    return DeviceInfo(device_id="pmq", app_version="1.0")


# =====================================================================
# DocFullTextParams 校验
# =====================================================================

def test_params_file_id_only():
    p = DocFullTextParams(file_id="a.docx")
    assert p.file_id == "a.docx"
    assert p.doc_hash is None


def test_params_doc_hash_only():
    p = DocFullTextParams(doc_hash="hash123")
    assert p.doc_hash == "hash123"
    assert p.file_id is None


def test_params_both_id_and_hash_allowed():
    """file_id 与 doc_hash 同时传是允许的（下游以 file_id 为准）。"""
    p = DocFullTextParams(file_id="a.docx", doc_hash="hash123")
    assert p.file_id == "a.docx"
    assert p.doc_hash == "hash123"


def test_params_neither_id_nor_hash_rejected():
    with pytest.raises(PydanticValidationError):
        DocFullTextParams()


# =====================================================================
# splitter 映射
# =====================================================================

def test_splitter_default_is_small_chunk():
    p = DocFullTextParams(file_id="a.docx")
    assert p.splitter == 1  # SMALL_CHUNK


@pytest.mark.parametrize("value,expected", [(0, 0), (1, 1)])
def test_splitter_valid_values(value, expected):
    p = DocFullTextParams(file_id="a.docx", splitter=value)
    assert p.splitter == expected


@pytest.mark.parametrize("invalid", [2, -1, 5, 99])
def test_splitter_invalid_values_rejected(invalid):
    with pytest.raises(PydanticValidationError):
        DocFullTextParams(file_id="a.docx", splitter=invalid)


def test_splitter_enum_mapping_in_build():
    """build_downstream_request 应把 int splitter 转成 SplitterType。"""
    p = DocFullTextParams(file_id="a.docx", splitter=0)
    req = build_downstream_request(p, request_id="rid", default_device=_default_device())
    assert req.splitter == SplitterType.CHAPTER
    assert int(req.splitter) == 0


# =====================================================================
# 非法页码
# =====================================================================

def test_pages_default_empty():
    p = DocFullTextParams(file_id="a.docx")
    assert p.pages == []


def test_pages_valid_non_negative():
    p = DocFullTextParams(file_id="a.docx", pages=[0, 1, 2])
    assert p.pages == [0, 1, 2]


def test_pages_negative_rejected():
    with pytest.raises(PydanticValidationError):
        DocFullTextParams(file_id="a.docx", pages=[-1])


def test_pages_non_integer_rejected():
    with pytest.raises(PydanticValidationError):
        DocFullTextParams(file_id="a.docx", pages=[1.5])  # type: ignore[list-item]


def test_pages_string_rejected():
    with pytest.raises(PydanticValidationError):
        DocFullTextParams(file_id="a.docx", pages=["x"])  # type: ignore[list-item]


# =====================================================================
# build_downstream_request：file_id/doc_hash 透传
# =====================================================================

def test_build_request_file_id_carried():
    p = DocFullTextParams(file_id="a.docx")
    req = build_downstream_request(p, request_id="rid", default_device=_default_device())
    payload = req.to_dict()
    assert payload["file_id"] == "a.docx"
    # 无 doc_hash 时不下发 doc_hash 字段
    assert "doc_hash" not in payload


def test_build_request_doc_hash_carried():
    p = DocFullTextParams(doc_hash="hash123")
    req = build_downstream_request(p, request_id="rid", default_device=_default_device())
    payload = req.to_dict()
    assert payload["doc_hash"] == "hash123"


def test_build_request_request_id_passed():
    p = DocFullTextParams(file_id="a.docx")
    req = build_downstream_request(p, request_id="my-rid", default_device=_default_device())
    assert req.request_id == "my-rid"
    assert req.to_dict()["request_id"] == "my-rid"


def test_build_request_device_override():
    """请求级 device 覆盖默认设备的指定字段，未覆盖字段保留默认。"""
    from academic_service.app.schemas.document import DeviceParams
    p = DocFullTextParams(
        file_id="a.docx",
        device=DeviceParams(device_id="custom-id"),
    )
    default = DeviceInfo(device_id="pmq", app_version="1.0", device_model="M")
    req = build_downstream_request(p, request_id="rid", default_device=default)
    assert req.device.device_id == "custom-id"      # 覆盖
    assert req.device.app_version == "1.0"          # 保留默认
    assert req.device.device_model == "M"           # 保留默认
