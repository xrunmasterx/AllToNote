import json
from pathlib import Path

import pytest

from app.core.errors import DomainError, ErrorCategory
from app.routers import config as config_router
from app.services.cookie_manager import CookieConfigManager


class _Secret:
    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value


class _FakeBroker:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.calls: list[tuple[str, str, str | None]] = []
        self.set_error: Exception | None = None

    def resolve(self, profile: str) -> _Secret:
        self.calls.append(("resolve", profile, None))
        if profile not in self.values:
            raise DomainError(
                "credential_missing",
                ErrorCategory.POLICY_DENIED,
                "Credential profile is unavailable",
            )
        return _Secret(self.values[profile])

    def set(self, profile: str, secret: str) -> None:
        self.calls.append(("set", profile, secret))
        if self.set_error is not None:
            raise self.set_error
        self.values[profile] = secret

    def delete(self, profile: str) -> None:
        self.calls.append(("delete", profile, None))
        if profile not in self.values:
            raise DomainError(
                "credential_missing",
                ErrorCategory.POLICY_DENIED,
                "Credential profile is unavailable",
            )
        del self.values[profile]


def _manager(tmp_path: Path, broker: _FakeBroker) -> CookieConfigManager:
    return CookieConfigManager(
        filepath=str(tmp_path / "downloader.json"),
        broker=broker,
    )


def test_constructor_does_not_create_plaintext_config(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _FakeBroker())

    assert not manager.path.exists()


def test_cookie_is_stored_only_in_secure_broker(tmp_path: Path) -> None:
    broker = _FakeBroker()
    manager = _manager(tmp_path, broker)

    manager.set("bilibili", "SESSDATA=secret")

    assert broker.values == {"cookies/bilibili-main": "SESSDATA=secret"}
    assert not manager.path.exists()
    assert manager.get("bilibili") == "SESSDATA=secret"


def test_legacy_plaintext_entries_are_all_stored_before_source_is_deleted(
    tmp_path: Path,
) -> None:
    broker = _FakeBroker()
    manager = _manager(tmp_path, broker)
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    manager.path.write_text(
        json.dumps(
            {
                "bilibili": {"cookie": "SESSDATA=legacy"},
                "youtube": {"cookie": "SID=legacy"},
            }
        ),
        encoding="utf-8",
    )

    assert manager.get("bilibili") == "SESSDATA=legacy"

    assert broker.values == {
        "cookies/bilibili-main": "SESSDATA=legacy",
        "cookies/youtube-main": "SID=legacy",
    }
    assert not manager.path.exists()


def test_failed_legacy_migration_preserves_plaintext_source(tmp_path: Path) -> None:
    broker = _FakeBroker()
    broker.set_error = DomainError(
        "credential_backend_unavailable",
        ErrorCategory.RETRYABLE_RUNTIME,
        "Secure credential backend is unavailable",
    )
    manager = _manager(tmp_path, broker)
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    manager.path.write_text(
        '{"bilibili":{"cookie":"SESSDATA=legacy"}}',
        encoding="utf-8",
    )

    with pytest.raises(DomainError, match="credential_backend_unavailable"):
        manager.get("bilibili")

    assert manager.path.exists()
    assert "SESSDATA=legacy" in manager.path.read_text(encoding="utf-8")


def test_delete_is_idempotent_and_never_writes_plaintext(tmp_path: Path) -> None:
    broker = _FakeBroker()
    broker.values["cookies/bilibili-main"] = "SESSDATA=secret"
    manager = _manager(tmp_path, broker)

    manager.delete("bilibili")
    manager.delete("bilibili")

    assert broker.values == {}
    assert not manager.path.exists()


@pytest.mark.parametrize(
    "platform",
    ("", "../bilibili", "bilibili/main", "BILIBILI", "bilibili_main"),
)
def test_invalid_platform_is_rejected_before_broker_access(
    tmp_path: Path, platform: str
) -> None:
    broker = _FakeBroker()
    manager = _manager(tmp_path, broker)

    with pytest.raises(DomainError, match="credential_profile_invalid"):
        manager.get(platform)

    assert broker.calls == []


def test_cookie_status_endpoint_never_returns_secret(monkeypatch) -> None:
    class FakeManager:
        def exists(self, platform: str) -> bool:
            assert platform == "bilibili"
            return True

        def get(self, platform: str) -> str:
            raise AssertionError("status endpoint must not resolve the secret")

    monkeypatch.setattr(config_router, "cookie_manager", FakeManager())

    response = config_router.get_cookie("bilibili")
    payload = json.loads(response.body)

    assert payload["data"] == {"platform": "bilibili", "configured": True}
    assert "cookie" not in response.body.decode("utf-8").casefold()


def test_cookie_delete_endpoint_revokes_secure_profile(monkeypatch) -> None:
    deleted: list[str] = []

    class FakeManager:
        def delete(self, platform: str) -> None:
            deleted.append(platform)

    monkeypatch.setattr(config_router, "cookie_manager", FakeManager())

    response = config_router.delete_cookie("bilibili")
    payload = json.loads(response.body)

    assert deleted == ["bilibili"]
    assert payload["data"] == {"platform": "bilibili", "configured": False}
