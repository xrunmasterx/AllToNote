from __future__ import annotations

from pathlib import Path

import pytest

from spikes.document_parsing.contracts import (
    BoundingBox,
    DocumentBlock,
    DocumentPage,
    ParsedDocument,
    make_block_id,
    text_hash,
)


def _block(page_number: int = 1) -> DocumentBlock:
    text = "A native line"
    return DocumentBlock(
        block_id=make_block_id(
            parser_id="test",
            page_number=page_number,
            reading_order=0,
            text=text,
        ),
        page_number=page_number,
        reading_order=0,
        kind="text",
        text=text,
        content_hash=text_hash(text),
        bbox=BoundingBox(1, 2, 30, 40),
    )


def test_spike_contract_serializes_parser_neutral_page_evidence() -> None:
    document = ParsedDocument(
        schema_version=1,
        source_name="sample.pdf",
        source_sha256="a" * 64,
        parser_id="test-parser",
        parser_version="1.0",
        pages=(DocumentPage(1, 612, 792, (_block(),)),),
        metadata={"title": "Sample"},
        warnings=(),
        duration_ms=10,
        peak_rss_bytes=100,
    )

    encoded = document.to_json_dict()

    assert encoded["page_count"] == 1
    assert encoded["block_count"] == 1
    assert encoded["native_text_chars"] == len("A native line")
    assert encoded["pages"][0]["blocks"][0]["bbox"]["origin"] == "top-left"


def test_spike_contract_rejects_cross_page_blocks_and_non_sha256_identity() -> None:
    with pytest.raises(ValueError, match="wrong page"):
        DocumentPage(2, 612, 792, (_block(1),))
    with pytest.raises(ValueError, match="full SHA-256"):
        ParsedDocument(
            1,
            "sample.pdf",
            "short",
            "test-parser",
            "1.0",
            (),
            {},
            (),
            0,
            0,
        )


def test_spike_contract_writes_utf8_json(tmp_path: Path) -> None:
    output = tmp_path / "parsed.json"
    ParsedDocument(
        1,
        "样本.pdf",
        "b" * 64,
        "test-parser",
        "1.0",
        (DocumentPage(1, 1, 1, (_block(),)),),
        {},
        (),
        0,
        0,
    ).write_json(output)

    assert '"source_name": "样本.pdf"' in output.read_text(encoding="utf-8")
