from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.adapters.documents.document_basic_pack_verifier import (
        DocumentPackTrustKey,
    )


def official_document_pack_trust_keys() -> Mapping[str, DocumentPackTrustKey]:
    """Return embedded release trust roots; empty until a release key is provisioned."""

    return {}


__all__ = ["official_document_pack_trust_keys"]
