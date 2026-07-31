from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli.main import main
from app.core.errors import DomainError, ErrorCategory
from app.adapters.video_packs.official_video_pack import MEDIA_BASIC
from tests.document_pack_support import write_pack_source
from tests.video_pack_support import write_pack_source as write_video_pack_source


class FakePackService:
    def __init__(self) -> None:
        self.doctor_result: dict[str, object] = {
            "pack_id": "document-basic",
            "pack_version": "docling-2.117.0-tableformer-v2.3.0",
            "installed": True,
            "healthy": True,
            "dynamic": False,
            "manifest_sha256": "sha256:" + "a" * 64,
            "checks": (
                {
                    "code": "pack.document-basic.static",
                    "status": "pass",
                    "action": "No action required",
                    "dynamic": False,
                },
            ),
        }
        self.install_result: dict[str, object] = {
            "pack_id": "document-basic",
            "pack_version": "docling-2.117.0-tableformer-v2.3.0",
            "manifest_sha256": "sha256:" + "a" * 64,
            "result": "installed",
        }
        self.doctor_dynamic: list[bool] = []
        self.install_calls: list[tuple[Path, bool]] = []
        self.error: BaseException | None = None

    def doctor(self, *, dynamic: bool) -> dict[str, object]:
        if self.error is not None:
            raise self.error
        self.doctor_dynamic.append(dynamic)
        return {**self.doctor_result, "dynamic": dynamic}

    def install(self, source: Path, *, repair: bool) -> dict[str, object]:
        if self.error is not None:
            raise self.error
        self.install_calls.append((source, repair))
        return self.install_result


def test_pack_doctor_json_is_one_stable_envelope(capsys) -> None:
    service = FakePackService()

    exit_code = main(
        ["pack", "doctor", "document-basic", "--dynamic", "--json"],
        pack_service=service,
    )
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert envelope["command"] == "pack doctor"
    expected = {**service.doctor_result, "dynamic": True}
    expected["checks"] = list(expected["checks"])
    assert envelope["data"] == expected
    assert service.doctor_dynamic == [True]


def test_pack_doctor_unhealthy_is_completed_diagnostic_with_exit_zero(capsys) -> None:
    service = FakePackService()
    service.doctor_result["installed"] = False
    service.doctor_result["healthy"] = False
    service.doctor_result["manifest_sha256"] = None
    service.doctor_result["checks"] = (
        {
            "code": "pack.document-basic.static",
            "status": "fail",
            "action": "Install document-basic",
            "dynamic": False,
        },
    )

    exit_code = main(
        ["pack", "doctor", "document-basic", "--json"],
        pack_service=service,
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert envelope["ok"] is True
    assert envelope["data"]["healthy"] is False


def test_pack_install_projects_no_source_or_destination_path(
    tmp_path: Path,
    capsys,
) -> None:
    service = FakePackService()
    source = tmp_path / "signed source"

    exit_code = main(
        [
            "pack",
            "install",
            "document-basic",
            "--source",
            str(source),
            "--repair",
            "--json",
        ],
        pack_service=service,
    )
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert envelope["command"] == "pack install"
    assert envelope["data"] == service.install_result
    assert str(source) not in captured.out
    assert service.install_calls == [(source, True)]


def test_pack_install_projects_already_active_as_success(
    tmp_path: Path,
    capsys,
) -> None:
    service = FakePackService()
    service.install_result["result"] = "already_active"

    exit_code = main(
        [
            "pack",
            "install",
            "document-basic",
            "--source",
            str(tmp_path / "signed source"),
            "--json",
        ],
        pack_service=service,
    )
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert envelope["data"]["result"] == "already_active"


def test_default_pack_install_rejects_non_official_signature(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALLTONOTE_DOCUMENT_BASIC_PYTHON", raising=False)
    monkeypatch.delenv("ALLTONOTE_DOCUMENT_BASIC_ARTIFACTS", raising=False)
    source = tmp_path / "private signed source"
    source.mkdir()
    write_pack_source(source)

    exit_code = main(
        [
            "pack",
            "install",
            "document-basic",
            "--source",
            str(source),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert exit_code == 40
    assert captured.err == ""
    assert envelope["error"]["code"] == "pack_signature_invalid"
    assert str(source) not in captured.out


def test_pack_human_output_is_concise(capsys, tmp_path: Path) -> None:
    service = FakePackService()

    assert (
        main(
            [
                "pack",
                "install",
                "document-basic",
                "--source",
                str(tmp_path / "source"),
            ],
            pack_service=service,
        )
        == 0
    )
    captured = capsys.readouterr()

    assert captured.out == (
        "document-basic docling-2.117.0-tableformer-v2.3.0: installed\n"
    )
    assert captured.err == ""


@pytest.mark.parametrize("pack_id", ("media-basic", "transcribe-cpu"))
def test_video_pack_ids_are_supported_by_doctor_cli(
    pack_id: str,
    capsys,
) -> None:
    service = FakePackService()
    service.doctor_result["pack_id"] = pack_id
    service.doctor_result["pack_version"] = "fixture-v1"

    assert main(
        ["pack", "doctor", pack_id, "--json"],
        pack_service=service,
    ) == 0

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["data"]["pack_id"] == pack_id


def test_default_media_pack_install_rejects_non_official_signature(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALLTONOTE_MEDIA_BASIC_ROOT", raising=False)
    source = tmp_path / "test-signed-media-pack"
    source.mkdir()
    write_video_pack_source(
        source,
        MEDIA_BASIC,
        platform_tag="windows-x86_64",
    )

    exit_code = main(
        [
            "pack",
            "install",
            "media-basic",
            "--source",
            str(source),
            "--json",
        ]
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 40
    assert envelope["error"]["code"] == "pack_signature_untrusted"
    assert str(source) not in json.dumps(envelope)


@pytest.mark.parametrize(
    ("category", "expected_exit"),
    (
        (ErrorCategory.INVALID_REQUEST, 2),
        (ErrorCategory.WORKSPACE_INCOMPATIBLE, 10),
        (ErrorCategory.CONFLICT, 20),
        (ErrorCategory.RETRYABLE_RUNTIME, 30),
        (ErrorCategory.POLICY_DENIED, 40),
    ),
)
def test_pack_domain_error_uses_frozen_exit_mapping(
    category: ErrorCategory,
    expected_exit: int,
    capsys,
) -> None:
    service = FakePackService()
    service.error = DomainError("pack_test_failure", category, "safe failure")

    exit_code = main(
        ["pack", "doctor", "document-basic", "--json"],
        pack_service=service,
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == expected_exit
    assert envelope["error"]["code"] == "pack_test_failure"


def test_pack_unexpected_failure_is_safe_internal_error(capsys) -> None:
    service = FakePackService()
    service.error = RuntimeError("C:\\secret\\source token=should-not-leak")

    assert (
        main(
            ["pack", "doctor", "document-basic", "--json"],
            pack_service=service,
        )
        == 70
    )
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert captured.err == ""
    assert envelope["error"]["code"] == "internal_error"
    assert "should-not-leak" not in captured.out


def test_pack_install_keyboard_interrupt_maps_to_130(capsys, tmp_path: Path) -> None:
    service = FakePackService()
    service.error = KeyboardInterrupt()

    exit_code = main(
        [
            "pack",
            "install",
            "document-basic",
            "--source",
            str(tmp_path / "source"),
            "--json",
        ],
        pack_service=service,
    )
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 130
    assert envelope["error"]["code"] == "interrupted"


@pytest.mark.parametrize(
    "arguments",
    (
        ["pack", "doctor", "--json"],
        ["pack", "install", "document-basic", "--json"],
        ["pack", "doctor", "unknown", "--json"],
    ),
)
def test_pack_usage_error_preserves_command_identity(arguments, capsys) -> None:
    exit_code = main(arguments)
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert envelope["command"] in {"pack doctor", "pack install"}
    assert envelope["error"]["code"] == "cli_usage_invalid"
