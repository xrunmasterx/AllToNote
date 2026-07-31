from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.adapters.documents.document_basic_pack import (
    DOCLING_SLIM_VERSION,
    NUMPY_VERSION,
    OPENCV_HEADLESS_VERSION,
    PACK_ID,
    PACK_VERSION,
    SCIPY_VERSION,
    TORCH_VERSION,
)
from app.adapters.documents.document_basic_pack_verifier import (
    DocumentPackTrustKey,
    canonical_manifest_bytes,
    verify_document_basic_pack_source,
)
from tools import document_basic_pack_release as release_tool
from tools.document_basic_pack_release import (
    ReleaseError,
    assemble_document_basic_pack,
    initialize_signing_key,
    load_signing_key,
    sign_document_basic_pack,
)


_KEY_ID = "alltonote-document-basic-test-1"
_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


class _MemoryVault:
    def __init__(self) -> None:
        self.persist: str | None = None
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password


def _public_bytes(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _trust(private_key: Ed25519PrivateKey = _PRIVATE_KEY) -> dict[str, DocumentPackTrustKey]:
    return {
        _KEY_ID: DocumentPackTrustKey(
            key_id=_KEY_ID,
            publisher="alltonote-official",
            public_key=_public_bytes(private_key),
        )
    }


def _write_sources(root: Path) -> tuple[Path, Path]:
    python_root = root / "prepared-python"
    site_packages = python_root / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    (python_root / "python.exe").write_bytes(b"prepared-cpython")
    (python_root / "LICENSE.txt").write_text("PSF license\n", encoding="utf-8")
    versions = {
        "docling-slim": DOCLING_SLIM_VERSION,
        "scipy": SCIPY_VERSION,
        "torch": TORCH_VERSION,
        "numpy": NUMPY_VERSION,
        "opencv-python-headless": OPENCV_HEADLESS_VERSION,
    }
    for name, version in versions.items():
        dist_info = site_packages / f"{name.replace('-', '_')}-{version}.dist-info"
        (dist_info / "licenses").mkdir(parents=True)
        license_metadata = (
            "License: BSD-3-Clause" if name == "scipy" else "License-Expression: MIT"
        )
        (dist_info / "METADATA").write_text(
            "\n".join(
                (
                    "Metadata-Version: 2.4",
                    f"Name: {name}",
                    f"Version: {version}",
                    license_metadata,
                    "License-File: LICENSE.txt",
                    "",
                )
            ),
            encoding="utf-8",
        )
        (dist_info / "licenses" / "LICENSE.txt").write_text(
            f"fixture license for {name}\n",
            encoding="utf-8",
        )
    tokenizers = site_packages / "tokenizers-0.22.2.dist-info"
    tokenizers.mkdir()
    (tokenizers / "METADATA").write_text(
        "\n".join(
            (
                "Metadata-Version: 2.4",
                "Name: tokenizers",
                "Version: 0.22.2",
                "Classifier: License :: OSI Approved :: Apache Software License",
                "",
            )
        ),
        encoding="utf-8",
    )
    native_notice = site_packages / "cv2" / "LICENSE-3RD-PARTY.txt"
    native_notice.parent.mkdir()
    native_notice.write_text("native fixture notice\n", encoding="utf-8")

    artifacts_root = root / "prepared-artifacts"
    layout = artifacts_root / "docling-project--docling-layout-heron"
    table = artifacts_root / "docling-project--docling-models"
    layout.mkdir(parents=True)
    table.mkdir(parents=True)
    (layout / "README.md").write_text("license: apache-2.0\n", encoding="utf-8")
    (layout / "model.bin").write_bytes(b"layout")
    (table / "README.md").write_text(
        "license: cdla-permissive-2.0\n",
        encoding="utf-8",
    )
    (table / "model.bin").write_bytes(b"table")
    return python_root, artifacts_root


def _assemble(source_root: Path, output: Path) -> None:
    python_root, artifacts_root = _write_sources(source_root)
    assemble_document_basic_pack(
        python_root=python_root,
        artifacts_root=artifacts_root,
        output=output,
        backend_root=Path(__file__).parents[2],
        doctor=lambda _python, _artifacts, _backend: None,
        python_identity_probe=lambda _python: {
            "implementation": "cpython",
            "platform": "win32",
            "machine": "x86_64",
            "pointer_bits": 64,
            "version": "3.11.15",
        },
    )


def test_signing_key_is_generated_only_in_vault_and_cannot_be_overwritten() -> None:
    vault = _MemoryVault()

    public_key = initialize_signing_key(_KEY_ID, vault=vault)

    assert vault.persist == "local machine"
    assert len(public_key) == 32
    assert _public_bytes(load_signing_key(_KEY_ID, vault=vault)) == public_key
    assert all(bytes(value, "utf-8").find(public_key) == -1 for value in vault.values.values())
    with pytest.raises(ReleaseError, match="already exists"):
        initialize_signing_key(_KEY_ID, vault=vault)


def test_builder_creates_deterministic_verifier_accepted_pack(tmp_path: Path) -> None:
    first_assembled = tmp_path / "first-assembled"
    second_assembled = tmp_path / "second-assembled"
    first = tmp_path / "first-signed"
    second = tmp_path / "second-signed"
    _assemble(tmp_path / "source-1", first_assembled)
    _assemble(tmp_path / "source-2", second_assembled)
    assert not (first_assembled / "manifest.json").exists()
    sign_document_basic_pack(
        assembled_root=first_assembled,
        output=first,
        key_id=_KEY_ID,
        private_key=_PRIVATE_KEY,
        trusted_keys=_trust(),
    )
    sign_document_basic_pack(
        assembled_root=second_assembled,
        output=second,
        key_id=_KEY_ID,
        private_key=_PRIVATE_KEY,
        trusted_keys=_trust(),
    )

    first_manifest = (first / "manifest.json").read_bytes()
    assert first_manifest == (second / "manifest.json").read_bytes()
    manifest = json.loads(first_manifest)
    assert first_manifest == canonical_manifest_bytes(manifest)
    assert manifest["pack_id"] == PACK_ID
    assert manifest["version"] == PACK_VERSION
    assert manifest["signature"]["key_id"] == _KEY_ID
    assert {item["component"] for item in manifest["licenses"]} == {
        "cpython",
        "docling-layout-heron",
        "docling-models",
        "native-and-vendored-runtime",
        "python-dependencies",
    }
    assert json.loads((first / "sbom.cdx.json").read_text(encoding="utf-8"))[
        "bomFormat"
    ] == "CycloneDX"
    assert json.loads(
        (first / "document-basic-lock.json").read_text(encoding="utf-8")
    )["python"]["version"] == "3.11.15"
    license_inventory = json.loads(
        (first / "licenses" / "python-packages.json").read_text(encoding="utf-8")
    )
    scipy = next(
        item for item in license_inventory["components"] if item["name"] == "scipy"
    )
    assert scipy["license_expression"] == "BSD-3-Clause"
    assert scipy["declared_license_files"] == ["LICENSE.txt"]
    assert scipy["license_files"]
    tokenizers = next(
        item
        for item in license_inventory["components"]
        if item["name"] == "tokenizers"
    )
    assert tokenizers["license_expression"] == "Apache-2.0"
    assert tokenizers["license_source"] == "classifier"
    native_inventory = json.loads(
        (first / "licenses" / "native-runtime-files.json").read_text(encoding="utf-8")
    )
    assert "python/Lib/site-packages/cv2/LICENSE-3RD-PARTY.txt" in native_inventory[
        "files"
    ]

    verified = verify_document_basic_pack_source(
        first,
        trusted_keys=_trust(),
        platform_tag="windows-x86_64",
    )
    assert verified.signature_key_id == _KEY_ID


def test_builder_fails_closed_before_publishing_for_wrong_trust_root(
    tmp_path: Path,
) -> None:
    python_root, artifacts_root = _write_sources(tmp_path / "source")
    assembled = tmp_path / "assembled"
    assemble_document_basic_pack(
        python_root=python_root,
        artifacts_root=artifacts_root,
        output=assembled,
        backend_root=Path(__file__).parents[2],
        doctor=lambda _python, _artifacts, _backend: None,
        python_identity_probe=lambda _python: {
            "implementation": "cpython",
            "platform": "win32",
            "machine": "x86_64",
            "pointer_bits": 64,
            "version": "3.11.15",
        },
    )
    other_key = Ed25519PrivateKey.generate()
    output = tmp_path / "output"

    with pytest.raises(ReleaseError, match="does not match"):
        sign_document_basic_pack(
            assembled_root=assembled,
            output=output,
            key_id=_KEY_ID,
            private_key=_PRIVATE_KEY,
            trusted_keys=_trust(other_key),
        )

    assert not output.exists()


def test_builder_rejects_hardlinked_source_file(tmp_path: Path) -> None:
    python_root, artifacts_root = _write_sources(tmp_path / "source")
    hardlink = python_root / "python-copy.exe"
    hardlink.hardlink_to(python_root / "python.exe")

    with pytest.raises(ReleaseError, match="unsafe filesystem entry"):
        assemble_document_basic_pack(
            python_root=python_root,
            artifacts_root=artifacts_root,
            output=tmp_path / "output",
            backend_root=Path(__file__).parents[2],
            doctor=lambda _python, _artifacts, _backend: None,
            python_identity_probe=lambda _python: {
                "implementation": "cpython",
                "platform": "win32",
                "machine": "x86_64",
                "pointer_bits": 64,
                "version": "3.11.15",
            },
        )


def test_signer_never_executes_pack_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assembled = tmp_path / "assembled"
    _assemble(tmp_path / "source", assembled)

    def reject_subprocess(*_args, **_kwargs):
        raise AssertionError("signer must not execute a subprocess")

    monkeypatch.setattr(subprocess, "run", reject_subprocess)
    signed = tmp_path / "signed"
    sign_document_basic_pack(
        assembled_root=assembled,
        output=signed,
        key_id=_KEY_ID,
        private_key=_PRIVATE_KEY,
        trusted_keys=_trust(),
    )

    assert (signed / "manifest.json").is_file()


def test_assembler_rejects_wrong_interpreter_identity(tmp_path: Path) -> None:
    python_root, artifacts_root = _write_sources(tmp_path / "source")
    output = tmp_path / "assembled"

    with pytest.raises(ReleaseError, match="Windows x86_64 CPython 3.11.15"):
        assemble_document_basic_pack(
            python_root=python_root,
            artifacts_root=artifacts_root,
            output=output,
            backend_root=Path(__file__).parents[2],
            doctor=lambda _python, _artifacts, _backend: None,
            python_identity_probe=lambda _python: {
                "implementation": "pypy",
                "platform": "linux",
                "machine": "arm64",
                "pointer_bits": 32,
                "version": "3.11.15",
            },
        )

    assert not output.exists()


def test_assembler_rejects_output_inside_input_before_creating_it(
    tmp_path: Path,
) -> None:
    python_root, artifacts_root = _write_sources(tmp_path / "source")
    output = python_root / "release-output"

    with pytest.raises(ReleaseError, match="disjoint"):
        assemble_document_basic_pack(
            python_root=python_root,
            artifacts_root=artifacts_root,
            output=output,
            backend_root=Path(__file__).parents[2],
            doctor=lambda _python, _artifacts, _backend: None,
            python_identity_probe=lambda _python: {
                "implementation": "cpython",
                "platform": "win32",
                "machine": "x86_64",
                "pointer_bits": 64,
                "version": "3.11.15",
            },
        )

    assert not output.exists()


def test_assembler_retries_transient_windows_publication_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python_root, artifacts_root = _write_sources(tmp_path / "source")
    output = tmp_path / "assembled"
    real_rename = release_tool.os.rename
    attempts = 0

    def flaky_rename(source, target):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("transient scanner lock")
        return real_rename(source, target)

    monkeypatch.setattr(release_tool.os, "rename", flaky_rename)
    monkeypatch.setattr(release_tool.time, "sleep", lambda _seconds: None)
    assemble_document_basic_pack(
        python_root=python_root,
        artifacts_root=artifacts_root,
        output=output,
        backend_root=Path(__file__).parents[2],
        doctor=lambda _python, _artifacts, _backend: None,
        python_identity_probe=lambda _python: {
            "implementation": "cpython",
            "platform": "win32",
            "machine": "x86_64",
            "pointer_bits": 64,
            "version": "3.11.15",
        },
    )

    assert attempts == 2
    assert output.is_dir()
