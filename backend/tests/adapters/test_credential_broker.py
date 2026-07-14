from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import tomli_w

from app.adapters.credentials.keyring_broker import CredentialBroker, SecretValue
from app.adapters.credentials.profile_catalog import CredentialProfileCatalog
from app.core.errors import DomainError, ErrorCategory


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str, str | None]] = []
        self.set_error: Exception | None = None
        self.delete_error: Exception | None = None

    def get_password(self, service: str, profile: str) -> str | None:
        self.calls.append(("get", profile, None))
        return self.values.get((service, profile))

    def set_password(self, service: str, profile: str, secret: str) -> None:
        self.calls.append(("set", profile, secret))
        if self.set_error is not None:
            raise self.set_error
        self.values[(service, profile)] = secret

    def delete_password(self, service: str, profile: str) -> None:
        self.calls.append(("delete", profile, None))
        if self.delete_error is not None:
            raise self.delete_error
        del self.values[(service, profile)]


def _catalog(tmp_path: Path, *timestamps: str) -> CredentialProfileCatalog:
    values = iter(timestamps or ("2026-07-14T12:00:00.000Z",))
    return CredentialProfileCatalog(tmp_path / "credential-profiles.toml", clock=values.__next__)


def _catalog_entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "profile_id": "providers/openai-main",
        "kind": "providers",
        "created_at": "2026-07-14T12:00:00.000Z",
        "updated_at": "2026-07-14T12:01:00.000Z",
    }
    entry.update(overrides)
    return entry


def _write_catalog(
    path: Path, *, catalog_version: object = 1, profiles: list[object] | None = None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        tomli_w.dump(
            {
                "catalog_version": catalog_version,
                "profiles": profiles if profiles is not None else [_catalog_entry()],
            },
            stream,
        )


def test_environment_secret_wins_and_repr_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLTONOTE_CREDENTIAL_OPENAI_MAIN", "secret-value")
    keyring_backend = FakeKeyring()
    keyring_backend.values[("AllToNote", "providers/openai-main")] = "keyring-value"
    broker = CredentialBroker(
        keyring_backend=keyring_backend,
        catalog=_catalog(tmp_path),
        legacy_credentials={"providers/openai-main": "legacy-value"},
    )

    value = broker.resolve("providers/openai-main")

    assert isinstance(value, SecretValue)
    assert value.reveal() == "secret-value"
    assert "secret-value" not in repr(value)
    assert "secret-value" not in str(value)
    assert keyring_backend.calls == []


def test_cross_kind_environment_names_do_not_collide(tmp_path: Path) -> None:
    keyring_backend = FakeKeyring()
    broker = CredentialBroker(
        keyring_backend=keyring_backend,
        catalog=_catalog(tmp_path),
        environ={
            "ALLTONOTE_CREDENTIAL_OPENAI_MAIN": "provider-secret",
            "ALLTONOTE_CREDENTIAL_TRANSCRIBERS__OPENAI_MAIN": "transcriber-secret",
            "ALLTONOTE_CREDENTIAL_COOKIES__BILIBILI_MAIN": "cookie-secret",
        },
    )

    assert broker.resolve("providers/openai-main").reveal() == "provider-secret"
    assert (
        broker.resolve("transcribers/openai-main").reveal()
        == "transcriber-secret"
    )
    assert broker.resolve("cookies/bilibili-main").reveal() == "cookie-secret"
    assert keyring_backend.calls == []


@pytest.mark.parametrize("operation", ["resolve", "set", "delete"])
@pytest.mark.parametrize(
    "profile",
    [
        "providers/",
        "openai-main",
        "providers//openai-main",
        "providers/openai_main",
        "Providers/openai-main",
        "providers/openai--main",
        "providers/-openai",
        "providers/openai-",
        "providers/open.ai",
    ],
)
def test_broker_rejects_noncanonical_profiles_before_side_effects(
    tmp_path: Path, operation: str, profile: str
) -> None:
    keyring_backend = FakeKeyring()
    catalog = _catalog(tmp_path)
    broker = CredentialBroker(
        keyring_backend=keyring_backend, catalog=catalog, environ={}
    )

    with pytest.raises(DomainError, match="credential_profile_invalid") as exc_info:
        if operation == "resolve":
            broker.resolve(profile)
        elif operation == "set":
            broker.set(profile, "do-not-print")
        else:
            broker.delete(profile)

    assert exc_info.value.category is ErrorCategory.INVALID_REQUEST
    assert "do-not-print" not in str(exc_info.value)
    assert "do-not-print" not in repr(exc_info.value)
    assert keyring_backend.calls == []
    assert not catalog.path.exists()


def test_secret_precedence_falls_back_to_keyring_then_explicit_legacy(
    tmp_path: Path,
) -> None:
    keyring_backend = FakeKeyring()
    keyring_backend.values[("AllToNote", "providers/openai-main")] = "keyring-value"
    broker = CredentialBroker(
        keyring_backend=keyring_backend,
        catalog=_catalog(tmp_path),
        environ={},
        legacy_credentials={
            "providers/openai-main": "legacy-shadowed",
            "providers/legacy-only": "legacy-value",
        },
    )

    assert broker.resolve("providers/openai-main").reveal() == "keyring-value"
    assert broker.resolve("providers/legacy-only").reveal() == "legacy-value"


def test_missing_credential_has_stable_safe_domain_error(tmp_path: Path) -> None:
    broker = CredentialBroker(
        keyring_backend=FakeKeyring(), catalog=_catalog(tmp_path), environ={}
    )

    with pytest.raises(DomainError, match="credential_missing") as exc_info:
        broker.resolve("providers/missing")

    assert exc_info.value.category is ErrorCategory.POLICY_DENIED
    assert exc_info.value.details == {"profile": "providers/missing"}


def test_set_updates_non_secret_catalog_only_after_keyring_success(
    tmp_path: Path,
) -> None:
    keyring_backend = FakeKeyring()
    catalog = _catalog(tmp_path)
    broker = CredentialBroker(
        keyring_backend=keyring_backend, catalog=catalog, environ={}
    )

    broker.set("providers/openai-main", "super-secret")

    profiles = catalog.list_profiles()
    assert [(item.profile_id, item.kind) for item in profiles] == [
        ("providers/openai-main", "providers")
    ]
    catalog_text = catalog.path.read_text(encoding="utf-8")
    assert "super-secret" not in catalog_text
    assert "credential" not in tomllib.loads(catalog_text)["profiles"][0]


def test_failed_keyring_set_does_not_update_catalog(tmp_path: Path) -> None:
    keyring_backend = FakeKeyring()
    keyring_backend.set_error = RuntimeError("keyring unavailable")
    catalog = _catalog(tmp_path)
    broker = CredentialBroker(
        keyring_backend=keyring_backend, catalog=catalog, environ={}
    )

    with pytest.raises(RuntimeError, match="keyring unavailable"):
        broker.set("providers/openai-main", "super-secret")

    assert catalog.list_profiles() == ()


def test_failed_keyring_delete_preserves_catalog(tmp_path: Path) -> None:
    keyring_backend = FakeKeyring()
    catalog = _catalog(
        tmp_path,
        "2026-07-14T12:00:00.000Z",
        "2026-07-14T12:01:00.000Z",
    )
    broker = CredentialBroker(
        keyring_backend=keyring_backend, catalog=catalog, environ={}
    )
    broker.set("providers/openai-main", "super-secret")
    keyring_backend.delete_error = RuntimeError("keyring unavailable")

    with pytest.raises(RuntimeError, match="keyring unavailable"):
        broker.delete("providers/openai-main")

    assert [item.profile_id for item in catalog.list_profiles()] == [
        "providers/openai-main"
    ]


def test_delete_removes_catalog_entry_after_keyring_success(tmp_path: Path) -> None:
    keyring_backend = FakeKeyring()
    catalog = _catalog(
        tmp_path,
        "2026-07-14T12:00:00.000Z",
        "2026-07-14T12:01:00.000Z",
    )
    broker = CredentialBroker(
        keyring_backend=keyring_backend, catalog=catalog, environ={}
    )
    broker.set("providers/openai-main", "super-secret")

    broker.delete("providers/openai-main")

    assert catalog.list_profiles() == ()


def test_catalog_list_is_sorted_and_never_enumerates_keyring(tmp_path: Path) -> None:
    keyring_backend = FakeKeyring()
    catalog = _catalog(
        tmp_path,
        "2026-07-14T12:00:00.000Z",
        "2026-07-14T12:01:00.000Z",
    )
    broker = CredentialBroker(
        keyring_backend=keyring_backend, catalog=catalog, environ={}
    )
    broker.set("transcribers/groq-main", "secret-two")
    broker.set("providers/openai-main", "secret-one")
    keyring_backend.calls.clear()

    profiles = catalog.list_profiles()

    assert [item.profile_id for item in profiles] == [
        "providers/openai-main",
        "transcribers/groq-main",
    ]
    assert keyring_backend.calls == []


@pytest.mark.parametrize("operation", ["store", "delete"])
def test_catalog_mutations_validate_profile_before_file_access(
    tmp_path: Path, operation: str
) -> None:
    catalog = _catalog(tmp_path)

    with pytest.raises(DomainError, match="credential_profile_invalid"):
        if operation == "store":
            catalog.store_profile("providers/", "providers")
        else:
            catalog.delete_profile("providers/")

    assert not catalog.path.exists()


def test_catalog_store_rejects_kind_mismatch_before_file_access(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)

    with pytest.raises(DomainError, match="credential_profile_invalid"):
        catalog.store_profile("providers/openai-main", "transcribers")

    assert not catalog.path.exists()


@pytest.mark.parametrize(
    ("catalog_version", "error_code"),
    [
        (True, "credential_catalog_invalid"),
        ("1", "credential_catalog_invalid"),
        (2, "credential_catalog_version_unsupported"),
    ],
)
def test_catalog_version_requires_exact_integer_one(
    tmp_path: Path, catalog_version: object, error_code: str
) -> None:
    catalog = _catalog(tmp_path)
    _write_catalog(catalog.path, catalog_version=catalog_version)

    with pytest.raises(DomainError, match=error_code) as exc_info:
        catalog.list_profiles()

    assert exc_info.value.category is ErrorCategory.INVALID_REQUEST


@pytest.mark.parametrize(
    "entry",
    [
        {
            "profile_id": "providers/openai-main",
            "kind": "providers",
            "created_at": "2026-07-14T12:00:00.000Z",
        },
        {**_catalog_entry(), "unexpected": "value"},
        _catalog_entry(kind=1),
    ],
)
def test_catalog_profile_entries_require_exact_fields_and_string_values(
    tmp_path: Path, entry: dict[str, object]
) -> None:
    catalog = _catalog(tmp_path)
    _write_catalog(catalog.path, profiles=[entry])

    with pytest.raises(DomainError, match="credential_catalog_invalid"):
        catalog.list_profiles()


def test_catalog_rejects_duplicate_profile_ids(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _write_catalog(catalog.path, profiles=[_catalog_entry(), _catalog_entry()])

    with pytest.raises(DomainError, match="credential_catalog_invalid"):
        catalog.list_profiles()


@pytest.mark.parametrize(
    "entry",
    [
        _catalog_entry(profile_id="providers/openai_main"),
        _catalog_entry(kind="transcribers"),
    ],
)
def test_catalog_rejects_noncanonical_profile_or_kind_mismatch(
    tmp_path: Path, entry: dict[str, object]
) -> None:
    catalog = _catalog(tmp_path)
    _write_catalog(catalog.path, profiles=[entry])

    with pytest.raises(DomainError, match="credential_catalog_invalid"):
        catalog.list_profiles()


@pytest.mark.parametrize(
    ("created_at", "updated_at"),
    [
        ("2026-07-14T12:00:00Z", "2026-07-14T12:01:00.000Z"),
        ("2026-07-14T12:00:00.000+00:00", "2026-07-14T12:01:00.000Z"),
        ("2026-02-30T12:00:00.000Z", "2026-07-14T12:01:00.000Z"),
        ("2026-07-14T12:00:00.000z", "2026-07-14T12:01:00.000Z"),
        ("2026-07-14T12:00:00.0000Z", "2026-07-14T12:01:00.000Z"),
        ("2026-07-14T12:01:00.000Z", "2026-07-14T12:00:00.000Z"),
    ],
)
def test_catalog_rejects_noncanonical_or_reversed_timestamps(
    tmp_path: Path, created_at: str, updated_at: str
) -> None:
    catalog = _catalog(tmp_path)
    _write_catalog(
        catalog.path,
        profiles=[_catalog_entry(created_at=created_at, updated_at=updated_at)],
    )

    with pytest.raises(DomainError, match="credential_catalog_invalid"):
        catalog.list_profiles()
