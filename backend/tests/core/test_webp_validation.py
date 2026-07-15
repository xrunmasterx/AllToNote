from __future__ import annotations

import base64

import pytest

from app.core.application.video_checkpoints import (
    decode_screenshots,
    encode_object,
    encode_screenshots,
)
from app.core.errors import DomainError
from app.core.portable.bundle_assembler import DisplayAssetInput
from app.core.portable.webp import is_valid_webp


VALID_WEBP = bytes.fromhex(
    "524946461a000000574542505650384c0d0000002f00000010071011118888fe0700"
)
ARTIFACT_ID = "art_018cc251-f400-7000-8000-000000000003"


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return tag + len(payload).to_bytes(4, "little") + payload + (b"\0" if len(payload) & 1 else b"")


def _vp8x(flags: int) -> bytes:
    return bytes((flags, 0, 0, 0)) + b"\0\0\0" + b"\0\0\0"


def _vp8() -> bytes:
    return b"\0\0\0\x9d\x01\x2a\x01\0\x01\0"


def _vp8l(*, alpha: bool) -> bytes:
    header = 1 << 28 if alpha else 0
    return b"\x2f" + header.to_bytes(4, "little")


def _extended(flags: int, *chunks: tuple[bytes, bytes]) -> bytes:
    body = _chunk(b"VP8X", _vp8x(flags)) + b"".join(
        _chunk(tag, value) for tag, value in chunks
    )
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WEBP" + body


def _asset() -> DisplayAssetInput:
    return DisplayAssetInput(
        ARTIFACT_ID, f"assets/{ARTIFACT_ID}.webp", "image/webp", VALID_WEBP
    )


@pytest.mark.parametrize(
    "payload",
    (
        b"RIFF\x0c\x00\x00\x00WEBPVP8L\x00\x00\x00\x00",
        b"RIFF\x0e\x00\x00\x00WEBPVP8L\x05\x00\x00\x00\x2f\x00\x00\x00\xe0\x01",
        b"RIFF\x10\x00\x00\x00WEBPVP8 \x04\x00\x00\x00\x00\x00\x00\x00",
        b"RIFF\x16\x00\x00\x00WEBPVP8X\x0a\x00\x00\x00" + b"\0" * 10,
    ),
)
def test_incomplete_or_imageless_webp_is_rejected_everywhere(payload: bytes) -> None:
    assert not is_valid_webp(payload)
    with pytest.raises(DomainError, match="payload is not valid WebP"):
        DisplayAssetInput(
            ARTIFACT_ID, f"assets/{ARTIFACT_ID}.webp", "image/webp", payload
        )


def test_genuine_tiny_webp_and_historical_empty_checkpoint_round_trip() -> None:
    assert is_valid_webp(VALID_WEBP)
    assert decode_screenshots(encode_screenshots((_asset(),))) == (_asset(),)
    assert decode_screenshots(encode_screenshots(())) == ()


@pytest.mark.parametrize("mutation", ("extra", "missing", "wrong_type", "duplicate"))
def test_checkpoint_assets_require_exact_typed_unique_records(mutation: str) -> None:
    item = {
        "artifact_id": ARTIFACT_ID,
        "relative_path": f"assets/{ARTIFACT_ID}.webp",
        "media_type": "image/webp",
        "payload_base64": base64.b64encode(VALID_WEBP).decode("ascii"),
        "artifact_type": "evidence.asset.v1",
    }
    assets = [dict(item)]
    if mutation == "extra":
        assets[0]["extra"] = "no"
    elif mutation == "missing":
        del assets[0]["media_type"]
    elif mutation == "wrong_type":
        assets[0]["payload_base64"] = 7
    else:
        assets.append(dict(item))
    payload = encode_object({"step": "optional_screenshots", "assets": assets})
    with pytest.raises(DomainError, match="checkpoint_content_invalid"):
        decode_screenshots(payload)


def test_extended_metadata_sequence_is_accepted() -> None:
    payload = _extended(
        0x20 | 0x08 | 0x04,
        (b"ICCP", b"profile"),
        (b"VP8 ", _vp8()),
        (b"EXIF", b"exif"),
        (b"XMP ", b"xmp"),
    )

    assert is_valid_webp(payload)


def test_extended_lossy_alpha_sequence_is_accepted() -> None:
    assert is_valid_webp(
        _extended(0x10, (b"ALPH", b"alpha"), (b"VP8 ", _vp8()))
    )


@pytest.mark.parametrize(
    "chunks",
    (
        ((b"ALPH", b"alpha"), (b"ICCP", b"profile"), (b"VP8 ", _vp8())),
        ((b"ALPH", b"alpha"), (b"EXIF", b"exif"), (b"VP8 ", _vp8())),
        ((b"VP8 ", _vp8()), (b"XMP ", b"xmp"), (b"EXIF", b"exif")),
    ),
)
def test_extended_chunk_order_must_match_supported_sequence(
    chunks: tuple[tuple[bytes, bytes], ...],
) -> None:
    flags = 0x10 | 0x20 if chunks[0][0] == b"ALPH" and chunks[1][0] == b"ICCP" else (
        0x10 | 0x08 if chunks[0][0] == b"ALPH" else 0x04 | 0x08
    )

    assert not is_valid_webp(_extended(flags, *chunks))


@pytest.mark.parametrize(("intrinsic_alpha", "alpha_flag"), ((False, False), (True, True)))
def test_extended_vp8l_alpha_flag_matches_intrinsic_header(
    intrinsic_alpha: bool, alpha_flag: bool
) -> None:
    flags = 0x10 if alpha_flag else 0
    assert is_valid_webp(_extended(flags, (b"VP8L", _vp8l(alpha=intrinsic_alpha))))


@pytest.mark.parametrize(("intrinsic_alpha", "alpha_flag"), ((False, True), (True, False)))
def test_extended_vp8l_alpha_flag_mismatch_is_rejected(
    intrinsic_alpha: bool, alpha_flag: bool
) -> None:
    flags = 0x10 if alpha_flag else 0
    assert not is_valid_webp(_extended(flags, (b"VP8L", _vp8l(alpha=intrinsic_alpha))))
