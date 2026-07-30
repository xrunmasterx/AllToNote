from __future__ import annotations

import importlib.metadata
import time
from collections import defaultdict
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


def parse(path: Path, *, artifacts_path: Path) -> ParsedDocument:
    from docling.datamodel.accelerator_options import (
        AcceleratorDevice,
        AcceleratorOptions,
    )
    from docling.datamodel.base_models import ConversionStatus, InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling_core.types.doc import TableItem

    started = time.perf_counter()
    source_hash = sha256_file(path)
    options = PdfPipelineOptions(
        artifacts_path=artifacts_path,
        accelerator_options=AcceleratorOptions(
            num_threads=4,
            device=AcceleratorDevice.CPU,
        ),
        do_ocr=False,
        do_table_structure=False,
        enable_remote_services=False,
        allow_external_plugins=False,
    )
    converter = DocumentConverter(
        allowed_formats=[InputFormat.PDF],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=options),
        },
    )
    result = converter.convert(path, raises_on_error=True)
    if result.status not in (
        ConversionStatus.SUCCESS,
        ConversionStatus.PARTIAL_SUCCESS,
    ):
        raise RuntimeError(f"Docling conversion ended with {result.status.value}")

    document = result.document
    blocks_by_page: dict[int, list[DocumentBlock]] = defaultdict(list)
    page_sizes = {
        int(number): (float(page.size.width), float(page.size.height))
        for number, page in document.pages.items()
    }
    for item, _level in document.iterate_items():
        item_text = str(getattr(item, "text", "")).strip()
        table_text = (
            item.export_to_markdown(document).strip()
            if not item_text and isinstance(item, TableItem)
            else ""
        )
        if not item_text and not table_text:
            continue
        kind = str(item.label.value)
        for provenance in item.prov:
            page_number = int(provenance.page_no)
            page_size = page_sizes.get(page_number)
            if page_size is None:
                continue
            bbox = provenance.bbox.to_top_left_origin(page_size[1])
            reading_order = len(blocks_by_page[page_number])
            text = table_text or item_text
            if item_text and len(item.prov) > 1:
                start, end = provenance.charspan
                if 0 <= start < end <= len(item_text):
                    text = item_text[start:end].strip()
            if not text:
                continue
            blocks_by_page[page_number].append(
                DocumentBlock(
                    block_id=make_block_id(
                        parser_id="docling",
                        page_number=page_number,
                        reading_order=reading_order,
                        text=text,
                    ),
                    page_number=page_number,
                    reading_order=reading_order,
                    kind=kind,
                    text=text,
                    content_hash=text_hash(text),
                    bbox=BoundingBox(
                        left=float(bbox.l),
                        top=float(bbox.t),
                        right=float(bbox.r),
                        bottom=float(bbox.b),
                    ),
                )
            )

    warnings = [
        f"{error.error_message}"
        for error in result.errors
        if getattr(error, "error_message", None)
    ]
    pages = tuple(
        DocumentPage(
            page_number=page_number,
            width=page_sizes[page_number][0],
            height=page_sizes[page_number][1],
            blocks=tuple(blocks_by_page[page_number]),
        )
        for page_number in sorted(page_sizes)
    )
    metadata = {"document_name": document.name, "status": result.status.value}
    memory = psutil.Process().memory_info()
    peak_rss = int(getattr(memory, "peak_wset", memory.rss))
    return ParsedDocument(
        schema_version=1,
        source_name=path.name,
        source_sha256=source_hash,
        parser_id="docling",
        parser_version=importlib.metadata.version("docling-slim"),
        pages=pages,
        metadata=metadata,
        warnings=tuple(warnings),
        duration_ms=round((time.perf_counter() - started) * 1000),
        peak_rss_bytes=peak_rss,
    )
