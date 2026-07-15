from __future__ import annotations


_IMAGE_CHUNKS = {b"VP8 ", b"VP8L"}
_EXTENDED_CHUNKS = {b"ICCP", b"ALPH", b"VP8 ", b"VP8L", b"EXIF", b"XMP "}


def _valid_vp8(payload: bytes) -> bool:
    if len(payload) < 10 or payload[0] & 1 or payload[3:6] != b"\x9d\x01\x2a":
        return False
    width = int.from_bytes(payload[6:8], "little") & 0x3FFF
    height = int.from_bytes(payload[8:10], "little") & 0x3FFF
    return width > 0 and height > 0


def _vp8l_alpha(payload: bytes) -> bool | None:
    if len(payload) < 5 or payload[0] != 0x2F:
        return None
    header = int.from_bytes(payload[1:5], "little")
    if header >> 29 != 0:
        return None
    return bool(header & (1 << 28))


def is_valid_webp(payload: bytes) -> bool:
    """Validate the complete, still-image WebP container accepted by AllToNote."""
    if (
        type(payload) is not bytes
        or len(payload) < 25
        or payload[:4] != b"RIFF"
        or payload[8:12] != b"WEBP"
        or int.from_bytes(payload[4:8], "little") != len(payload) - 8
    ):
        return False

    chunks: list[tuple[bytes, bytes]] = []
    offset = 12
    while offset < len(payload):
        if offset + 8 > len(payload):
            return False
        tag = payload[offset : offset + 4]
        length = int.from_bytes(payload[offset + 4 : offset + 8], "little")
        if length == 0:
            return False
        start = offset + 8
        end = start + length
        padded_end = end + (length & 1)
        if end > len(payload) or padded_end > len(payload):
            return False
        if length & 1 and payload[end] != 0:
            return False
        chunks.append((tag, payload[start:end]))
        offset = padded_end
    if not chunks or offset != len(payload):
        return False

    image_chunks = [(tag, data) for tag, data in chunks if tag in _IMAGE_CHUNKS]
    if len(image_chunks) != 1:
        return False
    image_tag, image = image_chunks[0]
    if image_tag == b"VP8 " and not _valid_vp8(image):
        return False
    intrinsic_alpha = False
    if image_tag == b"VP8L":
        parsed_alpha = _vp8l_alpha(image)
        if parsed_alpha is None:
            return False
        intrinsic_alpha = parsed_alpha

    if chunks[0][0] != b"VP8X":
        return len(chunks) == 1 and chunks[0][0] in _IMAGE_CHUNKS

    header = chunks[0][1]
    if (
        len(header) != 10
        or header[0] & 0xC1
        or header[1:4] != b"\x00\x00\x00"
        or header[0] & 0x02
    ):
        return False
    if any(tag not in _EXTENDED_CHUNKS for tag, _ in chunks[1:]):
        return False
    tags = [tag for tag, _ in chunks[1:]]
    if len(tags) != len(set(tags)):
        return False

    index = 0
    has_iccp = index < len(tags) and tags[index] == b"ICCP"
    index += int(has_iccp)
    has_alpha = False
    if image_tag == b"VP8L":
        if index >= len(tags) or tags[index] != b"VP8L":
            return False
        has_alpha = intrinsic_alpha
        index += 1
    else:
        if index < len(tags) and tags[index] == b"ALPH":
            has_alpha = True
            index += 1
        if index >= len(tags) or tags[index] != b"VP8 ":
            return False
        index += 1
    has_exif = index < len(tags) and tags[index] == b"EXIF"
    index += int(has_exif)
    has_xmp = index < len(tags) and tags[index] == b"XMP "
    index += int(has_xmp)
    if index != len(tags):
        return False
    expected_flags = (
        (0x20 if has_iccp else 0)
        | (0x10 if has_alpha else 0)
        | (0x08 if has_exif else 0)
        | (0x04 if has_xmp else 0)
    )
    return header[0] == expected_flags


__all__ = ["is_valid_webp"]
