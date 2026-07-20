from __future__ import annotations

import json

import pytest

from app.core.domain.ids import sha256_digest
from app.core.errors import DomainError, ErrorCategory
from app.core.recipes.video.compilation.contracts import (
    KnowledgeImportance,
    KnowledgeItemKind,
    KnowledgeMapParserLimitsV1,
)
from app.core.recipes.video.compilation.pipeline import parse_chunk_knowledge_map


_TRANSCRIPT_SHA256 = sha256_digest(b"transcript")
_CHUNK_SHA256 = sha256_digest(b"chunk")
_ALLOWED_SEGMENTS = ("seg_000001", "seg_000002", "seg_000003")


def _limits(**changes: int) -> KnowledgeMapParserLimitsV1:
    values = {
        "max_response_bytes": 4_096,
        "max_items": 4,
        "max_title_characters": 80,
        "max_statement_characters": 400,
        "max_segment_refs_per_item": 3,
        "max_term_candidates": 4,
        "max_term_characters": 40,
        "max_warnings": 4,
        "max_warning_characters": 80,
    }
    values.update(changes)
    return KnowledgeMapParserLimitsV1(**values)


def _response(**changes: object) -> str:
    value: dict[str, object] = {
        "schema_version": 1,
        "chunk_ordinal": 2,
        "items": [
            {
                "item_ordinal": 0,
                "kind": "concept",
                "title": "Stable concept",
                "statement": "The source explains a reusable concept.",
                "importance": "core",
                "source_segment_ids": ["seg_000002", "seg_000001"],
            },
            {
                "item_ordinal": 1,
                "kind": "warning",
                "title": "Important limit",
                "statement": "The approach has a stated limit.",
                "importance": "supporting",
                "source_segment_ids": ["seg_000003"],
            },
        ],
        "term_candidates": ["Canonical term"],
        "warnings": ["Ambiguous acronym"],
    }
    value.update(changes)
    return json.dumps(value, ensure_ascii=False)


def _parse(response: str, **changes: object):
    values: dict[str, object] = {
        "stage_id": "knowledge-map",
        "stage_version": 1,
        "transcript_sha256": _TRANSCRIPT_SHA256,
        "chunk_ordinal": 2,
        "chunk_sha256": _CHUNK_SHA256,
        "allowed_segment_ids": _ALLOWED_SEGMENTS,
        "limits": _limits(),
    }
    values.update(changes)
    return parse_chunk_knowledge_map(response, **values)


def test_parser_builds_minimal_map_and_core_owned_stable_ids() -> None:
    first = _parse(_response())
    second = _parse(_response())

    assert first == second
    assert first.schema_version == 1
    assert first.chunk_ordinal == 2
    assert first.chunk_sha256 == _CHUNK_SHA256
    assert first.term_candidates == ("Canonical term",)
    assert first.warnings == ("Ambiguous acronym",)
    assert [item.kind for item in first.items] == [
        KnowledgeItemKind.CONCEPT,
        KnowledgeItemKind.WARNING,
    ]
    assert first.items[0].importance is KnowledgeImportance.CORE
    assert first.items[0].source_segment_ids == (
        "seg_000001",
        "seg_000002",
    )
    assert all(item.knowledge_item_id.startswith("ki_") for item in first.items)
    assert not any(
        item.knowledge_item_id.startswith(prefix)
        for item in first.items
        for prefix in ("art_", "ev_", "src_", "rev_")
    )


def test_parser_normalizes_a_complete_one_based_item_ordinal_sequence() -> None:
    value = json.loads(_response())
    for ordinal, item in enumerate(value["items"], start=1):
        item["item_ordinal"] = ordinal

    parsed = _parse(json.dumps(value, ensure_ascii=False))

    assert parsed == _parse(_response())


@pytest.mark.parametrize(
    "ordinals",
    (
        [0, 2],
        [1, 1],
        [2, 3],
        [True, 1],
        [0.0, 1.0],
    ),
)
def test_parser_rejects_noncanonical_item_ordinal_sequences(
    ordinals: list[object],
) -> None:
    value = json.loads(_response())
    for item, ordinal in zip(value["items"], ordinals, strict=True):
        item["item_ordinal"] = ordinal

    with pytest.raises(DomainError, match="knowledge_map_response_invalid"):
        _parse(json.dumps(value, ensure_ascii=False))


@pytest.mark.parametrize("ordinal", (False, True, 0.0, 1.0))
def test_parser_rejects_non_integer_single_item_ordinal(
    ordinal: object,
) -> None:
    item = json.loads(_response())["items"][0]
    item["item_ordinal"] = ordinal

    with pytest.raises(DomainError, match="knowledge_map_response_invalid"):
        _parse(_response(items=[item]))


def test_internal_id_binds_stage_transcript_chunk_item_and_content() -> None:
    baseline = _parse(_response()).items[0].knowledge_item_id
    variants = (
        _parse(_response(), stage_version=2).items[0].knowledge_item_id,
        _parse(
            _response(), transcript_sha256=sha256_digest(b"other-transcript")
        ).items[0].knowledge_item_id,
        _parse(
            _response(chunk_ordinal=3),
            chunk_ordinal=3,
            chunk_sha256=sha256_digest(b"other-chunk"),
        ).items[0].knowledge_item_id,
        _parse(
            _response(
                items=[
                    {
                        "item_ordinal": 0,
                        "kind": "concept",
                        "title": "Changed concept",
                        "statement": "The source explains a reusable concept.",
                        "importance": "core",
                        "source_segment_ids": ["seg_000001"],
                    }
                ]
            )
        ).items[0].knowledge_item_id,
    )
    assert len(set((baseline, *variants))) == 5


@pytest.mark.parametrize(
    ("source_ids", "code"),
    [
        (["seg_999999"], "knowledge_map_segment_unknown"),
        (["seg_000001", "seg_000001"], "knowledge_map_segment_duplicate"),
    ],
)
def test_parser_rejects_unknown_and_duplicate_segment_references(
    source_ids: list[str], code: str
) -> None:
    item = json.loads(_response())["items"][0]
    item["source_segment_ids"] = source_ids

    with pytest.raises(DomainError, match=code) as exc_info:
        _parse(_response(items=[item]))
    assert exc_info.value.category is ErrorCategory.RECIPE_FAILED


def test_same_segment_may_support_distinct_knowledge_items() -> None:
    value = json.loads(_response())
    value["items"][1]["source_segment_ids"] = ["seg_000001"]
    parsed = _parse(json.dumps(value))
    assert len(parsed.items) == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"artifact_id": "art_forged"}),
        lambda value: value["items"][0].update({"knowledge_item_id": "ki_forged"}),
        lambda value: value["items"][0].update({"evidence_id": "ev_forged"}),
    ],
)
def test_strict_fields_reject_model_forged_trusted_ids(mutate) -> None:
    value = json.loads(_response())
    mutate(value)
    with pytest.raises(DomainError, match="knowledge_map_response_invalid"):
        _parse(json.dumps(value))


@pytest.mark.parametrize(
    ("response", "limits"),
    [
        (_response(), _limits(max_response_bytes=100)),
        (_response(), _limits(max_items=1)),
        (_response(), _limits(max_title_characters=5)),
        (_response(), _limits(max_statement_characters=5)),
        (_response(), _limits(max_segment_refs_per_item=1)),
        (_response(term_candidates=["a", "b"]), _limits(max_term_candidates=1)),
        (_response(term_candidates=["too long"]), _limits(max_term_characters=3)),
        (_response(warnings=["a", "b"]), _limits(max_warnings=1)),
        (_response(warnings=["too long"]), _limits(max_warning_characters=3)),
    ],
)
def test_parser_rejects_every_bounded_dimension(
    response: str, limits: KnowledgeMapParserLimitsV1
) -> None:
    with pytest.raises(DomainError, match="knowledge_map_response_budget_exceeded"):
        _parse(response, limits=limits)


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        '{"schema_version":1,"schema_version":1}',
        '{"schema_version":NaN}',
        json.dumps({"schema_version": 1}),
        _response(schema_version=2),
        _response(chunk_ordinal=True),
        _response(items=[]),
        _response(items=[{"item_ordinal": 1}]),
        _response(term_candidates=["same", "same"]),
        _response(warnings=["same", "same"]),
    ],
)
def test_parser_fails_closed_for_malformed_or_contract_invalid_json(
    response: str,
) -> None:
    with pytest.raises(DomainError, match="knowledge_map_response_invalid"):
        _parse(response)


def test_parser_rejects_chunk_mismatch_and_invalid_parse_context() -> None:
    with pytest.raises(DomainError, match="knowledge_map_chunk_mismatch"):
        _parse(_response(chunk_ordinal=9))
    with pytest.raises(DomainError, match="knowledge_map_parse_context_invalid"):
        _parse(_response(), allowed_segment_ids=("seg_000001", "seg_000001"))


def test_parser_rejects_excessive_json_nesting_before_decode() -> None:
    nested = "[" * 12 + "0" + "]" * 12
    response = (
        '{"schema_version":1,"chunk_ordinal":2,"items":'
        + nested
        + ',"term_candidates":[],"warnings":[]}'
    )
    with pytest.raises(DomainError, match="knowledge_map_response_budget_exceeded"):
        _parse(response)
