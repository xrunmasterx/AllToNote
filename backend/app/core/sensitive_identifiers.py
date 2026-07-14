from __future__ import annotations

import re


_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "accesstoken",
        "apikey",
        "authtoken",
        "authorization",
        "cookie",
        "password",
        "refreshtoken",
        "secret",
        "setcookie",
        "token",
        "xapikey",
    }
)
_SENSITIVE_TOKENS = frozenset(
    {
        "authorization",
        "bearer",
        "cookie",
        "passwd",
        "password",
        "secret",
        "token",
    }
)
_KEY_QUALIFIERS = frozenset(
    {"access", "api", "client", "encryption", "private", "signing"}
)
_IDENTIFIER_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9]+")
_IDENTIFIER_TOKEN_PATTERN = re.compile(
    r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+"
)


def _identifier_tokens(identifier: str) -> tuple[str, ...]:
    return tuple(
        token.group(0).casefold()
        for segment in _IDENTIFIER_SEGMENT_PATTERN.finditer(identifier)
        for token in _IDENTIFIER_TOKEN_PATTERN.finditer(segment.group(0))
    )


def is_sensitive_identifier(identifier: str) -> bool:
    """Return whether a field or enum identifier denotes secret material."""
    normalized = "".join(
        character for character in identifier.casefold() if character.isalnum()
    )
    if normalized in _SENSITIVE_FIELD_NAMES:
        return True
    tokens = frozenset(_identifier_tokens(identifier))
    return bool(tokens & _SENSITIVE_TOKENS) or (
        "key" in tokens and bool(tokens & _KEY_QUALIFIERS)
    )


__all__ = ["is_sensitive_identifier"]
