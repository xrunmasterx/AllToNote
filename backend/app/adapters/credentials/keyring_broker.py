from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NoReturn, Protocol

import keyring
from keyring.errors import KeyringLocked, PasswordDeleteError

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


@dataclass(frozen=True)
class CredentialStatus:
    present: bool
    validated: bool | None
    last_checked_at: str


def _utc_now_millis() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


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
        clock: Callable[[], str] = _utc_now_millis,
    ) -> None:
        self._keyring = keyring_backend
        self._catalog = catalog or CredentialProfileCatalog()
        self._environ = environ
        self._legacy_credentials = dict(legacy_credentials or {})
        self._service_name = service_name
        self._clock = clock

    def resolve(self, profile: str) -> SecretValue:
        environment_name = _profile_environment_name(profile)
        environ = os.environ if self._environ is None else self._environ
        if environment_name in environ:
            return SecretValue(self._validate_secret(environ[environment_name]))

        secret = self._get_keyring_secret(profile)
        if secret is not None:
            return SecretValue(self._validate_secret(secret))

        legacy_secret = self._legacy_credentials.get(profile)
        if legacy_secret is not None:
            return SecretValue(self._validate_secret(legacy_secret))

        raise DomainError(
            "credential_missing",
            ErrorCategory.POLICY_DENIED,
            "Credential profile is unavailable",
            {"profile": profile},
        )

    def set(self, profile: str, secret: str) -> None:
        kind, _ = validate_credential_profile(profile)
        value = self._validate_secret(secret)
        try:
            self._keyring.set_password(self._service_name, profile, value)
        except Exception as error:
            self._raise_backend_error(error)
        self._catalog.store_profile(profile, kind)

    def delete(self, profile: str) -> None:
        validate_credential_profile(profile)
        try:
            self._keyring.delete_password(self._service_name, profile)
        except PasswordDeleteError as error:
            raise DomainError(
                "credential_missing",
                ErrorCategory.POLICY_DENIED,
                "Credential profile is unavailable",
                {"profile": profile},
            ) from error
        except Exception as error:
            self._raise_backend_error(error)
        self._catalog.delete_profile(profile)

    def status(self, profile: str) -> CredentialStatus:
        environment_name = _profile_environment_name(profile)
        environ = os.environ if self._environ is None else self._environ
        if environment_name in environ:
            self._validate_secret(environ[environment_name])
            present = True
        else:
            secret = self._get_keyring_secret(profile)
            if secret is None:
                secret = self._legacy_credentials.get(profile)
            if secret is not None:
                self._validate_secret(secret)
            present = secret is not None
        return CredentialStatus(
            present=present,
            validated=None,
            last_checked_at=self._clock(),
        )

    def _get_keyring_secret(self, profile: str) -> str | None:
        validate_credential_profile(profile)
        try:
            return self._keyring.get_password(self._service_name, profile)
        except Exception as error:
            self._raise_backend_error(error)

    @staticmethod
    def _validate_secret(secret: object) -> str:
        if type(secret) is not str or not secret.strip():
            raise DomainError(
                "credential_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Credential value is invalid",
            )
        return secret

    @staticmethod
    def _raise_backend_error(error: Exception) -> NoReturn:
        if isinstance(error, KeyringLocked):
            raise DomainError(
                "credential_backend_locked",
                ErrorCategory.POLICY_DENIED,
                "Credential backend is locked",
            ) from error
        raise DomainError(
            "credential_backend_unavailable",
            ErrorCategory.RETRYABLE_RUNTIME,
            "Secure credential backend is unavailable",
        ) from error


KeyringCredentialBroker = CredentialBroker
