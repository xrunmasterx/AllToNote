"""Read WebP canvas dimensions without decoding pixels or importing an image library."""
from app.core.errors import DomainError, ErrorCategory


def webp_dimensions(payload: bytes) -> tuple[int, int]:
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        offset = 12
        while offset + 8 <= len(payload):
            kind = payload[offset:offset + 4]
            size = int.from_bytes(payload[offset + 4:offset + 8], "little")
            data = memoryview(payload)[offset + 8:offset + 8 + size]
            if len(data) != size:
                break
            if kind == b"VP8X" and len(data) >= 10:
                return (1 + int.from_bytes(data[4:7], "little"),
                        1 + int.from_bytes(data[7:10], "little"))
            if kind == b"VP8L" and len(data) >= 5 and data[0] == 0x2f:
                bits = int.from_bytes(data[1:5], "little")
                return (1 + (bits & 0x3fff), 1 + ((bits >> 14) & 0x3fff))
            if kind == b"VP8 " and len(data) >= 10 and data[3:6] == b"\x9d\x01\x2a":
                width = int.from_bytes(data[6:8], "little") & 0x3fff
                height = int.from_bytes(data[8:10], "little") & 0x3fff
                if width and height:
                    return width, height
                break
            offset += 8 + size + size % 2
    raise DomainError("model_image_dimensions_invalid", ErrorCategory.INVALID_REQUEST,
                      "Cannot read dimensions of the supplied WebP evidence")
