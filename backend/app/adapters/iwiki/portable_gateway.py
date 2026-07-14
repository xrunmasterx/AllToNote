from __future__ import annotations

import json
import re
from collections import OrderedDict
from importlib import metadata, resources
from pathlib import Path
from threading import Lock

from iwiki.errors import ErrorCode, IWikiError
from iwiki.portable import (
    CommitResult,
    PortableBundleRef,
    PortableContractInfo,
    PortableValidationReport,
    PreparedBundle,
    ValidationLevel,
    commit_prepared_bundle,
    inspect_portable_contract,
    prepare_bundle_commit,
    validate_bundle,
)
from iwiki.workspace import Workspace, open_workspace

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
_CONSUMED_TOMBSTONE_LIMIT = 128
_CLAIM_ACQUIRED = 1
_CLAIM_IN_PROGRESS = 2
_CLAIM_CONSUMED = 3
_CLAIM_UNKNOWN = 4
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


_ERROR_MAPPINGS = {
    ErrorCode.INVALID_ARGUMENT: (
        "portable_request_invalid",
        ErrorCategory.INVALID_REQUEST,
        "iwiki rejected the portable request",
    ),
    ErrorCode.INVALID_WORKSPACE: (
        "workspace_invalid",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "iwiki rejected the workspace",
    ),
    ErrorCode.SCHEMA_TOO_NEW: (
        "portable_schema_too_new",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "iwiki workspace schema is newer than supported",
    ),
    ErrorCode.VALIDATION_FAILED: (
        "portable_bundle_validation_failed",
        ErrorCategory.RECIPE_FAILED,
        "iwiki rejected the portable bundle",
    ),
    ErrorCode.CONFLICT: (
        "bundle_id_conflict",
        ErrorCategory.CONFLICT,
        "iwiki reported a bundle conflict",
    ),
    ErrorCode.PERMISSION_DENIED: (
        "workspace_write_denied",
        ErrorCategory.POLICY_DENIED,
        "iwiki denied workspace access",
    ),
    ErrorCode.RETRYABLE_RUNTIME: (
        "iwiki_runtime_retryable",
        ErrorCategory.RETRYABLE_RUNTIME,
        "iwiki operation failed temporarily",
    ),
    ErrorCode.INTERNAL: (
        "iwiki_internal_error",
        ErrorCategory.INTERNAL,
        "iwiki operation failed",
    ),
}


def _map_iwiki_error(error: IWikiError) -> DomainError:
    code, category, message = _ERROR_MAPPINGS[error.code]
    return DomainError(
        code,
        category,
        message,
        {
            "upstream": "iwiki",
            "upstream_code": error.code.wire_value,
        },
    )


def _contract_incompatible() -> DomainError:
    return DomainError(
        "portable_contract_incompatible",
        ErrorCategory.WORKSPACE_INCOMPATIBLE,
        "Installed iwiki portable contract is incompatible with this runtime",
    )


def _prepared_bundle_invalid() -> DomainError:
    return DomainError(
        "portable_prepared_bundle_invalid",
        ErrorCategory.INVALID_REQUEST,
        "Prepared bundle was not created by this gateway or was already consumed",
    )


def _canonical_distribution_name(name: str) -> str:
    if not name.isascii():
        return ""
    return re.sub(r"[-_.]+", "-", name).lower()


def _load_runtime_lock() -> dict[str, object]:
    try:
        payload = json.loads(
            resources.files("app")
            .joinpath("runtime-lock.json")
            .read_text(encoding="utf-8")
        )
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
            or any(
                type(value) is not str or not value for value in string_fields
            )
        ):
            raise _contract_incompatible()

        package_name, expected_version = package_spec.split("==", 1)
        if (
            _canonical_distribution_name(package_name)
            != _TRUSTED_IWIKI_DISTRIBUTION
            or not expected_version
        ):
            raise _contract_incompatible()
        installed_version = metadata.version(_TRUSTED_IWIKI_DISTRIBUTION)
    except _COMPATIBILITY_ERRORS:
        raise _contract_incompatible() from None
    if installed_version != expected_version:
        raise _contract_incompatible()
    return payload


def _open_locked_workspace(
    workspace_root: Path,
) -> tuple[Workspace, PortableContractInfo]:
    runtime_lock = _load_runtime_lock()
    try:
        workspace = open_workspace(workspace_root, writable=True)
        info = inspect_portable_contract(workspace)
        if (
            info.iwiki_sdk_api_version != runtime_lock["portable_api_version"]
            or info.contract_id != runtime_lock["portable_contract_id"]
            or info.schema_set_id != runtime_lock["schema_set_id"]
            or info.schema_set_sha256 != runtime_lock["schema_sha256"]
        ):
            raise _contract_incompatible()
    except IWikiError as error:
        raise _map_iwiki_error(error) from None
    except _COMPATIBILITY_ERRORS:
        raise _contract_incompatible() from None
    return workspace, info


class IWikiPortableGateway:
    def __init__(self) -> None:
        self._prepared: dict[int, tuple[PreparedBundle, Path]] = {}
        self._claimed: dict[int, PreparedBundle] = {}
        self._consumed: OrderedDict[int, PreparedBundle] = OrderedDict()
        self._prepared_lock = Lock()

    def _claim_prepared(
        self,
        prepared: PreparedBundle,
    ) -> tuple[int, tuple[PreparedBundle, Path] | None]:
        prepared_id = id(prepared)
        with self._prepared_lock:
            binding = self._prepared.get(prepared_id)
            if binding is not None and binding[0] is prepared:
                del self._prepared[prepared_id]
                self._claimed[prepared_id] = prepared
                return _CLAIM_ACQUIRED, binding
            if self._claimed.get(prepared_id) is prepared:
                return _CLAIM_IN_PROGRESS, None
            if self._consumed.get(prepared_id) is prepared:
                self._consumed.move_to_end(prepared_id)
                return _CLAIM_CONSUMED, None
            return _CLAIM_UNKNOWN, None

    def _finish_prepared(self, prepared: PreparedBundle) -> None:
        prepared_id = id(prepared)
        with self._prepared_lock:
            claimed = self._claimed.pop(prepared_id)
            assert claimed is prepared
            self._consumed[prepared_id] = prepared
            if len(self._consumed) > _CONSUMED_TOMBSTONE_LIMIT:
                self._consumed.popitem(last=False)

    @staticmethod
    def _close_prepared(prepared: PreparedBundle) -> None:
        try:
            prepared.close()
        except IWikiError as error:
            raise _map_iwiki_error(error) from None
        except _COMPATIBILITY_ERRORS:
            raise _contract_incompatible() from None

    def inspect(self, workspace_root: Path) -> PortableContractInfo:
        _, info = _open_locked_workspace(workspace_root)
        return info

    def validate_candidate(
        self,
        workspace_root: Path,
        staging_relative_path: str,
    ) -> PortableValidationReport:
        workspace, _ = _open_locked_workspace(workspace_root)
        try:
            return validate_bundle(
                workspace,
                PortableBundleRef.staging(staging_relative_path),
                ValidationLevel.SEMANTIC,
            )
        except IWikiError as error:
            raise _map_iwiki_error(error) from None
        except _COMPATIBILITY_ERRORS:
            raise _contract_incompatible() from None

    def prepare_candidate(
        self,
        workspace_root: Path,
        staging_relative_path: str,
        *,
        expected_bundle_id: str,
        expected_manifest_sha256: str,
    ) -> PreparedBundle:
        workspace, _ = _open_locked_workspace(workspace_root)
        try:
            prepared = prepare_bundle_commit(
                workspace,
                PortableBundleRef.staging(staging_relative_path),
                expected_bundle_id=expected_bundle_id,
                expected_manifest_sha256=expected_manifest_sha256,
            )
        except IWikiError as error:
            raise _map_iwiki_error(error) from None
        except _COMPATIBILITY_ERRORS:
            raise _contract_incompatible() from None
        with self._prepared_lock:
            self._prepared[id(prepared)] = (prepared, workspace.root)
        return prepared

    def commit_prepared(self, prepared: PreparedBundle) -> CommitResult:
        if not isinstance(prepared, PreparedBundle):
            raise _prepared_bundle_invalid()
        claim_state, binding = self._claim_prepared(prepared)
        if claim_state != _CLAIM_ACQUIRED or binding is None:
            raise _prepared_bundle_invalid()
        try:
            _open_locked_workspace(binding[1])
            try:
                result = commit_prepared_bundle(prepared)
            except IWikiError as error:
                if error.code is ErrorCode.INVALID_ARGUMENT:
                    raise _prepared_bundle_invalid() from None
                raise _map_iwiki_error(error) from None
            except _COMPATIBILITY_ERRORS:
                raise _contract_incompatible() from None
        except BaseException:
            try:
                self._close_prepared(prepared)
            except Exception:
                pass
            raise
        else:
            self._close_prepared(prepared)
            return result
        finally:
            self._finish_prepared(prepared)

    def discard_prepared(self, prepared: PreparedBundle) -> None:
        if not isinstance(prepared, PreparedBundle):
            raise _prepared_bundle_invalid()
        claim_state, _ = self._claim_prepared(prepared)
        if claim_state == _CLAIM_CONSUMED:
            return
        if claim_state != _CLAIM_ACQUIRED:
            raise _prepared_bundle_invalid()
        try:
            self._close_prepared(prepared)
        finally:
            self._finish_prepared(prepared)
