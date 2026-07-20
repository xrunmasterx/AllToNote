from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import tomli_w
from filelock import FileLock
from app.core.errors import DomainError, ErrorCategory
from app.runtime_paths import resolve_runtime_paths


_CATALOG_VERSION = 1
_CATALOG_FILENAME = "credential-profiles.toml"
_PROFILE_KEYS = frozenset({"profile_id", "kind", "created_at", "updated_at"})
_PROFILE_PATTERN = re.compile(
    r"(?P<kind>[a-z][a-z0-9]*(?:-[a-z0-9]+)*)/"
    r"(?P<name>[a-z][a-z0-9]*(?:-[a-z0-9]+)*)"
)
_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z"
)


def _utc_now_millis() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _catalog_error(code: str, message: str) -> DomainError:
    return DomainError(code, ErrorCategory.INVALID_REQUEST, message)


def validate_credential_profile(profile_id: object) -> tuple[str, str]:
    if type(profile_id) is str:
        match = _PROFILE_PATTERN.fullmatch(profile_id)
        if match is not None:
            return match.group("kind"), match.group("name")
    raise DomainError(
        "credential_profile_invalid",
        ErrorCategory.INVALID_REQUEST,
        "Credential profile is invalid",
    )


def _parse_catalog_timestamp(value: object) -> datetime:
    if type(value) is not str or _TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError("Timestamp is not canonical UTC milliseconds")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )


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
            else resolve_runtime_paths().credential_catalog_file
        )
        self._clock = clock
        self._lock = FileLock(f"{self.path}.lock")

    def list_profiles(self) -> tuple[CredentialProfileMetadata, ...]:
        with self._lock:
            profiles = self._read_unlocked()
        return tuple(sorted(profiles, key=lambda profile: profile.profile_id))

    def store_profile(self, profile_id: str, kind: str) -> None:
        profile_kind, _ = validate_credential_profile(profile_id)
        if type(kind) is not str or kind != profile_kind:
            raise DomainError(
                "credential_profile_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Credential profile is invalid",
            )
        with self._lock:
            profiles = self._read_unlocked()
            timestamp = self._clock()
            try:
                updated_at = _parse_catalog_timestamp(timestamp)
            except ValueError as exc:
                raise _catalog_error(
                    "credential_catalog_invalid",
                    "Credential profile catalog is invalid",
                ) from exc
            existing = next(
                (profile for profile in profiles if profile.profile_id == profile_id),
                None,
            )
            if (
                existing is not None
                and updated_at < _parse_catalog_timestamp(existing.created_at)
            ):
                raise _catalog_error(
                    "credential_catalog_invalid",
                    "Credential profile catalog is invalid",
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
        validate_credential_profile(profile_id)
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
        catalog_version = values["catalog_version"]
        if type(catalog_version) is not int:
            raise _catalog_error(
                "credential_catalog_invalid", "Credential profile catalog is invalid"
            )
        if catalog_version != _CATALOG_VERSION:
            raise _catalog_error(
                "credential_catalog_version_unsupported",
                "Credential profile catalog version is not supported",
            )
        raw_profiles = values["profiles"]
        if type(raw_profiles) is not list:
            raise _catalog_error(
                "credential_catalog_invalid", "Credential profile catalog is invalid"
            )

        profiles: list[CredentialProfileMetadata] = []
        profile_ids: set[str] = set()
        for raw_profile in raw_profiles:
            if type(raw_profile) is not dict or set(raw_profile) != _PROFILE_KEYS:
                raise _catalog_error(
                    "credential_catalog_invalid", "Credential profile catalog is invalid"
                )
            if not all(type(raw_profile[key]) is str for key in _PROFILE_KEYS):
                raise _catalog_error(
                    "credential_catalog_invalid", "Credential profile catalog is invalid"
                )
            profile_id = raw_profile["profile_id"]
            try:
                profile_kind, _ = validate_credential_profile(profile_id)
                created_at = _parse_catalog_timestamp(raw_profile["created_at"])
                updated_at = _parse_catalog_timestamp(raw_profile["updated_at"])
            except (DomainError, ValueError) as exc:
                raise _catalog_error(
                    "credential_catalog_invalid",
                    "Credential profile catalog is invalid",
                ) from exc
            if (
                raw_profile["kind"] != profile_kind
                or profile_id in profile_ids
                or updated_at < created_at
            ):
                raise _catalog_error(
                    "credential_catalog_invalid",
                    "Credential profile catalog is invalid",
                )
            profile_ids.add(profile_id)
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
