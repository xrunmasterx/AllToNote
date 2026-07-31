from __future__ import annotations

from collections.abc import Mapping

from app.adapters.video_packs.official_video_pack_verifier import PackTrustKey


_OFFICIAL_KEY_ID = "alltonote-video-packs-2026-01"
_OFFICIAL_PUBLIC_KEY = bytes.fromhex(
    "a76db48580e02230041685c599ebefa93d39fc6115b4bfa1d1bdbdacff5b49e2"
)


def official_video_pack_trust_keys() -> Mapping[str, PackTrustKey]:
    return {
        _OFFICIAL_KEY_ID: PackTrustKey(
            key_id=_OFFICIAL_KEY_ID,
            publisher="alltonote-official",
            public_key=_OFFICIAL_PUBLIC_KEY,
        )
    }


__all__ = ["official_video_pack_trust_keys"]
