# -*- coding: utf-8 -*-
"""HTTP /api/v1/navigator/query 的 type 路由测试。

覆盖：
    - type=docid + queries → 走 docid_search，返回拼接 results；
    - type=docid + 单个 query → docids=[query]；
    - 默认（不传 type）→ fileid（doc_fulltext），不触碰 docid client；
    - fileid + queries → 400；
    - docid 既无 query 也无 queries → 422；
    - 非法 type → 422。
"""

from __future__ import annotations


def _fileid_success_body():
    return {
        "code": 0, "request_id": "r", "status": "success", "doc_hash": "h", "message": "ok",
        "data": [{"content": "FULLTEXT", "metadata": {"chunk_id": 0}}],
    }


# =====================================================================
# docid 路由
# =====================================================================

def test_docid_with_queries_routes_and_assembles(client, fake_docid_client):
    fake_docid_client.set_script([
        {"title": "T1", "extrainfo": {"meta_data": {"chunks": '["c1","c2"]'}}},
    ])
    resp = client.post("/api/v1/navigator/query", json={"queries": ["a", "b"], "type": "docid"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    assert body["data"]["results"] == "[1]title:T1|||content:c1\nc2"
    # 入参拼接 + logid 透传
    assert fake_docid_client.calls[0]["docids"] == ["a", "b"]
    assert fake_docid_client.calls[0]["logid"]


def test_docid_single_query_becomes_one_docid(client, fake_docid_client):
    fake_docid_client.set_script([{"title": "T", "extrainfo": {"meta_data": {"chunks": '["x"]'}}}])
    resp = client.post("/api/v1/navigator/query", json={"query": "onlyid", "type": "docid"})
    assert resp.status_code == 200
    assert fake_docid_client.calls[0]["docids"] == ["onlyid"]


def test_docid_data_envelope_only_has_results(client, fake_docid_client):
    fake_docid_client.set_script([{"title": "T", "extrainfo": {"meta_data": {"chunks": '["x"]'}}}])
    resp = client.post("/api/v1/navigator/query", json={"queries": ["a"], "type": "docid"})
    assert resp.json()["data"] == {"results": "[1]title:T|||content:x"}


# =====================================================================
# 默认 type = fileid（向后兼容）
# =====================================================================

def test_default_type_routes_to_fileid(client, fake_client, fake_docid_client):
    fake_client.set_script(_fileid_success_body())
    resp = client.post("/api/v1/navigator/query", json={"query": "f.docx"})
    assert resp.status_code == 200
    # docid client 完全未被调用
    assert fake_docid_client.calls == []


# =====================================================================
# 拒绝路径
# =====================================================================

def test_fileid_with_queries_rejected(client):
    resp = client.post("/api/v1/navigator/query", json={"query": "f", "queries": ["a"], "type": "fileid"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION_ERROR"


def test_fileid_default_with_queries_rejected(client):
    # 不传 type（默认 fileid）+ queries 同样拒绝
    resp = client.post("/api/v1/navigator/query", json={"query": "f", "queries": ["a"]})
    assert resp.status_code == 400


def test_docid_without_query_or_queries_rejected(client):
    resp = client.post("/api/v1/navigator/query", json={"type": "docid"})
    # schema 层 model_validator：query 与 queries 至少一个（被全局处理器映射为 400）
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION_ERROR"


def test_invalid_type_rejected(client):
    resp = client.post("/api/v1/navigator/query", json={"query": "f", "type": "bogus"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION_ERROR"
