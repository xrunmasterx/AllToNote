from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

import app.core.config.loader as config_loader_module
from app.cli.main import main
from app.core.config.loader import load_runtime_config, write_runtime_config
from app.core.config.model import (
    ProviderProfileConfig,
    RecipeDefaults,
    RuntimeConfig,
)
from app.core.domain.ids import sha256_digest
from app.core.domain.video import JobState, VideoProduceRequest
from app.core.errors import DomainError
from app.runtime import create_fake_runtime
from app.runtime_config import (
    ConfigDrift,
    RuntimeConfigService,
    classify_config_drift,
    effective_runtime_config,
)
from app.runtime_paths import resolve_runtime_paths


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "workspace-v2"


def _paths(tmp_path: Path):
    return resolve_runtime_paths(machine_state_root=tmp_path / "machine-state")


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "Vault 配置 快照"
    shutil.copytree(FIXTURE_ROOT, root)
    shutil.rmtree(root / "raw" / "personal" / ".staging")
    for relative in (
        "raw/common",
        "raw/personal/.staging",
        "wiki/common",
        "wiki/personal",
        ".cache",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root


def _user_config(tmp_path: Path, *, workspace: Path | None = None) -> RuntimeConfig:
    return RuntimeConfig(
        default_workspace=workspace,
        default_provider_profile="user-provider",
        default_transcriber_profile="user-transcriber",
        providers={
            "user-provider": ProviderProfileConfig(
                provider_type="openai-compatible",
                base_url="https://user.example/v1",
                default_model="user-model",
                credential_ref="providers/user-provider",
            )
        },
        recipe_defaults=RecipeDefaults(
            output_language="en-US",
            quality_preset="balanced",
            style="user-style",
            screenshot_policy="off",
        ),
    )


def test_effective_config_precedence_is_default_user_profile_env_flags(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    write_runtime_config(_user_config(tmp_path), paths.config_file)
    profile_dir = paths.config_dir / "profiles"
    profile_dir.mkdir(parents=True)
    (profile_dir / "agent.toml").write_text(
        "\n".join(
            (
                'default_provider_profile="profile-provider"',
                "[providers.user-provider]",
                'base_url="https://profile.example/v1"',
                "[recipe_defaults]",
                'output_language="ja-JP"',
                'style="profile-style"',
            )
        ),
        encoding="utf-8",
    )
    profile_only = RuntimeConfigService(paths=paths).effective(profile="agent")
    service = RuntimeConfigService(
        paths=paths,
        environ={
            "ALLTONOTE_DEFAULT_PROVIDER_PROFILE": "env-provider",
            "ALLTONOTE_RECIPE_OUTPUT_LANGUAGE": "ko-KR",
            "ALLTONOTE_RECIPE_STYLE": "env-style",
        },
    )

    effective = service.effective(
        profile="agent",
        cli_overrides={
            "default_provider_profile": "cli-provider",
            "recipe_defaults": {
                "output_language": "fr-FR",
                "style": "cli-style",
            },
        },
    )

    assert profile_only.config.default_provider_profile == "profile-provider"
    assert (
        profile_only.config.providers["user-provider"].base_url
        == "https://profile.example/v1"
    )
    assert effective.config.default_provider_profile == "cli-provider"
    assert effective.config.recipe_defaults.output_language == "fr-FR"
    assert effective.config.recipe_defaults.style == "cli-style"


def test_arbitrary_environment_and_ephemeral_secret_are_not_config_layers(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    write_runtime_config(_user_config(tmp_path), paths.config_file)
    secret = "secret-config-canary-7419"
    effective = RuntimeConfigService(
        paths=paths,
        environ={
            "ALLTONOTE_UNDECLARED_SETTING": "ignored",
            "ALLTONOTE_CREDENTIAL_USER_PROVIDER": secret,
        },
    ).effective()

    serialized = json.dumps(effective.values, default=str, sort_keys=True)

    assert secret not in serialized
    assert "ignored" not in serialized
    assert effective.config.default_provider_profile == "user-provider"


def test_verifier_provider_profile_round_trips_and_is_semantic(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    base = _user_config(tmp_path)
    configured = replace(
        base,
        default_verifier_provider_profile="document-reviewer",
        providers={
            **base.providers,
            "document-reviewer": ProviderProfileConfig(
                provider_type="codex-app-server",
                default_model="fixture/verifier-v1",
                credential_ref="codex/local-login",
            ),
        },
    )
    write_runtime_config(configured, paths.config_file)

    loaded = RuntimeConfigService(paths=paths).effective()

    assert loaded.config.default_verifier_provider_profile == "document-reviewer"
    assert loaded.values["default_verifier_provider_profile"] == (
        "document-reviewer"
    )
    assert loaded.semantic_digest != effective_runtime_config(base).semantic_digest


def test_effective_config_digest_distinguishes_semantic_drift() -> None:
    base_config = RuntimeConfig()
    base = effective_runtime_config(base_config)
    non_semantic = effective_runtime_config(
        replace(base_config, log_level="DEBUG")
    )
    semantic = effective_runtime_config(
        replace(
            base_config,
            recipe_defaults=replace(base_config.recipe_defaults, style="tutorial"),
        )
    )

    assert classify_config_drift(base, base) is ConfigDrift.NONE
    assert classify_config_drift(base, non_semantic) is ConfigDrift.NON_SEMANTIC
    assert classify_config_drift(base, semantic) is ConfigDrift.SEMANTIC


def test_config_cli_get_validate_profiles_and_set_are_one_safe_envelope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _paths(tmp_path)
    user_config = _user_config(tmp_path)
    write_runtime_config(
        replace(
            user_config,
            providers={
                **user_config.providers,
                "user-provider": ProviderProfileConfig(
                    provider_type="codex-app-server",
                    default_model="user-model",
                    credential_ref="codex/local-login",
                ),
                "document-reviewer": ProviderProfileConfig(
                    provider_type="codex-app-server",
                    default_model="reviewer-model",
                    credential_ref="codex/local-login",
                ),
            },
        ),
        paths.config_file,
    )
    profile_dir = paths.config_dir / "profiles"
    profile_dir.mkdir(parents=True)
    (profile_dir / "agent.toml").write_text(
        '[recipe_defaults]\nstyle="agent-style"\n', encoding="utf-8"
    )
    secret = "secret-cli-config-canary-8520"
    service = RuntimeConfigService(
        paths=paths,
        environ={"ALLTONOTE_CREDENTIAL_USER_PROVIDER": secret},
    )

    assert main(["config", "get", "--json"], config_service=service) == 0
    get_output = capsys.readouterr()
    get_envelope = json.loads(get_output.out)
    assert get_output.err == ""
    assert secret not in get_output.out
    assert "default_workspace" not in get_envelope["data"]["config"]
    assert get_envelope["data"]["digest"].startswith("sha256:")

    assert (
        main(
            ["config", "validate", "--profile", "agent", "--json"],
            config_service=service,
        )
        == 0
    )
    validate_envelope = json.loads(capsys.readouterr().out)
    assert validate_envelope["data"]["valid"] is True
    assert validate_envelope["data"]["profile"] == "agent"

    assert main(["config", "profiles", "--json"], config_service=service) == 0
    profiles_envelope = json.loads(capsys.readouterr().out)
    assert profiles_envelope["data"]["profiles"] == ["agent"]

    assert (
        main(
            ["config", "set", "recipe_defaults.style", "concise", "--json"],
            config_service=service,
        )
        == 0
    )
    set_envelope = json.loads(capsys.readouterr().out)
    assert set_envelope["data"]["updated"] == "recipe_defaults.style"
    assert load_runtime_config(paths.config_file, {}).recipe_defaults.style == "concise"
    assert not tuple(paths.config_dir.glob("*.tmp"))

    assert (
        main(
            [
                "config",
                "set",
                "default_verifier_provider_profile",
                "document-reviewer",
                "--json",
            ],
            config_service=service,
        )
        == 0
    )
    capsys.readouterr()
    assert load_runtime_config(
        paths.config_file, {}
    ).default_verifier_provider_profile == "document-reviewer"


def test_config_provider_set_creates_an_idempotent_safe_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _paths(tmp_path)
    service = RuntimeConfigService(paths=paths)
    arguments = [
        "config",
        "provider",
        "set",
        "document-composer",
        "--type",
        "codex-app-server",
        "--model",
        "openai/gpt-5.3-codex",
        "--credential-ref",
        "codex/local-login",
        "--json",
    ]

    assert main(arguments, config_service=service) == 0
    first_output = capsys.readouterr()
    first = json.loads(first_output.out)
    first_bytes = paths.config_file.read_bytes()

    assert first_output.err == ""
    assert first["command"] == "config provider set"
    assert first["data"] == {
        "profile": "document-composer",
        "provider_type": "codex-app-server",
        "default_model": "openai/gpt-5.3-codex",
        "changed": True,
        "digest": first["data"]["digest"],
        "semantic_digest": first["data"]["semantic_digest"],
    }
    assert "codex/local-login" not in first_output.out
    configured = load_runtime_config(paths.config_file, {})
    assert configured.providers["document-composer"] == ProviderProfileConfig(
        provider_type="codex-app-server",
        default_model="openai/gpt-5.3-codex",
        credential_ref="codex/local-login",
    )

    monkeypatch.setattr(
        config_loader_module.os,
        "replace",
        lambda _source, _target: (_ for _ in ()).throw(
            AssertionError("an exact repeat must not rewrite config.toml")
        ),
    )
    assert main(arguments, config_service=service) == 0
    second = json.loads(capsys.readouterr().out)

    assert second["data"]["changed"] is False
    assert paths.config_file.read_bytes() == first_bytes


def test_config_provider_set_rejects_invalid_input_without_rewriting_config(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _paths(tmp_path)
    write_runtime_config(_user_config(tmp_path), paths.config_file)
    before = paths.config_file.read_bytes()
    service = RuntimeConfigService(paths=paths)

    exit_code = main(
        [
            "config",
            "provider",
            "set",
            "../unsafe",
            "--type",
            "codex-app-server",
            "--model",
            "composer-model",
            "--json",
        ],
        config_service=service,
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert envelope["command"] == "config provider set"
    assert envelope["error"]["code"] == "config_provider_profile_invalid"
    assert paths.config_file.read_bytes() == before


def test_config_provider_set_defaults_codex_identity_and_rejects_collisions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _paths(tmp_path)
    write_runtime_config(
        RuntimeConfig(
            providers={
                "legacy": ProviderProfileConfig(
                    provider_type="openai-compatible",
                    base_url="https://legacy.example/v1",
                    default_model="legacy-model",
                    credential_ref="providers/legacy",
                )
            }
        ),
        paths.config_file,
    )
    service = RuntimeConfigService(paths=paths)

    assert (
        main(
            [
                "config",
                "provider",
                "set",
                "composer",
                "--type",
                "codex-app-server",
                "--model",
                "composer-model",
                "--json",
            ],
            config_service=service,
        )
        == 0
    )
    capsys.readouterr()
    configured = load_runtime_config(paths.config_file, {})
    assert configured.providers["composer"].credential_ref == "codex/local-login"
    before_conflict = paths.config_file.read_bytes()

    exit_code = main(
        [
            "config",
            "provider",
            "set",
            "legacy",
            "--type",
            "codex-app-server",
            "--model",
            "replacement-model",
            "--json",
        ],
        config_service=service,
    )
    conflict = json.loads(capsys.readouterr().out)

    assert exit_code == 20
    assert conflict["error"]["code"] == "config_provider_profile_conflict"
    assert paths.config_file.read_bytes() == before_conflict


def test_config_defaults_require_existing_usable_provider_profiles(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _paths(tmp_path)
    service = RuntimeConfigService(paths=paths)

    exit_code = main(
        [
            "config",
            "set",
            "default_provider_profile",
            "missing",
            "--json",
        ],
        config_service=service,
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert envelope["error"]["code"] == "default_provider_profile_invalid"
    assert not paths.config_file.exists()


def test_config_validate_rejects_dangling_provider_defaults(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _paths(tmp_path)
    write_runtime_config(
        RuntimeConfig(default_provider_profile="missing"),
        paths.config_file,
    )

    exit_code = main(
        ["config", "validate", "--json"],
        config_service=RuntimeConfigService(paths=paths),
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert envelope["error"]["code"] == "default_provider_profile_invalid"


def test_config_provider_update_preserves_verifier_independence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _paths(tmp_path)
    write_runtime_config(
        RuntimeConfig(
            default_provider_profile="composer",
            default_verifier_provider_profile="verifier",
            providers={
                "composer": ProviderProfileConfig(
                    provider_type="codex-app-server",
                    default_model="composer-model",
                    credential_ref="codex/local-login",
                ),
                "verifier": ProviderProfileConfig(
                    provider_type="codex-app-server",
                    default_model="verifier-model",
                    credential_ref="codex/local-login",
                ),
            },
        ),
        paths.config_file,
    )
    before = paths.config_file.read_bytes()

    exit_code = main(
        [
            "config",
            "provider",
            "set",
            "verifier",
            "--type",
            "codex-app-server",
            "--model",
            "composer-model",
            "--json",
        ],
        config_service=RuntimeConfigService(paths=paths),
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert envelope["error"]["code"] == "document_verifier_not_independent"
    assert paths.config_file.read_bytes() == before


def test_config_provider_set_rejects_explicit_empty_credential_ref(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _paths(tmp_path)

    exit_code = main(
        [
            "config",
            "provider",
            "set",
            "composer",
            "--type",
            "codex-app-server",
            "--model",
            "composer-model",
            "--credential-ref",
            "",
            "--json",
        ],
        config_service=RuntimeConfigService(paths=paths),
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert envelope["error"]["code"] == "config_provider_credential_ref_invalid"
    assert not paths.config_file.exists()


def test_config_provider_set_never_accepts_a_plaintext_secret_option(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "provider-profile-secret-canary-4821"

    exit_code = main(
        [
            "config",
            "provider",
            "set",
            "composer",
            "--type",
            "codex-app-server",
            "--model",
            "composer-model",
            "--api-key",
            secret,
            "--json",
        ],
        config_service=RuntimeConfigService(paths=_paths(tmp_path)),
    )
    output = capsys.readouterr()
    envelope = json.loads(output.out)

    assert exit_code == 2
    assert envelope["command"] == "config provider set"
    assert envelope["error"]["code"] == "cli_usage_invalid"
    assert secret not in output.out
    assert secret not in output.err


@pytest.mark.parametrize(
    "model",
    (
        " composer-model",
        "composer-model ",
        "composer\nmodel",
        "composer\0model",
        "composer:model",
        "composer\tmodel",
        "m" * 129,
    ),
)
def test_config_provider_set_rejects_noncanonical_model_identity(
    tmp_path: Path,
    model: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _paths(tmp_path)

    exit_code = main(
        [
            "config",
            "provider",
            "set",
            "composer",
            "--type",
            "codex-app-server",
            "--model",
            model,
            "--json",
        ],
        config_service=RuntimeConfigService(paths=paths),
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert envelope["error"]["code"] == "config_provider_model_invalid"
    assert not paths.config_file.exists()


def test_config_set_invalid_value_fails_without_rewriting_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _paths(tmp_path)
    write_runtime_config(_user_config(tmp_path), paths.config_file)
    before = paths.config_file.read_bytes()
    service = RuntimeConfigService(paths=paths)

    exit_code = main(
        ["config", "set", "log_level", "TRACE", "--json"],
        config_service=service,
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert envelope["error"]["code"] == "config_value_invalid"
    assert paths.config_file.read_bytes() == before


def test_config_set_permission_failure_is_stable_and_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _paths(tmp_path)
    write_runtime_config(_user_config(tmp_path), paths.config_file)
    before = paths.config_file.read_bytes()
    service = RuntimeConfigService(paths=paths)
    monkeypatch.setattr(
        config_loader_module.os,
        "replace",
        lambda _source, _target: (_ for _ in ()).throw(
            PermissionError("injected read-only config directory")
        ),
    )

    exit_code = main(
        ["config", "set", "log_level", "DEBUG", "--json"],
        config_service=service,
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 40
    assert envelope["error"]["code"] == "config_write_failed"
    assert paths.config_file.read_bytes() == before
    assert not tuple(paths.config_dir.glob("*.tmp"))


def test_video_job_persists_only_effective_semantic_request_snapshot(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = _workspace(tmp_path)
    paths = _paths(tmp_path)
    config = _user_config(tmp_path, workspace=workspace)
    write_runtime_config(config, paths.config_file)
    service = RuntimeConfigService(
        paths=paths,
        environ={"ALLTONOTE_CREDENTIAL_USER_PROVIDER": "job-secret-canary-9631"},
    )
    runtime = create_fake_runtime(tmp_path / "job-machine")
    before = service.effective()

    assert (
        main(
            ["produce", "video", "fixture://course", "--json"],
            runtime=runtime,
            config_service=service,
        )
        == 0
    )
    envelope = json.loads(capsys.readouterr().out)
    job_id = envelope["data"]["job_id"]
    request_json = runtime.job_repository.get_job_request(job_id)
    assert request_json is not None
    request = json.loads(request_json)

    assert request["workspace_root"] == str(workspace.resolve())
    assert request["provider_profile"] == "user-provider"
    assert request["model_override"] == "user-model"
    assert request["transcriber_profile"] == "user-transcriber"
    assert request["output_language"] == "en-US"
    assert request["style"] == "user-style"
    assert "credential_ref" not in request_json
    assert "job-secret-canary-9631" not in request_json
    assert runtime.job_repository.get_job(job_id).request_hash == sha256_digest(
        request_json
    )
    config_events = tuple(
        event
        for event in runtime.job_repository.list_events(job_id)
        if event.event_type == "configuration.snapshot.v1"
    )
    assert len(config_events) == 1
    assert json.loads(config_events[0].payload_json)["digest"] == before.digest
    assert "job-secret-canary-9631" not in config_events[0].payload_json

    after_log_change = service.set_value("log_level", "DEBUG")
    assert classify_config_drift(before, after_log_change) is ConfigDrift.NON_SEMANTIC
    assert runtime.job_repository.get_job_request(job_id) == request_json

    after_style_change = service.set_value("recipe_defaults.style", "new-style")
    assert classify_config_drift(after_log_change, after_style_change) is ConfigDrift.SEMANTIC
    assert runtime.job_repository.get_job_request(job_id) == request_json


def test_recovery_allows_non_semantic_config_drift(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    machine_root = tmp_path / "recovery-machine"
    base = effective_runtime_config(
        RuntimeConfig(default_workspace=workspace)
    )
    changed = effective_runtime_config(
        RuntimeConfig(default_workspace=workspace, log_level="DEBUG")
    )
    first = create_fake_runtime(machine_root)
    submitted = first.submit_video(
        VideoProduceRequest(
            request_schema_version=1,
            workspace_root=workspace,
            input_value="fixture://course",
            client_request_id="non-semantic-config-drift",
            config_snapshot=base.job_snapshot(),
        )
    )

    recovered = create_fake_runtime(
        machine_root,
        current_config_snapshot=changed.job_snapshot(),
    ).wait_job(submitted.job_id)

    assert recovered.state is JobState.SUCCEEDED


def test_recovery_rejects_semantic_config_drift_before_execution(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    machine_root = tmp_path / "semantic-drift-machine"
    base_config = RuntimeConfig(default_workspace=workspace)
    changed_config = replace(
        base_config,
        recipe_defaults=replace(base_config.recipe_defaults, style="changed"),
    )
    first = create_fake_runtime(machine_root)
    submitted = first.submit_video(
        VideoProduceRequest(
            request_schema_version=1,
            workspace_root=workspace,
            input_value="fixture://course",
            client_request_id="semantic-config-drift",
            config_snapshot=effective_runtime_config(base_config).job_snapshot(),
        )
    )
    restarted = create_fake_runtime(
        machine_root,
        current_config_snapshot=effective_runtime_config(
            changed_config
        ).job_snapshot(),
    )

    with pytest.raises(DomainError, match="effective_config_drift"):
        restarted.wait_job(submitted.job_id)

    assert restarted.get_job(submitted.job_id).state is JobState.QUEUED
