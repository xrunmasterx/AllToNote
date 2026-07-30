from __future__ import annotations

import argparse
from pathlib import Path

from .contracts import sha256_file


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare one real PDF through a parser-neutral spike DTO."
    )
    parser.add_argument("--parser", choices=("pdfplumber", "docling"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifacts-path", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    source = args.input.resolve(strict=True)
    before = source.stat()
    before_hash = sha256_file(source)
    if args.parser == "pdfplumber":
        from .pdfplumber_adapter import parse

        parsed = parse(source)
    else:
        if args.artifacts_path is None:
            raise SystemExit("--artifacts-path is required for Docling")
        from .docling_adapter import parse

        parsed = parse(source, artifacts_path=args.artifacts_path.resolve(strict=True))
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("Parser modified the source PDF")
    if parsed.source_sha256 != before_hash or sha256_file(source) != before_hash:
        raise RuntimeError("Parser changed the source PDF content")
    parsed.write_json(args.output.resolve())
    print(
        f"{parsed.parser_id} {parsed.parser_version}: "
        f"{len(parsed.pages)} pages, "
        f"{sum(len(page.blocks) for page in parsed.pages)} blocks, "
        f"{parsed.duration_ms} ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
