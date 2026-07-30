from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.adapters.documents.document_basic_pack import PACK_ID, PACK_VERSION
from app.adapters.documents.document_basic_pack_verifier import (
    DocumentPackTrustKey,
    canonical_manifest_bytes,
    current_pack_platform,
)


PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
KEY_ID = "test-document-pack-key"


def trust_keys() -> dict[str, DocumentPackTrustKey]:
    public_key = PRIVATE_KEY.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        KEY_ID: DocumentPackTrustKey(
            key_id=KEY_ID,
            publisher="alltonote-official",
            public_key=public_key,
        )
    }


def write_pack_source(root: Path) -> dict[str, object]:
    python_relative = (
        "python/python.exe" if os.name == "nt" else "python/bin/python"
    )
    files = {
        python_relative: b"python",
        "artifacts/model.bin": b"model",
        "document-basic-lock.json": b"{}",
        "licenses/docling.txt": b"MIT",
        "sbom.cdx.json": b"{}",
    }
    for relative, content in files.items():
        target = root.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def digest(relative: str) -> str:
        content = root.joinpath(*relative.split("/")).read_bytes()
        return "sha256:" + hashlib.sha256(content).hexdigest()

    manifest: dict[str, object] = {
        "manifest_version": 1,
        "pack_id": PACK_ID,
        "version": PACK_VERSION,
        "platform": current_pack_platform(),
        "runtime_api": {"min": 1, "max": 1},
        "recipe_contracts": {"alltonote.document-note": [1]},
        "capabilities": ["recipe.document.parse.pdf"],
        "entrypoints": [
            {
                "name": "python",
                "type": "process",
                "relative_path": python_relative,
            }
        ],
        "files": [
            {
                "path": relative,
                "byte_length": len(content),
                "sha256": digest(relative),
            }
            for relative, content in sorted(files.items())
        ],
        "licenses": [
            {
                "component": "docling-fixture",
                "spdx": "MIT",
                "file": "licenses/docling.txt",
            }
        ],
        "sbom": {
            "format": "cyclonedx-json",
            "file": "sbom.cdx.json",
            "sha256": digest("sbom.cdx.json"),
        },
        "publisher": "alltonote-official",
    }
    signature = PRIVATE_KEY.sign(canonical_manifest_bytes(manifest))
    manifest["signature"] = {
        "algorithm": "ed25519",
        "key_id": KEY_ID,
        "value": base64.b64encode(signature).decode("ascii"),
    }
    (root / "manifest.json").write_bytes(canonical_manifest_bytes(manifest))
    return manifest
