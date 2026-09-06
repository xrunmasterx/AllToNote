import pytest

from app.core.domain.video import TranscriptDocument, TranscriptSegment
from app.core.errors import DomainError
from app.core.recipes.video.illustrated_reading import bind_illustrated_ranges


TRANSCRIPT = TranscriptDocument("en", (
    TranscriptSegment("seg_000001", 22_520, 26_621, "First"),
    TranscriptSegment("seg_000002", 26_620, 28_420, "Second"),
    TranscriptSegment("seg_000003", 31_000, 34_000, "Third"),
))


def test_chapter_times_are_computed_from_source_not_model_clock_arithmetic():
    markdown = (
        "# Reading\n\n## First [RANGE:seg_000001:seg_000001] [^seg_000001] \n\n"
        "First.[^seg_000001]\n[SCREENSHOT:seg_000001]\n\n"
        "## Second [RANGE:seg_000002:seg_000003]\n\nSecond.[^seg_000003]"
    )
    result = bind_illustrated_ranges(markdown, TRANSCRIPT)
    assert "## 00:22–00:26 First" in result
    assert "## 00:26–00:34 Second" in result
    assert "RANGE:" not in result
    assert "[SCREENSHOT:seg_000001]" in result


def test_range_binding_ignores_code_headings_and_literal_citations():
    markdown = (
        "# Note\n\n## First [RANGE:seg_000001:seg_000001]\n\nFirst.[^seg_000001]\n\n"
        "```markdown\n## Literal heading\n[^seg_000003]\n```"
    )
    result = bind_illustrated_ranges(markdown, TRANSCRIPT)
    assert "## 00:22–00:26 First" in result
    assert "## Literal heading" in result


@pytest.mark.parametrize("markdown", [
    "## Missing range\nFirst.[^seg_000001]",
    "## Unknown [RANGE:seg_000001:seg_999999]\nFirst.[^seg_000001]",
    "## Backwards [RANGE:seg_000003:seg_000001]\nFirst.[^seg_000001]",
    "## Wrong picture [RANGE:seg_000001:seg_000001]\nFirst.[^seg_000001]\n[SCREENSHOT:seg_000003]",
    "## Wrong citation [RANGE:seg_000001:seg_000001]\nSecond.[^seg_000002]",
    "## First [RANGE:seg_000001:seg_000002]\nFirst.[^seg_000001]\n\n"
    "## Overlap [RANGE:seg_000002:seg_000003]\nSecond.[^seg_000002]",
])
def test_invalid_or_overlapping_chapter_ranges_cannot_be_published(markdown):
    with pytest.raises(DomainError, match="illustrated_section_range_invalid"):
        bind_illustrated_ranges(markdown, TRANSCRIPT)
