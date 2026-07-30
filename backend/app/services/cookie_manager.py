from __future__ import annotations

import json
from pathlib import Path

from filelock import FileLock

from app.adapters.credentials.keyring_broker import CredentialBroker
from app.adapters.credentials.profile_catalog import validate_credential_profile
from app.core.errors import DomainError, ErrorCategory


class CookieConfigManager:
    """Resolve downloader cookies through the OS credential store.

    ``filepath`` is retained only as a one-time migration source for historical
    plaintext ``config/downloader.json`` files. New secrets are never written
    there.
    """

    def __init__(
        self,
        filepath: str = "config/downloader.json",
        *,
        broker: CredentialBroker | None = None,
    ) -> None:
        self.path = Path(filepath)
        self._broker = broker or CredentialBroker()
        self._legacy_lock = FileLock(f"{self.path}.lock")

    @staticmethod
    def _profile(platform: str) -> str:
        profile = f"cookies/{platform}-main"
        validate_credential_profile(profile)
        return profile

    def _read_legacy_unlocked(self) -> dict[str, str]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DomainError(
                "credential_migration_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Legacy downloader credential file is invalid",
            ) from exc
        if type(raw) is not dict:
            raise DomainError(
                "credential_migration_invalid",
                ErrorCategory.INVALID_REQUEST,
                "Legacy downloader credential file is invalid",
            )

        credentials: dict[str, str] = {}
        for platform, value in raw.items():
            if type(platform) is not str or type(value) is not dict:
                raise DomainError(
                    "credential_migration_invalid",
                    ErrorCategory.INVALID_REQUEST,
                    "Legacy downloader credential file is invalid",
                )
            self._profile(platform)
            cookie = value.get("cookie")
            if type(cookie) is not str or not cookie.strip():
                raise DomainError(
                    "credential_migration_invalid",
                    ErrorCategory.INVALID_REQUEST,
                    "Legacy downloader credential file is invalid",
                )
            credentials[platform] = cookie
        return credentials

    def _migrate_legacy(self) -> None:
        if not self.path.exists():
            return
        with self._legacy_lock:
            if not self.path.exists():
                return
            credentials = self._read_legacy_unlocked()
            for platform, cookie in credentials.items():
                self._broker.set(self._profile(platform), cookie)
            try:
                self.path.unlink()
            except OSError as exc:
                raise DomainError(
                    "credential_migration_cleanup_failed",
                    ErrorCategory.RETRYABLE_RUNTIME,
                    "Legacy downloader credential file could not be removed",
                ) from exc

    def get(self, platform: str) -> str | None:
        profile = self._profile(platform)
        self._migrate_legacy()
        try:
            return self._broker.resolve(profile).reveal()
        except DomainError as exc:
            if exc.code == "credential_missing":
                return None
            raise

    def set(self, platform: str, cookie: str) -> None:
        profile = self._profile(platform)
        self._migrate_legacy()
        self._broker.set(profile, cookie)

    def delete(self, platform: str) -> None:
        profile = self._profile(platform)
        self._migrate_legacy()
        try:
            self._broker.delete(profile)
        except DomainError as exc:
            if exc.code != "credential_missing":
                raise

    def exists(self, platform: str) -> bool:
        return self.get(platform) is not None
