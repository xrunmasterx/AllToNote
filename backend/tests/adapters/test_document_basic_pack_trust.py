from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.adapters.documents.document_basic_pack_trust import (
    official_document_pack_trust_keys,
)


def test_official_document_pack_trust_root_is_public_only_and_self_consistent() -> None:
    key_id = "alltonote-document-basic-2026-01"
    trust_keys = official_document_pack_trust_keys()

    assert set(trust_keys) == {key_id}
    trust = trust_keys[key_id]
    assert trust.key_id == key_id
    assert trust.publisher == "alltonote-official"
    assert trust.public_key.hex() == (
        "cbb36968e7b17649b7b3ac0d8c41e34bb2f05ba1801604915f6c47db5ebf3f2b"
    )
    signature = base64.b64decode(
        "SW8P6pVOoBQcnZoByC+6mW61PytNa6UmQm9Mi/BvGFmWmT9owLs+HQjNC/"
        "SYyB4d3o20eCj/EHm/2CQGWfDuCw==",
        validate=True,
    )
    Ed25519PublicKey.from_public_bytes(trust.public_key).verify(
        signature,
        b"AllToNote document-basic trust root self-test v1",
    )
