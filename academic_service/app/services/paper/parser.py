"""论文正文规范化、章节识别与稳定 chunk 切分。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from html.parser import HTMLParser
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
_HTML_TABLE_RE = re.compile(r"(?is)<table\b[^>]*>.*?</table>")
_HTML_STYLE_RE = re.compile(r"(?is)<style\b[^>]*>.*?</style>")
_HTML_WRAPPER_RE = re.compile(r"(?is)</?(?:html|body)\b[^>]*>|<meta\b[^>]*>")
_INLINE_MARKDOWN_HEADING_RE = re.compile(r"(?<!\n)[ \t]*(?=#\s+#\s+)")
_HEADING_CONNECTORS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into",
    "of", "on", "or", "the", "to", "versus", "via", "with", "without",
}


class _TableTextParser(HTMLParser):
    """把单个 HTML table 转成稳定的按行纯文本结构。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized == "tr":
            self._row = []
        elif normalized in {"td", "th"}:
            if self._row is None:
                self._row = []
            self._cell_parts = []
        elif normalized == "br" and self._cell_parts is not None:
            self._cell_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in {"td", "th"} and self._cell_parts is not None:
            cell = re.sub(r"\s+", " ", "".join(self._cell_parts)).strip()
            if self._row is None:
                self._row = []
            self._row.append(cell)
            self._cell_parts = None
        elif normalized == "tr" and self._row is not None:
            if any(cell for cell in self._row):
                self.rows.append(self._row)
            self._row = None


def _table_to_text(match: re.Match[str]) -> str:
    parser = _TableTextParser()
    try:
        parser.feed(match.group(0))
        parser.close()
    except (ValueError, TypeError):
        # HTMLParser 对异常标签通常可容错；极端损坏时至少移除标签并保留文字。
        fallback = re.sub(r"(?is)<[^>]+>", " ", match.group(0))
        return "\n[TABLE]\n" + re.sub(r"\s+", " ", fallback).strip() + "\n[/TABLE]\n"
    lines = [" | ".join(cell.replace(" | ", " / ") for cell in row) for row in parser.rows]
    return "\n[TABLE]\n" + "\n".join(lines) + "\n[/TABLE]\n"


def _clean_html_tables(text: str) -> str:
    """清洗 HTML table 为行式文本，确保 chunk 不会落在标签中间。"""
    value = _HTML_TABLE_RE.sub(_table_to_text, text)
    value = _HTML_STYLE_RE.sub("", value)
    return _HTML_WRAPPER_RE.sub("", value)


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

    # OCR/抓取结果常把 ``# #`` 章节标记粘在元信息、表格或上一段末尾。
    value = _INLINE_MARKDOWN_HEADING_RE.sub("\n", value)
    value = _clean_html_tables(value)

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


def _looks_like_author_heading(title: str) -> bool:
    lower = title.lower()
    if "," in title and (" and " in lower or "*" in title or "^" in title):
        return True
    if re.search(r"\b(?:university|institute|department|school|laboratory)\b", lower):
        return True
    return False


def _inline_heading_prefix(title: str, document_title: str = "") -> tuple[str, int] | None:
    """从“章节标题 正文...”粘连行中推断标题和正文起点。"""
    compact_document_title = re.sub(r"\s+", " ", document_title).strip()
    if compact_document_title and title.lower().startswith(compact_document_title.lower()):
        return compact_document_title, len(compact_document_title)

    for common in sorted(_COMMON_HEADINGS, key=len, reverse=True):
        match = re.match(rf"(?i)({re.escape(common)})(?:\s*[:：.]\s*|\s+)", title)
        if match:
            return match.group(1).rstrip(":：."), match.end()

    words = list(re.finditer(r"\S+", title))
    if len(words) < 5:
        return None
    for index in range(2, min(len(words) - 1, 16)):
        current = words[index]
        token = current.group(0).strip("()[]{}:;,.—–-")
        next_token = words[index + 1].group(0).strip("()[]{}:;,.—–-")
        if not token:
            continue
        is_title_word = token.lower() in _HEADING_CONNECTORS or token[0].isupper() or token.isupper()
        if not is_title_word:
            return title[:current.start()].rstrip(), current.start()
        # 标题后正文通常以首字母大写的句首词开始，随后立即出现普通小写词。
        if (
            token.lower() not in _HEADING_CONNECTORS
            and token[0].isupper()
            and next_token
            and next_token[0].islower()
        ):
            return title[:current.start()].rstrip(), current.start()
    return None


def _heading_candidate(line: str, document_title: str = "") -> tuple[int, str, int] | None:
    stripped = line.strip()
    if not stripped:
        return None
    lower = stripped.lower()
    if lower.startswith(_FALSE_HEADING_PREFIXES):
        return None

    match = _MARKDOWN_HEADING_RE.match(line)
    if match:
        malformed_extra_marker = bool(re.match(r"^\s*#", match.group(2)))
        raw_title = re.sub(r"^#+\s*", "", match.group(2)).strip(" #")
        lower_raw_title = raw_title.lower()
        should_infer_inline = (
            len(raw_title) > 140
            or bool(document_title and lower_raw_title.startswith(document_title.strip().lower()))
            or any(
                re.match(rf"(?i){re.escape(common)}(?:\s*[:：.]\s*|\s+)", raw_title)
                for common in _COMMON_HEADINGS
            )
        )
        inferred = _inline_heading_prefix(raw_title, document_title) if should_infer_inline else None
        title = inferred[0] if inferred else raw_title
        if not title or title.lower().startswith(_FALSE_HEADING_PREFIXES):
            return None
        if re.match(r"(?i)^table\s+\d+\s*[:：.]", title):
            return None
        if len(title) > 140 or _looks_like_author_heading(title):
            return None
        number = re.match(r"^(\d+(?:[.・]\d+)*)\b", title)
        level = max(len(match.group(1)), 2 if malformed_extra_marker else 1)
        if number:
            number_depth = number.group(1).count(".") + number.group(1).count("・") + 1
            level = max(level, number_depth + 1)
        if re.sub(r"\s+", " ", title).lower().rstrip(":：") in _COMMON_HEADINGS:
            level = 2
        content_offset = len(line)
        if inferred:
            title_start = line.find(raw_title)
            content_offset = title_start + inferred[1]
        return min(level, 6), title, content_offset

    numbered = _NUMBERED_HEADING_RE.match(line)
    if numbered:
        number, title = numbered.groups()
        if not re.search(r"[A-Za-z\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", title):
            return None
        level = number.count(".") + number.count("・") + 2
        return min(level, 6), f"{number} {title}".strip(), len(line)

    normalized = re.sub(r"\s+", " ", stripped).lower().rstrip(":：")
    if normalized in _COMMON_HEADINGS:
        return 2, stripped.rstrip(":："), len(line)
    return None


def parse_sections(text: str, docid: str, title: str = "") -> list[PaperSection]:
    """把规范化全文解析成有序、带完整路径的扁平章节列表。"""
    headings: list[tuple[int, str, int, int]] = []
    normalized_title = re.sub(r"\s+", " ", title).strip().lower()
    seen_title = False
    candidates: list[tuple[int, str, int, int]] = []
    for line, start, end in _line_spans(text):
        candidate = _heading_candidate(line, title)
        if candidate is None:
            continue
        level, heading, content_offset = candidate
        candidates.append((level, heading, start, min(start + content_offset, end)))

    candidate_counts: dict[str, int] = {}
    for _, heading, _, _ in candidates:
        key = re.sub(r"\s+", " ", heading).strip().lower()
        candidate_counts[key] = candidate_counts.get(key, 0) + 1

    heading_occurrences: dict[str, int] = {}
    for level, heading, start, content_start in candidates:
        heading_normalized = re.sub(r"\s+", " ", heading).strip().lower()
        heading_occurrences[heading_normalized] = heading_occurrences.get(heading_normalized, 0) + 1
        is_numbered = bool(re.match(r"^(?:\d+(?:[.・]\d+)*|[IVXLC]+|[一二三四五六七八九十]+)\b", heading, re.I))
        is_repeated_title = (
            candidate_counts.get(heading_normalized, 0) > 1
            and heading_normalized not in _COMMON_HEADINGS
        )
        # 文档开头的一级标题是论文标题，不作为正文章节；重复论文标题也排除。
        if level == 1 and start < 500 and not seen_title and not is_numbered:
            seen_title = True
            if not normalized_title or heading_normalized == normalized_title:
                continue
        if (normalized_title and heading_normalized == normalized_title) or is_repeated_title:
            # 抓取正文常在推荐文章块后重复一次论文标题；把后一次作为正文边界，
            # 但不把论文标题本身暴露为章节。
            if heading_occurrences[heading_normalized] > 1:
                headings.append((2, "Body", start, content_start))
            continue
        headings.append((level, heading, start, content_start))

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
    # 剩余内容未超过硬上限时整体返回，避免制造很短的尾 chunk。
    if hard_end >= len(spans):
        return hard_end

    minimum = min(target_end, start_index + max(1, target_tokens // 2))

    candidates: dict[int, list[int]] = {priority: [] for priority in range(4)}
    sentence_endings = {".", "!", "?", "。", "！", "？"}
    semicolons = {";", "；"}
    for end_index in range(minimum, hard_end + 1):
        char_end = spans[end_index - 1][1]
        next_char_start = spans[end_index][0] if end_index < len(spans) else len(text)
        between = text[char_end:next_char_start]
        if "\n\n" in between:
            candidates[0].append(end_index)
        elif text[char_end - 1] in sentence_endings:
            candidates[1].append(end_index)
        elif "\n" in between:
            candidates[2].append(end_index)
        elif text[char_end - 1] in semicolons:
            candidates[3].append(end_index)

    for priority in range(4):
        if candidates[priority]:
            # 同级边界选择最接近 target 的位置；距离相同时优先 target 之前。
            return min(
                candidates[priority],
                key=lambda index: (
                    abs(index - target_end),
                    index > target_end,
                    index,
                ),
            )
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
