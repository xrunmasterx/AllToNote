from __future__ import annotations

import re


_EXECUTOR_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@+-]{0,127}\Z")


def is_executor_identity(value: object) -> bool:
    """Return whether a value is safe for Portable executor provenance."""

    return isinstance(value, str) and _EXECUTOR_IDENTITY.fullmatch(value) is not None


__all__ = ["is_executor_identity"]
