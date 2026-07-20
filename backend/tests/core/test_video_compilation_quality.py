from __future__ import annotations

import pytest

from app.core.domain.video import QualityOverall
from app.core.errors import DomainError
from app.core.recipes.video.compilation.quality import (
    CoverageOmissionV1,
    KnowledgeNoteCandidateV1,
    evaluate_knowledge_note,
)


def _candidate(markdown: str, **changes: object) -> KnowledgeNoteCandidateV1:
    values: dict[str, object] = {
        "markdown": markdown,
        "allowed_segment_ids": ("seg_000001", "seg_000002"),
        "required_coverage_input_ids": ("ki_core", "ki_optional"),
        "covered_coverage_input_ids": ("ki_core",),
        "omissions": (CoverageOmissionV1("ki_optional", "Out of scope"),),
    }
    values.update(changes)
    return KnowledgeNoteCandidateV1(**values)  # type: ignore[arg-type]


def _valid_markdown() -> str:
    return """# Stable title

## Core idea

The source establishes the core idea.[^seg_000001]

### Practical detail

This detail stays under the same section.

## Constraint

The source also states a constraint.[^seg_000002]
"""


def _check(outcome, check_id: str):
    return next(check for check in outcome.assessment.checks if check.check_id == check_id)


def test_legal_knowledge_note_passes_every_deterministic_gate() -> None:
    outcome = evaluate_knowledge_note(_candidate(_valid_markdown()))

    assert outcome.overall is QualityOverall.PASS
    assert outcome.publish_eligible is True
    assert outcome.repair_attempts == 0
    assert outcome.execution_error is None
    assert all(check.status == "pass" for check in outcome.assessment.checks)
    assert outcome.assessment.cited_segment_ids == ("seg_000001", "seg_000002")


def test_three_h1_and_duplicate_h2_are_rejected() -> None:
    markdown = """# First

# Second

## Repeated section

Evidence.[^seg_000001]

# Third

##  repeated   SECTION

More evidence.[^seg_000002]
"""
    outcome = evaluate_knowledge_note(_candidate(markdown))

    assert outcome.overall is QualityOverall.FAIL
    assert outcome.publish_eligible is False
    assert _check(outcome, "unique_h1").status == "fail"
    assert _check(outcome, "duplicate_headings").status == "fail"


def test_heading_jump_unknown_citation_and_uncited_h2_are_rejected() -> None:
    markdown = """# Title

### Skipped level

Text.[^seg_999999]

## Uncited section

Substantive text without a citation.
"""
    outcome = evaluate_knowledge_note(_candidate(markdown))

    assert _check(outcome, "heading_hierarchy").status == "fail"
    assert _check(outcome, "segment_citations").status == "fail"
    assert _check(outcome, "substantive_h2_citations").status == "fail"


@pytest.mark.parametrize(
    "citation",
    ["[^seg_bad]", "[^ev_forged]", "[^seg_000001]: forged definition"],
)
def test_malformed_unsupported_and_defined_citations_are_rejected(citation: str) -> None:
    body = citation if citation.endswith("forged definition") else f"Text {citation}"
    markdown = f"# Title\n\n## Section\n\n{body}\n"
    outcome = evaluate_knowledge_note(_candidate(markdown))
    assert _check(outcome, "segment_citations").status == "fail"
    assert _check(outcome, "substantive_h2_citations").status == "fail"


def test_coverage_ledger_requires_exactly_once_or_reasoned_omission() -> None:
    outcome = evaluate_knowledge_note(
        _candidate(
            _valid_markdown(),
            covered_coverage_input_ids=("ki_core", "ki_core", "ki_unknown"),
            omissions=(CoverageOmissionV1("ki_optional", ""),),
        )
    )

    check = _check(outcome, "coverage_ledger")
    assert check.status == "fail"
    assert set(check.related_ids) == {"ki_core", "ki_optional", "ki_unknown"}


def test_placeholders_and_unsafe_markdown_fail_without_hiding_other_results() -> None:
    markdown = """# Title

## Section

TODO: finish this.[^seg_000001]

<script>alert(1)</script>
"""
    outcome = evaluate_knowledge_note(_candidate(markdown))

    assert _check(outcome, "placeholders").status == "fail"
    assert _check(outcome, "markdown_safety").status == "fail"
    assert outcome.overall is QualityOverall.FAIL


def test_literal_code_does_not_forge_headings_citations_or_placeholders() -> None:
    markdown = """# Title

```markdown
# Forged title
## Repeated
TODO [^seg_999999]
```

## Real section

Real evidence.[^seg_000001]
"""
    outcome = evaluate_knowledge_note(
        _candidate(
            markdown,
            required_coverage_input_ids=(),
            covered_coverage_input_ids=(),
            omissions=(),
        )
    )

    assert outcome.overall is QualityOverall.PASS


def test_one_targeted_repair_reruns_all_gates_and_can_pass() -> None:
    calls = []

    def repair(request):
        calls.append(request)
        assert {check.check_id for check in request.failed_checks} == {
            "segment_citations",
            "substantive_h2_citations",
        }
        return _candidate(_valid_markdown())

    initial = _candidate("# Title\n\n## Section\n\nUncited text.\n")
    outcome = evaluate_knowledge_note(initial, repair=repair)

    assert len(calls) == 1
    assert outcome.repair_attempts == 1
    assert outcome.overall is QualityOverall.PASS
    assert outcome.publish_eligible is True
    assert all(check.status == "pass" for check in outcome.assessment.checks)


def test_failed_repair_is_not_retried_or_disguised_as_pass() -> None:
    calls = 0

    def repair(_request):
        nonlocal calls
        calls += 1
        return _candidate("# Title\n\n## Section\n\nStill uncited.\n")

    outcome = evaluate_knowledge_note(
        _candidate("# Title\n\n## Section\n\nUncited.\n"),
        repair=repair,
    )

    assert calls == 1
    assert outcome.repair_attempts == 1
    assert outcome.overall is QualityOverall.FAIL
    assert outcome.publish_eligible is False
    assert outcome.execution_error is None


def test_repair_cannot_expand_trusted_allow_sets() -> None:
    def repair(_request):
        return _candidate(
            _valid_markdown(),
            allowed_segment_ids=("seg_000001", "seg_000002", "seg_999999"),
        )

    outcome = evaluate_knowledge_note(
        _candidate("# Title\n\n## Section\n\nUncited.\n"),
        repair=repair,
    )

    assert outcome.overall is QualityOverall.FAIL
    assert outcome.publish_eligible is False
    assert outcome.repair_attempts == 1
    assert outcome.execution_error is not None
    assert outcome.execution_error.code == "knowledge_note_repair_failed"


def test_repair_exception_is_reported_and_original_failure_is_preserved() -> None:
    def repair(_request):
        raise RuntimeError("provider unavailable")

    outcome = evaluate_knowledge_note(
        _candidate("# Title\n\n## Section\n\nUncited.\n"),
        repair=repair,
    )

    assert outcome.overall is QualityOverall.FAIL
    assert outcome.publish_eligible is False
    assert outcome.repair_attempts == 1
    assert outcome.execution_error is not None
    assert outcome.execution_error.code == "knowledge_note_repair_failed"


def test_invalid_core_allow_set_is_rejected_as_input_not_quality() -> None:
    with pytest.raises(DomainError, match="knowledge_note_quality_input_invalid"):
        _candidate(_valid_markdown(), allowed_segment_ids=("seg_bad",))
