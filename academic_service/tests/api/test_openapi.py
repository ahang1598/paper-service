# -*- coding: utf-8 -*-
"""OpenAPI 文档与模型存在性测试。"""

from __future__ import annotations


def test_openapi_available(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    assert spec["info"]["title"] == "paper-service"


def test_openapi_contains_query_schemas(client):
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    # 统一查询请求/响应模型应出现在 schema 中
    assert "NavigatorQueryRequest" in schemas
    assert "NavigatorQueryResponse" in schemas


def test_docfulltext_params_schema_is_pydantic():
    """action 专属参数模型本身是可校验的 pydantic 模型（虽不直接出现在 OpenAPI 中）。"""
    from academic_service.app.schemas.document import DocFullTextParams
    p = DocFullTextParams(file_id="a.docx")
    assert p.splitter == 1
    # 确认它可作为 params_schema 被 handler 引用
    from academic_service.app.core.registry import get_handler_class
    assert get_handler_class("doc_fulltext").params_schema is DocFullTextParams


def test_query_path_present(client):
    spec = client.get("/openapi.json").json()
    assert "/api/v1/navigator/query" in spec["paths"]


def test_docs_page(client):
    assert client.get("/docs").status_code == 200
