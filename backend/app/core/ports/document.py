from pathlib import Path
from typing import Protocol

from app.core.domain.document import ParsedDocument


class DocumentParserPort(Protocol):
    def parse(self, source: Path, *, work_root: Path) -> ParsedDocument: ...


__all__ = ["DocumentParserPort"]
