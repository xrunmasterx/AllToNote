from __future__ import annotations

import hashlib
import os
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.errors import DomainError, ErrorCategory
from app.core.jobs.resource_lease import ResourceLeaseStorePort, ResourceOwner


_PUBLISH_LEASE_TTL_SECONDS = 300
_RESOURCE_PREFIX = "workspace:publish:v1:"


def workspace_publish_resource_name(
    workspace_identity: str,
    workspace_root: Path,
) -> str:
    if type(workspace_identity) is not str or not workspace_identity.strip():
        raise DomainError(
            "workspace_publish_identity_invalid",
            ErrorCategory.INVALID_REQUEST,
            "Workspace publication requires a stable Workspace identity",
        )
    canonical_root = unicodedata.normalize(
        "NFC",
        os.path.normcase(str(Path(workspace_root).resolve(strict=True))),
    )
    digest = hashlib.sha256(
        f"{workspace_identity}\0{canonical_root}".encode("utf-8")
    ).hexdigest()
    return f"{_RESOURCE_PREFIX}{digest}"


class WorkspacePublishCoordinator:
    """Short-held machine lease around one Workspace portable commit."""

    def __init__(
        self,
        store: ResourceLeaseStorePort,
        owner: ResourceOwner,
        *,
        workspace_root: Path,
    ) -> None:
        self._store = store
        self._owner = owner
        self.resource_name = workspace_publish_resource_name(
            owner.workspace_identity,
            workspace_root,
        )

    @contextmanager
    def hold(self) -> Iterator[None]:
        lease = self._store.acquire(
            self.resource_name,
            self._owner,
            ttl_seconds=_PUBLISH_LEASE_TTL_SECONDS,
        )
        try:
            yield
        finally:
            try:
                lease.release()
            except Exception:
                pass


__all__ = [
    "WorkspacePublishCoordinator",
    "workspace_publish_resource_name",
]
