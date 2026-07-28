# -*- coding: utf-8 -*-
"""docid 搜索客户端的纯函数测试：请求构建、HMAC 签名、结果拼接。

HTTP 网络层在 endpoint/集成测试里用替身覆盖；此处覆盖核心逻辑。
"""

from __future__ import annotations

import pytest

from academic_service.app.clients.docid_search_client import (
    assemble_results,
    build_query,
    build_search_body,
    sign_request,
)


# =====================================================================
# build_query：docid 列表 → "docid:a,b"
# =====================================================================

def test_build_query_joins_with_comma():
    assert build_query(["a", "b"]) == "docid:a,b"


def test_build_query_single():
    assert build_query(["x"]) == "docid:x"


def test_build_query_empty_list():
    assert build_query([]) == "docid:"


# =====================================================================
# build_search_body：logid 透传、query 由入参拼接、固定模板字段
# =====================================================================

def test_build_body_logid_and_query():
    body = build_search_body(["a", "b"], logid="rid-1")
    assert body["logid"] == "rid-1"
    assert body["query"] == "docid:a,b"


def test_build_body_fixed_template_fields():
    body = build_search_body(["a"], logid="rid")
    assert body["lang"] == "en"
    assert body["return_type"] == "json"
    assert body["region"]["sregion"] == "cn"
    assert body["user_info"]["user_id"] == "*"
    ei = body["extrainfo"]
    assert ei["skip_cache"] == "true"
    assert ei["r_chunk_type"] == "0"
    assert ei["gpt_mode"] == "2"
    assert ei["skip_sw"] == "true"
    assert ei["skip_df"] == "true"


# =====================================================================
# sign_request：与参考脚本一致的 HMAC-SHA256 hexdigest
# =====================================================================

def test_sign_request_known_vector():
    body_str = '{"logid": "rid1", "query": "docid:a,b"}'
    auth = sign_request(
        secret_key="test-key",
        timestamp="1700000000000",
        uri="/search",
        body_str=body_str,
    )
    assert auth == "a4e178afd5fe975e2ac16d8a0545de07657f05d4b09f077a4ec89e6cfeccf40f"


def test_sign_request_is_64_hex_and_changes_with_body():
    a = sign_request("k", "1", "/search", '{"x":1}')
    b = sign_request("k", "1", "/search", '{"x":2}')
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)
    assert a != b


# =====================================================================
# assemble_results：title + chunks → "[i]title:..|||content:.."，跳过损坏项
# =====================================================================

def _result(title, chunks_list):
    import json as _json
    return {
        "title": title,
        "extrainfo": {"meta_data": {"chunks": _json.dumps(chunks_list)}},
    }


def test_assemble_basic_two_results():
    results = [
        _result("T1", ["C1a", "C1b"]),
        _result("T2", ["C2"]),
    ]
    assert assemble_results(results) == (
        "[1]title:T1|||content:C1a\nC1b\n[2]title:T2|||content:C2"
    )


def test_assemble_skip_malformed_and_renumber():
    results = [
        _result("Good", ["c1"]),
        {"title": "Bad", "extrainfo": {"meta_data": {"chunks": "[xxx]"}}},  # 非法 JSON
        {"title": "NoChunks"},  # 缺 chunks
        _result("Empty", []),  # 空 chunks
        _result("Also", ["c2"]),
    ]
    # 仅 Good、Also 保留，顺序编号
    assert assemble_results(results) == "[1]title:Good|||content:c1\n[2]title:Also|||content:c2"


def test_assemble_missing_title_uses_empty():
    results = [{"extrainfo": {"meta_data": {"chunks": '["only"]'}}}]
    assert assemble_results(results) == "[1]title:|||content:only"


def test_assemble_empty_input():
    assert assemble_results([]) == ""
    assert assemble_results(None) == ""


def test_assemble_chunks_as_list_passthrough():
    # chunks 已是 list（非 JSON 串）也应支持
    results = [{"title": "T", "extrainfo": {"meta_data": {"chunks": ["a", "b"]}}}]
    assert assemble_results(results) == "[1]title:T|||content:a\nb"


# =====================================================================
# 容错：真实下游可能把 extrainfo / meta_data 以 JSON 字符串返回（曾导致 .get 崩溃）
# =====================================================================

def test_assemble_extrainfo_as_json_string():
    """extrainfo 整体是 JSON 字符串 → 解码后取 chunks。"""
    import json as _json
    extrainfo_str = _json.dumps({"meta_data": {"chunks": _json.dumps(["a", "b"])}})
    results = [{"title": "T", "extrainfo": extrainfo_str}]
    assert assemble_results(results) == "[1]title:T|||content:a\nb"


def test_assemble_meta_data_as_json_string():
    """meta_data 是 JSON 字符串 → 解码后取 chunks。"""
    import json as _json
    meta_str = _json.dumps({"chunks": _json.dumps(["x"])})
    results = [{"title": "T", "extrainfo": {"meta_data": meta_str}}]
    assert assemble_results(results) == "[1]title:T|||content:x"


def test_assemble_garbage_extrainfo_string_skipped_no_crash():
    """extrainfo 是非法 JSON 字符串 → 跳过该条，不抛异常。"""
    results = [
        {"title": "Bad", "extrainfo": "not-json"},
        {"title": "Good", "extrainfo": {"meta_data": {"chunks": '["y"]'}}},
    ]
    assert assemble_results(results) == "[1]title:Good|||content:y"

