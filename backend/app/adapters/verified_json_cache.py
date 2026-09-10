"""Atomic, content-keyed optimization cache; invalid/missing entries are misses."""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from app.adapters.models.model_result_store import _path_chain_has_reparse_point
from app.core.domain.ids import sha256_digest
from app.core.portable.jsonio import encode_json


class VerifiedJsonCache:
    def __init__(self, root: Path) -> None:
        self._root = Path(root).absolute()

    def _target(self, key: str) -> Path:
        if (type(key) is not str or not key.startswith("sha256:") or len(key) != 71
                or any(value not in "0123456789abcdef" for value in key[7:])):
            raise ValueError("Invalid cache key")
        target = self._root / f"{key[7:]}.json"
        if _path_chain_has_reparse_point(target):
            raise ValueError("Unsafe cache path")
        return target

    def load(self, key: str) -> dict | None:
        try:
            target = self._target(key)
            if target.stat().st_size > 32 * 1024 * 1024:
                return None
            with target.open("rb") as handle:
                raw = handle.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                return None
            envelope = json.loads(raw)
            payload = envelope["payload"]
            if (set(envelope) != {"payload", "sha256"} or type(payload) is not dict
                    or set(payload) != {"key", "value"} or payload["key"] != key
                    or type(payload["value"]) is not dict
                    or sha256_digest(encode_json(payload)) != envelope["sha256"]):
                return None
            self._target(key)
            return payload["value"]
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return None

    def save(self, key: str, value: dict) -> None:
        temporary = None
        try:
            target = self._target(key)
            payload = {"key": key, "value": value}
            encoded = encode_json({"payload": payload, "sha256": sha256_digest(encode_json(payload))})
            if len(encoded) > 32 * 1024 * 1024:
                return
            self._root.mkdir(parents=True, exist_ok=True)
            self._target(key)
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            with temporary.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self._target(key)
            os.replace(temporary, target)
        except (OSError, ValueError, TypeError, RecursionError):
            return
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
