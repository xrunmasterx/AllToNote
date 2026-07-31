from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.adapters.video_packs.official_video_pack import (
    MEDIA_BASIC,
    TRANSCRIBE_CPU,
)
from app.adapters.video_packs.official_video_pack_verifier import (
    PackTrustKey,
    verify_official_video_pack_source,
)
from tools.document_basic_pack_release import ReleaseError
from tools.video_pack_release import (
    assemble_video_pack,
    sign_video_pack,
)


_KEY_ID = "alltonote-video-pack-test-1"
_PRIVATE = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
_IDENTITY = {
    "implementation": "cpython",
    "platform": "win32",
    "machine": "x86_64",
    "pointer_bits": 64,
    "version": "3.11.15",
}


def _trust() -> dict[str, PackTrustKey]:
    return {
        _KEY_ID: PackTrustKey(
            key_id=_KEY_ID,
            publisher="alltonote-official",
            public_key=_PRIVATE.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            ),
        )
    }


def _python(root: Path, versions: dict[str, str]) -> Path:
    root.mkdir()
    (root / "python.exe").write_bytes(b"python")
    (root / "LICENSE.txt").write_text("PSF-2.0\n", encoding="utf-8")
    site = root / "Lib" / "site-packages"
    site.mkdir(parents=True)
    for name, version in versions.items():
        dist = site / f"{name.replace('-', '_')}-{version}.dist-info"
        (dist / "licenses").mkdir(parents=True)
        (dist / "METADATA").write_text(
            "\n".join(
                (
                    "Metadata-Version: 2.4",
                    f"Name: {name}",
                    f"Version: {version}",
                    "License-Expression: MIT",
                    "",
                )
            ),
            encoding="utf-8",
        )
        (dist / "licenses" / "LICENSE.txt").write_text(
            "fixture license\n",
            encoding="utf-8",
        )
    return root


def _model(root: Path) -> Path:
    root.mkdir()
    for name in (
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.txt",
    ):
        (root / name).write_bytes(f"fixture:{name}".encode())
    return root


@pytest.mark.parametrize("pack_id", ("media-basic", "transcribe-cpu"))
def test_assembled_and_signed_video_pack_is_verifier_accepted(
    tmp_path: Path,
    pack_id: str,
) -> None:
    contract = MEDIA_BASIC if pack_id == "media-basic" else TRANSCRIBE_CPU
    versions = (
        {"requests": "2.32.3", "yt-dlp": "2026.7.4"}
        if contract is MEDIA_BASIC
        else {
            "av": "14.2.0",
            "ctranslate2": "4.6.0",
            "faster-whisper": "1.1.1",
            "setuptools": "80.10.2",
            "tokenizers": "0.21.1",
        }
    )
    python = _python(tmp_path / "python", versions)
    assembled = tmp_path / "assembled"
    signed = tmp_path / "signed"
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_bytes(b"ffmpeg")
    ffprobe.write_bytes(b"ffprobe")

    assemble_video_pack(
        contract=contract,
        python_root=python,
        ffmpeg=ffmpeg if contract is MEDIA_BASIC else None,
        ffprobe=ffprobe if contract is MEDIA_BASIC else None,
        model_root=(
            _model(tmp_path / "model")
            if contract is TRANSCRIBE_CPU
            else None
        ),
        output=assembled,
        probe=lambda _contract, _root, _entrypoints: None,
        python_identity_probe=lambda _python: _IDENTITY,
    )
    verified = sign_video_pack(
        contract=contract,
        assembled_root=assembled,
        output=signed,
        key_id=_KEY_ID,
        private_key=_PRIVATE,
        trusted_keys=_trust(),
    )

    assert verified.manifest_sha256.startswith("sha256:")
    assert verify_official_video_pack_source(
        signed,
        contract=contract,
        trusted_keys=_trust(),
        platform_tag="windows-x86_64",
    ).manifest_sha256 == verified.manifest_sha256


def test_builder_rejects_dependency_drift_before_publication(
    tmp_path: Path,
) -> None:
    python = _python(
        tmp_path / "python",
        {"requests": "2.32.3", "yt-dlp": "future"},
    )
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_bytes(b"ffmpeg")
    ffprobe.write_bytes(b"ffprobe")
    output = tmp_path / "assembled"

    with pytest.raises(ReleaseError, match="yt-dlp must be 2026.7.4"):
        assemble_video_pack(
            contract=MEDIA_BASIC,
            python_root=python,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            output=output,
            probe=lambda _contract, _root, _entrypoints: None,
            python_identity_probe=lambda _python: _IDENTITY,
        )

    assert not output.exists()
