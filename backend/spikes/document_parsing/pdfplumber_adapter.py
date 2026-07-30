from __future__ import annotations

import importlib.metadata
import re
import time
from pathlib import Path

import psutil

from .contracts import (
    BoundingBox,
    DocumentBlock,
    DocumentPage,
    ParsedDocument,
    make_block_id,
    sha256_file,
    text_hash,
)


_WHITESPACE = re.compile(r"\s+")


def parse(path: Path) -> ParsedDocument:
    import pdfplumber

    started = time.perf_counter()
    source_hash = sha256_file(path)
    pages: list[DocumentPage] = []
    warnings: list[str] = []

    with pdfplumber.open(path) as pdf:
        metadata = {
            str(key): str(value)
            for key, value in (pdf.metadata or {}).items()
            if value is not None
        }
        for page_number, page in enumerate(pdf.pages, start=1):
            blocks: list[DocumentBlock] = []
            lines = page.extract_text_lines(layout=True, return_chars=False)
            for reading_order, line in enumerate(lines):
                text = _WHITESPACE.sub(" ", str(line.get("text", ""))).strip()
                if not text:
                    continue
                bbox = BoundingBox(
                    left=float(line["x0"]),
                    top=float(line["top"]),
                    right=float(line["x1"]),
                    bottom=float(line["bottom"]),
                )
                blocks.append(
                    DocumentBlock(
                        block_id=make_block_id(
                            parser_id="pdfplumber",
                            page_number=page_number,
                            reading_order=reading_order,
                            text=text,
                        ),
                        page_number=page_number,
                        reading_order=reading_order,
                        kind="text-line",
                        text=text,
                        content_hash=text_hash(text),
                        bbox=bbox,
                    )
                )
            if not blocks:
                warnings.append(f"page-{page_number}-has-no-native-text")
            pages.append(
                DocumentPage(
                    page_number=page_number,
                    width=float(page.width),
                    height=float(page.height),
                    blocks=tuple(blocks),
                )
            )

    memory = psutil.Process().memory_info()
    peak_rss = int(getattr(memory, "peak_wset", memory.rss))
    return ParsedDocument(
        schema_version=1,
        source_name=path.name,
        source_sha256=source_hash,
        parser_id="pdfplumber",
        parser_version=importlib.metadata.version("pdfplumber"),
        pages=tuple(pages),
        metadata=metadata,
        warnings=tuple(warnings),
        duration_ms=round((time.perf_counter() - started) * 1000),
        peak_rss_bytes=peak_rss,
    )
