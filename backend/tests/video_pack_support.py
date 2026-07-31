from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.adapters.video_packs.official_video_pack import OfficialVideoPackContract
from app.adapters.video_packs.official_video_pack_verifier import (
    PackTrustKey,
    canonical_manifest_bytes,
)


PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(reversed(range(32))))
KEY_ID = "test-video-pack-key"


def trust_keys() -> dict[str, PackTrustKey]:
    public_key = PRIVATE_KEY.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        KEY_ID: PackTrustKey(
            key_id=KEY_ID,
            publisher="alltonote-official",
            public_key=public_key,
        )
    }


def write_pack_source(
    root: Path,
    contract: OfficialVideoPackContract,
    *,
    platform_tag: str,
) -> dict[str, object]:
    files = {
        relative_path: f"fixture:{relative_path}".encode()
        for relative_path in contract.fixture_file_paths(platform_tag)
    }
    for relative_path, content in files.items():
        target = root.joinpath(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def digest(relative_path: str) -> str:
        return "sha256:" + hashlib.sha256(files[relative_path]).hexdigest()

    manifest: dict[str, object] = {
        "manifest_version": 1,
        "pack_id": contract.pack_id,
        "version": contract.pack_version,
        "platform": platform_tag,
        "runtime_api": {"min": 1, "max": 1},
        "recipe_contracts": {
            recipe_id: list(versions)
            for recipe_id, versions in contract.recipe_contracts.items()
        },
        "capabilities": list(contract.capabilities),
        "entrypoints": [
            {
                "name": name,
                "type": "process",
                "relative_path": relative_path,
            }
            for name, relative_path in contract.entrypoints(platform_tag).items()
        ],
        "files": [
            {
                "path": relative_path,
                "byte_length": len(content),
                "sha256": digest(relative_path),
            }
            for relative_path, content in sorted(files.items())
        ],
        "licenses": [
            {
                "component": contract.pack_id,
                "spdx": "MIT",
                "file": f"licenses/{contract.pack_id}.txt",
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


def write_receipt(
    root: Path,
    contract: OfficialVideoPackContract,
    manifest_sha256: str,
) -> None:
    payload = {
        "schema_version": 1,
        "pack_id": contract.pack_id,
        "pack_version": contract.pack_version,
        "manifest_sha256": manifest_sha256,
        "verified": True,
    }
    (root / "receipt.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
