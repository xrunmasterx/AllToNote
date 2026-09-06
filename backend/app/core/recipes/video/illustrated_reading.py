from __future__ import annotations

import re

from app.core.domain.video import TranscriptDocument
from app.core.errors import DomainError, ErrorCategory
from app.core.portable.markdown_safety import is_backslash_escaped, markdown_visible_mask


_RANGE = re.compile(
    r"^## (.+?) \[RANGE:(seg_\d{6,}):(seg_\d{6,})\](?:[ \t]+\[\^seg_\d{6,}\])*[ \t]*$",
    re.MULTILINE,
)
_LOCAL_REF = re.compile(r"\[\^(seg_\d{6,})\]|\[SCREENSHOT:(seg_\d{6,})\]")


def bind_illustrated_ranges(markdown: str, transcript: TranscriptDocument) -> str:
    """Resolve model-selected segment boundaries; never trust generated clock math."""
    segments = {segment.segment_id: segment for segment in transcript.segments}
    order = {segment.segment_id: index for index, segment in enumerate(transcript.segments)}
    visible = markdown_visible_mask(markdown)
    headings = tuple(match for match in _RANGE.finditer(markdown) if visible[match.start()])

    def invalid() -> DomainError:
        return DomainError(
            "illustrated_section_range_invalid", ErrorCategory.RECIPE_FAILED,
            "Illustrated chapters require ordered source ranges containing their citations and frames",
        )

    if not headings or len(headings) != sum(
        visible[match.start()] for match in re.finditer(r"^## ", markdown, re.MULTILINE)
    ):
        raise invalid()
    previous_last = -1
    replacements = []
    for index, match in enumerate(headings):
        title, first_id, last_id = match.groups()
        first, last = segments.get(first_id), segments.get(last_id)
        if (first is None or last is None or order[first_id] <= previous_last
                or order[last_id] < order[first_id]):
            raise invalid()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
        local_ids = [reference.group(1) or reference.group(2)
                     for reference in _LOCAL_REF.finditer(markdown, match.end(), end)
                     if visible[reference.start()] and not is_backslash_escaped(markdown, reference.start())]
        if not local_ids or any(
            value not in segments
            or not order[first_id] <= order[value] <= order[last_id]
            for value in local_ids
        ):
            raise invalid()
        previous_last = order[last_id]

        def clock(value: int) -> str:
            minutes, seconds = divmod(value // 1_000, 60)
            return f"{minutes:02d}:{seconds:02d}"

        replacements.append((match.start(), match.end(), f"## {clock(first.start_ms)}–{clock(last.end_ms)} {title}"))
    for start, end, replacement in reversed(replacements):
        markdown = markdown[:start] + replacement + markdown[end:]
    return markdown
