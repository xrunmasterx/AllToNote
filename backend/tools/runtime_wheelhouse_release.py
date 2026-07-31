from __future__ import annotations

import argparse
import ast
import configparser
import hashlib
import io
import json
import re
import subprocess
import tomllib
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedWheel:
    distribution: str
    version: str
    filename: str
    byte_length: int
    sha256: str

    def payload(self) -> dict[str, object]:
        return {
            "distribution": self.distribution,
            "version": self.version,
            "filename": self.filename,
            "byte_length": self.byte_length,
            "sha256": self.sha256,
        }


GitRunner = Callable[[Path, tuple[str, ...]], str]

_DISTRIBUTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_VERSION_SPECIFIER = re.compile(
    r"(===|==|~=|!=|<=|>=|<|>)[A-Za-z0-9][A-Za-z0-9.*+!_-]*"
)
_MAX_WHEEL_ENTRIES = 10_000
_MAX_WHEEL_ENTRY_BYTES = 64 * 1024 * 1024
_MAX_WHEEL_BYTES = 256 * 1024 * 1024
_MAX_WHEEL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
_RUNTIME_FORBIDDEN_PREFIXES = ("backend/", "tests/", "tools/")
_RUNTIME_FORBIDDEN_CONTENT = (
    b"Ed25519PrivateKey",
    b"initialize_signing_key",
    b"load_signing_key",
    b"AllToNote/release/document-basic/ed25519",
)
_IWIKI_REQUIRED_FILES = frozenset(
    {
        "iwiki/portable/__init__.py",
        "iwiki/portable/commit.py",
        "iwiki/portable/content_validation.py",
        "iwiki/portable/contract.py",
        "iwiki/portable/jsonio.py",
        "iwiki/portable/path_policy.py",
        "iwiki/portable/types.py",
        "iwiki/portable/validator.py",
        (
            "iwiki/portable/contracts/required/"
            "alltonote-video-output-profile-v2.schema.json"
        ),
        "iwiki/portable/contracts/v1/schema-set.json",
    }
)


def _canonical_distribution(value: str) -> str:
    if not isinstance(value, str) or _DISTRIBUTION.fullmatch(value) is None:
        return ""
    return re.sub(r"[-_.]+", "-", value).casefold()


def _wheel_entries(path: Path) -> tuple[dict[str, bytes], bytes]:
    try:
        wheel_bytes = path.read_bytes()
        if len(wheel_bytes) > _MAX_WHEEL_BYTES:
            raise ReleaseError(f"wheel is too large: {path.name}")
        with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
            entries: dict[str, bytes] = {}
            uncompressed_bytes = 0
            for item in archive.infolist():
                name = item.filename
                pure = PurePosixPath(name)
                if item.is_dir():
                    continue
                if (
                    not name
                    or "\\" in name
                    or pure.is_absolute()
                    or ".." in pure.parts
                ):
                    raise ReleaseError(f"wheel contains an unsafe path: {name}")
                if name in entries:
                    raise ReleaseError(f"wheel contains a duplicate entry: {name}")
                if item.file_size > _MAX_WHEEL_ENTRY_BYTES:
                    raise ReleaseError(f"wheel entry is too large: {name}")
                uncompressed_bytes += item.file_size
                if uncompressed_bytes > _MAX_WHEEL_UNCOMPRESSED_BYTES:
                    raise ReleaseError("wheel uncompressed content is too large")
                entries[name] = archive.read(item)
                if len(entries) > _MAX_WHEEL_ENTRIES:
                    raise ReleaseError("wheel contains too many entries")
            return entries, wheel_bytes
    except (OSError, zipfile.BadZipFile) as error:
        raise ReleaseError(f"wheel is invalid: {path.name}") from error


def _metadata(
    entries: Mapping[str, bytes],
    expected_distribution: str,
) -> tuple[str, str, str]:
    expected = _canonical_distribution(expected_distribution)
    candidates = [
        name
        for name in entries
        if name.endswith(".dist-info/METADATA")
        and "/" not in name.removesuffix(".dist-info/METADATA")
    ]
    if len(candidates) != 1:
        raise ReleaseError("wheel must contain exactly one top-level METADATA")
    metadata_name = candidates[0]
    message = BytesParser(policy=policy.default).parsebytes(entries[metadata_name])
    distribution = message.get("Name", "")
    version = message.get("Version", "")
    if _canonical_distribution(distribution) != expected or not version:
        raise ReleaseError("wheel metadata does not match the expected distribution")
    return distribution, version, metadata_name.removesuffix("METADATA")


def _requirement_identity(requirement: str) -> tuple[str, str]:
    compact = "".join(requirement.split())
    match = _DISTRIBUTION.match(compact)
    if match is None:
        raise ReleaseError("dependency declaration is invalid")
    name = _canonical_distribution(match.group(0))
    remainder = compact[match.end() :]
    specifiers = remainder.split(",") if remainder else []
    if any(_VERSION_SPECIFIER.fullmatch(item) is None for item in specifiers):
        raise ReleaseError("dependency declaration is not a pinned release constraint")
    return name, f"{name}{','.join(sorted(specifiers))}"


def _dependency_identities(metadata_bytes: bytes) -> dict[str, str]:
    message = BytesParser(policy=policy.default).parsebytes(metadata_bytes)
    identities: dict[str, str] = {}
    for requirement in message.get_all("Requires-Dist", []):
        name, identity = _requirement_identity(requirement)
        if name in identities:
            raise ReleaseError("wheel contains a duplicate dependency declaration")
        identities[name] = identity
    return identities


def _project_dependency_identities(dependencies: object) -> dict[str, str]:
    if not isinstance(dependencies, list):
        raise ReleaseError("project dependencies are invalid")
    identities: dict[str, str] = {}
    for requirement in dependencies:
        if not isinstance(requirement, str):
            raise ReleaseError("project dependencies are invalid")
        try:
            name, identity = _requirement_identity(requirement)
        except ReleaseError as error:
            raise ReleaseError("project dependencies are invalid") from error
        if name in identities:
            raise ReleaseError("project contains a duplicate dependency declaration")
        identities[name] = identity
    return identities


def _verified(
    path: Path,
    distribution: str,
    version: str,
    wheel_bytes: bytes,
) -> VerifiedWheel:
    return VerifiedWheel(
        distribution=_canonical_distribution(distribution),
        version=version,
        filename=path.name,
        byte_length=len(wheel_bytes),
        sha256=f"sha256:{hashlib.sha256(wheel_bytes).hexdigest()}",
    )


def verify_runtime_wheel(
    path: Path,
    *,
    backend_root: Path,
    _wheel_data: tuple[dict[str, bytes], bytes] | None = None,
) -> VerifiedWheel:
    path = Path(path)
    backend_root = Path(backend_root)
    try:
        pyproject = tomllib.loads(
            (backend_root / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = pyproject["project"]
        expected_distribution = project["name"]
        expected_version = project["version"]
        expected_scripts = project["scripts"]
        package_data = pyproject["tool"]["setuptools"]["package-data"]["app"]
    except (
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        tomllib.TOMLDecodeError,
    ) as error:
        raise ReleaseError("Runtime project metadata is invalid") from error
    if (
        not isinstance(expected_distribution, str)
        or not isinstance(expected_version, str)
        or not isinstance(expected_scripts, dict)
        or any(
            not isinstance(name, str) or not isinstance(value, str)
            for name, value in expected_scripts.items()
        )
        or not isinstance(package_data, list)
        or any(not isinstance(item, str) for item in package_data)
    ):
        raise ReleaseError("Runtime project metadata is invalid")

    entries, wheel_bytes = _wheel_data or _wheel_entries(path)
    distribution, version, dist_info = _metadata(entries, expected_distribution)
    if version != expected_version:
        raise ReleaseError("Runtime wheel version does not match pyproject.toml")

    required_dist_info = {
        f"{dist_info}{name}"
        for name in ("METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt")
    }
    missing_dist_info = required_dist_info.difference(entries)
    if missing_dist_info:
        raise ReleaseError("Runtime wheel is missing required dist-info files")

    parser = configparser.ConfigParser()
    try:
        parser.read_string(entries[f"{dist_info}entry_points.txt"].decode("utf-8"))
    except (UnicodeDecodeError, configparser.Error) as error:
        raise ReleaseError("Runtime wheel entry points are invalid") from error
    if expected_scripts != {"alltonote": "app.cli.main:entrypoint"}:
        raise ReleaseError("Runtime project entry point contract is invalid")
    if (
        parser.sections() != ["console_scripts"]
        or dict(parser["console_scripts"]) != expected_scripts
    ):
        raise ReleaseError("Runtime wheel entry point contract is invalid")

    expected_python = {
        "app/" + item.relative_to(backend_root / "app").as_posix()
        for item in (backend_root / "app").rglob("*.py")
        if item.is_file()
    }
    actual_python = {
        name for name in entries if name.startswith("app/") and name.endswith(".py")
    }
    if actual_python != expected_python:
        raise ReleaseError("Runtime wheel Python package does not match backend/app")
    expected_files = expected_python | {f"app/{relative}" for relative in package_data}
    expected_entries = expected_files | required_dist_info
    unexpected_entries = set(entries).difference(expected_entries)
    if unexpected_entries:
        raise ReleaseError("Runtime wheel contains unexpected files")
    for name in expected_files:
        try:
            source_bytes = backend_root.joinpath(*PurePosixPath(name).parts).read_bytes()
            entry_bytes = entries[name]
        except (KeyError, OSError) as error:
            raise ReleaseError(f"Runtime wheel is missing package file: {name}") from error
        if source_bytes != entry_bytes:
            raise ReleaseError(f"Runtime wheel does not match source file: {name}")

    expected_dependencies = _project_dependency_identities(
        project.get("dependencies")
    )
    actual_dependencies = _dependency_identities(entries[f"{dist_info}METADATA"])
    if actual_dependencies != expected_dependencies:
        raise ReleaseError("Runtime wheel dependencies do not match pyproject.toml")

    for name, content in entries.items():
        if name.startswith(_RUNTIME_FORBIDDEN_PREFIXES):
            raise ReleaseError(f"Runtime wheel contains a forbidden path: {name}")
        if name.endswith(".py") and any(
            marker in content for marker in _RUNTIME_FORBIDDEN_CONTENT
        ):
            raise ReleaseError("Runtime wheel contains release signing capability")
    return _verified(path, distribution, version, wheel_bytes)


def _iwiki_constants(contract_bytes: bytes) -> dict[str, object]:
    try:
        tree = ast.parse(contract_bytes.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError) as error:
        raise ReleaseError("iwiki portable contract module is invalid") from error
    values: dict[str, object] = {}
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and value is not None:
            try:
                values[target.id] = ast.literal_eval(value)
            except (ValueError, TypeError):
                continue
    return values


def _schema_set_sha256(entries: Mapping[str, bytes]) -> str:
    catalog_name = "iwiki/portable/contracts/v1/schema-set.json"
    try:
        catalog_bytes = entries[catalog_name]
        catalog = json.loads(catalog_bytes)
        names = catalog["schemas"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ReleaseError("iwiki schema catalog is invalid") from error
    if (
        not isinstance(names, list)
        or any(not isinstance(name, str) or PurePosixPath(name).name != name for name in names)
        or names != sorted(names)
        or len(names) != len(set(names))
    ):
        raise ReleaseError("iwiki schema catalog is not canonical")
    digest = hashlib.sha256()
    for name in names:
        path = f"iwiki/portable/contracts/v1/{name}"
        try:
            content = entries[path]
        except KeyError as error:
            raise ReleaseError(f"iwiki wheel is missing schema: {name}") from error
        encoded_name = name.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def _runtime_lock(backend_root: Path) -> dict[str, object]:
    try:
        payload = json.loads(
            (backend_root / "app" / "runtime-lock.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseError("Runtime lock is invalid") from error
    if not isinstance(payload, dict):
        raise ReleaseError("Runtime lock is invalid")
    return payload


def verify_iwiki_wheel(
    path: Path,
    *,
    backend_root: Path,
    _wheel_data: tuple[dict[str, bytes], bytes] | None = None,
) -> VerifiedWheel:
    lock = _runtime_lock(Path(backend_root))
    package = lock.get("iwiki_package")
    if not isinstance(package, str) or package.count("==") != 1:
        raise ReleaseError("Runtime lock iwiki package is invalid")
    expected_distribution, expected_version = package.split("==", 1)

    entries, wheel_bytes = _wheel_data or _wheel_entries(Path(path))
    distribution, version, dist_info = _metadata(entries, expected_distribution)
    if version != expected_version:
        raise ReleaseError("iwiki wheel version does not match the Runtime lock")
    required_dist_info = {
        f"{dist_info}{name}" for name in ("METADATA", "WHEEL", "RECORD")
    }
    if required_dist_info.difference(entries):
        raise ReleaseError("iwiki wheel is missing required dist-info files")
    if any(
        not (
            name.startswith("iwiki/")
            or name.startswith("tools/")
            or name.startswith(dist_info)
        )
        for name in entries
    ):
        raise ReleaseError("iwiki wheel contains an unexpected top-level path")
    missing = _IWIKI_REQUIRED_FILES.difference(entries)
    if missing:
        raise ReleaseError("iwiki wheel is missing the required portable contract")

    constants = _iwiki_constants(entries["iwiki/portable/contract.py"])
    expected_constants = {
        "PORTABLE_SDK_API_VERSION": lock.get("portable_api_version"),
        "PORTABLE_CONTRACT_ID": lock.get("portable_contract_id"),
        "PORTABLE_SCHEMA_SET_ID": lock.get("schema_set_id"),
    }
    if any(constants.get(name) != value for name, value in expected_constants.items()):
        raise ReleaseError("iwiki wheel portable identity does not match the Runtime lock")
    if _schema_set_sha256(entries) != lock.get("schema_sha256"):
        raise ReleaseError("iwiki wheel schema hash does not match the Runtime lock")
    return _verified(Path(path), distribution, version, wheel_bytes)


def _run_git(source_root: Path, arguments: tuple[str, ...]) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), *arguments],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReleaseError("iwiki source Git identity is unavailable") from error
    return completed.stdout.strip()


def verify_iwiki_source(
    source_root: Path,
    wheel: Path,
    *,
    backend_root: Path,
    git_runner: GitRunner = _run_git,
    _wheel_data: tuple[dict[str, bytes], bytes] | None = None,
) -> str:
    source_root = Path(source_root)
    lock = _runtime_lock(Path(backend_root))
    expected_commit = lock.get("source_commit")
    if (
        not isinstance(expected_commit, str)
        or git_runner(source_root, ("rev-parse", "HEAD")) != expected_commit
    ):
        raise ReleaseError("iwiki source commit does not match the Runtime lock")
    if git_runner(source_root, ("status", "--porcelain", "--untracked-files=all")):
        raise ReleaseError("iwiki source worktree is not clean")

    entries, _wheel_bytes = _wheel_data or _wheel_entries(Path(wheel))
    package = lock.get("iwiki_package")
    if not isinstance(package, str) or package.count("==") != 1:
        raise ReleaseError("Runtime lock iwiki package is invalid")
    expected_distribution, expected_version = package.split("==", 1)
    _distribution, wheel_version, dist_info = _metadata(
        entries,
        expected_distribution,
    )
    try:
        source_project = tomllib.loads(
            (source_root / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        source_distribution = source_project["name"]
        source_version = source_project["version"]
        source_scripts = source_project.get("scripts", {})
    except (
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        tomllib.TOMLDecodeError,
    ) as error:
        raise ReleaseError("iwiki source project metadata is invalid") from error
    if (
        _canonical_distribution(source_distribution) != _canonical_distribution(
            expected_distribution
        )
        or source_version != expected_version
        or wheel_version != source_version
        or not isinstance(source_scripts, dict)
        or any(
            not isinstance(name, str) or not isinstance(value, str)
            for name, value in source_scripts.items()
        )
    ):
        raise ReleaseError("iwiki source project identity does not match the wheel")
    if _project_dependency_identities(
        source_project.get("dependencies")
    ) != _dependency_identities(entries[f"{dist_info}METADATA"]):
        raise ReleaseError("iwiki wheel dependencies do not match source")
    entry_points_name = f"{dist_info}entry_points.txt"
    if source_scripts:
        try:
            parser = configparser.ConfigParser()
            parser.read_string(entries[entry_points_name].decode("utf-8"))
        except (KeyError, UnicodeDecodeError, configparser.Error) as error:
            raise ReleaseError("iwiki wheel entry points are invalid") from error
        if parser.sections() != ["console_scripts"] or dict(
            parser["console_scripts"]
        ) != source_scripts:
            raise ReleaseError("iwiki wheel entry points do not match source")
    elif entry_points_name in entries:
        raise ReleaseError("iwiki wheel contains undeclared entry points")

    source_files = {
        name: source_root.joinpath(*PurePosixPath(name).parts)
        for name in entries
        if not name.startswith(dist_info)
    }
    if not source_files:
        raise ReleaseError("iwiki wheel contains no iwiki package")
    for name, source in source_files.items():
        try:
            content = source.read_bytes()
        except OSError as error:
            raise ReleaseError(f"iwiki source is missing wheel file: {name}") from error
        if content != entries[name]:
            raise ReleaseError(f"iwiki source does not match wheel file: {name}")
    return expected_commit


def verify_runtime_source(
    backend_root: Path,
    *,
    git_runner: GitRunner = _run_git,
) -> str:
    backend_root = Path(backend_root)
    source_commit = git_runner(backend_root, ("rev-parse", "HEAD"))
    if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise ReleaseError("Runtime source Git identity is invalid")
    if git_runner(
        backend_root,
        ("status", "--porcelain", "--untracked-files=all"),
    ):
        raise ReleaseError("Runtime source worktree is not clean")
    return source_commit


def verify_wheelhouse(
    wheelhouse: Path,
    *,
    backend_root: Path,
    iwiki_source: Path,
    git_runner: GitRunner = _run_git,
) -> dict[str, object]:
    wheelhouse = Path(wheelhouse)
    try:
        wheels = sorted(wheelhouse.glob("*.whl"))
    except OSError as error:
        raise ReleaseError("wheelhouse is unavailable") from error
    runtime_candidates = [
        path
        for path in wheels
        if _canonical_distribution(path.name.split("-", 1)[0]) == "alltonote-runtime"
    ]
    iwiki_candidates = [
        path
        for path in wheels
        if _canonical_distribution(path.name.split("-", 1)[0]) == "llm-iwiki"
    ]
    if len(runtime_candidates) != 1 or len(iwiki_candidates) != 1 or len(wheels) != 2:
        raise ReleaseError(
            "wheelhouse must contain exactly one Runtime wheel and one iwiki wheel"
        )
    runtime_wheel_data = _wheel_entries(runtime_candidates[0])
    iwiki_wheel_data = _wheel_entries(iwiki_candidates[0])
    runtime = verify_runtime_wheel(
        runtime_candidates[0],
        backend_root=backend_root,
        _wheel_data=runtime_wheel_data,
    )
    runtime_source_commit = verify_runtime_source(
        backend_root,
        git_runner=git_runner,
    )
    iwiki = verify_iwiki_wheel(
        iwiki_candidates[0],
        backend_root=backend_root,
        _wheel_data=iwiki_wheel_data,
    )
    source_commit = verify_iwiki_source(
        iwiki_source,
        iwiki_candidates[0],
        backend_root=backend_root,
        git_runner=git_runner,
        _wheel_data=iwiki_wheel_data,
    )
    return {
        "schema_version": 1,
        "runtime_lock": _runtime_lock(Path(backend_root)),
        "runtime_source_commit": runtime_source_commit,
        "iwiki_source_commit": source_commit,
        "wheels": [runtime.payload(), iwiki.payload()],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify the AllToNote Runtime wheelhouse")
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--iwiki-source", type=Path, required=True)
    parser.add_argument(
        "--backend-root",
        type=Path,
        default=Path(__file__).parents[1],
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        payload = verify_wheelhouse(
            args.wheelhouse,
            backend_root=args.backend_root,
            iwiki_source=args.iwiki_source,
        )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except ReleaseError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
