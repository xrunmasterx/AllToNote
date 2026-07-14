from __future__ import annotations

from pathlib import Path

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
from iwiki.workspace import open_workspace

from app.core.errors import DomainError, ErrorCategory


EXPECTED_PORTABLE_API_VERSION = 1
EXPECTED_CONTRACT_ID = "iwiki-portable-contract-v1"
EXPECTED_SCHEMA_SET_ID = "2026-07-portable-v1"
EXPECTED_SCHEMA_SHA256 = (
    "sha256:f8ded2d23197685dc0046e3949e573097fa4ae13e12cfbba240ff0544ca2c9d9"
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


class IWikiPortableGateway:
    def inspect(self, workspace_root: Path) -> PortableContractInfo:
        try:
            workspace = open_workspace(workspace_root, writable=True)
            info = inspect_portable_contract(workspace)
        except IWikiError as error:
            raise _map_iwiki_error(error) from None

        if (
            info.iwiki_sdk_api_version != EXPECTED_PORTABLE_API_VERSION
            or info.contract_id != EXPECTED_CONTRACT_ID
            or info.schema_set_id != EXPECTED_SCHEMA_SET_ID
            or info.schema_set_sha256 != EXPECTED_SCHEMA_SHA256
        ):
            raise DomainError(
                "portable_contract_incompatible",
                ErrorCategory.WORKSPACE_INCOMPATIBLE,
                "Installed iwiki portable contract is incompatible with this runtime",
            )
        return info

    def validate_candidate(
        self,
        workspace_root: Path,
        staging_relative_path: str,
    ) -> PortableValidationReport:
        try:
            workspace = open_workspace(workspace_root, writable=True)
            return validate_bundle(
                workspace,
                PortableBundleRef.staging(staging_relative_path),
                ValidationLevel.SEMANTIC,
            )
        except IWikiError as error:
            raise _map_iwiki_error(error) from None

    def prepare_candidate(
        self,
        workspace_root: Path,
        staging_relative_path: str,
        *,
        expected_bundle_id: str,
        expected_manifest_sha256: str,
    ) -> PreparedBundle:
        try:
            workspace = open_workspace(workspace_root, writable=True)
            return prepare_bundle_commit(
                workspace,
                PortableBundleRef.staging(staging_relative_path),
                expected_bundle_id=expected_bundle_id,
                expected_manifest_sha256=expected_manifest_sha256,
            )
        except IWikiError as error:
            raise _map_iwiki_error(error) from None

    def commit_prepared(self, prepared: PreparedBundle) -> CommitResult:
        try:
            return commit_prepared_bundle(prepared)
        except IWikiError as error:
            raise _map_iwiki_error(error) from None
