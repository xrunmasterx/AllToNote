from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Protocol

import keyring

from app.adapters.credentials.profile_catalog import (
    CredentialProfileCatalog,
    validate_credential_profile,
)
from app.core.errors import DomainError, ErrorCategory


class _KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class SecretValue:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretValue(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"


def _profile_environment_name(profile: str) -> str:
    kind, name = validate_credential_profile(profile)
    normalized_name = name.replace("-", "_").upper()
    if kind == "providers":
        return f"ALLTONOTE_CREDENTIAL_{normalized_name}"
    normalized_kind = kind.replace("-", "_").upper()
    return f"ALLTONOTE_CREDENTIAL_{normalized_kind}__{normalized_name}"


class CredentialBroker:
    def __init__(
        self,
        *,
        keyring_backend: _KeyringBackend = keyring,
        catalog: CredentialProfileCatalog | None = None,
        environ: Mapping[str, str] | None = None,
        legacy_credentials: Mapping[str, str] | None = None,
        service_name: str = "AllToNote",
    ) -> None:
        self._keyring = keyring_backend
        self._catalog = catalog or CredentialProfileCatalog()
        self._environ = environ
        self._legacy_credentials = dict(legacy_credentials or {})
        self._service_name = service_name

    def resolve(self, profile: str) -> SecretValue:
        environment_name = _profile_environment_name(profile)
        environ = os.environ if self._environ is None else self._environ
        if environment_name in environ:
            return SecretValue(environ[environment_name])

        secret = self._keyring.get_password(self._service_name, profile)
        if secret is not None:
            return SecretValue(secret)

        legacy_secret = self._legacy_credentials.get(profile)
        if legacy_secret is not None:
            return SecretValue(legacy_secret)

        raise DomainError(
            "credential_missing",
            ErrorCategory.POLICY_DENIED,
            "Credential profile is unavailable",
            {"profile": profile},
        )

    def set(self, profile: str, secret: str) -> None:
        kind, _ = validate_credential_profile(profile)
        self._keyring.set_password(self._service_name, profile, secret)
        self._catalog.store_profile(profile, kind)

    def delete(self, profile: str) -> None:
        validate_credential_profile(profile)
        self._keyring.delete_password(self._service_name, profile)
        self._catalog.delete_profile(profile)


KeyringCredentialBroker = CredentialBroker
