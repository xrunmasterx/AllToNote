from __future__ import annotations

import os
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import tomli_w
from filelock import FileLock
from platformdirs import user_config_path

from app.core.errors import DomainError, ErrorCategory


_CATALOG_VERSION = 1
_CATALOG_FILENAME = "credential-profiles.toml"
_PROFILE_KEYS = frozenset({"profile_id", "kind", "created_at", "updated_at"})


def _utc_now_millis() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _catalog_error(code: str, message: str) -> DomainError:
    return DomainError(code, ErrorCategory.INVALID_REQUEST, message)


@dataclass(frozen=True)
class CredentialProfileMetadata:
    profile_id: str
    kind: str
    created_at: str
    updated_at: str


class CredentialProfileCatalog:
    def __init__(
        self,
        path: Path | None = None,
        *,
        clock: Callable[[], str] = _utc_now_millis,
    ) -> None:
        self.path = (
            Path(path)
            if path is not None
            else Path(user_config_path("AllToNote")) / _CATALOG_FILENAME
        )
        self._clock = clock
        self._lock = FileLock(f"{self.path}.lock")

    def list_profiles(self) -> tuple[CredentialProfileMetadata, ...]:
        with self._lock:
            profiles = self._read_unlocked()
        return tuple(sorted(profiles, key=lambda profile: profile.profile_id))

    def store_profile(self, profile_id: str, kind: str) -> None:
        with self._lock:
            profiles = self._read_unlocked()
            timestamp = self._clock()
            existing = next(
                (profile for profile in profiles if profile.profile_id == profile_id),
                None,
            )
            metadata = CredentialProfileMetadata(
                profile_id=profile_id,
                kind=kind,
                created_at=existing.created_at if existing is not None else timestamp,
                updated_at=timestamp,
            )
            updated = [
                profile for profile in profiles if profile.profile_id != profile_id
            ]
            updated.append(metadata)
            self._write_unlocked(updated)

    def delete_profile(self, profile_id: str) -> None:
        with self._lock:
            profiles = self._read_unlocked()
            updated = [
                profile for profile in profiles if profile.profile_id != profile_id
            ]
            self._write_unlocked(updated)

    def _read_unlocked(self) -> list[CredentialProfileMetadata]:
        if not self.path.exists():
            return []
        try:
            with self.path.open("rb") as stream:
                values = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise _catalog_error(
                "credential_catalog_invalid", "Credential profile catalog is invalid"
            ) from exc

        if set(values) != {"catalog_version", "profiles"}:
            raise _catalog_error(
                "credential_catalog_invalid", "Credential profile catalog is invalid"
            )
        if values["catalog_version"] != _CATALOG_VERSION:
            raise _catalog_error(
                "credential_catalog_version_unsupported",
                "Credential profile catalog version is not supported",
            )
        raw_profiles = values["profiles"]
        if not isinstance(raw_profiles, list):
            raise _catalog_error(
                "credential_catalog_invalid", "Credential profile catalog is invalid"
            )

        profiles: list[CredentialProfileMetadata] = []
        for raw_profile in raw_profiles:
            if not isinstance(raw_profile, Mapping) or set(raw_profile) != _PROFILE_KEYS:
                raise _catalog_error(
                    "credential_catalog_invalid", "Credential profile catalog is invalid"
                )
            if not all(type(raw_profile[key]) is str for key in _PROFILE_KEYS):
                raise _catalog_error(
                    "credential_catalog_invalid", "Credential profile catalog is invalid"
                )
            profiles.append(CredentialProfileMetadata(**raw_profile))
        return profiles

    def _write_unlocked(self, profiles: list[CredentialProfileMetadata]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        values = {
            "catalog_version": _CATALOG_VERSION,
            "profiles": [
                {
                    "profile_id": profile.profile_id,
                    "kind": profile.kind,
                    "created_at": profile.created_at,
                    "updated_at": profile.updated_at,
                }
                for profile in sorted(profiles, key=lambda item: item.profile_id)
            ],
        }
        temporary_path = self.path.with_name(
            f".{self.path.name}.{uuid4().hex}.tmp"
        )
        try:
            with temporary_path.open("wb") as stream:
                tomli_w.dump(values, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)
