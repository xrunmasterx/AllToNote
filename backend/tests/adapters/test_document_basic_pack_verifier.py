from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.adapters.documents.document_basic_pack import PACK_ID, PACK_VERSION
from app.adapters.documents.document_basic_pack_verifier import (
    DocumentPackTrustKey,
    canonical_manifest_bytes,
    current_pack_platform,
    verify_document_basic_pack_generation,
    verify_document_basic_pack_source,
)
from app.core.errors import DomainError


_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
_KEY_ID = "test-document-pack-key"


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _trust() -> dict[str, DocumentPackTrustKey]:
    public_key = _PRIVATE_KEY.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        _KEY_ID: DocumentPackTrustKey(
            key_id=_KEY_ID,
            publisher="alltonote-official",
            public_key=public_key,
        )
    }


def _source(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "document-basic-source"
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
    file_entries = [
        {
            "path": relative,
            "byte_length": len(content),
            "sha256": _sha256(root.joinpath(*relative.split("/"))),
        }
        for relative, content in sorted(files.items())
    ]
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
        "files": file_entries,
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
            "sha256": _sha256(root / "sbom.cdx.json"),
        },
        "publisher": "alltonote-official",
    }
    signature = _PRIVATE_KEY.sign(canonical_manifest_bytes(manifest))
    manifest["signature"] = {
        "algorithm": "ed25519",
        "key_id": _KEY_ID,
        "value": base64.b64encode(signature).decode("ascii"),
    }
    (root / "manifest.json").write_text(
        canonical_manifest_bytes(manifest).decode("utf-8"),
        encoding="utf-8",
    )
    return root, manifest


def _write_manifest(root: Path, manifest: dict[str, object]) -> None:
    unsigned = dict(manifest)
    unsigned.pop("signature", None)
    signature = _PRIVATE_KEY.sign(canonical_manifest_bytes(unsigned))
    manifest["signature"] = {
        "algorithm": "ed25519",
        "key_id": _KEY_ID,
        "value": base64.b64encode(signature).decode("ascii"),
    }
    (root / "manifest.json").write_text(
        canonical_manifest_bytes(manifest).decode("utf-8"),
        encoding="utf-8",
    )


def test_verifier_accepts_exact_signed_document_pack_directory(
    tmp_path: Path,
) -> None:
    root, _manifest = _source(tmp_path)

    verified = verify_document_basic_pack_source(root, trusted_keys=_trust())

    assert verified.pack_id == PACK_ID
    assert verified.pack_version == PACK_VERSION
    assert verified.platform == current_pack_platform()
    assert verified.manifest_sha256.startswith("sha256:")
    assert verified.python_relative_path in {"python/python.exe", "python/bin/python"}
    assert len(verified.files) == 5


def test_verifier_rejects_noncanonical_manifest_bytes(tmp_path: Path) -> None:
    root, manifest = _source(tmp_path)
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(DomainError) as raised:
        verify_document_basic_pack_source(root, trusted_keys=_trust())

    assert raised.value.code == "pack_manifest_invalid"


def test_generation_verifier_requires_exact_verified_receipt(tmp_path: Path) -> None:
    root, _manifest = _source(tmp_path)
    verified = verify_document_basic_pack_source(root, trusted_keys=_trust())
    (root / "receipt.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pack_id": PACK_ID,
                "pack_version": PACK_VERSION,
                "manifest_sha256": verified.manifest_sha256,
                "verified": True,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    generation = verify_document_basic_pack_generation(root, trusted_keys=_trust())

    assert generation.manifest_sha256 == verified.manifest_sha256

    receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
    receipt["verified"] = False
    (root / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(DomainError) as raised:
        verify_document_basic_pack_generation(root, trusted_keys=_trust())
    assert raised.value.code == "pack_manifest_invalid"

    receipt["verified"] = True
    (root / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with pytest.raises(DomainError) as raised:
        verify_document_basic_pack_generation(root, trusted_keys=_trust())
    assert raised.value.code == "pack_manifest_invalid"


def test_verifier_rejects_tampered_file_with_policy_error(tmp_path: Path) -> None:
    root, _manifest = _source(tmp_path)
    (root / "artifacts" / "model.bin").write_bytes(b"tampered")

    with pytest.raises(DomainError) as raised:
        verify_document_basic_pack_source(root, trusted_keys=_trust())

    assert raised.value.code == "pack_hash_mismatch"


def test_verifier_rejects_invalid_signature(tmp_path: Path) -> None:
    root, manifest = _source(tmp_path)
    signature = dict(manifest["signature"])
    signature["value"] = base64.b64encode(b"x" * 64).decode("ascii")
    manifest["signature"] = signature
    (root / "manifest.json").write_bytes(canonical_manifest_bytes(manifest))

    with pytest.raises(DomainError) as raised:
        verify_document_basic_pack_source(root, trusted_keys=_trust())

    assert raised.value.code == "pack_signature_invalid"


def test_verifier_rejects_untrusted_publisher(tmp_path: Path) -> None:
    root, manifest = _source(tmp_path)
    manifest["publisher"] = "other-publisher"
    (root / "manifest.json").write_bytes(canonical_manifest_bytes(manifest))

    with pytest.raises(DomainError) as raised:
        verify_document_basic_pack_source(root, trusted_keys=_trust())

    assert raised.value.code == "pack_publisher_untrusted"


def test_verifier_fails_closed_without_runtime_trust(tmp_path: Path) -> None:
    root, _manifest = _source(tmp_path)

    with pytest.raises(DomainError) as raised:
        verify_document_basic_pack_source(root, trusted_keys={})

    assert raised.value.code == "pack_trust_unconfigured"


def test_verifier_rejects_wrong_platform_before_activation(tmp_path: Path) -> None:
    root, manifest = _source(tmp_path)
    manifest["platform"] = "other-os-other-arch"
    _write_manifest(root, manifest)

    with pytest.raises(DomainError) as raised:
        verify_document_basic_pack_source(root, trusted_keys=_trust())

    assert raised.value.code == "pack_platform_incompatible"


@pytest.mark.parametrize(
    "unsafe_path",
    (
        "../escape.bin",
        "C:/escape.bin",
        "//server/share.bin",
        "python/file:stream",
        "python\\backslash.exe",
        "python/CON",
    ),
)
def test_verifier_rejects_unsafe_manifest_paths(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    root, manifest = _source(tmp_path)
    manifest["files"][0]["path"] = unsafe_path
    _write_manifest(root, manifest)

    with pytest.raises(DomainError) as raised:
        verify_document_basic_pack_source(root, trusted_keys=_trust())

    assert raised.value.code == "pack_manifest_invalid"


def test_verifier_rejects_case_colliding_manifest_paths(tmp_path: Path) -> None:
    root, manifest = _source(tmp_path)
    duplicate = dict(manifest["files"][0])
    duplicate["path"] = str(duplicate["path"]).upper()
    manifest["files"].append(duplicate)
    _write_manifest(root, manifest)

    with pytest.raises(DomainError) as raised:
        verify_document_basic_pack_source(root, trusted_keys=_trust())

    assert raised.value.code == "pack_manifest_invalid"


def test_verifier_rejects_unlisted_source_file(tmp_path: Path) -> None:
    root, _manifest = _source(tmp_path)
    (root / "unexpected.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(DomainError) as raised:
        verify_document_basic_pack_source(root, trusted_keys=_trust())

    assert raised.value.code == "pack_manifest_invalid"


def test_verifier_rejects_boolean_manifest_version(tmp_path: Path) -> None:
    root, manifest = _source(tmp_path)
    manifest["manifest_version"] = True
    _write_manifest(root, manifest)

    with pytest.raises(DomainError) as raised:
        verify_document_basic_pack_source(root, trusted_keys=_trust())

    assert raised.value.code == "pack_manifest_invalid"


def test_verifier_rejects_duplicate_manifest_keys(tmp_path: Path) -> None:
    root, _manifest = _source(tmp_path)
    manifest_path = root / "manifest.json"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            '"manifest_version":1',
            '"manifest_version":1,"manifest_version":1',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(DomainError) as raised:
        verify_document_basic_pack_source(root, trusted_keys=_trust())

    assert raised.value.code == "pack_manifest_invalid"


def test_verifier_rejects_linked_source_root(tmp_path: Path) -> None:
    root, _manifest = _source(tmp_path)
    linked_root = tmp_path / "linked-source"
    try:
        linked_root.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")

    with pytest.raises(DomainError) as raised:
        verify_document_basic_pack_source(linked_root, trusted_keys=_trust())

    assert raised.value.code == "pack_archive_unsafe"
