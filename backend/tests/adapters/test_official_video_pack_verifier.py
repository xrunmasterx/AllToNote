from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.adapters.video_packs.official_video_pack import (
    MEDIA_BASIC,
    TRANSCRIBE_CPU,
)
from app.adapters.video_packs.official_video_pack_verifier import (
    verify_official_video_pack_generation,
    verify_official_video_pack_source,
)
from app.core.errors import DomainError
from tests.video_pack_support import trust_keys, write_pack_source, write_receipt


PLATFORM = "windows-x86_64"


@pytest.mark.parametrize("contract", (MEDIA_BASIC, TRANSCRIBE_CPU))
def test_fixed_official_video_pack_source_verifies(
    tmp_path: Path,
    contract,
) -> None:
    write_pack_source(tmp_path, contract, platform_tag=PLATFORM)

    verified = verify_official_video_pack_source(
        tmp_path,
        contract=contract,
        trusted_keys=trust_keys(),
        platform_tag=PLATFORM,
    )

    assert verified.pack_id == contract.pack_id
    assert verified.pack_version == contract.pack_version
    assert verified.platform == PLATFORM
    assert dict(verified.entrypoints) == contract.entrypoints(PLATFORM)
    assert verified.manifest_sha256.startswith("sha256:")


@pytest.mark.parametrize(
    ("contract", "field", "value", "code"),
    (
        (MEDIA_BASIC, "pack_id", "other-pack", "pack_manifest_invalid"),
        (MEDIA_BASIC, "version", "next", "pack_version_unsupported"),
        (MEDIA_BASIC, "platform", "linux-x86_64", "pack_platform_incompatible"),
        (TRANSCRIBE_CPU, "capabilities", [], "pack_manifest_invalid"),
    ),
)
def test_fixed_contract_fields_fail_closed(
    tmp_path: Path,
    contract,
    field: str,
    value: object,
    code: str,
) -> None:
    manifest = write_pack_source(tmp_path, contract, platform_tag=PLATFORM)
    manifest[field] = value
    (tmp_path / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(DomainError) as caught:
        verify_official_video_pack_source(
            tmp_path,
            contract=contract,
            trusted_keys=trust_keys(),
            platform_tag=PLATFORM,
        )

    assert caught.value.code == code


def test_signature_and_file_hash_are_verified(tmp_path: Path) -> None:
    write_pack_source(tmp_path, MEDIA_BASIC, platform_tag=PLATFORM)
    ffmpeg = tmp_path.joinpath(*MEDIA_BASIC.entrypoints(PLATFORM)["ffmpeg"].split("/"))
    ffmpeg.write_bytes(b"tampered")

    with pytest.raises(DomainError) as caught:
        verify_official_video_pack_source(
            tmp_path,
            contract=MEDIA_BASIC,
            trusted_keys=trust_keys(),
            platform_tag=PLATFORM,
        )

    assert caught.value.code == "pack_hash_mismatch"


def test_extra_file_is_rejected(tmp_path: Path) -> None:
    write_pack_source(tmp_path, MEDIA_BASIC, platform_tag=PLATFORM)
    (tmp_path / "undeclared.bin").write_bytes(b"extra")

    with pytest.raises(DomainError) as caught:
        verify_official_video_pack_source(
            tmp_path,
            contract=MEDIA_BASIC,
            trusted_keys=trust_keys(),
            platform_tag=PLATFORM,
        )

    assert caught.value.code == "pack_manifest_invalid"


def test_generation_requires_exact_install_receipt(tmp_path: Path) -> None:
    write_pack_source(tmp_path, TRANSCRIBE_CPU, platform_tag=PLATFORM)
    verified = verify_official_video_pack_source(
        tmp_path,
        contract=TRANSCRIBE_CPU,
        trusted_keys=trust_keys(),
        platform_tag=PLATFORM,
    )

    with pytest.raises(DomainError) as caught:
        verify_official_video_pack_generation(
            tmp_path,
            contract=TRANSCRIBE_CPU,
            trusted_keys=trust_keys(),
            platform_tag=PLATFORM,
        )
    assert caught.value.code == "pack_manifest_invalid"

    write_receipt(
        tmp_path,
        TRANSCRIBE_CPU,
        verified.manifest_sha256,
    )
    generation = verify_official_video_pack_generation(
        tmp_path,
        contract=TRANSCRIBE_CPU,
        trusted_keys=trust_keys(),
        platform_tag=PLATFORM,
    )
    assert generation == verified
