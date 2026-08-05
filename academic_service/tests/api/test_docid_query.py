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


def test_docid_data_envelope_keeps_results_and_adds_structured_papers(client, fake_docid_client):
    fake_docid_client.set_script([{"title": "T", "extrainfo": {"meta_data": {"chunks": '["x"]'}}}])
    resp = client.post("/api/v1/navigator/query", json={"queries": ["a"], "type": "docid"})
    data = resp.json()["data"]
    assert data["results"] == "[1]title:T|||content:x"
    assert data["processing"]["intent"] == "fulltext"
    assert data["papers"][0]["title"] == "T"
    assert data["papers"][0]["content"] == "x"


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


# =====================================================================
# docid 论文数据意图
# =====================================================================

def test_docid_relevant_requires_question(client, fake_docid_client):
    resp = client.post(
        "/api/v1/navigator/query",
        json={"query": "d1", "type": "docid", "options": {"intent": "relevant"}},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION_ERROR"
    assert fake_docid_client.calls == []


def test_docid_rejects_unknown_intent(client, fake_docid_client):
    resp = client.post(
        "/api/v1/navigator/query",
        json={"query": "d1", "type": "docid", "options": {"intent": "metadata"}},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION_ERROR"
    assert fake_docid_client.calls == []


def test_docid_relevant_returns_segments_and_bm25_degradation(client, fake_docid_client):
    fake_docid_client.set_script([
        {
            "docid": "d1",
            "title": "Epinephrine Case",
            "extrainfo": {
                "chunks": '["# Epinephrine Case\\n\\n## Case Report\\n\\nIntravenous epinephrine caused atrial fibrillation and requires continuous monitoring."]'
            },
        }
    ])
    resp = client.post(
        "/api/v1/navigator/query",
        json={
            "query": "d1",
            "type": "docid",
            "options": {
                "intent": "relevant",
                "question": "What are the risks of intravenous epinephrine?",
            },
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["processing"]["intent"] == "relevant"
    assert data["processing"]["reranker"]["degraded"] is True
    assert data["papers"][0]["docid"] == "d1"
    assert data["papers"][0]["segments"]
    assert "atrial fibrillation" in data["results"]


def test_docid_duplicate_queries_are_removed_before_downstream(client, fake_docid_client):
    fake_docid_client.set_script([])
    resp = client.post(
        "/api/v1/navigator/query",
        json={"queries": ["a", "a", "b", "a"], "type": "docid"},
    )
    assert resp.status_code == 200
    assert fake_docid_client.calls[0]["docids"] == ["a", "b"]


def test_docid_partial_success_keeps_broken_paper_status(client, fake_docid_client):
    fake_docid_client.set_script([
        {"docid": "bad", "title": "Broken", "extrainfo": {"chunks": "not-json"}},
        {"docid": "good", "title": "Good", "extrainfo": {"chunks": '["usable"]'}},
    ])
    resp = client.post(
        "/api/v1/navigator/query",
        json={"queries": ["bad", "good"], "type": "docid"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["papers"][0]["status"] == "no_content"
    assert "NO_VALID_CHUNKS" in data["papers"][0]["warnings"]
    assert data["papers"][1]["status"] == "ok"
    assert data["results"] == "[1]title:Good|||content:usable"
