from pathlib import Path
from typing import Protocol

from app.core.domain.document import ParsedDocument
from app.core.ports.source import CancellationTokenPort


class DocumentParserPort(Protocol):
    def parse(
        self,
        source: Path,
        *,
        work_root: Path,
        cancellation_token: CancellationTokenPort,
    ) -> ParsedDocument: ...


__all__ = ["DocumentParserPort"]
