"""论文正文规范化、章节识别与稳定 chunk 切分。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Iterable

from academic_service.app.services.paper.models import PaperChunk, PaperSection


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TOKEN_RE = re.compile(
    r"[A-Za-z0-9]+(?:[._/+:-][A-Za-z0-9]+)*"
    r"|[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]"
    r"|[^\s]",
    re.UNICODE,
)
_MARKDOWN_HEADING_RE = re.compile(r"^\s*(#{1,6})\s*(.*?)\s*$")
_NUMBERED_HEADING_RE = re.compile(
    r"^\s*((?:\d+(?:[.・]\d+)*|[IVXLC]+|[一二三四五六七八九十]+)"
    r"[.、:：\-]?)[ \t]+(.{1,100})\s*$",
    re.IGNORECASE,
)
_COMMON_HEADINGS = {
    "abstract", "introduction", "background", "related work", "method",
    "methods", "methodology", "materials and methods", "experiment",
    "experiments", "results", "discussion", "conclusion", "conclusions",
    "limitations", "references", "acknowledgements", "acknowledgments",
    "摘要", "引言", "绪言", "背景", "相关工作", "方法", "实验", "结果",
    "讨论", "结论", "局限", "参考文献",
}
_FALSE_HEADING_PREFIXES = (
    "citation", "articles you may be interested", "recommended articles",
    "published by", "received ", "accepted ", "copyright",
)


class TokenCounter:
    """优先使用部署的 tokenizer.json，否则使用确定性多语言兜底分词。"""

    def __init__(self, tokenizer_path: str = "") -> None:
        self._tokenizer = None
        if tokenizer_path:
            path = Path(tokenizer_path)
            if path.is_file():
                try:
                    from tokenizers import Tokenizer  # type: ignore
                    self._tokenizer = Tokenizer.from_file(str(path))
                except (ImportError, OSError, ValueError):
                    self._tokenizer = None

    def count(self, text: str) -> int:
        if self._tokenizer is not None:
            return len(self._tokenizer.encode(text, add_special_tokens=False).ids)
        return sum(1 for _ in _TOKEN_RE.finditer(text))

    def spans(self, text: str, offset: int = 0) -> list[tuple[int, int]]:
        # 精确字符切分需要 spans。即使使用模型 tokenizer，也使用同一确定性
        # 多语言边界生成候选，再用 count() 校验最终硬上限。
        return [(offset + m.start(), offset + m.end()) for m in _TOKEN_RE.finditer(text)]


def normalize_text(text: str) -> str:
    """规范化论文文本，所有下游字符区间以返回值为准。"""
    value = unicodedata.normalize("NFKC", text or "")
    value = value.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    value = _CONTROL_RE.sub("", value)

    literal_newlines = value.count(r"\n")
    actual_newlines = value.count("\n")
    if literal_newlines >= 3 and actual_newlines <= max(2, literal_newlines // 4):
        value = value.replace(r"\n", "\n")
    else:
        value = value.replace(r"\n\n", "\n\n")
        value = re.sub(r"\\n(?=\s*#{1,6}\s)", "\n", value)

    lines = [re.sub(r"[\t \u00a0]+", " ", line).rstrip() for line in value.split("\n")]
    value = "\n".join(lines)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _line_spans(text: str) -> Iterable[tuple[str, int, int]]:
    cursor = 0
    for line in text.splitlines(keepends=True):
        end = cursor + len(line)
        yield line.rstrip("\r\n"), cursor, end
        cursor = end
    if cursor < len(text):
        yield text[cursor:], cursor, len(text)


def _heading_candidate(line: str) -> tuple[int, str] | None:
    stripped = line.strip()
    if not stripped or len(stripped) > 140:
        return None
    lower = stripped.lower()
    if lower.startswith(_FALSE_HEADING_PREFIXES):
        return None

    match = _MARKDOWN_HEADING_RE.match(line)
    if match:
        title = re.sub(r"^#+\s*", "", match.group(2)).strip(" #")
        if not title or title.lower().startswith(_FALSE_HEADING_PREFIXES):
            return None
        number = re.match(r"^(\d+(?:[.・]\d+)*)\b", title)
        level = len(match.group(1))
        if number:
            level = max(level, number.group(1).count(".") + number.group(1).count("・") + 1)
        return min(level, 6), title

    numbered = _NUMBERED_HEADING_RE.match(line)
    if numbered:
        number, title = numbered.groups()
        level = number.count(".") + number.count("・") + 1
        return min(level, 6), f"{number} {title}".strip()

    normalized = re.sub(r"\s+", " ", stripped).lower().rstrip(":：")
    if normalized in _COMMON_HEADINGS:
        return 2, stripped.rstrip(":：")
    return None


def parse_sections(text: str, docid: str, title: str = "") -> list[PaperSection]:
    """把规范化全文解析成有序、带完整路径的扁平章节列表。"""
    headings: list[tuple[int, str, int, int]] = []
    normalized_title = re.sub(r"\s+", " ", title).strip().lower()
    seen_title = False
    for line, start, end in _line_spans(text):
        candidate = _heading_candidate(line)
        if candidate is None:
            continue
        level, heading = candidate
        heading_normalized = re.sub(r"\s+", " ", heading).strip().lower()
        # 文档开头的一级标题是论文标题，不作为正文章节；重复论文标题也排除。
        if level == 1 and start < 500 and not seen_title:
            seen_title = True
            if not normalized_title or heading_normalized == normalized_title:
                continue
        if normalized_title and heading_normalized == normalized_title:
            continue
        headings.append((level, heading, start, end))

    raw_sections: list[tuple[str, tuple[str, ...], int, int, int]] = []
    stack: list[tuple[int, str]] = []
    cursor = 0
    if headings and headings[0][2] > 0:
        raw_sections.append(("Unsectioned", ("Unsectioned",), 1, 0, headings[0][2]))
    for index, (level, heading, start, heading_end) in enumerate(headings):
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, heading))
        path = tuple(item[1] for item in stack)
        end = headings[index + 1][2] if index + 1 < len(headings) else len(text)
        content_start = min(heading_end, end)
        if content_start < end:
            raw_sections.append((heading, path, level, content_start, end))
        cursor = end

    if not raw_sections:
        raw_sections.append(("Unsectioned", ("Unsectioned",), 1, 0, len(text)))
    elif cursor < len(text):
        raw_sections.append(("Unsectioned", ("Unsectioned",), 1, cursor, len(text)))

    sections: list[PaperSection] = []
    for order, (heading, path, level, start, end) in enumerate(raw_sections):
        if not text[start:end].strip():
            continue
        digest = hashlib.sha256(
            json.dumps([docid, path, start, end], ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        sections.append(
            PaperSection(
                section_id=f"sec_{digest}",
                title=heading,
                path=path,
                level=level,
                order=order,
                char_start=start,
                char_end=end,
            )
        )
    return sections


def _choose_chunk_end(
    text: str,
    spans: list[tuple[int, int]],
    start_index: int,
    target_tokens: int,
    max_tokens: int,
) -> int:
    hard_end = min(len(spans), start_index + max_tokens)
    target_end = min(hard_end, start_index + target_tokens)
    minimum = min(target_end, start_index + max(1, target_tokens // 2))
    boundary_chars = {".", "!", "?", "。", "！", "？", ";", "；", "\n"}
    for end_index in range(target_end, minimum - 1, -1):
        char_end = spans[end_index - 1][1]
        if char_end >= len(text) or text[char_end - 1] in boundary_chars:
            return end_index
        between = text[char_end: min(len(text), char_end + 2)]
        if "\n" in between:
            return end_index
    return target_end


def chunk_sections(
    text: str,
    docid: str,
    sections: list[PaperSection],
    *,
    tokenizer: TokenCounter,
    target_tokens: int = 400,
    max_tokens: int = 450,
    overlap_tokens: int = 60,
    schema_version: str = "v1",
) -> list[PaperChunk]:
    """在章节内部生成稳定、有字符区间的 chunks。"""
    if target_tokens <= 0 or max_tokens < target_tokens:
        raise ValueError("chunk token 配置非法")
    if overlap_tokens < 0 or overlap_tokens >= target_tokens:
        raise ValueError("chunk overlap 必须小于 target_tokens")

    chunks: list[PaperChunk] = []
    global_order = 0
    for section in sections:
        section_text = text[section.char_start:section.char_end]
        spans = tokenizer.spans(section_text, offset=section.char_start)
        if not spans:
            continue
        start_index = 0
        section_order = 0
        while start_index < len(spans):
            end_index = _choose_chunk_end(
                text, spans, start_index, target_tokens, max_tokens
            )
            char_start = spans[start_index][0]
            char_end = spans[end_index - 1][1]
            chunk_text = text[char_start:char_end]
            # 若模型 tokenizer 的实际计数超过硬上限，向前收缩到满足为止。
            while end_index > start_index + 1 and tokenizer.count(chunk_text) > max_tokens:
                end_index -= 1
                char_end = spans[end_index - 1][1]
                chunk_text = text[char_start:char_end]

            content_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
            identity = [
                docid, schema_version, section.path, char_start, char_end, content_hash
            ]
            chunk_digest = hashlib.sha256(
                json.dumps(identity, ensure_ascii=False).encode("utf-8")
            ).hexdigest()[:20]
            chunks.append(
                PaperChunk(
                    chunk_id=f"chk_{chunk_digest}",
                    docid=docid,
                    section_id=section.section_id,
                    section_path=section.path,
                    order=global_order,
                    section_order=section_order,
                    char_start=char_start,
                    char_end=char_end,
                    text=chunk_text,
                    content_hash=content_hash,
                    token_count=tokenizer.count(chunk_text),
                )
            )
            global_order += 1
            section_order += 1
            if end_index >= len(spans):
                break
            next_start = max(start_index + 1, end_index - overlap_tokens)
            start_index = next_start
    return chunks
