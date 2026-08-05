# -*- coding: utf-8 -*-
"""paper-example 片段的规范化、章节解析与 chunk 不变量。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from academic_service.app.services.paper.parser import (
    TokenCounter,
    chunk_sections,
    normalize_text,
    parse_sections,
)


SAMPLE_FILE = Path(__file__).resolve().parent.parent / "fixtures" / "paper_fragments.txt"


def _fragments() -> list[str]:
    raw = SAMPLE_FILE.read_text(encoding="utf-8")
    return [part.strip() for part in re.split(r"(?m)^\s*-{3,}\s*$", raw) if part.strip()]


def test_paper_example_contains_seven_fragments():
    assert len(_fragments()) == 7


@pytest.mark.parametrize("fragment_index", range(7))
def test_sample_fragments_parse_deterministically_and_preserve_ranges(fragment_index: int):
    fragment = _fragments()[fragment_index]
    normalized = normalize_text(fragment)
    assert normalized
    assert normalize_text(normalized) == normalized

    docid = f"sample-{fragment_index + 1}"
    sections_a = parse_sections(normalized, docid)
    sections_b = parse_sections(normalized, docid)
    assert sections_a == sections_b
    assert sections_a

    tokenizer = TokenCounter()
    chunks_a = chunk_sections(
        normalized,
        docid,
        sections_a,
        tokenizer=tokenizer,
        target_tokens=400,
        max_tokens=450,
        overlap_tokens=60,
    )
    chunks_b = chunk_sections(
        normalized,
        docid,
        sections_b,
        tokenizer=tokenizer,
        target_tokens=400,
        max_tokens=450,
        overlap_tokens=60,
    )
    assert chunks_a == chunks_b
    assert chunks_a
    assert all(chunk.token_count <= 450 for chunk in chunks_a)
    assert all(normalized[chunk.char_start:chunk.char_end] == chunk.text for chunk in chunks_a)
    assert len({chunk.chunk_id for chunk in chunks_a}) == len(chunks_a)


def test_case_report_and_review_headings_are_recognized():
    case_text = normalize_text(_fragments()[0])
    case_paths = [title for section in parse_sections(case_text, "case") for title in section.path]
    assert "Abstract" in case_paths
    assert "INTRODUCTION" in case_paths
    assert "CASE REPORT" in case_paths

    review_text = normalize_text(_fragments()[1])
    review_paths = [title for section in parse_sections(review_text, "review") for title in section.path]
    assert "Materials and Methods" in review_paths
    assert "Discussion" in review_paths
    assert "Definition" in review_paths


def test_noisy_wse2_recommendation_heading_is_excluded():
    normalized = normalize_text(_fragments()[4])
    paths = [title.lower() for section in parse_sections(normalized, "wse2") for title in section.path]
    assert not any(title.startswith("articles you may be interested") for title in paths)
    assert "young's modulus" in normalized.lower()


def test_sample_separator_is_only_a_fixture_convention():
    # 生产 normalize/parse 不解释 ----；单个 fragment 内出现该文本时仍是正文。
    normalized = normalize_text("# Title\n\nA\n----\nB")
    assert "----" in normalized
