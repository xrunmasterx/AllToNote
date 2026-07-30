from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from collections import defaultdict
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifacts-path", type=Path, required=True)
    parser.add_argument("--expected-parser-version", required=True)
    parser.add_argument("--expected-model-revision", required=True)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    source = args.input.resolve(strict=True)
    before = (source.stat().st_size, source.stat().st_mtime_ns, _sha256(source))

    from docling.datamodel.accelerator_options import (
        AcceleratorDevice,
        AcceleratorOptions,
    )
    from docling.datamodel.base_models import ConversionStatus, InputFormat
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions,
        TableFormerMode,
        TableStructureOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling_core.types.doc import TableItem

    version = importlib.metadata.version("docling-slim")
    if version != args.expected_parser_version:
        raise RuntimeError("Docling Pack version mismatch")
    options = PdfPipelineOptions(
        artifacts_path=args.artifacts_path.resolve(strict=True),
        accelerator_options=AcceleratorOptions(
            num_threads=4,
            device=AcceleratorDevice.CPU,
        ),
        do_ocr=False,
        do_table_structure=True,
        table_structure_options=TableStructureOptions(
            do_cell_matching=True,
            mode=TableFormerMode.ACCURATE,
        ),
        enable_remote_services=False,
        allow_external_plugins=False,
    )
    converter = DocumentConverter(
        allowed_formats=[InputFormat.PDF],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
    )
    result = converter.convert(source, raises_on_error=True)
    if result.status not in (
        ConversionStatus.SUCCESS,
        ConversionStatus.PARTIAL_SUCCESS,
    ):
        raise RuntimeError("Docling conversion failed")

    document = result.document
    page_sizes = {
        int(number): (float(page.size.width), float(page.size.height))
        for number, page in document.pages.items()
    }
    blocks_by_page: dict[int, list[dict[str, object]]] = defaultdict(list)
    for item, _level in document.iterate_items():
        item_text = str(getattr(item, "text", "")).strip()
        table_text = (
            item.export_to_markdown(document).strip()
            if not item_text and isinstance(item, TableItem)
            else ""
        )
        if not item_text and not table_text:
            continue
        for provenance in item.prov:
            page_number = int(provenance.page_no)
            page_size = page_sizes.get(page_number)
            if page_size is None:
                continue
            text = table_text or item_text
            if item_text and len(item.prov) > 1:
                start, end = provenance.charspan
                if 0 <= start < end <= len(item_text):
                    text = item_text[start:end].strip()
            if not text:
                continue
            reading_order = len(blocks_by_page[page_number])
            bbox = provenance.bbox.to_top_left_origin(page_size[1])
            block_digest = hashlib.sha256(
                f"{page_number}\0{reading_order}\0{text}".encode("utf-8")
            ).hexdigest()
            blocks_by_page[page_number].append(
                {
                    "block_id": "blk_" + block_digest[:24],
                    "page_number": page_number,
                    "reading_order": reading_order,
                    "kind": str(item.label.value),
                    "text": text,
                    "content_sha256": "sha256:"
                    + hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "bbox": {
                        "left": float(bbox.l),
                        "top": float(bbox.t),
                        "right": float(bbox.r),
                        "bottom": float(bbox.b),
                        "origin": "top-left",
                    },
                    "basis": "native",
                }
            )

    after = (source.stat().st_size, source.stat().st_mtime_ns, _sha256(source))
    if after != before:
        raise RuntimeError("Source PDF changed during parsing")
    payload = {
        "schema_version": 1,
        "source_sha256": before[2],
        "source_name": source.name,
        "parser_id": "docling",
        "parser_version": version,
        "model_revision": args.expected_model_revision,
        "pages": [
            {
                "page_number": number,
                "width": page_sizes[number][0],
                "height": page_sizes[number][1],
                "blocks": blocks_by_page[number],
            }
            for number in sorted(page_sizes)
        ],
        "metadata": {
            "document_name": document.name,
            "status": result.status.value,
        },
        "warnings": [
            error.error_message
            for error in result.errors
            if getattr(error, "error_message", None)
        ],
    }
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
