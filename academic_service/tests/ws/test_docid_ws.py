# -*- coding: utf-8 -*-
"""WebSocket 的 type=docid 路由测试：started → done（同步下游）。"""

from __future__ import annotations


def test_ws_docid_done_event(client, fake_docid_client):
    fake_docid_client.set_script([{"title": "T", "extrainfo": {"meta_data": {"chunks": '["c1"]'}}}])
    with client.websocket_connect("/api/v1/navigator/ws") as ws:
        ws.send_json({"queries": ["a", "b"], "type": "docid"})
        events = []
        for _ in range(10):
            ev = ws.receive_json()
            events.append(ev)
            if ev.get("type") == "done":
                break
        types = [e["type"] for e in events]
        assert "progress" in types and "done" in types
        done = next(e for e in events if e["type"] == "done")
        assert done["data"]["results"] == "[1]title:T|||content:c1"
    assert fake_docid_client.calls[0]["docids"] == ["a", "b"]


def test_ws_docid_single_query(client, fake_docid_client):
    fake_docid_client.set_script([{"title": "X", "extrainfo": {"meta_data": {"chunks": '["y"]'}}}])
    with client.websocket_connect("/api/v1/navigator/ws") as ws:
        ws.send_json({"query": "onlyid", "type": "docid"})
        ev = ws.receive_json()  # progress started
        assert ev["type"] == "progress"
        done = ws.receive_json()
        assert done["type"] == "done"
    assert fake_docid_client.calls[0]["docids"] == ["onlyid"]


def test_ws_docid_missing_input_error(client, fake_docid_client):
    with client.websocket_connect("/api/v1/navigator/ws") as ws:
        ws.send_json({"type": "docid"})
        ev = ws.receive_json()
        assert ev["type"] == "error"
        assert ev["code"] == "VALIDATION_ERROR"
    assert fake_docid_client.calls == []


def test_ws_default_type_is_fileid(client, fake_client, fake_docid_client):
    # 不传 type → fileid（doc_fulltext）
    from academic_service.tests.conftest import make_chunk, make_success_body
    fake_client.set_script(make_success_body([make_chunk(0, "A")]))
    with client.websocket_connect("/api/v1/navigator/ws") as ws:
        ws.send_json({"query": "a.docx"})
        events = []
        for _ in range(10):
            ev = ws.receive_json()
            events.append(ev)
            if ev.get("type") == "done":
                break
        assert any(e["type"] == "done" for e in events)
    assert fake_docid_client.calls == []


def test_ws_docid_relevant_reports_processing_stages(client, fake_docid_client):
    fake_docid_client.set_script([
        {
            "docid": "d1",
            "title": "T",
            "extrainfo": {"chunks": '["## Method\\n\\nTARGET method evidence and result."]'},
        }
    ])
    with client.websocket_connect("/api/v1/navigator/ws") as ws:
        ws.send_json({
            "query": "d1",
            "type": "docid",
            "options": {"intent": "relevant", "question": "TARGET method"},
        })
        events = []
        for _ in range(10):
            event = ws.receive_json()
            events.append(event)
            if event.get("type") == "done":
                break
    progress = [event["message"] for event in events if event["type"] == "progress"]
    assert progress == ["started", "fetching", "parsing", "reranking", "merging"]
    done = next(event for event in events if event["type"] == "done")
    assert done["data"]["processing"]["intent"] == "relevant"
    assert done["data"]["papers"][0]["segments"]


def test_http_and_ws_docid_relevant_final_data_match(client, fake_docid_client):
    result = {
        "docid": "d1",
        "title": "T",
        "extrainfo": {"chunks": '["## Method\\n\\nTARGET method evidence and result."]'},
    }
    payload = {
        "query": "d1",
        "type": "docid",
        "options": {"intent": "relevant", "question": "TARGET method"},
    }
    fake_docid_client.set_script([result])
    http_data = client.post("/api/v1/navigator/query", json=payload).json()["data"]

    fake_docid_client.set_script([result])
    with client.websocket_connect("/api/v1/navigator/ws") as ws:
        ws.send_json(payload)
        while True:
            event = ws.receive_json()
            if event.get("type") == "done":
                ws_data = event["data"]
                break
    assert ws_data == http_data
