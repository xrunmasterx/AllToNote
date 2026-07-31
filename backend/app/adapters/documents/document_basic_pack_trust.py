from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.adapters.documents.document_basic_pack_verifier import (
        DocumentPackTrustKey,
    )

_OFFICIAL_KEY_ID = "alltonote-document-basic-2026-01"
_OFFICIAL_PUBLIC_KEY = bytes.fromhex(
    "cbb36968e7b17649b7b3ac0d8c41e34bb2f05ba1801604915f6c47db5ebf3f2b"
)


def official_document_pack_trust_keys() -> Mapping[str, DocumentPackTrustKey]:
    """Return the public release roots accepted by this Runtime build."""

    from app.adapters.documents.document_basic_pack_verifier import (
        DocumentPackTrustKey,
    )

    return {
        _OFFICIAL_KEY_ID: DocumentPackTrustKey(
            key_id=_OFFICIAL_KEY_ID,
            publisher="alltonote-official",
            public_key=_OFFICIAL_PUBLIC_KEY,
        )
    }


__all__ = ["official_document_pack_trust_keys"]
