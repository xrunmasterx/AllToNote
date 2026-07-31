from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from tools.runtime_wheelhouse_release import (
    ReleaseError,
    _schema_set_sha256,
    verify_iwiki_source,
    verify_iwiki_wheel,
    verify_runtime_source,
    verify_runtime_wheel,
    verify_wheelhouse,
)


_SOURCE_COMMIT = "a" * 40
_RUNTIME_VERSION = "0.1.0"
_IWIKI_VERSION = "0.1.2"


def _write_wheel(path: Path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            archive.writestr(name, entries[name])


def _iwiki_files() -> dict[str, bytes]:
    files = {
        "iwiki/__init__.py": b"",
        "iwiki/cli.py": b"def main():\n    return 0\n",
        "iwiki/portable/__init__.py": b"",
        "iwiki/portable/commit.py": b"",
        "iwiki/portable/content_validation.py": b"",
        "iwiki/portable/contract.py": (
            b"PORTABLE_SDK_API_VERSION = 1\n"
            b'PORTABLE_CONTRACT_ID = "iwiki-portable-contract-v1"\n'
            b'PORTABLE_SCHEMA_SET_ID = "2026-07-portable-v1"\n'
        ),
        (
            "iwiki/portable/contracts/required/"
            "alltonote-video-output-profile-v2.schema.json"
        ): b'{"$id":"urn:alltonote:video-producer:output-profile:v2"}\n',
        "iwiki/portable/contracts/v1/core.schema.json": b'{"type":"object"}\n',
        "iwiki/portable/contracts/v1/schema-set.json": (
            b'{"contract_id":"iwiki-portable-contract-v1",'
            b'"schema_set_id":"2026-07-portable-v1",'
            b'"schemas":["core.schema.json"]}\n'
        ),
        "iwiki/portable/jsonio.py": b"",
        "iwiki/portable/path_policy.py": b"",
        "iwiki/portable/types.py": b"",
        "iwiki/portable/validator.py": b"",
    }
    return files


def _write_backend(root: Path, *, schema_hash: str) -> Path:
    backend = root / "backend"
    app = backend / "app"
    (app / "cli").mkdir(parents=True)
    (app / "db").mkdir()
    (app / "__init__.py").write_text("", encoding="utf-8")
    (app / "cli" / "__init__.py").write_text("", encoding="utf-8")
    (app / "cli" / "main.py").write_text(
        "def entrypoint():\n    return 0\n",
        encoding="utf-8",
    )
    (app / "db" / "builtin_providers.json").write_text("[]\n", encoding="utf-8")
    (app / "runtime-lock.json").write_text(
        json.dumps(
            {
                "iwiki_package": f"llm-iwiki=={_IWIKI_VERSION}",
                "portable_api_version": 1,
                "portable_contract_id": "iwiki-portable-contract-v1",
                "schema_set_id": "2026-07-portable-v1",
                "schema_sha256": schema_hash,
                "source_commit": _SOURCE_COMMIT,
            }
        ),
        encoding="utf-8",
    )
    (backend / "pyproject.toml").write_text(
        "\n".join(
            (
                "[project]",
                'name = "alltonote-runtime"',
                f'version = "{_RUNTIME_VERSION}"',
                f'dependencies = ["llm-iwiki=={_IWIKI_VERSION}"]',
                "",
                "[project.scripts]",
                'alltonote = "app.cli.main:entrypoint"',
                "",
                "[tool.setuptools.package-data]",
                'app = ["runtime-lock.json", "db/builtin_providers.json"]',
                "",
            )
        ),
        encoding="utf-8",
    )
    return backend


def _runtime_wheel(backend: Path, wheelhouse: Path) -> Path:
    entries = {
        "app/" + path.relative_to(backend / "app").as_posix(): path.read_bytes()
        for path in (backend / "app").rglob("*")
        if path.is_file()
    }
    dist_info = f"alltonote_runtime-{_RUNTIME_VERSION}.dist-info/"
    entries.update(
        {
            dist_info + "METADATA": (
                "Metadata-Version: 2.4\n"
                "Name: alltonote-runtime\n"
                f"Version: {_RUNTIME_VERSION}\n"
                f"Requires-Dist: llm-iwiki=={_IWIKI_VERSION}\n\n"
            ).encode(),
            dist_info + "WHEEL": b"Wheel-Version: 1.0\n",
            dist_info + "RECORD": b"",
            dist_info + "entry_points.txt": (
                b"[console_scripts]\nalltonote = app.cli.main:entrypoint\n"
            ),
            dist_info + "top_level.txt": b"app\n",
        }
    )
    wheel = wheelhouse / f"alltonote_runtime-{_RUNTIME_VERSION}-py3-none-any.whl"
    _write_wheel(wheel, entries)
    return wheel


def _iwiki_wheel(
    wheelhouse: Path,
    source_root: Path,
    *,
    files: dict[str, bytes],
) -> Path:
    for name, content in files.items():
        destination = source_root.joinpath(*Path(name).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    (source_root / "pyproject.toml").write_text(
        "\n".join(
            (
                "[project]",
                'name = "llm-iwiki"',
                f'version = "{_IWIKI_VERSION}"',
                'dependencies = ["filelock>=3.18,<4", "PyYAML>=6.0,<7"]',
                "",
                "[project.scripts]",
                'iwiki = "iwiki.cli:main"',
                "",
            )
        ),
        encoding="utf-8",
    )
    entries = dict(files)
    dist_info = f"llm_iwiki-{_IWIKI_VERSION}.dist-info/"
    entries[dist_info + "METADATA"] = (
        "Metadata-Version: 2.4\n"
        "Name: llm-iwiki\n"
        f"Version: {_IWIKI_VERSION}\n"
        "Requires-Dist: filelock<4,>=3.18\n"
        "Requires-Dist: PyYAML<7,>=6.0\n\n"
    ).encode()
    entries[dist_info + "WHEEL"] = b"Wheel-Version: 1.0\n"
    entries[dist_info + "RECORD"] = b""
    entries[dist_info + "entry_points.txt"] = (
        b"[console_scripts]\niwiki = iwiki.cli:main\n"
    )
    wheel = wheelhouse / f"llm_iwiki-{_IWIKI_VERSION}-py3-none-any.whl"
    _write_wheel(wheel, entries)
    return wheel


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    source_root = tmp_path / "llm-iwiki"
    files = _iwiki_files()
    schema_hash = _schema_set_sha256(files)
    backend = _write_backend(tmp_path, schema_hash=schema_hash)
    runtime_wheel = _runtime_wheel(backend, wheelhouse)
    iwiki_wheel = _iwiki_wheel(wheelhouse, source_root, files=files)
    return backend, wheelhouse, source_root, runtime_wheel, iwiki_wheel


def _clean_pinned_git(_root: Path, arguments: tuple[str, ...]) -> str:
    if arguments == ("rev-parse", "HEAD"):
        return _SOURCE_COMMIT
    if arguments == ("status", "--porcelain", "--untracked-files=all"):
        return ""
    raise AssertionError(arguments)


def test_wheelhouse_verifier_binds_runtime_iwiki_schema_and_clean_source(
    tmp_path: Path,
) -> None:
    backend, wheelhouse, source, runtime_wheel, iwiki_wheel = _fixture(tmp_path)

    payload = verify_wheelhouse(
        wheelhouse,
        backend_root=backend,
        iwiki_source=source,
        git_runner=_clean_pinned_git,
    )

    assert verify_runtime_wheel(runtime_wheel, backend_root=backend).version == "0.1.0"
    assert verify_iwiki_wheel(iwiki_wheel, backend_root=backend).version == "0.1.2"
    assert payload["schema_version"] == 1
    assert payload["runtime_source_commit"] == _SOURCE_COMMIT
    assert payload["iwiki_source_commit"] == _SOURCE_COMMIT
    assert [item["distribution"] for item in payload["wheels"]] == [
        "alltonote-runtime",
        "llm-iwiki",
    ]
    assert all(str(item["sha256"]).startswith("sha256:") for item in payload["wheels"])
    runtime_payload = payload["wheels"][0]
    runtime_bytes = runtime_wheel.read_bytes()
    assert runtime_payload["byte_length"] == len(runtime_bytes)
    assert runtime_payload["sha256"] == (
        f"sha256:{hashlib.sha256(runtime_bytes).hexdigest()}"
    )


def test_runtime_wheel_rejects_missing_declared_package_data(tmp_path: Path) -> None:
    backend, _wheelhouse, _source, runtime_wheel, _iwiki_wheel_path = _fixture(
        tmp_path
    )
    entries = {
        name: content
        for name, content in zipfile_entries(runtime_wheel).items()
        if name != "app/db/builtin_providers.json"
    }
    _write_wheel(runtime_wheel, entries)

    with pytest.raises(ReleaseError, match="missing package file"):
        verify_runtime_wheel(runtime_wheel, backend_root=backend)


def test_runtime_wheel_rejects_unsafe_archive_path(tmp_path: Path) -> None:
    backend, _wheelhouse, _source, runtime_wheel, _iwiki_wheel_path = _fixture(
        tmp_path
    )
    entries = zipfile_entries(runtime_wheel)
    entries["../outside.txt"] = b"not allowed"
    _write_wheel(runtime_wheel, entries)

    with pytest.raises(ReleaseError, match="unsafe path"):
        verify_runtime_wheel(runtime_wheel, backend_root=backend)


def test_runtime_wheel_rejects_unbound_extra_file(tmp_path: Path) -> None:
    backend, _wheelhouse, _source, runtime_wheel, _iwiki_wheel_path = _fixture(
        tmp_path
    )
    entries = zipfile_entries(runtime_wheel)
    entries["extra_payload.py"] = b"print('not source-bound')\n"
    _write_wheel(runtime_wheel, entries)

    with pytest.raises(ReleaseError, match="unexpected files"):
        verify_runtime_wheel(runtime_wheel, backend_root=backend)


def test_runtime_wheel_rejects_dependency_constraint_drift(tmp_path: Path) -> None:
    backend, _wheelhouse, _source, runtime_wheel, _iwiki_wheel_path = _fixture(
        tmp_path
    )
    entries = zipfile_entries(runtime_wheel)
    metadata = f"alltonote_runtime-{_RUNTIME_VERSION}.dist-info/METADATA"
    entries[metadata] = entries[metadata].replace(
        b"llm-iwiki==0.1.2",
        b"llm-iwiki>=0",
    )
    _write_wheel(runtime_wheel, entries)

    with pytest.raises(ReleaseError, match="dependencies do not match"):
        verify_runtime_wheel(runtime_wheel, backend_root=backend)


def test_runtime_source_entry_point_must_match_fixed_contract(tmp_path: Path) -> None:
    backend, _wheelhouse, _source, runtime_wheel, _iwiki_wheel_path = _fixture(
        tmp_path
    )
    pyproject = backend / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            'alltonote = "app.cli.main:entrypoint"',
            'alltonote = "app.cli.main:other"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ReleaseError, match="project entry point contract"):
        verify_runtime_wheel(runtime_wheel, backend_root=backend)


def test_runtime_wheel_entry_point_must_match_source(tmp_path: Path) -> None:
    backend, _wheelhouse, _source, runtime_wheel, _iwiki_wheel_path = _fixture(
        tmp_path
    )
    entries = zipfile_entries(runtime_wheel)
    entry_points = (
        f"alltonote_runtime-{_RUNTIME_VERSION}.dist-info/entry_points.txt"
    )
    entries[entry_points] = (
        b"[console_scripts]\nalltonote = app.cli.main:other\n"
    )
    _write_wheel(runtime_wheel, entries)

    with pytest.raises(ReleaseError, match="wheel entry point contract"):
        verify_runtime_wheel(runtime_wheel, backend_root=backend)


def test_iwiki_wheel_rejects_schema_hash_drift(tmp_path: Path) -> None:
    backend, _wheelhouse, _source, _runtime_wheel_path, iwiki_wheel = _fixture(
        tmp_path
    )
    lock_path = backend / "app" / "runtime-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["schema_sha256"] = "sha256:" + "0" * 64
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(ReleaseError, match="schema hash"):
        verify_iwiki_wheel(iwiki_wheel, backend_root=backend)


def test_iwiki_wheel_rejects_unexpected_top_level_path(tmp_path: Path) -> None:
    backend, _wheelhouse, _source, _runtime_wheel_path, iwiki_wheel = _fixture(
        tmp_path
    )
    entries = zipfile_entries(iwiki_wheel)
    entries["payload/loader.py"] = b""
    _write_wheel(iwiki_wheel, entries)

    with pytest.raises(ReleaseError, match="unexpected top-level"):
        verify_iwiki_wheel(iwiki_wheel, backend_root=backend)


def test_iwiki_source_must_be_clean_and_pinned(tmp_path: Path) -> None:
    backend, _wheelhouse, source, _runtime_wheel_path, iwiki_wheel = _fixture(
        tmp_path
    )

    def dirty_git(_root: Path, arguments: tuple[str, ...]) -> str:
        return _SOURCE_COMMIT if arguments[0] == "rev-parse" else " M iwiki/portable/contract.py"

    with pytest.raises(ReleaseError, match="not clean"):
        verify_iwiki_source(
            source,
            iwiki_wheel,
            backend_root=backend,
            git_runner=dirty_git,
        )

    def wrong_commit(_root: Path, arguments: tuple[str, ...]) -> str:
        return "b" * 40 if arguments[0] == "rev-parse" else ""

    with pytest.raises(ReleaseError, match="does not match"):
        verify_iwiki_source(
            source,
            iwiki_wheel,
            backend_root=backend,
            git_runner=wrong_commit,
        )


def test_iwiki_source_binds_wheel_dependency_metadata(tmp_path: Path) -> None:
    backend, _wheelhouse, source, _runtime_wheel_path, iwiki_wheel = _fixture(
        tmp_path
    )
    entries = zipfile_entries(iwiki_wheel)
    metadata = f"llm_iwiki-{_IWIKI_VERSION}.dist-info/METADATA"
    entries[metadata] = entries[metadata].replace(
        b"filelock<4,>=3.18",
        b"filelock>=0",
    )
    _write_wheel(iwiki_wheel, entries)

    with pytest.raises(ReleaseError, match="dependencies do not match source"):
        verify_iwiki_source(
            source,
            iwiki_wheel,
            backend_root=backend,
            git_runner=_clean_pinned_git,
        )


def test_runtime_source_must_be_clean(tmp_path: Path) -> None:
    backend, _wheelhouse, _source, _runtime_wheel_path, _iwiki_wheel = _fixture(
        tmp_path
    )

    def dirty_runtime(_root: Path, arguments: tuple[str, ...]) -> str:
        return _SOURCE_COMMIT if arguments[0] == "rev-parse" else " M backend/app/runtime.py"

    with pytest.raises(ReleaseError, match="Runtime source worktree is not clean"):
        verify_runtime_source(backend, git_runner=dirty_runtime)


def zipfile_entries(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {
            item.filename: archive.read(item)
            for item in archive.infolist()
            if not item.is_dir()
        }
