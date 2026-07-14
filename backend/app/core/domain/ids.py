from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timezone
from uuid import UUID

from app.core.errors import DomainError, ErrorCategory


TYPED_ID_PREFIXES = frozenset(
    {
        "job",
        "run",
        "att",
        "evt",
        "chl",
        "corr",
        "op",
        "bnd",
        "src",
        "rev",
        "art",
        "ev",
    }
)


def new_typed_id(
    prefix: str,
    *,
    now_ms: int | None = None,
    randomness: bytes | None = None,
) -> str:
    if prefix not in TYPED_ID_PREFIXES:
        raise DomainError(
            "typed_id_prefix_invalid",
            ErrorCategory.INTERNAL,
            "Typed ID prefix is not registered",
            {"prefix": prefix},
        )

    timestamp = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or not 0 <= timestamp < 1 << 48
    ):
        raise DomainError(
            "typed_id_timestamp_invalid",
            ErrorCategory.INTERNAL,
            "Typed ID timestamp must be an unsigned 48-bit millisecond value",
        )

    random_bytes = secrets.token_bytes(10) if randomness is None else randomness
    if not isinstance(random_bytes, bytes) or len(random_bytes) != 10:
        raise DomainError(
            "typed_id_randomness_invalid",
            ErrorCategory.INTERNAL,
            "Typed ID randomness must contain exactly 10 bytes",
        )

    random_bits = int.from_bytes(random_bytes, "big") >> 6
    rand_a = random_bits >> 62
    rand_b = random_bits & ((1 << 62) - 1)
    uuid_int = (
        (timestamp << 80)
        | (0x7 << 76)
        | (rand_a << 64)
        | (0b10 << 62)
        | rand_b
    )
    return f"{prefix}_{UUID(int=uuid_int)}"


def sha256_digest(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if not isinstance(raw, bytes):
        raise DomainError(
            "digest_input_invalid",
            ErrorCategory.INTERNAL,
            "Digest input must be bytes or text",
        )
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def utc_now_millis(now: datetime | None = None) -> str:
    value = datetime.now(timezone.utc) if now is None else now
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainError(
            "timestamp_timezone_required",
            ErrorCategory.INTERNAL,
            "Timestamp must include a timezone",
        )
    utc = value.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"
