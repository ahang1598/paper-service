# -*- coding: utf-8 -*-
"""paper-example 片段的规范化、章节解析与 chunk 不变量。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from academic_service.app.services.paper.parser import (
    TokenCounter,
    _choose_chunk_end,
    chunk_sections,
    normalize_text,
    parse_sections,
)


SAMPLE_FILE = Path(__file__).resolve().parent.parent / "fixtures" / "paper_fragments.txt"


def _fragments() -> list[str]:
    raw = SAMPLE_FILE.read_text(encoding="utf-8")
    return [part.strip() for part in re.split(r"(?m)^\s*-{3,}\s*$", raw) if part.strip()]


def test_paper_example_contains_eight_fragments():
    assert len(_fragments()) == 8


@pytest.mark.parametrize("fragment_index", range(8))
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


def test_malformed_numbered_headings_keep_first_section_and_correct_hierarchy():
    normalized = normalize_text(_fragments()[3])
    sections = parse_sections(normalized, "japanese")
    paths = [section.path for section in sections]
    assert any(path[-1].startswith("1 ") for path in paths)
    subsection = next(section for section in sections if section.title.startswith("3・1"))
    assert subsection.level == 3
    assert len(subsection.path) == 2


def test_common_markdown_heading_levels_are_canonicalized():
    normalized = normalize_text("#### Abstract\nText\n\n### Conclusion\nDone")
    sections = parse_sections(normalized, "canonical")
    assert [section.level for section in sections] == [2, 2]
    assert [section.path for section in sections] == [("Abstract",), ("Conclusion",)]


def test_author_heading_and_repeated_paper_title_are_not_sections():
    normalized = normalize_text(_fragments()[4])
    sections = parse_sections(
        normalized,
        "wse2",
        "Elastic properties of suspended multilayer WSe$_{2}$",
    )
    titles = [section.title for section in sections]
    assert not any("Rui Zhang" in title for title in titles)
    assert not any(title.startswith("Elastic properties") for title in titles)
    assert "Body" in titles


def test_inline_headings_and_html_table_are_structured_without_raw_tags():
    normalized = normalize_text(_fragments()[7])
    assert normalized.count("[TABLE]") == 1
    assert "Algorithm Type | Performance Variation (%) | Efficiency Improvement (%)" in normalized
    assert not re.search(r"(?i)</?(?:html|body|table|thead|tr|td|th)\b", normalized)

    sections = parse_sections(
        normalized,
        "rl",
        "The Evolution and Impact of Reinforcement Learning in Modern Artificial Intelligence",
    )
    titles = [section.title for section in sections]
    assert "Introduction" in titles
    assert "Theoretical Foundations and Methodological Evolution" in titles
    assert "Practical Applications Across Industries" in titles
    assert "Conclusion" in titles

    chunks = chunk_sections(
        normalized,
        "rl",
        sections,
        tokenizer=TokenCounter(),
        target_tokens=40,
        max_tokens=55,
        overlap_tokens=8,
    )
    assert chunks
    assert all("<td" not in chunk.text.lower() and "</td" not in chunk.text.lower() for chunk in chunks)


def _chosen_boundary(text: str, target: int = 10, maximum: int = 14) -> str:
    spans = TokenCounter().spans(text)
    end_index = _choose_chunk_end(text, spans, 0, target, maximum)
    return text[:spans[end_index - 1][1]]


def test_chunk_boundary_priority_paragraph_over_sentence():
    text = "a b c d e\n\nf g h i. j k l m n o p q"
    assert _chosen_boundary(text) == "a b c d e"


def test_chunk_boundary_priority_sentence_over_single_newline():
    text = "a b c d. e f g h i\nj k l m n o p q"
    assert _chosen_boundary(text) == "a b c d."


def test_chunk_boundary_priority_single_newline_over_semicolon():
    text = "a b c d e\nf g h i; j k l m n o p q"
    assert _chosen_boundary(text) == "a b c d e"


def test_chunk_boundary_uses_semicolon_then_hard_target_fallback():
    with_semicolon = "a b c d e; f g h i j k l m n o p q"
    assert _chosen_boundary(with_semicolon) == "a b c d e;"

    without_boundary = "a b c d e f g h i j k l m n o p q"
    assert _chosen_boundary(without_boundary) == "a b c d e f g h i j"


def test_chunk_boundary_can_extend_past_target_up_to_hard_max():
    text = "a b c d e f g h i j k. l m n o p q"
    assert _chosen_boundary(text) == "a b c d e f g h i j k."
