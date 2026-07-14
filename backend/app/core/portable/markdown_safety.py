from __future__ import annotations

import html
import posixpath
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from app.core.errors import DomainError, ErrorCategory


_ACTIVE_TAG = re.compile(
    r"<\s*/?\s*(?:script|iframe|object|embed|form|svg|style|link|meta|base|math|video|audio|source|track|canvas)\b",
    re.IGNORECASE,
)
_ACTIVE_ATTRIBUTE = re.compile(
    r"\b(?:on[a-z0-9_-]+|style|srcdoc|srcset)\s*=",
    re.IGNORECASE,
)
_DANGEROUS_SCHEME = re.compile(
    r"(?:javascript|vbscript|file|data)\s*:",
    re.IGNORECASE,
)
_HTML_LINK = re.compile(
    r"\b(?:href|src|data|action|poster)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
    re.IGNORECASE,
)
_HTML_IMAGE = re.compile(r"<\s*img\b[^>]*>", re.IGNORECASE)
_HTML_IMAGE_SOURCE = re.compile(
    r"\bsrc\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
    re.IGNORECASE,
)
_AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.-]*:[^<>\s]*)>")
_REFERENCE_DEFINITION = re.compile(
    r"^ {0,3}\[(?!\^)[^\]\r\n]+\]:\s*(<[^>]*>|\S+)",
    re.MULTILINE,
)
_ENCODED_PATH_CONTROL = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[/\\]")
_FOOTNOTE = re.compile(r"\[\^(ev_[0-9a-f-]+)\]")
_FOOTNOTE_DEFINITION = re.compile(r"^ {0,3}\[\^(ev_[0-9a-f-]+)\]:")
_SEGMENT_CITATION = re.compile(r"\[\^seg_[^\]\r\n]*\]")
_H2 = re.compile(r"^ {0,3}##(?!#)\s+\S")
_SETEXT_H2_UNDERLINE = re.compile(r"^ {0,3}-+[ \t]*$")


def _unsafe() -> DomainError:
    return DomainError(
        "draft_markdown_unsafe",
        ErrorCategory.POLICY_DENIED,
        "Draft Markdown contains unsafe active content or links",
    )


@dataclass(frozen=True)
class _MarkdownScan:
    visible_text: str
    visible_mask: bytes
    has_mermaid_fence: bool


def _backslash_escaped(text: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _fence_run(line: str) -> tuple[int, str, int] | None:
    indent = 0
    while indent < len(line) and indent < 4 and line[indent] == " ":
        indent += 1
    if indent > 3 or indent >= len(line) or line[indent] not in "`~":
        return None
    marker = line[indent]
    end = indent
    while end < len(line) and line[end] == marker:
        end += 1
    length = end - indent
    return (indent, marker, length) if length >= 3 else None


def _hide(mask: bytearray, start: int, end: int) -> None:
    mask[start:end] = b"\x00" * (end - start)


def _scan_markdown(markdown: str) -> _MarkdownScan:
    mask = bytearray(b"\x01" * len(markdown))
    fence_marker: str | None = None
    fence_length = 0
    has_mermaid_fence = False
    line_start = 0
    while line_start < len(markdown):
        newline = markdown.find("\n", line_start)
        line_end = len(markdown) if newline < 0 else newline
        next_line = len(markdown) if newline < 0 else newline + 1
        line = markdown[line_start:line_end].rstrip("\r")
        fence = _fence_run(line)
        if fence_marker is not None:
            _hide(mask, line_start, next_line)
            if (
                fence is not None
                and fence[1] == fence_marker
                and fence[2] >= fence_length
                and not line[fence[0] + fence[2] :].strip(" \t")
            ):
                fence_marker = None
                fence_length = 0
            line_start = next_line
            continue
        if fence is not None:
            suffix = line[fence[0] + fence[2] :]
            if fence[1] != "`" or "`" not in suffix:
                fence_marker = fence[1]
                fence_length = fence[2]
                info = suffix.strip().split(None, 1)
                has_mermaid_fence = has_mermaid_fence or (
                    bool(info) and info[0].casefold() == "mermaid"
                )
                _hide(mask, line_start, next_line)
                line_start = next_line
                continue
        if line.startswith(("    ", "\t")):
            _hide(mask, line_start, next_line)
        line_start = next_line

    block_mask = bytes(mask)
    inline_start: int | None = None
    inline_length = 0
    cursor = 0
    while cursor < len(markdown):
        if block_mask[cursor] == 0:
            if inline_start is not None:
                mask[inline_start:cursor] = block_mask[inline_start:cursor]
                inline_start = None
                inline_length = 0
            cursor += 1
            continue
        if markdown[cursor] != "`" or (
            inline_start is None and _backslash_escaped(markdown, cursor)
        ):
            if inline_start is not None:
                mask[cursor] = 0
            cursor += 1
            continue
        run_end = cursor
        while (
            run_end < len(markdown)
            and block_mask[run_end] == 1
            and markdown[run_end] == "`"
        ):
            run_end += 1
        run_length = run_end - cursor
        if inline_start is None:
            inline_start = cursor
            inline_length = run_length
            _hide(mask, cursor, run_end)
        elif run_length == inline_length:
            _hide(mask, cursor, run_end)
            inline_start = None
            inline_length = 0
        else:
            _hide(mask, cursor, run_end)
        cursor = run_end
    if inline_start is not None:
        mask[inline_start:] = block_mask[inline_start:]

    visible_text = "".join(
        character if mask[index] else ("\n" if character == "\n" else " ")
        for index, character in enumerate(markdown)
    )
    return _MarkdownScan(
        visible_text=visible_text,
        visible_mask=bytes(mask),
        has_mermaid_fence=has_mermaid_fence,
    )


def _visible_lines(markdown: str) -> tuple[str, ...]:
    return tuple(_scan_markdown(markdown).visible_text.splitlines())


def _iter_markdown_destinations(markdown: str):
    cursor = 0
    while cursor < len(markdown):
        opening = markdown.find("[", cursor)
        if opening < 0:
            return
        if opening + 1 < len(markdown) and markdown[opening + 1] == "[":
            closing = markdown.find("]]", opening + 2)
            if closing < 0:
                return
            yield markdown[opening + 2 : closing].split("|", 1)[0], False
            cursor = closing + 2
            continue
        closing = markdown.find("]", opening + 1)
        if closing < 0:
            return
        destination_start = closing + 1
        if destination_start >= len(markdown) or markdown[destination_start] != "(":
            cursor = closing + 1
            continue
        destination_start += 1
        while destination_start < len(markdown) and markdown[destination_start] in " \t":
            destination_start += 1
        if destination_start < len(markdown) and markdown[destination_start] == "<":
            destination_end = markdown.find(">", destination_start + 1)
            if destination_end < 0:
                return
            is_image = (
                opening > 0
                and markdown[opening - 1] == "!"
                and not _backslash_escaped(markdown, opening - 1)
            )
            yield markdown[destination_start : destination_end + 1], is_image
            cursor = destination_end + 1
            continue
        destination_end = destination_start
        while (
            destination_end < len(markdown)
            and markdown[destination_end] not in " \t\r\n)"
        ):
            destination_end += 1
        if destination_end > destination_start:
            is_image = (
                opening > 0
                and markdown[opening - 1] == "!"
                and not _backslash_escaped(markdown, opening - 1)
            )
            yield markdown[destination_start:destination_end], is_image
        cursor = max(destination_end, closing + 1)


def _validate_destination(
    destination: str,
    bundle_relative_path: str,
    *,
    allow_external: bool = True,
) -> None:
    value = html.unescape(destination.strip())
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    if not value:
        raise _unsafe()
    decoded = value
    for _ in range(8):
        if _ENCODED_PATH_CONTROL.search(decoded) is not None:
            raise _unsafe()
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    else:
        raise _unsafe()
    if _DANGEROUS_SCHEME.search(decoded) is not None:
        raise _unsafe()
    if "\\" in decoded or _WINDOWS_DRIVE.match(decoded) is not None:
        raise _unsafe()

    try:
        parsed = urlsplit(decoded)
    except ValueError:
        raise _unsafe() from None
    if parsed.scheme:
        if parsed.scheme.lower() not in {"http", "https"} or not allow_external:
            raise _unsafe()
        return
    if decoded.startswith(("/", "//")):
        raise _unsafe()
    if decoded.startswith("#"):
        return

    relative_path = decoded.split("#", 1)[0].split("?", 1)[0]
    if not relative_path:
        return
    base_directory = posixpath.dirname(bundle_relative_path)
    normalized = posixpath.normpath(posixpath.join(base_directory, relative_path))
    if normalized == ".." or normalized.startswith("../") or normalized.startswith("/"):
        raise _unsafe()


def validate_markdown_safety(
    markdown: str,
    *,
    bundle_relative_path: str,
) -> None:
    if not isinstance(markdown, str) or not isinstance(bundle_relative_path, str):
        raise _unsafe()
    if (
        not bundle_relative_path
        or "\\" in bundle_relative_path
        or bundle_relative_path.startswith("/")
        or _WINDOWS_DRIVE.match(bundle_relative_path) is not None
        or posixpath.normpath(bundle_relative_path) != bundle_relative_path
        or posixpath.normpath(bundle_relative_path).startswith("../")
    ):
        raise _unsafe()
    if any(ord(character) < 32 and character not in "\t\n\r" for character in markdown):
        raise _unsafe()

    scan = _scan_markdown(markdown)
    if scan.has_mermaid_fence:
        raise _unsafe()
    visible = scan.visible_text
    decoded_visible = html.unescape(visible)
    if (
        _ACTIVE_TAG.search(decoded_visible) is not None
        or _ACTIVE_ATTRIBUTE.search(decoded_visible) is not None
        or _DANGEROUS_SCHEME.search(decoded_visible) is not None
    ):
        raise _unsafe()

    destinations: list[tuple[str, bool]] = []
    destinations.extend(_iter_markdown_destinations(visible))
    destinations.extend((match.group(1), False) for match in _AUTOLINK.finditer(visible))
    destinations.extend(
        (match.group(1), False) for match in _REFERENCE_DEFINITION.finditer(visible)
    )
    for match in _HTML_LINK.finditer(visible):
        destinations.append(
            (next(value for value in match.groups() if value is not None), False)
        )
    for image in _HTML_IMAGE.finditer(visible):
        source = _HTML_IMAGE_SOURCE.search(image.group(0))
        if source is not None:
            destinations.append(
                (next(value for value in source.groups() if value is not None), True)
            )
    for destination, is_image in destinations:
        _validate_destination(
            destination,
            bundle_relative_path,
            allow_external=not is_image,
        )


@dataclass(frozen=True)
class _MarkdownAnalysis:
    citation_ids: tuple[str, ...]
    definition_ids: tuple[str, ...]
    duplicate_definition_ids: frozenset[str]
    substantive_h2_citations: tuple[tuple[str, ...], ...]
    has_segment_citation: bool


def _analyze_markdown(markdown: str) -> _MarkdownAnalysis:
    citations: list[str] = []
    definitions: list[str] = []
    duplicate_definitions: set[str] = set()
    sections: list[list[str]] = []
    current_section: list[str] | None = None
    current_substantive = False
    current_citations: list[str] = []
    has_segment_citation = False

    def finish_section() -> None:
        nonlocal current_section, current_substantive, current_citations
        if current_section is not None and current_substantive:
            sections.append(list(current_citations))
        current_section = None
        current_substantive = False
        current_citations = []

    lines = _visible_lines(markdown)
    setext_h2_titles = {
        index
        for index in range(len(lines) - 1)
        if lines[index].strip() and _SETEXT_H2_UNDERLINE.match(lines[index + 1])
    }
    setext_h2_underlines = {index + 1 for index in setext_h2_titles}

    for index, line in enumerate(lines):
        if index in setext_h2_titles:
            finish_section()
            current_section = []
            continue
        if index in setext_h2_underlines:
            continue
        if _H2.match(line) is not None:
            finish_section()
            current_section = []
            continue
        definition = _FOOTNOTE_DEFINITION.match(line)
        if definition is not None:
            evidence_id = definition.group(1)
            if evidence_id in definitions:
                duplicate_definitions.add(evidence_id)
            definitions.append(evidence_id)
            continue
        line_citations = [match.group(1) for match in _FOOTNOTE.finditer(line)]
        citations.extend(line_citations)
        has_segment_citation = has_segment_citation or (
            _SEGMENT_CITATION.search(line) is not None
        )
        if current_section is not None and line.strip():
            current_substantive = True
            current_citations.extend(line_citations)
    finish_section()
    return _MarkdownAnalysis(
        citation_ids=tuple(citations),
        definition_ids=tuple(definitions),
        duplicate_definition_ids=frozenset(duplicate_definitions),
        substantive_h2_citations=tuple(tuple(values) for values in sections),
        has_segment_citation=has_segment_citation,
    )
