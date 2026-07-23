# -*- coding: utf-8 -*-
"""assemble_full_content 测试：排序 / 缺失 chunk_id / 重复 / 空。"""

from __future__ import annotations

from app.clients.doc_fulltext_client import assemble_full_content


def _chunk(chunk_id, content):
    return {"content": content, "metadata": {"chunk_id": chunk_id}}


def test_empty_data_returns_empty():
    assert assemble_full_content([]) == ""


def test_single_chunk():
    assert assemble_full_content([_chunk(0, "hello")]) == "hello"


def test_ordered_chunks():
    data = [_chunk(0, "A"), _chunk(1, "B"), _chunk(2, "C")]
    assert assemble_full_content(data) == "ABC"


def test_unordered_chunks_sorted_by_chunk_id():
    """乱序 chunk_id 应按升序拼接。"""
    data = [_chunk(2, "C"), _chunk(0, "A"), _chunk(1, "B")]
    assert assemble_full_content(data) == "ABC"


def test_missing_chunk_id_goes_last():
    """缺失 chunk_id 的元素排在末尾，且不抛异常。"""
    data = [
        _chunk(1, "B"),
        {"content": "MISSING_META"},            # 无 metadata
        {"content": "X", "metadata": {}},        # metadata 无 chunk_id
        _chunk(0, "A"),
    ]
    result = assemble_full_content(data)
    assert result == "AB" + "MISSING_META" + "X"


def test_duplicate_chunk_id_stable_order():
    """chunk_id 重复时保持原相对顺序（稳定排序）。"""
    data = [
        _chunk(0, "A1"),
        _chunk(1, "B"),
        _chunk(0, "A2"),
    ]
    result = assemble_full_content(data)
    assert result == "A1A2B"


def test_large_chunk_ids():
    data = [_chunk(49, "Z"), _chunk(0, "A")]
    assert assemble_full_content(data) == "AZ"


def test_none_content_treated_as_empty():
    """content 缺失时按空串处理。"""
    data = [{"metadata": {"chunk_id": 0}}, {"content": "B", "metadata": {"chunk_id": 1}}]
    assert assemble_full_content(data) == "B"
