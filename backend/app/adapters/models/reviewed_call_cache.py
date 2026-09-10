from __future__ import annotations

import json
from pathlib import Path

from app.adapters.models.model_result_store import ModelOperationResultStore
from app.adapters.verified_json_cache import VerifiedJsonCache
from app.core.errors import DomainError
from app.core.portable.jsonio import encode_json
from app.core.ports.model_executor import ModelExecutionResult


class ReviewedCallCache:
    """Workspace-scoped, atomic optimization cache; corruption is a cache miss.

    Uses the same bounded result contracts and path protections as anchored
    model results. It never replaces the authoritative operation result store.
    """

    def __init__(self, root: Path) -> None:
        self._cache = VerifiedJsonCache(root)

    def load(self, key: str) -> dict[str, ModelExecutionResult]:
        try:
            values = self._cache.load(key) or {}
            results = {}
            for request_hash, value in values.items():
                ModelOperationResultStore._check_hash(request_hash)
                results[request_hash] = ModelOperationResultStore._decode(encode_json(value), request_hash)
            return results
        except (DomainError, OSError, ValueError, TypeError, KeyError, RecursionError):
            return {}

    def save(self, key: str, results: dict[str, ModelExecutionResult]) -> None:
        self._cache.save(key, {
            request_hash: json.loads(ModelOperationResultStore._encode(request_hash, result))
            for request_hash, result in results.items()
        })
