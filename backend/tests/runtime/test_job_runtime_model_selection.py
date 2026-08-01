from __future__ import annotations

import json

import pytest

from app.core.errors import DomainError
from app.core.config.model import ProviderProfileConfig, RuntimeConfig
from app.job_runtime import _stored_video_model_selection
from app.runtime import _admit_codex_video_profile
from app.runtime_config import effective_runtime_config


def test_stored_video_model_selection_preserves_detached_request_identity() -> None:
    request = json.dumps(
        {
            "request_schema_version": 2,
            "provider_profile": "video-composer",
            "model_override": "openai/video-composer-model",
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    assert _stored_video_model_selection(request) == (
        "video-composer",
        "openai/video-composer-model",
    )


def test_stored_video_model_selection_falls_back_for_absent_or_invalid_data() -> None:
    assert _stored_video_model_selection(None) == (None, None)
    assert _stored_video_model_selection("not-json") == (None, None)
    assert _stored_video_model_selection("[]") == (None, None)


@pytest.mark.parametrize(
    "field,value",
    (
        ("provider_profile", "video profile"),
        ("model_override", "model:unbounded"),
        ("model_override", 42),
    ),
)
def test_stored_video_model_selection_rejects_noncanonical_identity(
    field: str,
    value: object,
) -> None:
    with pytest.raises(DomainError, match="job_request_invalid"):
        _stored_video_model_selection(json.dumps({field: value}))


@pytest.mark.parametrize(
    "provider",
    (
        ProviderProfileConfig(
            provider_type="openai-compatible",
            default_model="legacy-model",
            credential_ref="providers/legacy",
        ),
        ProviderProfileConfig(
            provider_type="codex-app-server",
            base_url="https://override.example/v1",
            default_model="codex-model",
            credential_ref="codex/local-login",
        ),
        ProviderProfileConfig(
            provider_type="codex-app-server",
            default_model="codex-model",
            credential_ref="providers/nonlocal",
        ),
    ),
)
def test_recovery_factory_rejects_nonlocal_codex_profile_binding(
    provider: ProviderProfileConfig,
) -> None:
    snapshot = effective_runtime_config(
        RuntimeConfig(
            default_provider_profile="video-composer",
            providers={"video-composer": provider},
        )
    ).job_snapshot()

    with pytest.raises(DomainError, match="model_provider_unsupported"):
        _admit_codex_video_profile(snapshot, "video-composer")


def test_recovery_factory_accepts_local_codex_and_pristine_default() -> None:
    admitted = effective_runtime_config(
        RuntimeConfig(
            default_provider_profile="video-composer",
            providers={
                "video-composer": ProviderProfileConfig(
                    provider_type="codex-app-server",
                    default_model="codex-model",
                    credential_ref="codex/local-login",
                )
            },
        )
    ).job_snapshot()
    pristine = effective_runtime_config(RuntimeConfig()).job_snapshot()

    _admit_codex_video_profile(admitted, "video-composer")
    _admit_codex_video_profile(pristine, "default")
