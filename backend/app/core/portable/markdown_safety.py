from __future__ import annotations

import html
import posixpath
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from app.core.errors import DomainError, ErrorCategory


_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_ACTIVE_TAG = re.compile(
    r"<\s*/?\s*(?:script|iframe|object|embed|form|svg|style|link|meta|base|math)\b",
    re.IGNORECASE,
)
_ACTIVE_ATTRIBUTE = re.compile(
    r"\b(?:on[a-z0-9_-]+|style|srcdoc)\s*=",
    re.IGNORECASE,
)
_DANGEROUS_SCHEME = re.compile(
    r"(?:javascript|vbscript|file|data)\s*:",
    re.IGNORECASE,
)
_MARKDOWN_LINK = re.compile(r"!?\[[^\]\r\n]*\]\(\s*(<[^>]*>|[^\s)]+)")
_OBSIDIAN_LINK = re.compile(r"\[\[([^\]\r\n|]+)(?:\|[^\]\r\n]*)?\]\]")
_HTML_LINK = re.compile(
    r"\b(?:href|src|data|action|poster)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
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


def _unsafe() -> DomainError:
    return DomainError(
        "draft_markdown_unsafe",
        ErrorCategory.POLICY_DENIED,
        "Draft Markdown contains unsafe active content or links",
    )


def _without_inline_code(line: str) -> str:
    visible: list[str] = []
    cursor = 0
    while cursor < len(line):
        marker = line.find("`", cursor)
        if marker < 0:
            visible.append(line[cursor:])
            break
        run_end = marker
        while run_end < len(line) and line[run_end] == "`":
            run_end += 1
        marker_text = line[marker:run_end]
        closing = line.find(marker_text, run_end)
        if closing < 0:
            visible.append(line[cursor:])
            break
        visible.append(line[cursor:marker])
        visible.append(" " * (closing + len(marker_text) - marker))
        cursor = closing + len(marker_text)
    return "".join(visible)


def _visible_lines(markdown: str) -> tuple[str, ...]:
    visible: list[str] = []
    fence_marker: str | None = None
    for line in markdown.splitlines():
        fence = _FENCE.match(line)
        if fence_marker is not None:
            visible.append("")
            if fence is not None and fence.group(1)[0] == fence_marker[0] and len(
                fence.group(1)
            ) >= len(fence_marker):
                fence_marker = None
            continue
        if fence is not None:
            fence_marker = fence.group(1)
            visible.append("")
            continue
        if line.startswith(("    ", "\t")):
            visible.append("")
            continue
        visible.append(_without_inline_code(line))
    return tuple(visible)


def _validate_destination(destination: str, bundle_relative_path: str) -> None:
    value = html.unescape(destination.strip())
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    if not value:
        raise _unsafe()
    if _ENCODED_PATH_CONTROL.search(value) is not None:
        raise _unsafe()
    decoded = unquote(value)
    if _DANGEROUS_SCHEME.search(decoded) is not None:
        raise _unsafe()
    if "\\" in decoded or _WINDOWS_DRIVE.match(decoded) is not None:
        raise _unsafe()

    parsed = urlsplit(decoded)
    if parsed.scheme:
        if parsed.scheme.lower() not in {"http", "https"}:
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
        or posixpath.normpath(bundle_relative_path).startswith("../")
    ):
        raise _unsafe()
    if any(ord(character) < 32 and character not in "\t\n\r" for character in markdown):
        raise _unsafe()

    visible = "\n".join(_visible_lines(markdown))
    decoded_visible = html.unescape(visible)
    if (
        _ACTIVE_TAG.search(decoded_visible) is not None
        or _ACTIVE_ATTRIBUTE.search(decoded_visible) is not None
        or _DANGEROUS_SCHEME.search(decoded_visible) is not None
    ):
        raise _unsafe()

    destinations: list[str] = []
    destinations.extend(match.group(1) for match in _MARKDOWN_LINK.finditer(visible))
    destinations.extend(match.group(1) for match in _OBSIDIAN_LINK.finditer(visible))
    destinations.extend(match.group(1) for match in _AUTOLINK.finditer(visible))
    destinations.extend(
        match.group(1) for match in _REFERENCE_DEFINITION.finditer(visible)
    )
    for match in _HTML_LINK.finditer(visible):
        destinations.append(next(value for value in match.groups() if value is not None))
    for destination in destinations:
        _validate_destination(destination, bundle_relative_path)


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

    for line in _visible_lines(markdown):
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
