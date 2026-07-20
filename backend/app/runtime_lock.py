from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata, resources
from typing import Any

from app.core.errors import DomainError, ErrorCategory


_RUNTIME_LOCK_KEYS = frozenset(
    {
        "iwiki_package",
        "portable_api_version",
        "portable_contract_id",
        "schema_set_id",
        "schema_sha256",
        "source_commit",
    }
)
_TRUSTED_IWIKI_DISTRIBUTION = "llm-iwiki"
_COMPATIBILITY_ERRORS = (
    AttributeError,
    ImportError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    UnicodeError,
    ValueError,
)


@dataclass(frozen=True)
class RuntimeLock:
    iwiki_distribution: str
    iwiki_version: str
    portable_api_version: int
    portable_contract_id: str
    schema_set_id: str
    schema_sha256: str
    source_commit: str

    def payload(self) -> dict[str, object]:
        return {
            "iwiki_package": f"{self.iwiki_distribution}=={self.iwiki_version}",
            "portable_api_version": self.portable_api_version,
            "portable_contract_id": self.portable_contract_id,
            "schema_set_id": self.schema_set_id,
            "schema_sha256": self.schema_sha256,
            "source_commit": self.source_commit,
        }


def _contract_incompatible() -> DomainError:
    return DomainError(
        "portable_contract_incompatible",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "Installed iwiki portable contract is incompatible with this runtime",
    )


def _canonical_distribution_name(name: str) -> str:
    if not name.isascii():
        return ""
    return re.sub(r"[-_.]+", "-", name).lower()


def load_runtime_lock(
    *,
    resource_files: Callable[[str], Any] | None = None,
    distribution_version: Callable[[str], str] | None = None,
) -> RuntimeLock:
    files = resource_files or resources.files
    version = distribution_version or metadata.version
    try:
        package_root = files("app")
        lock_resource = package_root.joinpath("runtime-lock.json")
        payload = json.loads(lock_resource.read_text(encoding="utf-8"))
        if type(payload) is not dict or frozenset(payload) != _RUNTIME_LOCK_KEYS:
            raise _contract_incompatible()

        package_spec = payload["iwiki_package"]
        api_version = payload["portable_api_version"]
        string_fields = (
            payload["portable_contract_id"],
            payload["schema_set_id"],
            payload["schema_sha256"],
            payload["source_commit"],
        )
        if (
            type(package_spec) is not str
            or package_spec.count("==") != 1
            or type(api_version) is not int
            or any(type(value) is not str or not value for value in string_fields)
        ):
            raise _contract_incompatible()

        package_name, expected_version = package_spec.split("==", 1)
        if (
            _canonical_distribution_name(package_name)
            != _TRUSTED_IWIKI_DISTRIBUTION
            or not expected_version
            or version(_TRUSTED_IWIKI_DISTRIBUTION) != expected_version
        ):
            raise _contract_incompatible()
        return RuntimeLock(
            iwiki_distribution=_TRUSTED_IWIKI_DISTRIBUTION,
            iwiki_version=expected_version,
            portable_api_version=api_version,
            portable_contract_id=string_fields[0],
            schema_set_id=string_fields[1],
            schema_sha256=string_fields[2],
            source_commit=string_fields[3],
        )
    except DomainError:
        raise
    except _COMPATIBILITY_ERRORS:
        raise _contract_incompatible() from None


__all__ = ["RuntimeLock", "load_runtime_lock"]
