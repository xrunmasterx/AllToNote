from __future__ import annotations

import hashlib
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

from tools.runtime_windows_release import (
    ReleaseError,
    _archive_members,
    _clean_environment,
    _file_manifest,
    _load_lock,
    _locked_wheel_install_arguments,
    _parser,
    _remove_direct_url_metadata,
    _remove_pip_console_scripts,
    _verify_inputs,
)


ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "runtime-windows-x86_64.lock.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> tuple[dict[str, object], Path, Path]:
    inputs = tmp_path / "inputs"
    wheelhouse = tmp_path / "wheelhouse"
    inputs.mkdir()
    wheelhouse.mkdir()
    python = inputs / "python.zip"
    spdx = inputs / "python.spdx.json"
    sqlite = inputs / "sqlite.zip"
    wheel = wheelhouse / "runtime.whl"
    python.write_bytes(b"python")
    spdx.write_bytes(b"spdx")
    sqlite.write_bytes(b"sqlite")
    wheel.write_bytes(b"wheel")
    lock: dict[str, object] = {
        "schema_version": 1,
        "platform": "windows-x86_64",
        "python": {
            "archive": python.name,
            "sha256": _sha256(python),
            "spdx": spdx.name,
            "spdx_sha256": _sha256(spdx),
        },
        "sqlite": {
            "archive": sqlite.name,
            "sha3_256": hashlib.sha3_256(sqlite.read_bytes()).hexdigest(),
        },
        "wheels": [
            {
                "filename": wheel.name,
                "byte_length": wheel.stat().st_size,
                "sha256": _sha256(wheel),
            }
        ],
    }
    return lock, inputs, wheelhouse


def test_checked_in_lock_freezes_the_validated_runtime_inputs() -> None:
    lock = _load_lock(LOCK)

    assert lock["runtime_source_commit"] == "8d59ac5b38460649426d5472789b8a1d5b4aeedc"
    assert lock["python"]["version"] == "3.14.6"
    assert lock["sqlite"] == {
        "version": "3.53.4",
        "archive": "sqlite-dll-win-x64-3530400.zip",
        "url": "https://www.sqlite.org/2026/sqlite-dll-win-x64-3530400.zip",
        "sha3_256": "deddee963c810d1eeac3ce5e15c7c41da21a1c54d7a39cf54fbf577d2f50de3a",
        "source_id": "2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc",
    }
    assert len(lock["wheels"]) == 15


def test_cli_lock_option_maps_to_the_assembler_parameter(tmp_path: Path) -> None:
    arguments = _parser().parse_args(
        [
            "--lock",
            str(tmp_path / "lock.json"),
            "--inputs",
            str(tmp_path / "inputs"),
            "--wheelhouse",
            str(tmp_path / "wheels"),
            "--builder-python",
            str(tmp_path / "python.exe"),
            "--output",
            str(tmp_path / "output"),
            "--gate-root",
            str(tmp_path / "gate"),
        ]
    )

    assert arguments.lock_path == tmp_path / "lock.json"


def test_release_input_verifier_requires_the_exact_wheelhouse(tmp_path: Path) -> None:
    lock, inputs, wheelhouse = _fixture(tmp_path)

    _python, _spdx, _sqlite, wheels = _verify_inputs(lock, inputs, wheelhouse)

    assert wheels == tuple(lock["wheels"])

    (wheelhouse / "extra.whl").write_bytes(b"extra")
    with pytest.raises(ReleaseError, match="wheelhouse"):
        _verify_inputs(lock, inputs, wheelhouse)


def test_release_input_verifier_rejects_hash_drift(tmp_path: Path) -> None:
    lock, inputs, wheelhouse = _fixture(tmp_path)
    (inputs / "sqlite.zip").write_bytes(b"tampered")

    with pytest.raises(ReleaseError, match="SQLite archive hash"):
        _verify_inputs(lock, inputs, wheelhouse)


def test_pip_install_uses_only_absolute_locked_wheels(tmp_path: Path) -> None:
    builder = tmp_path / "builder" / "python.exe"
    site_packages = tmp_path / "stage" / "site-packages"
    wheelhouse = tmp_path / "wheelhouse"
    wheels = (
        {"filename": "alltonote_runtime-0.1.0-py3-none-any.whl"},
        {"filename": "llm_iwiki-0.1.3-py3-none-any.whl"},
    )

    arguments = _locked_wheel_install_arguments(
        builder,
        site_packages,
        wheelhouse,
        wheels,
    )

    assert "--isolated" in arguments
    assert "--no-index" in arguments
    assert "--no-deps" in arguments
    assert "--find-links" not in arguments
    assert arguments[-2:] == tuple(
        str((wheelhouse / item["filename"]).resolve(strict=False))
        for item in wheels
    )


def test_pip_environment_discards_external_configuration() -> None:
    environment = _clean_environment(
        {
            "PATH": "trusted-path",
            "PIP_FIND_LINKS": "untrusted-wheelhouse",
            "PIP_CONFIG_FILE": "untrusted-pip.ini",
            "PIP_CONSTRAINT": "untrusted-constraints.txt",
        }
    )

    assert environment["PATH"] == "trusted-path"
    assert environment["PIP_CONFIG_FILE"] == os.devnull
    assert environment["PIP_NO_INDEX"] == "1"
    assert "PIP_FIND_LINKS" not in environment
    assert "PIP_CONSTRAINT" not in environment


def test_direct_wheel_provenance_does_not_leak_the_builder_path(
    tmp_path: Path,
) -> None:
    site_packages = tmp_path / "site-packages"
    dist_info = site_packages / "example-1.0.dist-info"
    dist_info.mkdir(parents=True)
    direct_url = dist_info / "direct_url.json"
    direct_url.write_text(
        '{"url":"file:///private/wheelhouse/example.whl"}',
        encoding="utf-8",
    )
    record = dist_info / "RECORD"
    record.write_text(
        "example/__init__.py,sha256=payload,1\n"
        "example-1.0.dist-info/direct_url.json,sha256=private,50\n"
        "example-1.0.dist-info/RECORD,,\n",
        encoding="utf-8",
        newline="",
    )

    _remove_direct_url_metadata(site_packages, 1)

    assert not direct_url.exists()
    assert record.read_text(encoding="utf-8") == (
        "example/__init__.py,sha256=payload,1\n"
        "example-1.0.dist-info/RECORD,,\n"
    )


def test_pip_console_launchers_do_not_leak_the_builder_path(tmp_path: Path) -> None:
    site_packages = tmp_path / "site-packages"
    scripts = site_packages / "bin"
    scripts.mkdir(parents=True)
    names = ("alltonote.exe", "cffi-gen-src.exe", "iwiki.exe", "keyring.exe")
    for name in names:
        (scripts / name).write_text("C:/private/builder/python.exe", encoding="utf-8")
        dist_info = site_packages / f"{name}-1.0.dist-info"
        dist_info.mkdir()
        (dist_info / "RECORD").write_text(
            f"../../bin/{name},sha256=private,1\n"
            f"{dist_info.name}/RECORD,,\n",
            encoding="utf-8",
            newline="",
        )

    _remove_pip_console_scripts(site_packages)

    assert not scripts.exists()
    for name in names:
        record = site_packages / f"{name}-1.0.dist-info" / "RECORD"
        assert "../../bin/" not in record.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ("../outside", "/absolute", "C:/drive"))
def test_archive_rejects_unsafe_member_paths(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(name, b"unsafe")

    with pytest.raises(ReleaseError, match="unsafe entry"):
        _archive_members(archive)


def test_archive_rejects_symlink_members(tmp_path: Path) -> None:
    archive = tmp_path / "linked.zip"
    member = zipfile.ZipInfo("linked")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(member, b"target")

    with pytest.raises(ReleaseError, match="unsafe entry"):
        _archive_members(archive)


def test_file_manifest_uses_unique_relative_paths_and_excludes_itself(
    tmp_path: Path,
) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "release").mkdir()
    (tmp_path / "a.txt").write_bytes(b"a")
    (tmp_path / "nested" / "b.txt").write_bytes(b"b")
    (tmp_path / "release" / "file-manifest.json").write_text(
        "old", encoding="utf-8"
    )

    manifest = _file_manifest(tmp_path)

    assert manifest["file_count"] == 2
    assert [item["path"] for item in manifest["files"]] == [
        "a.txt",
        "nested/b.txt",
    ]
    assert len(json.dumps(manifest)) > 0
