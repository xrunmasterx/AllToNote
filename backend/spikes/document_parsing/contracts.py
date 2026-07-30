from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class BoundingBox:
    left: float
    top: float
    right: float
    bottom: float
    origin: str = "top-left"

    def __post_init__(self) -> None:
        if self.origin != "top-left":
            raise ValueError("Spike bounding boxes must use a top-left origin")
        if self.left > self.right or self.top > self.bottom:
            raise ValueError("Spike bounding box coordinates are inverted")


@dataclass(frozen=True, slots=True)
class DocumentBlock:
    block_id: str
    page_number: int
    reading_order: int
    kind: str
    text: str
    content_hash: str
    bbox: BoundingBox
    basis: str = "native"

    def __post_init__(self) -> None:
        if self.page_number < 1 or self.reading_order < 0:
            raise ValueError("Document block location is invalid")
        if not self.block_id or not self.kind or not self.text.strip():
            raise ValueError("Document block identity, kind, and text are required")
        if self.basis != "native":
            raise ValueError("The born-digital spike accepts native evidence only")


@dataclass(frozen=True, slots=True)
class DocumentPage:
    page_number: int
    width: float
    height: float
    blocks: tuple[DocumentBlock, ...]

    def __post_init__(self) -> None:
        if self.page_number < 1 or self.width <= 0 or self.height <= 0:
            raise ValueError("Document page geometry is invalid")
        if any(block.page_number != self.page_number for block in self.blocks):
            raise ValueError("Document block is attached to the wrong page")

    @property
    def native_text_chars(self) -> int:
        return sum(len(block.text) for block in self.blocks)


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    schema_version: int
    source_name: str
    source_sha256: str
    parser_id: str
    parser_version: str
    pages: tuple[DocumentPage, ...]
    metadata: Mapping[str, str]
    warnings: tuple[str, ...]
    duration_ms: int
    peak_rss_bytes: int

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("Only spike schema version 1 is supported")
        if len(self.source_sha256) != 64:
            raise ValueError("A full SHA-256 source identity is required")
        if not self.source_name or not self.parser_id or not self.parser_version:
            raise ValueError("Source and parser identities are required")
        expected_pages = tuple(range(1, len(self.pages) + 1))
        if tuple(page.page_number for page in self.pages) != expected_pages:
            raise ValueError("Document pages must be contiguous and one-based")
        if self.duration_ms < 0 or self.peak_rss_bytes < 0:
            raise ValueError("Parser resource measurements cannot be negative")

    def to_json_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["page_count"] = len(self.pages)
        value["block_count"] = sum(len(page.blocks) for page in self.pages)
        value["native_text_chars"] = sum(
            page.native_text_chars for page in self.pages
        )
        return value

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_json_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_block_id(
    *, parser_id: str, page_number: int, reading_order: int, text: str
) -> str:
    payload = f"{parser_id}\0{page_number}\0{reading_order}\0{text}".encode("utf-8")
    return "blk_" + hashlib.sha256(payload).hexdigest()[:24]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
