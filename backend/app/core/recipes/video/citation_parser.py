from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.domain.video import ScreenshotRequest
from app.core.errors import DomainError, ErrorCategory
from app.core.portable.markdown_safety import (
    is_backslash_escaped,
    markdown_visible_mask,
)


_SEGMENT_ID = re.compile(r"seg_[0-9]{6,}\Z")
_FOOTNOTE_DEFINITION = re.compile(r"(?m)^ {0,3}\[\^[^\]\r\n]*\]:")


@dataclass(frozen=True)
class ParsedModelOutput:
    markdown: str
    cited_segment_ids: tuple[str, ...]
    screenshot_requests: tuple[ScreenshotRequest, ...]


def _error(code: str, message: str) -> DomainError:
    return DomainError(code, ErrorCategory.RECIPE_FAILED, message)


def _visible_control_end(
    markdown: str,
    visible_mask: bytes,
    start: int,
) -> int:
    closing = start
    while closing < len(markdown) and markdown[closing] not in "]\r\n":
        closing += 1
    if (
        closing >= len(markdown)
        or markdown[closing] != "]"
        or not all(visible_mask[start : closing + 1])
    ):
        return -1
    return closing


def parse_model_output(
    markdown: str,
    *,
    known_segment_ids: tuple[str, ...],
    allow_screenshots: bool,
) -> ParsedModelOutput:
    """Parse visible model controls while treating code and escapes as literals."""

    if not isinstance(markdown, str) or not markdown.strip():
        raise _error("model_output_invalid", "Model output must be non-empty Markdown")
    known = frozenset(known_segment_ids)
    visible_mask = markdown_visible_mask(markdown)
    visible_text = "".join(
        character if visible_mask[index] else ("\n" if character == "\n" else " ")
        for index, character in enumerate(markdown)
    )
    if not visible_text.strip():
        raise _error("model_output_invalid", "Model output has no visible Markdown body")
    if _FOOTNOTE_DEFINITION.search(visible_text) is not None:
        raise _error(
            "model_citation_definition_forbidden",
            "Model output must not define transcript footnotes",
        )

    citations: list[str] = []
    citation_set: set[str] = set()
    screenshots: list[ScreenshotRequest] = []
    screenshot_set: set[str] = set()
    removals: list[tuple[int, int]] = []
    cursor = 0
    citation_prefix = "[^seg_"
    screenshot_prefix = "[SCREENSHOT:"
    while cursor < len(markdown):
        if not visible_mask[cursor] or is_backslash_escaped(markdown, cursor):
            cursor += 1
            continue
        if markdown.startswith(citation_prefix, cursor):
            closing = _visible_control_end(markdown, visible_mask, cursor)
            if closing < 0:
                raise _error(
                    "model_citation_invalid",
                    "Model output contains a malformed transcript citation",
                )
            segment_id = markdown[cursor + 2 : closing]
            if _SEGMENT_ID.fullmatch(segment_id) is None:
                raise _error(
                    "model_citation_invalid",
                    "Model output contains an invalid transcript citation",
                )
            if segment_id not in known:
                raise _error(
                    "model_citation_unknown",
                    "Model output cites a segment outside this chunk",
                )
            if segment_id not in citation_set:
                citation_set.add(segment_id)
                citations.append(segment_id)
            cursor = closing + 1
            continue
        if markdown.startswith("[^", cursor):
            raise _error(
                "model_citation_invalid",
                "Model output contains an unsupported footnote control",
            )
        if markdown.startswith(screenshot_prefix, cursor):
            closing = _visible_control_end(markdown, visible_mask, cursor)
            if closing < 0:
                raise _error(
                    "model_screenshot_invalid",
                    "Model output contains a malformed screenshot request",
                )
            segment_id = markdown[cursor + len(screenshot_prefix) : closing]
            if _SEGMENT_ID.fullmatch(segment_id) is None:
                raise _error(
                    "model_screenshot_invalid",
                    "Model output contains an invalid screenshot request",
                )
            if not allow_screenshots:
                raise _error(
                    "model_screenshot_not_allowed",
                    "Screenshot requests are disabled for this generation",
                )
            if segment_id not in known:
                raise _error(
                    "model_screenshot_unknown",
                    "Model output requests a segment outside this chunk",
                )
            if segment_id in screenshot_set:
                raise _error(
                    "model_screenshot_duplicate",
                    "Model output repeats a screenshot request",
                )
            screenshot_set.add(segment_id)
            screenshots.append(ScreenshotRequest(segment_id))
            removals.append((cursor, closing + 1))
            cursor = closing + 1
            continue
        if markdown.startswith("[SCREENSHOT", cursor):
            raise _error(
                "model_screenshot_invalid",
                "Model output contains an unsupported screenshot control",
            )
        cursor += 1

    output: list[str] = []
    copied_until = 0
    for start, end in removals:
        output.append(markdown[copied_until:start])
        copied_until = end
    output.append(markdown[copied_until:])
    parsed_markdown = "".join(output).strip()
    if not citations:
        raise _error(
            "model_citation_missing",
            "Model output must cite at least one segment from this chunk",
        )
    if (
        not parsed_markdown
        or not "".join(
            character if mask else ("\n" if character == "\n" else " ")
            for character, mask in zip(
                parsed_markdown,
                markdown_visible_mask(parsed_markdown),
            )
        ).strip()
    ):
        raise _error(
            "model_output_invalid",
            "Model output must contain Markdown in addition to control markers",
        )
    return ParsedModelOutput(
        markdown=parsed_markdown,
        cited_segment_ids=tuple(citations),
        screenshot_requests=tuple(screenshots),
    )


__all__ = ["ParsedModelOutput", "parse_model_output"]
